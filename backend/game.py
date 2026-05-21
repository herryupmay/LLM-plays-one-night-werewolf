"""Game runner: phase state machine + per-phase handlers.

Walks all 14 phases of One Night Werewolf. Pauses at configured pause-points
(phase_transition, gameskill_commit). Emits structured events to a queue
drained by the FastAPI SSE endpoint.
"""

from __future__ import annotations

import asyncio
import pickle
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import llm_client, logger, notes, prompts, randomization, tally
from .config import AppConfig
from .models import (
    PAUSE_POINT_GAMESKILL_COMMIT,
    PAUSE_POINT_PHASE_TRANSITION,
    GameState,
    NightAction,
    Phase,
    PlayerSlot,
    Turn,
)

CARD_NAMES = {1: "Werewolf", 2: "Seer", 3: "Troublemaker", 4: "Villager"}


class GameRunner:
    """Owns one game's state and drives it through the phase machine."""

    def __init__(self, app_config: AppConfig, workspace_root: Path):
        self.config = app_config
        self.root = workspace_root
        self.rules_text = (workspace_root / "backend" / "rules.md").read_text(encoding="utf-8")

        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._advance_signal = asyncio.Event()
        self.state = self._build_initial_state()

    # ---------- public API ----------

    @property
    def game_id(self) -> str:
        return self.state.game_id

    @property
    def game_dir(self) -> Path:
        return self.root / "games" / self.game_id

    async def advance(self) -> None:
        self._advance_signal.set()

    async def run(self) -> None:
        await self._emit({
            "type": "game_started",
            "game_id": self.game_id,
            "resumed": self.state.last_completed_phase is not None,
            "resumed_from_phase": (
                self.state.last_completed_phase.value
                if self.state.last_completed_phase else None
            ),
        })

        phase_order = list(self._phase_sequence())
        last_done = self.state.last_completed_phase
        skip_until_after = None
        if last_done is not None:
            skip_until_after = last_done

        for phase in phase_order:
            # Skip phases already completed on a previous run (resume case).
            if skip_until_after is not None:
                if phase == skip_until_after:
                    skip_until_after = None  # next iteration runs
                continue

            self.state.phase = phase
            await self._emit({"type": "phase_entered", "phase": phase.value})

            handler = self._handler_for(phase)
            try:
                await handler()
            except NotImplementedError as e:
                await self._emit({
                    "type": "phase_not_implemented",
                    "phase": phase.value,
                    "message": str(e),
                })
                # Snapshot so a future build with that phase implemented can resume.
                self._snapshot("not_implemented")
                return
            except Exception as e:
                await self._emit({
                    "type": "phase_error",
                    "phase": phase.value,
                    "error": type(e).__name__,
                    "message": str(e),
                })
                logger.log_event(self.root, self.game_id, {
                    "kind": "error",
                    "phase": phase.value,
                    "error": type(e).__name__,
                    "message": str(e),
                })
                # Snapshot the in-memory state before unwinding so the GM
                # can restart the engine and resume.
                self._snapshot("error")
                raise

            self.state.last_completed_phase = phase
            self._snapshot("phase_complete")
            await self._maybe_pause(after_phase=phase)

        await self._emit({"type": "game_ended", "game_id": self.game_id})

    # ---------- phase dispatch ----------

    def _phase_sequence(self) -> list[Phase]:
        return list(Phase)

    def _handler_for(self, phase: Phase):
        return {
            Phase.SETUP: self._phase_setup,
            Phase.NIGHT_WEREWOLF: self._phase_night_werewolf,
            Phase.NIGHT_SEER: self._phase_night_seer,
            Phase.NIGHT_TROUBLEMAKER: self._phase_night_troublemaker,
            Phase.NIGHT_VILLAGER: self._phase_night_villager,
            Phase.DAY_INTROS: self._phase_day_intros,
            Phase.DAY_POPCORN_R1: self._phase_day_popcorn_r1,
            Phase.DAY_POPCORN_R2: self._phase_day_popcorn_r2,
            Phase.DAY_REFLECTION: self._phase_day_reflection,
            Phase.VOTE: self._phase_vote,
            Phase.REVEAL: self._phase_reveal,
            Phase.AFTERTHOUGHT: self._phase_afterthought,
            Phase.GAMESKILL_UPDATE: self._phase_gameskill_update,
            Phase.END: self._phase_end,
        }[phase]

    async def _maybe_pause(self, *, after_phase: Phase) -> None:
        pause_points = set(self.state.pause_points)
        should_pause = False
        if PAUSE_POINT_PHASE_TRANSITION in pause_points and after_phase != Phase.END:
            should_pause = True
        if PAUSE_POINT_GAMESKILL_COMMIT in pause_points and after_phase == Phase.GAMESKILL_UPDATE:
            should_pause = True

        if should_pause:
            self.state.awaiting_gm_advance = True
            self.state.awaiting_reason = f"after_{after_phase.value}"
            await self._emit({
                "type": "awaiting_gm_advance",
                "after_phase": after_phase.value,
            })
            self._advance_signal.clear()
            await self._advance_signal.wait()
            self.state.awaiting_gm_advance = False
            self.state.awaiting_reason = None
            await self._emit({"type": "gm_advanced", "from_phase": after_phase.value})

    # ---------- phase handlers: setup + night ----------

    async def _phase_setup(self) -> None:
        nicknames = [slot.nickname for slot in self.config.slots]
        deal = randomization.deal_game(nicknames, seed=self.state.seed)

        self.state.card_holder_pre_swap = dict(deal.card_holders)
        self.state.card_holder_post_swap = dict(deal.card_holders)
        self.state.speaker_order = list(deal.intro_order)

        logger.write_seed(
            self.root, self.game_id,
            seed=deal.seed,
            card_holders=deal.card_holders,
            intro_order=deal.intro_order,
            r1_starter=deal.r1_starter,
            r2_starter=deal.r2_starter,
        )

        card_by_nick = {nick: card for card, nick in deal.card_holders.items()}
        for slot in self.state.slots.values():
            gameskill = notes.read_gameskill(self.root, slot.nickname)
            prefix = prompts.build_prefix(
                slot=slot,
                dealt_card=card_by_nick[slot.nickname],
                rules_text=self.rules_text,
                gameskill_text=gameskill,
            )
            self.state.private_chats[slot.nickname] = prefix

        self.state.r1_starter = deal.r1_starter
        self.state.r2_starter = deal.r2_starter
        self.state.reflection_order = list(deal.reflection_order)

        logger.log_event(self.root, self.game_id, {
            "kind": "setup_complete",
            "seed": deal.seed,
            "card_holders": deal.card_holders,
            "intro_order": deal.intro_order,
            "r1_starter": deal.r1_starter,
            "r2_starter": deal.r2_starter,
        })

        await self._emit({
            "type": "setup_complete",
            "intro_order": deal.intro_order,
            "r1_starter": deal.r1_starter,
            "r2_starter": deal.r2_starter,
        })
        await self._emit({
            "type": "announcement",
            "text": f"Game {self.game_id} -- cards dealt. Night begins.",
        })

    async def _phase_night_werewolf(self) -> None:
        ww_nick = self.state.card_holder_post_swap[1]
        slot = self.state.slots[ww_nick]
        others = [n for n in self.state.slots if n != ww_nick]

        messages = list(self.state.private_chats[ww_nick])
        step = prompts.build_night_werewolf_notes_step(others)
        messages.append(step)

        await self._emit({
            "type": "private_action_started",
            "nickname": ww_nick,
            "kind": "night_werewolf_notes",
            "role": "werewolf",
            "note": "writing night notes (no action)",
        })

        result = await self._run_completion(
            slot, messages, kind="night_werewolf_notes", strip_thinking_output=False,
        )

        notes.save_notes(self.root, self.game_id, ww_nick, result.text)
        self.state.notes[ww_nick] = result.text
        self.state.night_actions.append(NightAction(
            role="werewolf", actor=ww_nick, action_type="none",
            params={}, runner_result=None,
        ))
        self.state.private_chats[ww_nick].append(step)
        self.state.private_chats[ww_nick].append({"role": "assistant", "content": result.text})
        await self._emit({
            "type": "private_notes_updated",
            "nickname": ww_nick, "preview": result.text[:120],
        })

    async def _phase_night_seer(self) -> None:
        seer_nick = self.state.card_holder_pre_swap[2]
        slot = self.state.slots[seer_nick]
        others = [n for n in self.state.slots if n != seer_nick]

        await self._emit({
            "type": "private_action_started",
            "nickname": seer_nick, "kind": "night_seer_pick",
            "role": "seer", "note": "picking a player to peek at",
        })

        pick_step = prompts.build_night_seer_pick_step(others)
        target = await self._ask_for_nickname(
            slot=slot,
            base_messages=self.state.private_chats[seer_nick],
            step_message=pick_step,
            valid_choices=others,
            kind="night_seer_pick",
        )

        self.state.private_chats[seer_nick].append(pick_step)
        self.state.private_chats[seer_nick].append({"role": "assistant", "content": target})

        revealed_card = next(
            card for card, nick in self.state.card_holder_pre_swap.items() if nick == target
        )
        card_name = CARD_NAMES[revealed_card]

        await self._emit({
            "type": "gm_reveal",
            "nickname": seer_nick,
            "text": f"{target} is currently holding the {card_name} card.",
        })

        reveal_step = prompts.build_night_seer_reveal_step(target, card_name)
        await self._emit({
            "type": "private_action_started",
            "nickname": seer_nick, "kind": "night_seer_notes",
            "role": "seer", "note": "reflecting on peek + writing notes",
        })

        messages = list(self.state.private_chats[seer_nick]) + [reveal_step]
        result = await self._run_completion(
            slot, messages, kind="night_seer_notes", strip_thinking_output=False,
        )

        notes.save_notes(self.root, self.game_id, seer_nick, result.text)
        self.state.notes[seer_nick] = result.text
        self.state.night_actions.append(NightAction(
            role="seer", actor=seer_nick, action_type="peek",
            params={"target": target}, runner_result=f"{target}={card_name}",
        ))
        self.state.private_chats[seer_nick].append(reveal_step)
        self.state.private_chats[seer_nick].append({"role": "assistant", "content": result.text})
        await self._emit({
            "type": "private_notes_updated",
            "nickname": seer_nick, "preview": result.text[:120],
        })

    async def _phase_night_troublemaker(self) -> None:
        tm_nick = self.state.card_holder_pre_swap[3]
        slot = self.state.slots[tm_nick]
        all_players = list(self.state.slots.keys())

        await self._emit({
            "type": "private_action_started",
            "nickname": tm_nick, "kind": "night_troublemaker_pick",
            "role": "troublemaker", "note": "picking two players to swap",
        })

        pick_step = prompts.build_night_troublemaker_pick_step(all_players)
        swap_pair = await self._ask_for_two_nicknames(
            slot=slot,
            base_messages=self.state.private_chats[tm_nick],
            step_message=pick_step,
            valid_choices=all_players,
            kind="night_troublemaker_pick",
        )

        a, b = swap_pair
        card_a = next(c for c, n in self.state.card_holder_post_swap.items() if n == a)
        card_b = next(c for c, n in self.state.card_holder_post_swap.items() if n == b)
        self.state.card_holder_post_swap[card_a] = b
        self.state.card_holder_post_swap[card_b] = a
        self.state.swap_record = (a, b)

        # GM-only event: the human watching the game (and the cheat sheet
        # widget) sees the swap, but this is NOT injected into any player's
        # private chat. Per spec: "neither swapped player is told they were
        # involved; the Troublemaker is not told the result; everyone else
        # is not told it happened at all."
        await self._emit({
            "type": "gm_only_swap",
            "a": a, "b": b,
        })

        self.state.private_chats[tm_nick].append(pick_step)
        self.state.private_chats[tm_nick].append({"role": "assistant", "content": f"{a}, {b}"})

        notes_step = prompts.build_night_troublemaker_notes_step(swap_pair)
        await self._emit({
            "type": "private_action_started",
            "nickname": tm_nick, "kind": "night_troublemaker_notes",
            "role": "troublemaker", "note": "writing notes",
        })

        messages = list(self.state.private_chats[tm_nick]) + [notes_step]
        result = await self._run_completion(
            slot, messages, kind="night_troublemaker_notes", strip_thinking_output=False,
        )

        notes.save_notes(self.root, self.game_id, tm_nick, result.text)
        self.state.notes[tm_nick] = result.text
        self.state.night_actions.append(NightAction(
            role="troublemaker", actor=tm_nick, action_type="swap",
            params={"target1": a, "target2": b}, runner_result=None,
        ))
        self.state.private_chats[tm_nick].append(notes_step)
        self.state.private_chats[tm_nick].append({"role": "assistant", "content": result.text})
        await self._emit({
            "type": "private_notes_updated",
            "nickname": tm_nick, "preview": result.text[:120],
        })

    async def _phase_night_villager(self) -> None:
        villager_nick = self.state.card_holder_pre_swap[4]
        slot = self.state.slots[villager_nick]

        await self._emit({
            "type": "private_action_started",
            "nickname": villager_nick, "kind": "night_villager_notes",
            "role": "villager", "note": "writing notes (no action)",
        })

        step = prompts.build_night_villager_notes_step()
        messages = list(self.state.private_chats[villager_nick]) + [step]
        result = await self._run_completion(
            slot, messages, kind="night_villager_notes", strip_thinking_output=False,
        )

        notes.save_notes(self.root, self.game_id, villager_nick, result.text)
        self.state.notes[villager_nick] = result.text
        self.state.night_actions.append(NightAction(
            role="villager", actor=villager_nick, action_type="none",
            params={}, runner_result=None,
        ))
        self.state.private_chats[villager_nick].append(step)
        self.state.private_chats[villager_nick].append({"role": "assistant", "content": result.text})
        await self._emit({
            "type": "private_notes_updated",
            "nickname": villager_nick, "preview": result.text[:120],
        })

    # ---------- day handlers ----------

    async def _phase_day_intros(self) -> None:
        self.state.round_index = 0
        await self._emit({
            "type": "announcement",
            "text": f"Day begins. Round 0 intros -- order: {' -> '.join(self.state.speaker_order)}.",
        })

        for i, speaker_nick in enumerate(self.state.speaker_order):
            self.state.turn_index_in_round = i
            slot = self.state.slots[speaker_nick]
            others = [n for n in self.state.slots if n != speaker_nick]

            await self._emit({
                "type": "public_turn_started",
                "nickname": speaker_nick, "kind": "intro_statement",
            })
            stmt_step = prompts.build_intro_statement_step(speaker_nick, others)
            messages = list(self.state.private_chats[speaker_nick]) + [stmt_step]
            stmt_result = await self._run_completion(
                slot, messages, kind="intro_statement", strip_thinking_output=True,
            )
            statement = stmt_result.text.strip()

            self.state.private_chats[speaker_nick].append(stmt_step)
            self.state.private_chats[speaker_nick].append({"role": "assistant", "content": statement})

            turn = Turn(speaker=speaker_nick, statement=statement, timestamp=time.time())
            self.state.public_chat.append(turn)
            self._inject_public_turn(turn, exclude=speaker_nick)

            await self._emit({
                "type": "public_turn",
                "speaker": speaker_nick, "statement": statement,
                "target": None, "question": None, "round": "intros",
            })

            await self._emit({
                "type": "private_action_started",
                "nickname": speaker_nick, "kind": "intro_notes",
                "role": "speaker", "note": "updating notes after intro",
            })
            notes_step = prompts.build_intro_notes_step()
            messages = list(self.state.private_chats[speaker_nick]) + [notes_step]
            notes_result = await self._run_completion(
                slot, messages, kind="intro_notes", strip_thinking_output=False,
            )

            notes.save_notes(self.root, self.game_id, speaker_nick, notes_result.text)
            self.state.notes[speaker_nick] = notes_result.text
            self.state.private_chats[speaker_nick].append(notes_step)
            self.state.private_chats[speaker_nick].append({"role": "assistant", "content": notes_result.text})
            await self._emit({
                "type": "private_notes_updated",
                "nickname": speaker_nick, "preview": notes_result.text[:120],
            })

    async def _phase_day_popcorn_r1(self) -> None:
        await self._phase_day_popcorn(round_index=1, starter=self.state.r1_starter)

    async def _phase_day_popcorn_r2(self) -> None:
        await self._phase_day_popcorn(round_index=2, starter=self.state.r2_starter)

    async def _phase_day_popcorn(self, *, round_index: int, starter: str) -> None:
        self.state.round_index = round_index
        all_players = list(self.state.slots.keys())

        await self._emit({
            "type": "announcement",
            "text": f"Round {round_index} begins (popcorn). {starter} speaks first.",
        })

        unspoken = [n for n in all_players if n != starter]
        speaker = starter
        prior_q: Optional[str] = None
        prior_questioner: Optional[str] = None

        for turn_idx in range(len(all_players)):
            self.state.turn_index_in_round = turn_idx
            is_first = (turn_idx == 0)
            is_last = (turn_idx == len(all_players) - 1)

            slot = self.state.slots[speaker]

            await self._emit({
                "type": "public_turn_started",
                "nickname": speaker,
                "kind": f"popcorn_r{round_index}_statement",
            })
            stmt_step = prompts.build_popcorn_statement_step(
                round_index=round_index,
                is_first_of_round=is_first,
                prior_question=prior_q,
                prior_questioner=prior_questioner,
            )
            messages = list(self.state.private_chats[speaker]) + [stmt_step]
            stmt_result = await self._run_completion(
                slot, messages, kind=f"popcorn_r{round_index}_statement",
                strip_thinking_output=True,
            )
            statement = stmt_result.text.strip()
            self.state.private_chats[speaker].append(stmt_step)
            self.state.private_chats[speaker].append({"role": "assistant", "content": statement})

            next_speaker: Optional[str] = None
            question: Optional[str] = None

            if not is_last:
                pick_step = prompts.build_popcorn_pick_step(unspoken)
                next_speaker = await self._ask_for_nickname(
                    slot=slot,
                    base_messages=self.state.private_chats[speaker],
                    step_message=pick_step,
                    valid_choices=unspoken,
                    kind=f"popcorn_r{round_index}_pick",
                )
                self.state.private_chats[speaker].append(pick_step)
                self.state.private_chats[speaker].append({"role": "assistant", "content": next_speaker})

                q_step = prompts.build_popcorn_question_step(next_speaker)
                messages = list(self.state.private_chats[speaker]) + [q_step]
                q_result = await self._run_completion(
                    slot, messages, kind=f"popcorn_r{round_index}_question",
                    strip_thinking_output=True,
                )
                question = q_result.text.strip()
                self.state.private_chats[speaker].append(q_step)
                self.state.private_chats[speaker].append({"role": "assistant", "content": question})

            turn = Turn(
                speaker=speaker, statement=statement,
                target=next_speaker, question=question,
                timestamp=time.time(),
            )
            self.state.public_chat.append(turn)
            self._inject_public_turn(turn, exclude=speaker)
            await self._emit({
                "type": "public_turn",
                "speaker": speaker, "statement": statement,
                "target": next_speaker, "question": question,
                "round": f"popcorn_r{round_index}",
            })

            await self._emit({
                "type": "private_action_started",
                "nickname": speaker, "kind": f"popcorn_r{round_index}_notes",
                "role": "speaker", "note": "updating notes after popcorn turn",
            })
            notes_step = prompts.build_popcorn_notes_step()
            messages = list(self.state.private_chats[speaker]) + [notes_step]
            notes_result = await self._run_completion(
                slot, messages, kind=f"popcorn_r{round_index}_notes",
                strip_thinking_output=False,
            )

            notes.save_notes(self.root, self.game_id, speaker, notes_result.text)
            self.state.notes[speaker] = notes_result.text
            self.state.private_chats[speaker].append(notes_step)
            self.state.private_chats[speaker].append({"role": "assistant", "content": notes_result.text})
            await self._emit({
                "type": "private_notes_updated",
                "nickname": speaker, "preview": notes_result.text[:120],
            })

            if next_speaker:
                prior_q = question
                prior_questioner = speaker
                unspoken = [n for n in unspoken if n != next_speaker]
                speaker = next_speaker

    async def _phase_day_reflection(self) -> None:
        self.state.round_index = 3
        await self._emit({
            "type": "announcement",
            "text": "Round 3 begins (silent reflection). Each player records a final read.",
        })

        for nick in self.state.reflection_order:
            slot = self.state.slots[nick]

            await self._emit({
                "type": "private_action_started",
                "nickname": nick, "kind": "reflection_notes",
                "role": "speaker", "note": "writing final reflection",
            })
            step = prompts.build_reflection_notes_step()
            messages = list(self.state.private_chats[nick]) + [step]
            result = await self._run_completion(
                slot, messages, kind="reflection_notes", strip_thinking_output=False,
            )

            notes.save_notes(self.root, self.game_id, nick, result.text)
            self.state.notes[nick] = result.text
            self.state.private_chats[nick].append(step)
            self.state.private_chats[nick].append({"role": "assistant", "content": result.text})

            await self._emit({
                "type": "private_notes_updated",
                "nickname": nick, "preview": result.text[:120],
            })

            ack = "noted, ready to vote."
            turn = Turn(speaker=nick, statement=ack, timestamp=time.time())
            self.state.public_chat.append(turn)
            self._inject_public_turn(turn, exclude=nick)
            await self._emit({
                "type": "public_turn",
                "speaker": nick, "statement": ack,
                "target": None, "question": None, "round": "reflection",
            })

    async def _phase_vote(self) -> None:
        await self._emit({
            "type": "announcement",
            "text": "Vote phase. Votes are private and simultaneous.",
        })

        for nick in self.state.slots:
            slot = self.state.slots[nick]
            others = [n for n in self.state.slots if n != nick]

            await self._emit({
                "type": "private_action_started",
                "nickname": nick, "kind": "vote",
                "role": "speaker", "note": "casting vote",
            })

            vote_step = prompts.build_vote_step(others)
            picked = await self._ask_for_nickname(
                slot=slot,
                base_messages=self.state.private_chats[nick],
                step_message=vote_step,
                valid_choices=others,
                kind="vote",
            )
            self.state.votes[nick] = picked
            self.state.private_chats[nick].append(vote_step)
            self.state.private_chats[nick].append({"role": "assistant", "content": picked})

            await self._emit({"type": "vote_cast", "voter": nick})

    async def _phase_reveal(self) -> None:
        result = tally.resolve(self.state.votes, self.state.card_holder_post_swap)

        truth_msg = prompts.build_reveal_truth_message(
            pre_swap=self.state.card_holder_pre_swap,
            post_swap=self.state.card_holder_post_swap,
            swap_record=self.state.swap_record,
            votes=self.state.votes,
            top_voted=result.top_voted,
            werewolf_holder=result.werewolf_holder,
            village_wins=result.village_wins,
        )
        for nick in self.state.slots:
            self.state.private_chats[nick].append(truth_msg)

        await self._emit({
            "type": "reveal",
            "votes": self.state.votes,
            "tally": result.counts,
            "top_voted": result.top_voted,
            "werewolf_holder": result.werewolf_holder,
            "village_wins": result.village_wins,
            "pre_swap": self.state.card_holder_pre_swap,
            "post_swap": self.state.card_holder_post_swap,
            "swap_record": list(self.state.swap_record) if self.state.swap_record else None,
        })

    async def _phase_afterthought(self) -> None:
        await self._emit({
            "type": "announcement",
            "text": "Afterthought phase -- each model reflects on what they got wrong.",
        })

        for nick in self.state.slots:
            slot = self.state.slots[nick]
            await self._emit({
                "type": "private_action_started",
                "nickname": nick, "kind": "afterthought",
                "role": "speaker", "note": "writing afterthought",
            })
            step = prompts.build_afterthought_step()
            messages = list(self.state.private_chats[nick]) + [step]
            result = await self._run_completion(
                slot, messages, kind="afterthought", strip_thinking_output=False,
            )

            ap = notes.afterthought_path(self.root, self.game_id, nick)
            ap.parent.mkdir(parents=True, exist_ok=True)
            ap.write_text(result.text, encoding="utf-8")

            self.state.private_chats[nick].append(step)
            self.state.private_chats[nick].append({"role": "assistant", "content": result.text})
            await self._emit({
                "type": "afterthought_written",
                "nickname": nick, "preview": result.text[:160],
            })

    async def _phase_gameskill_update(self) -> None:
        threshold = self.config.game.gameskill_compress_after_games
        for nick in self.state.slots:
            slot = self.state.slots[nick]
            existing = notes.read_gameskill(self.root, nick)
            section_count = len(re.findall(r"^## Lessons from game_", existing, flags=re.MULTILINE))

            ap = notes.afterthought_path(self.root, self.game_id, nick)
            afterthought_text = ap.read_text(encoding="utf-8") if ap.exists() else ""

            notes.archive_gameskill(self.root, nick, self.game_id)

            await self._emit({
                "type": "private_action_started",
                "nickname": nick, "kind": "gameskill_update",
                "role": "speaker",
                "note": "compressing gameskill" if section_count >= threshold else "appending new lessons",
            })

            if section_count >= threshold:
                step = prompts.build_gameskill_compress_step(existing)
                messages = list(self.state.private_chats[nick]) + [step]
                result = await self._run_completion(
                    slot, messages, kind="gameskill_compress", strip_thinking_output=False,
                )
                new_content = result.text.strip()
            else:
                step = prompts.build_gameskill_append_step(self.game_id, afterthought_text)
                messages = list(self.state.private_chats[nick]) + [step]
                result = await self._run_completion(
                    slot, messages, kind="gameskill_append", strip_thinking_output=False,
                )
                new_section = result.text.strip()
                new_content = (existing.rstrip() + "\n\n" + new_section).strip() if existing.strip() else new_section

            if self.state.gameskill_auto_commit:
                notes.save_gameskill(self.root, nick, new_content)
                await self._emit({
                    "type": "gameskill_committed",
                    "nickname": nick, "compressed": section_count >= threshold,
                })
            else:
                staged_path = self.root / "gameskills" / "_staged" / f"{nick}.md"
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_text(new_content, encoding="utf-8")
                await self._emit({
                    "type": "gameskill_staged",
                    "nickname": nick, "staged_path": str(staged_path),
                    "compressed": section_count >= threshold,
                })

    async def _phase_end(self) -> None:
        logger.write_game_log(self.root, self.state)
        await self._emit({"type": "game_log_written", "game_id": self.game_id})

    # ---------- public-chat injection ----------

    def _inject_public_turn(self, turn: Turn, *, exclude: str) -> None:
        if turn.target and turn.question:
            text = (
                f"[Public chat -- {turn.speaker}]\n"
                f"\"{turn.statement.strip()}\"\n"
                f"{turn.speaker} -> {turn.target}: \"{turn.question.strip()}\""
            )
        else:
            text = f"[Public chat -- {turn.speaker}]\n\"{turn.statement.strip()}\""

        msg = {"role": "user", "content": text}
        for nick in self.state.private_chats:
            if nick == exclude:
                continue
            self.state.private_chats[nick].append(msg)

    def _inject_announcement_to_all(self, text: str) -> None:
        msg = {"role": "user", "content": f"[Announcement] {text}"}
        for nick in self.state.private_chats:
            self.state.private_chats[nick].append(msg)

    # ---------- internals ----------

    def _build_initial_state(self) -> GameState:
        game_id = self._next_game_id()
        seed = randomization.make_seed(self.config.game.random_seed)
        slots = {s.nickname: s for s in self.config.slots}
        st = GameState(
            game_id=game_id,
            seed=seed,
            slots=slots,
            pause_points=list(self.config.game.pause_points),
            gameskill_auto_commit=self.config.game.gameskill_auto_commit,
            hot_memory_last_k_turns=self.config.game.hot_memory_last_k_turns,
        )
        notes.game_dir(self.root, game_id)
        return st

    def _next_game_id(self) -> str:
        games_root = self.root / "games"
        games_root.mkdir(parents=True, exist_ok=True)
        existing = sorted(
            p.name for p in games_root.iterdir()
            if p.is_dir() and p.name.startswith("game_")
        )
        next_n = len(existing) + 1
        return f"game_{next_n:03d}"


    # ---------- snapshot / resume ----------

    def _snapshot(self, reason: str) -> None:
        """Persist self.state to disk as a pickle. Cheap (~10-50KB), safe to
        call after every phase. The pickle includes private_chats, public_chat,
        votes, card holdings, and all bookkeeping needed to resume."""
        p = self.game_dir / "state.pickle"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("wb") as f:
            pickle.dump(self.state, f)
        logger.log_event(self.root, self.game_id, {
            "kind": "snapshot",
            "reason": reason,
            "phase": self.state.phase.value,
            "last_completed_phase": (
                self.state.last_completed_phase.value
                if self.state.last_completed_phase else None
            ),
        })

    @classmethod
    def from_snapshot(
        cls,
        app_config: AppConfig,
        workspace_root: Path,
        game_id: str,
    ) -> "GameRunner":
        """Construct a runner from a previously-snapshotted state file.

        Bypasses __init__ (which would build a fresh GameState) and restores
        the pickled state. Re-creates the transient bits (events queue, advance
        signal, rules text)."""
        snap = workspace_root / "games" / game_id / "state.pickle"
        if not snap.exists():
            raise FileNotFoundError(f"No snapshot at {snap}")
        with snap.open("rb") as f:
            state: GameState = pickle.load(f)

        runner = object.__new__(cls)
        runner.config = app_config
        runner.root = workspace_root
        runner.rules_text = (workspace_root / "backend" / "rules.md").read_text(encoding="utf-8")
        runner.events = asyncio.Queue()
        runner._advance_signal = asyncio.Event()
        runner.state = state
        return runner

    async def _emit(self, event: dict[str, Any]) -> None:
        await self.events.put(event)
        logger.log_event(self.root, self.game_id, {"kind": "event", **event})

    async def _run_completion(
        self,
        slot: PlayerSlot,
        messages: list[dict[str, Any]],
        *,
        kind: str,
        strip_thinking_output: bool,
        max_retries: int = 1,
    ) -> llm_client.CompletionResult:
        async def on_delta(text: str) -> None:
            await self._emit({
                "type": "delta",
                "nickname": slot.nickname,
                "kind": kind,
                "text": text,
            })

        started = time.time()
        last_err: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                result = await llm_client.complete(
                    slot, messages,
                    strip_thinking_output=strip_thinking_output,
                    on_delta=on_delta,
                )
                break
            except llm_client.LLMRequestError as e:
                last_err = e
                if attempt < max_retries:
                    # Transient backend hiccup (StoryUI disconnect, refused
                    # connection, etc). Wait briefly and retry the same call.
                    await self._emit({
                        "type": "llm_retry",
                        "nickname": slot.nickname,
                        "step": kind,
                        "attempt": attempt + 1,
                        "error": str(e),
                    })
                    logger.log_event(self.root, self.game_id, {
                        "kind": "llm_retry",
                        "nickname": slot.nickname,
                        "step": kind,
                        "attempt": attempt + 1,
                        "error": str(e),
                    })
                    await asyncio.sleep(2.0)
                    continue
                logger.log_event(self.root, self.game_id, {
                    "kind": "llm_call_failed",
                    "nickname": slot.nickname,
                    "step": kind,
                    "error": type(e).__name__,
                    "message": str(e),
                    "elapsed": time.time() - started,
                })
                raise
            except llm_client.LLMTimeoutError as e:
                # Don't auto-retry timeouts -- the engine might be alive and
                # producing slowly. Surface it so the GM can intervene.
                logger.log_event(self.root, self.game_id, {
                    "kind": "llm_call_failed",
                    "nickname": slot.nickname,
                    "step": kind,
                    "error": type(e).__name__,
                    "message": str(e),
                    "elapsed": time.time() - started,
                })
                raise
        else:
            # Loop exhausted without break -- shouldn't reach here given raise
            # paths above, but defensive: re-raise the last error.
            raise last_err  # type: ignore

        logger.log_event(self.root, self.game_id, {
            "kind": "llm_call",
            "nickname": slot.nickname,
            "step": kind,
            "elapsed": result.elapsed_seconds,
            "token_delta_count": result.token_count_estimate,
            "finish_reason": result.finish_reason,
            "text": result.text,
            "raw_text": result.raw_text,
        })
        return result

    # ---------- validation helpers ----------

    async def _ask_for_nickname(
        self,
        *,
        slot: PlayerSlot,
        base_messages: list[dict[str, Any]],
        step_message: dict[str, Any],
        valid_choices: list[str],
        kind: str,
        max_attempts: int = 3,
    ) -> str:
        attempt = 0
        current_messages = list(base_messages) + [step_message]
        last_response = ""

        while attempt < max_attempts:
            attempt += 1
            result = await self._run_completion(
                slot, current_messages, kind=f"{kind}_attempt{attempt}",
                strip_thinking_output=True,
            )
            last_response = result.text or result.raw_text

            picked = _extract_single_nickname(result.text, valid_choices)
            if not picked and result.raw_text and result.raw_text != result.text:
                # Fallback: search the raw text (including any thinking blocks).
                # Take the LAST mention -- usually the model's conclusion.
                picked = _extract_last_nickname(result.raw_text, valid_choices)
            if picked:
                return picked

            err = (
                "That response didn't include a valid nickname. Valid choices are: "
                f"{', '.join(sorted(valid_choices))}. Reply with ONLY the nickname."
            )
            await self._emit({
                "type": "validation_retry",
                "nickname": slot.nickname, "kind": kind,
                "attempt": attempt, "error": err,
                "received": result.text[:200],
            })
            current_messages = current_messages + [
                {"role": "assistant", "content": last_response},
                {"role": "user", "content": err},
            ]

        await self._emit({
            "type": "intervention_required",
            "nickname": slot.nickname, "kind": kind,
            "reason": f"failed validation {max_attempts} times",
            "last_response": last_response[:500],
        })
        raise RuntimeError(
            f"{slot.nickname} failed validation on {kind} {max_attempts} times. "
            f"Last response: {last_response[:200]}"
        )

    async def _ask_for_two_nicknames(
        self,
        *,
        slot: PlayerSlot,
        base_messages: list[dict[str, Any]],
        step_message: dict[str, Any],
        valid_choices: list[str],
        kind: str,
        max_attempts: int = 3,
    ) -> tuple[str, str]:
        attempt = 0
        current_messages = list(base_messages) + [step_message]
        last_response = ""

        while attempt < max_attempts:
            attempt += 1
            result = await self._run_completion(
                slot, current_messages, kind=f"{kind}_attempt{attempt}",
                strip_thinking_output=True,
            )
            last_response = result.text or result.raw_text

            pair = _extract_two_nicknames(result.text, valid_choices)
            if not pair and result.raw_text and result.raw_text != result.text:
                pair = _extract_two_nicknames(result.raw_text, valid_choices)
            if pair:
                return pair

            err = (
                "That response didn't include two distinct valid nicknames. "
                f"Valid choices are: {', '.join(sorted(valid_choices))}. "
                "Reply with two different nicknames separated by a comma, e.g. `Owen, Gemma`."
            )
            await self._emit({
                "type": "validation_retry",
                "nickname": slot.nickname, "kind": kind,
                "attempt": attempt, "error": err,
                "received": result.text[:200],
            })
            current_messages = current_messages + [
                {"role": "assistant", "content": last_response},
                {"role": "user", "content": err},
            ]

        await self._emit({
            "type": "intervention_required",
            "nickname": slot.nickname, "kind": kind,
            "reason": f"failed validation {max_attempts} times",
            "last_response": last_response[:500],
        })
        raise RuntimeError(
            f"{slot.nickname} failed pair-validation on {kind} {max_attempts} times."
        )


# ---------- module-level helpers ----------

def _extract_single_nickname(text: str, valid: list[str]) -> Optional[str]:
    if not text:
        return None
    matches: list[tuple[int, str]] = []
    for nick in valid:
        m = re.search(rf"\b{re.escape(nick)}\b", text, flags=re.IGNORECASE)
        if m:
            matches.append((m.start(), nick))
    if not matches:
        return None
    matches.sort()
    return matches[0][1]


def _extract_last_nickname(text: str, valid: list[str]) -> Optional[str]:
    """Find the LAST valid nickname mentioned in `text` -- useful when scanning
    a reasoning trace where the model's conclusion is at the end."""
    if not text:
        return None
    matches: list[tuple[int, str]] = []
    for nick in valid:
        for m in re.finditer(rf"\b{re.escape(nick)}\b", text, flags=re.IGNORECASE):
            matches.append((m.start(), nick))
    if not matches:
        return None
    matches.sort()
    return matches[-1][1]


def _extract_two_nicknames(text: str, valid: list[str]) -> Optional[tuple[str, str]]:
    if not text:
        return None
    matches: list[tuple[int, str]] = []
    for nick in valid:
        for m in re.finditer(rf"\b{re.escape(nick)}\b", text, flags=re.IGNORECASE):
            matches.append((m.start(), nick))
    if not matches:
        return None
    matches.sort()
    seen: list[str] = []
    for _, nick in matches:
        if nick not in seen:
            seen.append(nick)
        if len(seen) == 2:
            return (seen[0], seen[1])
    return None
