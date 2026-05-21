"""Core dataclasses for the One Night Werewolf runner.

Kept deliberately plain (no pydantic) — these are internal state objects.
Pydantic is reserved for the config schema and the FastAPI request/response
layer, where validation actually pays for itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Phase(str, Enum):
    """The game state machine. Order matches the canonical phase progression
    in the spec; `advance()` walks this enum top-to-bottom."""

    SETUP = "setup"
    NIGHT_WEREWOLF = "night_werewolf"
    NIGHT_SEER = "night_seer"
    NIGHT_TROUBLEMAKER = "night_troublemaker"
    NIGHT_VILLAGER = "night_villager"
    DAY_INTROS = "day_intros"
    DAY_POPCORN_R1 = "day_popcorn_r1"
    DAY_POPCORN_R2 = "day_popcorn_r2"
    DAY_REFLECTION = "day_reflection"
    VOTE = "vote"
    REVEAL = "reveal"
    AFTERTHOUGHT = "afterthought"
    GAMESKILL_UPDATE = "gameskill_update"
    END = "end"


# Pause-point keys (used in config.game.pause_points and as keys when the
# state machine checks whether to yield). Kept as plain strings rather than an
# enum because the config file uses string lists and the set is small.
PAUSE_POINT_PHASE_TRANSITION = "phase_transition"
PAUSE_POINT_GAMESKILL_COMMIT = "gameskill_commit"


@dataclass
class PlayerSlot:
    """Configuration + identity for one of the four players.

    Sampler params live in a dict because backends differ (temperature for
    most, top_p for some, reasoning_effort for OpenAI-style, etc.) and we
    don't want to enumerate."""

    nickname: str
    endpoint: str
    model: str
    system_prompt: str
    timeout: int = 180
    inactivity_timeout: int = 45
    sampler: dict[str, Any] = field(default_factory=dict)
    api_key: Optional[str] = None


@dataclass
class Turn:
    """One unit of public-chat content. `target`/`question` are populated for
    popcorn turns where the speaker nominates the next speaker."""

    speaker: str
    statement: str
    target: Optional[str] = None
    question: Optional[str] = None
    timestamp: float = 0.0


@dataclass
class NightAction:
    """A night-phase action by one player. `runner_result` is what the runner
    reported back to the model (e.g. "Gemma is holding card 4" for the Seer)
    — None for actors who don't learn the result (Troublemaker, Villager)."""

    role: str            # "werewolf" | "seer" | "troublemaker" | "villager"
    actor: str           # nickname
    action_type: str     # "peek" | "swap" | "none"
    params: dict[str, Any] = field(default_factory=dict)
    runner_result: Optional[str] = None


@dataclass
class GameState:
    """All game-scoped state. Persisted to disk on phase boundaries so a crash
    mid-game leaves a useful breadcrumb."""

    game_id: str
    seed: int
    slots: dict[str, PlayerSlot]                      # by nickname

    # Card holdings. `pre_swap` is the deal; `post_swap` is the truth at vote
    # time. They differ iff the Troublemaker acted.
    card_holder_pre_swap: dict[int, str] = field(default_factory=dict)
    card_holder_post_swap: dict[int, str] = field(default_factory=dict)
    swap_record: Optional[tuple[str, str]] = None     # which two players swapped (None if no-op)

    # Public chat is what every player can see. Private chats are the full
    # per-player message lists (system, synthetic seed, accumulated history).
    public_chat: list[Turn] = field(default_factory=list)
    private_chats: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    night_actions: list[NightAction] = field(default_factory=list)

    # Notes: current notes content per nickname. The runner re-injects this
    # into context on every subsequent turn and overwrites the file when the
    # player produces a fresh notes response.
    notes: dict[str, str] = field(default_factory=dict)

    # State-machine cursor.
    phase: Phase = Phase.SETUP
    round_index: int = 0
    turn_index_in_round: int = 0
    speaker_order: list[str] = field(default_factory=list)

    # Config knobs that the state machine consults at runtime.
    pause_points: list[str] = field(default_factory=list)
    gameskill_auto_commit: bool = True
    hot_memory_last_k_turns: int = 20

    # Vote phase result.
    votes: dict[str, str] = field(default_factory=dict)  # voter_nickname -> voted_nickname

    # Set when the game machine wants the UI to gate before continuing.
    awaiting_gm_advance: bool = False
    awaiting_reason: Optional[str] = None

    # Resumability bookkeeping. `last_completed_phase` is set after each phase
    # handler returns successfully so a resumed runner can skip already-done
    # work. The popcorn starters and reflection order live on state so they
    # survive snapshot/load (otherwise they'd be lost when the runner dies).
    last_completed_phase: Optional[Phase] = None
    r1_starter: Optional[str] = None
    r2_starter: Optional[str] = None
    reflection_order: list[str] = field(default_factory=list)
