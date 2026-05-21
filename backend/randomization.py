"""Seeded RNG for all game randomness.

Three independent shuffles per the spec — card deal, intro order, popcorn
round starters — all driven by one seeded Random instance so a recorded
seed.json fully reproduces the game's structural randomness."""

from __future__ import annotations

import random
import secrets
from dataclasses import dataclass


@dataclass
class Deal:
    """Output of the setup shuffle. `card_holders` maps card number → nickname."""

    seed: int
    card_holders: dict[int, str]
    intro_order: list[str]
    r1_starter: str
    r2_starter: str
    reflection_order: list[str]


def make_seed(explicit: int | None) -> int:
    """Use the explicit seed if given; otherwise generate a fresh 63-bit int
    so it fits in JSON/YAML without surprises."""
    if explicit is not None:
        return int(explicit)
    return secrets.randbits(63)


def deal_game(nicknames: list[str], seed: int) -> Deal:
    """Produce all the game-start shuffles deterministically from one seed.

    Card 1=Werewolf, 2=Seer, 3=Troublemaker, 4=Villager (per rules.md)."""

    if len(nicknames) != 4:
        raise ValueError(f"deal_game expects 4 nicknames, got {len(nicknames)}.")

    rng = random.Random(seed)

    # Card deal: shuffle [1,2,3,4] against the four nicknames in some stable
    # order (sorted) so the seed alone determines outcome.
    cards = [1, 2, 3, 4]
    rng.shuffle(cards)
    sorted_nicks = sorted(nicknames)
    card_holders = {card: nick for card, nick in zip(cards, sorted_nicks)}

    intro_order = list(nicknames)
    rng.shuffle(intro_order)

    r1_starter = rng.choice(nicknames)
    r2_starter = rng.choice(nicknames)

    reflection_order = list(nicknames)
    rng.shuffle(reflection_order)

    return Deal(
        seed=seed,
        card_holders=card_holders,
        intro_order=intro_order,
        r1_starter=r1_starter,
        r2_starter=r2_starter,
        reflection_order=reflection_order,
    )
