"""OpenAI-compatible streaming chat client with the runner's specific needs:

* Two-layer timeout: inactivity + hard wall clock.
* Thinking-token stripping for public outputs; notes pass through unstripped.
* Streaming consumer that surfaces deltas live to the UI.
* Reasoning-field handling: if an engine streams only to `delta.reasoning`
  (some Gemma / DeepSeek setups), the runner still sees the content live AND
  the final result.text falls back to reasoning if content stayed empty.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

import httpx

from .models import PlayerSlot

_THINK_TAG_RE = re.compile(
    r"<\s*(think|thinking|reasoning)\s*>.*?<\s*/\s*\1\s*>",
    flags=re.DOTALL | re.IGNORECASE,
)


def strip_thinking(text: str) -> str:
    return _THINK_TAG_RE.sub("", text).strip()


class LLMTimeoutError(TimeoutError):
    def __init__(self, reason: str, elapsed: float, partial_text: str = ""):
        super().__init__(f"{reason} timeout after {elapsed:.1f}s")
        self.reason = reason
        self.elapsed = elapsed
        self.partial_text = partial_text


class LLMRequestError(RuntimeError):
    pass


@dataclass
class CompletionResult:
    text: str
    raw_text: str = ""
    reasoning_text: str = ""
    finish_reason: Optional[str] = None
    elapsed_seconds: float = 0.0
    token_count_estimate: int = 0
    prompt: list[dict[str, Any]] = field(default_factory=list)


StreamCallback = Callable[[str], Any]


async def complete(
    slot: PlayerSlot,
    messages: list[dict[str, Any]],
    *,
    strip_thinking_output: bool = True,
    sampler_overrides: Optional[dict[str, Any]] = None,
    on_delta: Optional[StreamCallback] = None,
) -> CompletionResult:
    url = f"{slot.endpoint}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if slot.api_key:
        headers["Authorization"] = f"Bearer {slot.api_key}"

    sampler = dict(slot.sampler)
    if sampler_overrides:
        sampler.update(sampler_overrides)

    body: dict[str, Any] = {
        "model": slot.model,
        "messages": messages,
        "stream": True,
        **sampler,
    }

    start = time.monotonic()
    parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason: Optional[str] = None
    delta_count = 0

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(slot.timeout, read=None, connect=10.0),
            trust_env=False,
        ) as client:
            async with client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    body_text = (await resp.aread()).decode("utf-8", errors="replace")
                    raise LLMRequestError(
                        f"POST {url} returned HTTP {resp.status_code}. Body: {body_text[:500]}"
                    )

                line_iter = resp.aiter_lines()

                async def hard_wrapper() -> None:
                    nonlocal finish_reason, delta_count
                    while True:
                        wait = slot.inactivity_timeout
                        try:
                            line = await asyncio.wait_for(line_iter.__anext__(), timeout=wait)
                        except StopAsyncIteration:
                            return
                        except asyncio.TimeoutError as e:
                            elapsed = time.monotonic() - start
                            raise LLMTimeoutError(
                                "inactivity", elapsed, partial_text="".join(parts)
                            ) from e

                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if payload == "[DONE]":
                            return
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        delta, fr, reasoning_delta = _extract_delta(evt)
                        if reasoning_delta:
                            reasoning_parts.append(reasoning_delta)
                            # Pipe reasoning through to the UI too -- otherwise
                            # engines that only stream `delta.reasoning` produce
                            # zero visible output in the UI.
                            if on_delta is not None:
                                r = on_delta(reasoning_delta)
                                if asyncio.iscoroutine(r):
                                    await r
                        if delta:
                            parts.append(delta)
                            delta_count += 1
                            if on_delta is not None:
                                r = on_delta(delta)
                                if asyncio.iscoroutine(r):
                                    await r
                        if fr is not None:
                            finish_reason = fr

                try:
                    await asyncio.wait_for(hard_wrapper(), timeout=slot.timeout)
                except asyncio.TimeoutError as e:
                    elapsed = time.monotonic() - start
                    raise LLMTimeoutError(
                        "hard", elapsed, partial_text="".join(parts)
                    ) from e
    except httpx.RequestError as e:
        raise LLMRequestError(f"Could not reach {url}: {e}") from e

    raw_text = "".join(parts)
    reasoning_full = "".join(reasoning_parts)
    if not raw_text.strip() and reasoning_full.strip():
        # Engine streamed only to `delta.reasoning`. Treat the reasoning as the
        # model's actual output -- otherwise we'd lose all content.
        raw_text = reasoning_full

    visible = strip_thinking(raw_text) if strip_thinking_output else raw_text
    if not visible.strip() and raw_text.strip():
        # Strip pass killed everything (model put all content inside <think>).
        # Fall back to raw so callers can still extract nicknames from it.
        visible = raw_text

    return CompletionResult(
        text=visible,
        raw_text=raw_text,
        reasoning_text=reasoning_full,
        finish_reason=finish_reason,
        elapsed_seconds=time.monotonic() - start,
        token_count_estimate=delta_count,
        prompt=messages,
    )


def _extract_delta(evt: dict[str, Any]) -> tuple[str, Optional[str], str]:
    """Pull content delta, finish_reason, and reasoning delta out of one chunk."""
    choices = evt.get("choices") or []
    if not choices:
        return "", None, ""
    choice = choices[0]
    delta = choice.get("delta") or {}
    content = delta.get("content") or ""
    if not isinstance(content, str):
        content = ""
    reasoning = delta.get("reasoning") or ""
    if not isinstance(reasoning, str):
        reasoning = ""
    finish = choice.get("finish_reason")
    return content, finish, reasoning


async def stream_to_text(
    slot: PlayerSlot,
    messages: list[dict[str, Any]],
    **kwargs: Any,
) -> AsyncIterator[str]:
    """Convenience wrapper that yields deltas as they arrive."""
    queue: asyncio.Queue = asyncio.Queue()

    async def producer() -> None:
        try:
            await complete(slot, messages, on_delta=queue.put_nowait, **kwargs)
        finally:
            await queue.put(None)

    task = asyncio.create_task(producer())
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item
    await task
