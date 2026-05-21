"""Vote tally and win condition.

Village wins iff the player currently holding card 1 (Werewolf) post-swap is
among those tied for the most votes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass
class TallyResult:
    counts: dict[str, int]
    top_voted: list[str]    # tied for most
    werewolf_holder: str    # nickname currently holding card 1 post-swap
    village_wins: bool


def resolve(votes: dict[str, str], card_holder_post_swap: dict[int, str]) -> TallyResult:
    counts: Counter[str] = Counter()
    for voter, target in votes.items():
        if voter == target:
            raise ValueError(f"{voter} voted for themselves; this should have been validated upstream.")
        counts[target] += 1

    if not counts:
        return TallyResult(counts={}, top_voted=[], werewolf_holder=card_holder_post_swap.get(1, ""), village_wins=False)

    top_count = max(counts.values())
    top_voted = sorted(n for n, c in counts.items() if c == top_count)
    werewolf_holder = card_holder_post_swap[1]
    village_wins = werewolf_holder in top_voted

    return TallyResult(
        counts=dict(counts),
        top_voted=top_voted,
        werewolf_holder=werewolf_holder,
        village_wins=village_wins,
    )
