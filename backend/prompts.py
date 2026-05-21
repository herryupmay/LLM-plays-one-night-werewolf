"""Prompt templates and message-list builders.

All builders return either a single message (dict) or a list of messages to
append to the player's private chat. Persistent prefix is constructed once
at SETUP via build_prefix().
"""

from __future__ import annotations

from typing import Any, Optional

from .models import PlayerSlot

CARD_NAMES = {1: "Werewolf", 2: "Seer", 3: "Troublemaker", 4: "Villager"}


# ---------- persistent prefix ----------

def build_prefix(
    slot: PlayerSlot,
    dealt_card: int,
    rules_text: str,
    gameskill_text: str,
) -> list[dict[str, Any]]:
    """The stable system + synthetic-seed prefix sent on every call for this
    player in this game. Engines prefix-cache this, so the cost is paid once."""

    seed_user = (
        f"**Your nickname in this game is `{slot.nickname}`.**\n\n"
        f"This is the ONLY name you should use for yourself. Even if your underlying "
        f"model was trained with a different name (e.g. Gemma, Llama, Qwen), in this "
        f"game you are `{slot.nickname}` and nothing else. The other players have "
        f"their own nicknames -- never claim to be them.\n\n"
        f"---\n\n"
        f"Here is the rule guide for One Night Werewolf:\n\n{rules_text.strip()}\n\n"
        f"---\n\n"
        f"You ({slot.nickname}) were dealt **card {dealt_card} -- {CARD_NAMES[dealt_card]}**.\n\n"
        f"Remember: your dealt card may not be your current card by vote time, "
        f"because the Troublemaker can swap silently.\n\n"
    )
    if gameskill_text.strip():
        seed_user += (
            f"---\n\nYour accumulated gameskill from prior games (your own notes "
            f"to yourself; use these to play better):\n\n{gameskill_text.strip()}\n\n"
        )
    seed_user += "Acknowledge that you understand."

    return [
        {"role": "system", "content": slot.system_prompt},
        {"role": "user", "content": seed_user},
        {"role": "assistant", "content": "Understood."},
    ]


# ---------- night phase ----------

def build_night_werewolf_notes_step(other_nicknames: list[str]) -> dict[str, Any]:
    others = ", ".join(sorted(other_nicknames))
    return {
        "role": "user",
        "content": (
            "It is the **Night phase**. The Werewolf is acting first.\n\n"
            f"You are the Werewolf. You are alone -- no other werewolves are in play. "
            f"The other three players are: {others}.\n\n"
            "You have no night action.\n\n"
            "Write your private notes for this game. Use them to plan your day-phase "
            "strategy and to track what other players say. Your notes are persistent "
            "across the game -- they will be re-injected on every subsequent turn -- so "
            "structure them so future-you can scan them quickly.\n\n"
            "Begin your notes with this section, exactly:\n\n"
            "## My goal\n"
            "Win as Werewolf. Win condition: at vote time, the player holding card 1 "
            "(currently me, unless the Troublemaker swapped me out) must NOT be tied "
            "for most votes. Steer suspicion toward someone else without contradicting "
            "what other roles plausibly know.\n\n"
            "Then add any other sections you want (observations, suspicions, plan). "
            "Respond with ONLY the notes content -- it will overwrite your notes file as-is."
        ),
    }


def build_night_seer_pick_step(other_nicknames: list[str]) -> dict[str, Any]:
    options = ", ".join(sorted(other_nicknames))
    return {
        "role": "user",
        "content": (
            "It is the **Night phase**. The Werewolf has just acted. It is now your turn "
            "as the **Seer**.\n\n"
            f"You may peek at one other player's card. The other players are: {options}.\n\n"
            "Reply with **only the nickname** of the player whose card you want to peek at. "
            "No explanation, no formatting -- just the name."
        ),
    }


def build_night_seer_reveal_step(target: str, card_name: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"GM: {target} is currently holding the **{card_name}** card.\n\n"
            "Now write your private notes for this game. Use them to plan your day-phase "
            "strategy and to track what you have learned. Notes are persistent across "
            "the game -- re-injected on every subsequent turn -- so structure them so future-you "
            "can scan them quickly.\n\n"
            "Begin your notes with this section, exactly:\n\n"
            "## My goal\n"
            "Win as Village. Win condition: at vote time, the player holding card 1 "
            "(Werewolf, post-swap) must be tied for most votes. I am the Seer -- share "
            "my peek strategically without immediately giving up that I'm the Seer if "
            "doing so paints a target on me too early.\n\n"
            "Then add: what you peeked, what it implies, and a plan for the day. "
            "Respond with ONLY the notes content -- it will overwrite your notes file as-is."
        ),
    }


def build_night_troublemaker_pick_step(all_nicknames: list[str]) -> dict[str, Any]:
    options = ", ".join(sorted(all_nicknames))
    return {
        "role": "user",
        "content": (
            "It is the **Night phase**. The Seer has just acted. It is now your turn "
            "as the **Troublemaker**.\n\n"
            "You may swap any two players' cards, including swapping your own card with "
            f"another player's. The players in play are: {options}.\n\n"
            "Important: neither swapped player will be told. You yourself will NOT be told "
            "the result of the swap -- only that it happened.\n\n"
            "Reply with **exactly two nicknames separated by a comma**, e.g. `Owen, Gemma`. "
            "No explanation. The two names must be different from each other."
        ),
    }


def build_night_troublemaker_notes_step(swap_pair: tuple) -> dict[str, Any]:
    a, b = swap_pair
    return {
        "role": "user",
        "content": (
            f"GM: You swapped {a} and {b}. You are not told what their cards were before "
            "or after. Your own card may or may not still be the Troublemaker -- if you swapped "
            "yourself, you no longer hold the Troublemaker card.\n\n"
            "Now write your private notes. Begin with:\n\n"
            "## My goal\n"
            "Win as Village. Win condition: at vote time, the player holding card 1 "
            "(Werewolf, post-swap) must be tied for most votes. I am the Troublemaker -- "
            "I shifted the card layout. If any player claims to know another player's card, "
            "I can cross-reference whether the swap I made would invalidate that claim.\n\n"
            "Then add sections for what you swapped, what you can deduce, and a plan. "
            "Respond with ONLY the notes content."
        ),
    }


def build_night_villager_notes_step() -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "It is the **Night phase**. The Werewolf, Seer, and Troublemaker have all acted. "
            "It is now the Villager's turn -- you have no night action.\n\n"
            "Write your private notes for this game. Notes are persistent across the game "
            "and re-injected each turn, so structure them so future-you can scan them quickly.\n\n"
            "Begin with this section, exactly:\n\n"
            "## My goal\n"
            "Win as Village. Win condition: at vote time, the player holding card 1 "
            "(Werewolf, post-swap) must be tied for most votes. I am a Villager with no "
            "information -- I will rely on Seer claims and inconsistencies in other players' "
            "stories to identify the Werewolf.\n\n"
            "Then add a plan for the day. Respond with ONLY the notes content."
        ),
    }


# ---------- day: intros ----------

def build_intro_statement_step(speaker: str, other_nicknames: list[str]) -> dict[str, Any]:
    others = ", ".join(sorted(other_nicknames))
    return {
        "role": "user",
        "content": (
            "It is the **Day phase, Round 0 (Introductions)**. It is your turn to introduce "
            "yourself.\n\n"
            f"The other players are: {others}.\n\n"
            "Give a brief introduction -- 1-3 sentences. You may claim a role or stay vague. "
            "Don't volunteer everything; remember you're playing to win. Reply with ONLY the "
            "introduction text. No reasoning, no headers."
        ),
    }


def build_intro_notes_step() -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Now update your private notes given what you just said and what other players "
            "have introduced themselves as so far. Keep the `## My goal` section unchanged at "
            "the top. Add or revise other sections (observations, suspicions, plan) as needed. "
            "Respond with ONLY the full notes content -- it overwrites your notes file as-is."
        ),
    }


# ---------- day: popcorn ----------

def build_popcorn_statement_step(
    round_index: int,
    is_first_of_round: bool,
    prior_question: Optional[str],
    prior_questioner: Optional[str],
) -> dict[str, Any]:
    intro_clause = (
        f"It is the **Day phase, Round {round_index} (Popcorn Q&A)**, and you have been "
        f"called on to speak."
    )
    if is_first_of_round:
        body = (
            f"{intro_clause} You are the first speaker this round.\n\n"
            "Make a statement -- 1-4 sentences. You may make a claim, ask a leading question, "
            "share an observation, or apply pressure. Don't reveal more than necessary.\n\n"
            "Reply with ONLY the statement text. No headers, no reasoning, no preamble."
        )
    else:
        q = (prior_question or "").strip()
        body = (
            f"{intro_clause}\n\n"
            f"{prior_questioner} just asked you: \"{q}\"\n\n"
            "Respond to their question first, then make a statement of your own -- 2-5 "
            "sentences total. Reply with ONLY the response + statement text. No headers, "
            "no reasoning, no preamble."
        )
    return {"role": "user", "content": body}


def build_popcorn_pick_step(other_nicknames: list[str]) -> dict[str, Any]:
    options = ", ".join(sorted(other_nicknames))
    return {
        "role": "user",
        "content": (
            "Now nominate the **next speaker** for this round. The remaining players who "
            f"have not yet spoken this round are: {options}.\n\n"
            "Reply with **only the nickname** of the player you want to speak next. "
            "No explanation, no formatting -- just the name."
        ),
    }


def build_popcorn_question_step(next_speaker: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"Now formulate your question for {next_speaker}. Keep it short -- one sentence. "
            "Your question will be added to the public chat, so it should pressure them, "
            "extract information, or test their claim. Reply with ONLY the question."
        ),
    }


def build_popcorn_notes_step() -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Now update your private notes given what happened on your turn. Keep the "
            "`## My goal` section unchanged at the top. Update observations, suspicions, "
            "and plan. Respond with ONLY the full notes content -- it overwrites your notes "
            "file as-is."
        ),
    }


# ---------- day: reflection ----------

def build_reflection_notes_step() -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "It is the **Day phase, Round 3 (Silent reflection)**. No more public statements "
            "will be made. Now is the time to settle on a vote.\n\n"
            "Update your private notes with your final read. Keep `## My goal` unchanged. "
            "Add a `## Final read` section: who you think holds the Werewolf card right now "
            "(post-swap), what evidence supports it, and what would change your mind. "
            "Respond with ONLY the full notes content."
        ),
    }


# ---------- vote ----------

def build_vote_step(other_nicknames: list[str]) -> dict[str, Any]:
    options = ", ".join(sorted(other_nicknames))
    return {
        "role": "user",
        "content": (
            "It is the **Vote phase**. Votes are private and simultaneous. Vote for the player "
            "you believe is currently holding the Werewolf card (card 1). You may NOT vote for "
            f"yourself. Your options are: {options}.\n\n"
            "Reply with **only the nickname** of the player you are voting for. No explanation, "
            "no formatting -- just the name."
        ),
    }


# ---------- reveal ----------

def build_reveal_truth_message(
    pre_swap: dict,
    post_swap: dict,
    swap_record: Optional[tuple],
    votes: dict,
    top_voted: list,
    werewolf_holder: str,
    village_wins: bool,
) -> dict[str, Any]:
    lines = [
        "GM: REVEAL. The game is over. Here is everything that happened:",
        "",
        "Dealt cards (start of night):",
    ]
    for card in sorted(pre_swap):
        lines.append(f"  card {card} ({CARD_NAMES[card]}) -> {pre_swap[card]}")
    if swap_record:
        a, b = swap_record
        lines.append(f"\nTroublemaker swap: {a} <-> {b}")
    else:
        lines.append("\nTroublemaker swap: none")

    lines.append("\nFinal cards (post-swap):")
    for card in sorted(post_swap):
        lines.append(f"  card {card} ({CARD_NAMES[card]}) -> {post_swap[card]}")

    lines.append("\nVotes cast:")
    for voter, target in votes.items():
        lines.append(f"  {voter} -> {target}")

    lines.append(f"\nMost-voted (tied if multiple): {', '.join(top_voted) or '(no votes)'}")
    lines.append(f"Werewolf card holder at vote time: {werewolf_holder}")
    lines.append(f"\nOutcome: {'VILLAGE WINS' if village_wins else 'WEREWOLF WINS'}.")

    return {"role": "user", "content": "\n".join(lines)}


# ---------- afterthought ----------

def build_afterthought_step() -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "You have just been shown the full truth of the game -- who held what card when, "
            "who voted for whom, and the outcome.\n\n"
            "Now write a personal afterthought (3-8 sentences). Focus on the gap between what "
            "you BELIEVED during the game and what was actually TRUE. What did you misread? "
            "What clue did you miss or misinterpret? What rule did you reason about incorrectly?\n\n"
            "Be honest and specific. This will be saved as your record for this game. "
            "Respond with the afterthought only."
        ),
    }


# ---------- gameskill update ----------

def build_gameskill_append_step(game_id: str, afterthought_text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"It's time to update your persistent gameskill -- your own notes-to-yourself that "
            f"persist across games. You just wrote this afterthought for {game_id}:\n\n"
            f"---\n{afterthought_text.strip()}\n---\n\n"
            "Now write a new section to APPEND to your gameskill. Format it exactly like:\n\n"
            f"## Lessons from {game_id}\n"
            "- (lesson 1)\n"
            "- (lesson 2)\n"
            "- ...\n\n"
            "Each lesson should be one concise rule-of-thumb you derived from this game that "
            "would help future-you play better. Reply with ONLY the new section markdown, "
            f"starting with `## Lessons from {game_id}` and nothing else."
        ),
    }


def build_gameskill_compress_step(existing_gameskill: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": (
            "Your gameskill has grown long enough that it should be compressed.\n\n"
            "Here is your current gameskill, which is a series of appended `## Lessons from "
            "game_NNN` sections plus any earlier canonical structure:\n\n"
            f"---\n{existing_gameskill.strip()}\n---\n\n"
            "Rewrite it into a single concise document with sections like:\n"
            "- `## Core principles` -- the highest-confidence rules-of-thumb you've derived.\n"
            "- `## Role-specific tactics` -- sub-sections per role.\n"
            "- `## Failure modes I should avoid` -- patterns from past mistakes.\n\n"
            "Preserve any lesson that appeared in multiple games. Drop lessons that were "
            "contradicted by later games. Cut redundancy. Respond with ONLY the rewritten "
            "gameskill markdown."
        ),
    }
