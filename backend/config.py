"""Config loading and startup validation.

Validation is deliberately strict and fail-loud. The spec's bargain with the
user is: "edit config.yaml, hit start, watch a game." That only holds if any
misconfiguration is caught up-front with an actionable message, not 90s into
a night phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

from .models import PlayerSlot

NICKNAME_RE = re.compile(r"^[A-Za-z0-9]{1,12}$")
DEFAULT_SYSTEM_PROMPT_TEMPLATE = (
    "You are `{nickname}`, a player in One Night Werewolf. You are playing to win.\n\n"
    "**Your name is `{nickname}`.** When you refer to yourself, always say `{nickname}`. "
    "Never call yourself by any other name (such as the name of your underlying model). "
    "The other players have different nicknames; do not confuse yourself with them.\n\n"
    "Hard rules:\n"
    "- You may not vote for yourself.\n"
    "- Swapped players are NOT told they were swapped; the Troublemaker is NOT told the result.\n"
    "- Your dealt card may not be your current card.\n\n"
    "Reply concisely. Do not include reasoning traces in public statements. "
    "If your engine supports it, prefer minimal-reasoning mode (/no_think)."
)


@dataclass
class GameConfig:
    """The `game:` block from config.yaml."""

    hot_memory_last_k_turns: int = 20
    pause_points: list[str] = field(default_factory=list)
    gameskill_auto_commit: bool = True
    random_seed: Optional[int] = None
    gameskill_compress_after_games: int = 5


@dataclass
class AppConfig:
    """The full parsed config."""

    slots: list[PlayerSlot]
    game: GameConfig


class ConfigError(ValueError):
    """Raised for any user-correctable config problem. Message is intended to
    be shown verbatim in the UI."""


def load_config(path: str | Path) -> AppConfig:
    """Parse and statically validate config.yaml. Does NOT touch the network.
    For endpoint reachability + model existence, call `validate_endpoints`."""

    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}. "
            f"Copy config.yaml.example to config.yaml and edit it."
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Could not parse {path} as YAML: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"Config root must be a mapping, got {type(raw).__name__}.")

    slots_raw = raw.get("slots")
    if not isinstance(slots_raw, list) or len(slots_raw) != 4:
        raise ConfigError(
            f"`slots` must be a list of exactly 4 entries; got "
            f"{len(slots_raw) if isinstance(slots_raw, list) else type(slots_raw).__name__}."
        )

    slots = [_parse_slot(s, i) for i, s in enumerate(slots_raw)]

    # Uniqueness check across slots.
    seen: set[str] = set()
    for s in slots:
        if s.nickname in seen:
            raise ConfigError(f"Duplicate nickname `{s.nickname}` in slots.")
        seen.add(s.nickname)

    game_raw = raw.get("game", {}) or {}
    if not isinstance(game_raw, dict):
        raise ConfigError("`game` block must be a mapping.")
    game = _parse_game(game_raw)

    return AppConfig(slots=slots, game=game)


def _parse_slot(raw: Any, index: int) -> PlayerSlot:
    if not isinstance(raw, dict):
        raise ConfigError(f"slots[{index}] must be a mapping.")

    nickname = raw.get("nickname")
    if not isinstance(nickname, str) or not NICKNAME_RE.match(nickname):
        raise ConfigError(
            f"slots[{index}].nickname must be 1-12 alphanumeric characters; got `{nickname!r}`."
        )

    endpoint = raw.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
        raise ConfigError(
            f"slots[{index}].endpoint must be an http(s) URL; got `{endpoint!r}`."
        )

    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise ConfigError(f"slots[{index}].model must be a non-empty string.")

    system_prompt = raw.get("system_prompt") or DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        nickname=nickname
    )
    if not isinstance(system_prompt, str):
        raise ConfigError(f"slots[{index}].system_prompt must be a string if set.")

    timeout = int(raw.get("timeout_seconds", 180))
    inactivity = int(raw.get("inactivity_timeout_seconds", 45))
    api_key = raw.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        raise ConfigError(f"slots[{index}].api_key must be a string or null.")

    sampler: dict[str, Any] = {}
    for key in ("temperature", "top_p", "top_k", "reasoning_effort"):
        if key in raw and raw[key] is not None:
            sampler[key] = raw[key]

    return PlayerSlot(
        nickname=nickname,
        endpoint=endpoint.rstrip("/"),
        model=model,
        system_prompt=system_prompt,
        timeout=timeout,
        inactivity_timeout=inactivity,
        sampler=sampler,
        api_key=api_key,
    )


def _parse_game(raw: dict[str, Any]) -> GameConfig:
    cfg = GameConfig()
    cfg.hot_memory_last_k_turns = int(raw.get("hot_memory_last_k_turns", cfg.hot_memory_last_k_turns))
    pause_points = raw.get("pause_points", [])
    if not isinstance(pause_points, list) or not all(isinstance(p, str) for p in pause_points):
        raise ConfigError("`game.pause_points` must be a list of strings.")
    cfg.pause_points = pause_points
    cfg.gameskill_auto_commit = bool(raw.get("gameskill_auto_commit", cfg.gameskill_auto_commit))
    seed = raw.get("random_seed")
    if seed is not None and not isinstance(seed, int):
        raise ConfigError("`game.random_seed` must be an integer or null.")
    cfg.random_seed = seed
    cfg.gameskill_compress_after_games = int(
        raw.get("gameskill_compress_after_games", cfg.gameskill_compress_after_games)
    )
    return cfg


@dataclass
class EndpointReport:
    """One slot's endpoint-validation result. Returned by `validate_endpoints`
    so the UI can render which specific slot is the problem."""

    nickname: str
    endpoint: str
    model: str
    reachable: bool
    model_present: bool
    available_models: list[str] = field(default_factory=list)
    error: Optional[str] = None


async def validate_endpoints(
    slots: list[PlayerSlot], *, timeout_seconds: float = 10.0
) -> list[EndpointReport]:
    """Ping each slot's `/models` endpoint and check the configured model is
    listed. Returns one report per slot — does NOT raise on failure; the UI
    decides whether the user can proceed (e.g. a backend that doesn't expose
    /models can still be usable)."""

    reports: list[EndpointReport] = []
    # trust_env=False so HTTPS_PROXY/SOCKS_PROXY in the user's shell don't
    # try to route localhost LLM traffic through a corporate proxy.
    async with httpx.AsyncClient(timeout=timeout_seconds, trust_env=False) as client:
        for slot in slots:
            report = EndpointReport(
                nickname=slot.nickname,
                endpoint=slot.endpoint,
                model=slot.model,
                reachable=False,
                model_present=False,
            )
            url = f"{slot.endpoint}/models"
            headers: dict[str, str] = {}
            if slot.api_key:
                headers["Authorization"] = f"Bearer {slot.api_key}"
            try:
                resp = await client.get(url, headers=headers)
                report.reachable = resp.status_code < 500
                if resp.status_code >= 400:
                    report.error = (
                        f"GET {url} returned HTTP {resp.status_code}. "
                        f"Body: {resp.text[:200]}"
                    )
                else:
                    data = resp.json()
                    ids = _extract_model_ids(data)
                    report.available_models = ids
                    report.model_present = slot.model in ids
                    if not report.model_present and ids:
                        report.error = (
                            f"Model `{slot.model}` not listed at {url}. "
                            f"Available: {', '.join(ids[:10])}"
                            f"{' ...' if len(ids) > 10 else ''}"
                        )
            except httpx.RequestError as e:
                report.error = f"Could not reach {url}: {e}"
            except ValueError as e:
                # JSON parse failure — backend responded but not JSON.
                report.reachable = True
                report.error = f"{url} returned non-JSON response: {e}"
            reports.append(report)
    return reports


def _extract_model_ids(payload: Any) -> list[str]:
    """OpenAI shape: {"data": [{"id": "..."}, ...]}.
    Tolerate ollama shape too: {"models": [{"name": "..."}]}."""
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("models") or []
        out: list[str] = []
        for entry in data:
            if isinstance(entry, dict):
                ident = entry.get("id") or entry.get("name")
                if isinstance(ident, str):
                    out.append(ident)
            elif isinstance(entry, str):
                out.append(entry)
        return out
    return []
