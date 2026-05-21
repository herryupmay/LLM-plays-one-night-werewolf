"""Transcript and game-log writers.

`transcript.jsonl` is the machine-readable record of every inference call,
intervention, and phase boundary. Append-only JSONL for crash-safety.

`game_log.md` is the human-readable post-game artifact: cast, swap, public
chat, vote tally, outcome, afterthoughts as appendices.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

from .notes import afterthought_path, game_dir

if TYPE_CHECKING:
    from .models import GameState

CARD_NAMES = {1: "Werewolf", 2: "Seer", 3: "Troublemaker", 4: "Villager"}


def transcript_path(workspace_root: Path, game_id: str) -> Path:
    return game_dir(workspace_root, game_id) / "transcript.jsonl"


def seed_path(workspace_root: Path, game_id: str) -> Path:
    return game_dir(workspace_root, game_id) / "seed.json"


def game_log_path(workspace_root: Path, game_id: str) -> Path:
    return game_dir(workspace_root, game_id) / "game_log.md"


def log_event(workspace_root: Path, game_id: str, event: dict[str, Any]) -> None:
    event.setdefault("timestamp", time.time())
    p = transcript_path(workspace_root, game_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def write_seed(
    workspace_root: Path,
    game_id: str,
    *,
    seed: int,
    card_holders: dict[int, str],
    intro_order: list[str],
    r1_starter: str,
    r2_starter: str,
) -> Path:
    p = seed_path(workspace_root, game_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "seed": seed,
                "card_holders": card_holders,
                "intro_order": intro_order,
                "r1_starter": r1_starter,
                "r2_starter": r2_starter,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return p


def read_transcript(workspace_root: Path, game_id: str) -> list[dict[str, Any]]:
    p = transcript_path(workspace_root, game_id)
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def write_game_log(workspace_root: Path, state: "GameState") -> Path:
    """Compose a human-readable game log from the final state."""

    lines: list[str] = []
    lines.append(f"# {state.game_id} -- game log\n")

    lines.append("## Cast")
    for nick, slot in state.slots.items():
        lines.append(f"- **{nick}** -- model `{slot.model}` @ `{slot.endpoint}`")
    lines.append("")

    lines.append("## Deal (pre-swap)")
    for card in sorted(state.card_holder_pre_swap):
        lines.append(f"- card {card} ({CARD_NAMES[card]}) -> {state.card_holder_pre_swap[card]}")
    lines.append("")

    if state.swap_record:
        a, b = state.swap_record
        lines.append(f"## Troublemaker swap\n\n{a} <-> {b}\n")
    else:
        lines.append("## Troublemaker swap\n\n(no swap)\n")

    lines.append("## Final cards (post-swap)")
    for card in sorted(state.card_holder_post_swap):
        lines.append(f"- card {card} ({CARD_NAMES[card]}) -> {state.card_holder_post_swap[card]}")
    lines.append("")

    lines.append("## Public chat")
    for turn in state.public_chat:
        if turn.target and turn.question:
            lines.append(f"- **{turn.speaker}**: {turn.statement}")
            lines.append(f"  - -> **{turn.target}**: _{turn.question}_")
        else:
            lines.append(f"- **{turn.speaker}**: {turn.statement}")
    lines.append("")

    lines.append("## Votes")
    for voter, target in state.votes.items():
        lines.append(f"- {voter} -> {target}")
    lines.append("")

    from .tally import resolve
    if state.votes:
        result = resolve(state.votes, state.card_holder_post_swap)
        lines.append("## Outcome")
        lines.append(f"- Top voted: {', '.join(result.top_voted)}")
        lines.append(f"- Werewolf card holder (post-swap): {result.werewolf_holder}")
        lines.append(f"- **{'Village wins' if result.village_wins else 'Werewolf wins'}**")
        lines.append("")

    lines.append("## Afterthoughts")
    for nick in state.slots:
        p = afterthought_path(workspace_root, state.game_id, nick)
        if p.exists():
            lines.append(f"\n### {nick}\n")
            lines.append(p.read_text(encoding="utf-8").strip())
            lines.append("")

    path = game_log_path(workspace_root, state.game_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
