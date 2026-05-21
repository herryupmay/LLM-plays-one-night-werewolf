"""FastAPI routes.

Single-game design for v1: at most one GameRunner active in the process. The
runner is held on `app.state.runner`. Starting a new game while one is in
flight returns 409.
"""

from __future__ import annotations

import asyncio
import json
import pickle
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config as config_mod
from .game import GameRunner

# Resolve the workspace root (parent of backend/). Override with env var if
# you ever want to run from a different cwd.
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="One Night Werewolf Runner")

# Serve the frontend.
FRONTEND_DIR = WORKSPACE_ROOT / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    """Serve the single-page app."""
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    """Return the parsed config, useful for the setup screen to pre-populate.
    404 if no config.yaml exists yet."""
    cfg_path = WORKSPACE_ROOT / "config.yaml"
    if not cfg_path.exists():
        raise HTTPException(404, detail="config.yaml not found; copy from config.yaml.example")
    try:
        cfg = config_mod.load_config(cfg_path)
    except config_mod.ConfigError as e:
        raise HTTPException(400, detail=str(e))
    return {
        "slots": [
            {"nickname": s.nickname, "endpoint": s.endpoint, "model": s.model}
            for s in cfg.slots
        ],
        "game": {
            "pause_points": cfg.game.pause_points,
            "gameskill_auto_commit": cfg.game.gameskill_auto_commit,
            "random_seed": cfg.game.random_seed,
        },
    }


@app.post("/api/validate")
async def validate() -> dict[str, Any]:
    """Run static config validation + endpoint reachability check.

    UI is expected to call this before /api/start so the setup screen can
    show a clean green/red status per slot."""
    cfg_path = WORKSPACE_ROOT / "config.yaml"
    if not cfg_path.exists():
        return {
            "ok": False,
            "config_error": "config.yaml not found. Copy config.yaml.example to config.yaml.",
        }
    try:
        cfg = config_mod.load_config(cfg_path)
    except config_mod.ConfigError as e:
        return {"ok": False, "config_error": str(e)}

    reports = await config_mod.validate_endpoints(cfg.slots)
    all_ok = all(r.reachable for r in reports)
    return {
        "ok": all_ok,
        "config_error": None,
        "endpoint_reports": [
            {
                "nickname": r.nickname,
                "endpoint": r.endpoint,
                "model": r.model,
                "reachable": r.reachable,
                "model_present": r.model_present,
                "available_models": r.available_models[:20],
                "error": r.error,
            }
            for r in reports
        ],
    }


@app.post("/api/start")
async def start_game() -> dict[str, Any]:
    """Kick off a new game in a background task."""
    runner: Optional[GameRunner] = getattr(app.state, "runner", None)
    if runner is not None and getattr(app.state, "runner_task", None) is not None:
        task: asyncio.Task = app.state.runner_task
        if not task.done():
            raise HTTPException(409, detail="A game is already in progress.")

    cfg_path = WORKSPACE_ROOT / "config.yaml"
    try:
        cfg = config_mod.load_config(cfg_path)
    except config_mod.ConfigError as e:
        raise HTTPException(400, detail=str(e))

    runner = GameRunner(cfg, WORKSPACE_ROOT)
    app.state.runner = runner
    app.state.runner_task = asyncio.create_task(runner.run(), name=f"runner-{runner.game_id}")
    return {"game_id": runner.game_id, "seed": runner.state.seed}


@app.post("/api/advance")
async def advance() -> dict[str, str]:
    """Unblock the runner from a pause-point."""
    runner: Optional[GameRunner] = getattr(app.state, "runner", None)
    if runner is None:
        raise HTTPException(404, detail="No game in progress.")
    await runner.advance()
    return {"status": "advanced"}


@app.get("/api/state")
async def state_snapshot() -> dict[str, Any]:
    """Lightweight snapshot the UI can poll (in addition to the SSE stream)."""
    runner: Optional[GameRunner] = getattr(app.state, "runner", None)
    if runner is None:
        return {"running": False}
    st = runner.state
    return {
        "running": True,
        "game_id": st.game_id,
        "phase": st.phase.value,
        "awaiting_gm_advance": st.awaiting_gm_advance,
        "awaiting_reason": st.awaiting_reason,
        "card_holders_post_swap": st.card_holder_post_swap,
        "public_chat": [
            {
                "speaker": t.speaker,
                "statement": t.statement,
                "target": t.target,
                "question": t.question,
            }
            for t in st.public_chat
        ],
    }




@app.post("/api/discover")
async def discover_models(payload: dict[str, Any]) -> dict[str, Any]:
    """Hit an OpenAI-compatible /v1/models on behalf of the setup UI so it
    can populate the per-slot model dropdowns before any config is saved.

    Returns {ok: True, models: [...]} on success, {ok: False, error: "..."}
    on failure. Never raises so the UI can show inline error messages."""
    import httpx
    endpoint = (payload.get("endpoint") or "").rstrip("/")
    api_key = payload.get("api_key") or None

    if not endpoint or not endpoint.startswith(("http://", "https://")):
        return {"ok": False, "error": "endpoint must be an http(s) URL"}

    url = f"{endpoint}/models"
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            resp = await client.get(url, headers=headers)
    except httpx.RequestError as e:
        return {"ok": False, "error": f"could not reach {url}: {e}"}

    if resp.status_code >= 400:
        return {
            "ok": False,
            "error": f"HTTP {resp.status_code} from {url}. {resp.text[:300]}",
        }

    try:
        data = resp.json()
    except ValueError as e:
        return {"ok": False, "error": f"non-JSON response from {url}: {e}"}

    ids = config_mod._extract_model_ids(data)
    return {"ok": True, "models": ids, "count": len(ids)}


@app.post("/api/save_config")
async def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist the UI-built config to config.yaml so /api/start can read it.

    Validates structure (4 slots, unique nicknames, valid model fields, etc.)
    but does NOT touch the network. For network validation, the UI follows up
    with /api/validate."""
    import yaml
    slots_raw = payload.get("slots")
    if not isinstance(slots_raw, list) or len(slots_raw) != 4:
        raise HTTPException(400, detail="must have exactly 4 slots")

    # Sanity-check each slot up-front and build the YAML structure.
    clean_slots: list[dict[str, Any]] = []
    seen_nicks: set[str] = set()
    for i, s in enumerate(slots_raw):
        if not isinstance(s, dict):
            raise HTTPException(400, detail=f"slots[{i}] must be an object")
        nick = (s.get("nickname") or "").strip()
        if not config_mod.NICKNAME_RE.match(nick):
            raise HTTPException(400, detail=f"slots[{i}].nickname must be 1-12 alphanumeric chars")
        if nick in seen_nicks:
            raise HTTPException(400, detail=f"duplicate nickname `{nick}`")
        seen_nicks.add(nick)

        endpoint = (s.get("endpoint") or "").strip()
        if not endpoint.startswith(("http://", "https://")):
            raise HTTPException(400, detail=f"slots[{i}].endpoint must be an http(s) URL")

        model = (s.get("model") or "").strip()
        if not model:
            raise HTTPException(400, detail=f"slots[{i}].model is required")

        entry: dict[str, Any] = {
            "nickname": nick,
            "endpoint": endpoint,
            "model": model,
            "timeout_seconds": int(s.get("timeout_seconds", 180)),
            "inactivity_timeout_seconds": int(s.get("inactivity_timeout_seconds", 45)),
            "temperature": float(s.get("temperature", 0.7)),
        }
        ak = s.get("api_key")
        if ak:
            entry["api_key"] = ak
        clean_slots.append(entry)

    game_raw = payload.get("game") or {}
    game: dict[str, Any] = {
        "hot_memory_last_k_turns": int(game_raw.get("hot_memory_last_k_turns", 20)),
        "pause_points": list(game_raw.get("pause_points") or []),
        "gameskill_auto_commit": bool(game_raw.get("gameskill_auto_commit", True)),
        "gameskill_compress_after_games": int(game_raw.get("gameskill_compress_after_games", 5)),
    }
    seed = game_raw.get("random_seed")
    if seed is not None and seed != "":
        try:
            game["random_seed"] = int(seed)
        except (TypeError, ValueError):
            raise HTTPException(400, detail="random_seed must be an integer or null")
    else:
        game["random_seed"] = None

    full = {"slots": clean_slots, "game": game}

    cfg_path = WORKSPACE_ROOT / "config.yaml"
    cfg_path.write_text(
        yaml.dump(full, sort_keys=False, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Re-parse to surface any sneaky errors the field-level check missed.
    try:
        config_mod.load_config(cfg_path)
    except config_mod.ConfigError as e:
        raise HTTPException(400, detail=f"config saved but failed validation: {e}")

    return {"ok": True, "path": str(cfg_path), "slot_count": len(clean_slots)}



@app.get("/api/game_state/{game_id}")
async def get_game_state(game_id: str) -> dict[str, Any]:
    """Return the snapshotted state of a game as JSON so the UI can replay
    everything that already happened (public chat, night actions, notes,
    votes) when resuming."""
    snap = WORKSPACE_ROOT / "games" / game_id / "state.pickle"
    if not snap.exists():
        raise HTTPException(404, detail=f"No snapshot for {game_id}")
    try:
        with snap.open("rb") as f:
            state = pickle.load(f)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, detail=f"Could not load snapshot: {e}")

    return {
        "game_id": state.game_id,
        "phase": state.phase.value,
        "last_completed_phase": (
            state.last_completed_phase.value
            if state.last_completed_phase else None
        ),
        "card_holder_pre_swap": state.card_holder_pre_swap,
        "card_holder_post_swap": state.card_holder_post_swap,
        "swap_record": list(state.swap_record) if state.swap_record else None,
        "speaker_order": state.speaker_order,
        "r1_starter": state.r1_starter,
        "r2_starter": state.r2_starter,
        "public_chat": [
            {
                "speaker": t.speaker,
                "statement": t.statement,
                "target": t.target,
                "question": t.question,
            }
            for t in state.public_chat
        ],
        "night_actions": [
            {
                "role": a.role,
                "actor": a.actor,
                "action_type": a.action_type,
                "params": a.params,
                "runner_result": a.runner_result,
            }
            for a in state.night_actions
        ],
        "notes": dict(state.notes),
        "votes": dict(state.votes),
    }


@app.get("/api/games")
async def list_games() -> dict[str, Any]:
    """List games on disk that can be resumed (have a state.pickle but no
    game_log.md yet -- i.e. were started but didn't complete)."""
    games_root = WORKSPACE_ROOT / "games"
    if not games_root.exists():
        return {"games": []}
    out: list[dict[str, Any]] = []
    for d in sorted(games_root.iterdir(), reverse=True):
        if not d.is_dir() or not d.name.startswith("game_"):
            continue
        snap = d / "state.pickle"
        completed = (d / "game_log.md").exists()
        if not snap.exists() or completed:
            continue
        try:
            with snap.open("rb") as f:
                state = pickle.load(f)
            last = state.last_completed_phase.value if state.last_completed_phase else None
        except Exception as e:  # noqa: BLE001
            last = f"unreadable: {e}"
        out.append({
            "game_id": d.name,
            "last_completed_phase": last,
            "mtime": snap.stat().st_mtime,
        })
    return {"games": out}


@app.post("/api/resume")
async def resume_game(payload: dict[str, Any]) -> dict[str, Any]:
    """Resume a game from its snapshot. Takes {"game_id": "game_NNN"}."""
    runner: Optional[GameRunner] = getattr(app.state, "runner", None)
    if runner is not None and getattr(app.state, "runner_task", None) is not None:
        task: asyncio.Task = app.state.runner_task
        if not task.done():
            raise HTTPException(409, detail="A game is already in progress.")

    game_id = payload.get("game_id")
    if not isinstance(game_id, str) or not game_id:
        raise HTTPException(400, detail="game_id required")

    cfg_path = WORKSPACE_ROOT / "config.yaml"
    try:
        cfg = config_mod.load_config(cfg_path)
    except config_mod.ConfigError as e:
        raise HTTPException(400, detail=str(e))

    try:
        runner = GameRunner.from_snapshot(cfg, WORKSPACE_ROOT, game_id)
    except FileNotFoundError as e:
        raise HTTPException(404, detail=str(e))

    app.state.runner = runner
    app.state.runner_task = asyncio.create_task(runner.run(), name=f"runner-{runner.game_id}")
    return {
        "game_id": runner.game_id,
        "resumed_from_phase": (
            runner.state.last_completed_phase.value
            if runner.state.last_completed_phase else None
        ),
    }


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    """Server-sent events drain of the runner's event queue."""
    runner: Optional[GameRunner] = getattr(app.state, "runner", None)
    if runner is None:
        raise HTTPException(404, detail="No game in progress.")

    async def gen():
        # Emit a synthetic 'hello' immediately so the client sees the
        # connection working.
        yield "event: hello\ndata: {}\n\n"
        while True:
            try:
                event = await asyncio.wait_for(runner.events.get(), timeout=15.0)
            except asyncio.TimeoutError:
                # Keep-alive comment so proxies don't drop us.
                yield ": keepalive\n\n"
                continue
            yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
            if event.get("type") in {"game_ended", "phase_not_implemented", "phase_error"}:
                # Don't close — the client may want to reconnect — but stop
                # pumping until the queue gets more (or the client tears down).
                continue

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
