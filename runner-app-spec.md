# Werewolf Runner App — Build Spec

> Standalone app to run One Night Werewolf with 4 LLM players. Engine-agnostic, autonomous after config, single-user localhost app. Lifts the manual-GM workflow into a self-running tool that any LLM enthusiast (or non-LLM person) can use to watch four models play social deduction.

## Audience and goals

Two user profiles, served by the same app:

1. **Casual LLM enthusiast.** Has Ollama installed, pulls four models, edits a config, hits Start, watches a game. Wants to compare models. Doesn't want to learn anything about prompt engineering or game theory.
2. **Research operator (J's mode).** Wants the same autonomous run but with pause-points at meaningful checkpoints (gameskill commits, optionally phase transitions), full transcripts, and intervention controls (regen, skip, abort, manual edits). Uses the app as a research instrument for the rule-vs-self-belief reasoning failure documented across StoryUI Games 1–2.

Both modes use the same codebase. A `pause_points` config flag and a `gameskill_auto_commit` flag distinguish them at runtime.

## Game rules

See `README.md` in this folder for full rules. Summary: four cards (1=Werewolf, 2=Seer, 3=Troublemaker, 4=Villager), one of each, randomized holder per game. Night phase resolves actions in canonical role order (WW → Seer → TM → Villager). Day phase has Round 0 intros, Rounds 1–2 popcorn Q&A, Round 3 silent reflection. Private votes, simultaneous public reveal. Village wins if the model currently holding card 1 post-swap is among those tied for most votes.

## Architecture

- **Backend**: Python 3.11+, FastAPI, httpx, pydantic, uvicorn, pyyaml. ~5 deps total.
- **Frontend**: Vanilla HTML/JS/CSS. No build step.
- **Serving**: localhost only. `python run.py` boots FastAPI on `:8000`, opens browser.
- **Backend abstraction**: OpenAI-compatible HTTP API only. Works with Ollama, LM Studio, llama-server, vLLM, SGLang, TGI, online APIs (OpenAI, Anthropic via shim, OpenRouter, Groq, etc.). Engine-agnostic at the boundary.
- **File I/O**: 100% runner-owned. Models never make tool calls. Notes, cards, gameskills, rule guide all injected into context by the runner.
- **Mode**: Strict sub-step prompting only. Each turn decomposed into discrete prompted steps with explicit validation between them. No free-form bundled-decision turns.

## Directory layout

```
werewolf/
├── README.md                 Setup & quickstart
├── requirements.txt          fastapi, httpx, pydantic, uvicorn, pyyaml
├── run.py                    Entrypoint; boots FastAPI + opens browser
├── config.yaml.example       Four-slot config template
│
├── backend/
│   ├── server.py             FastAPI routes
│   ├── game.py               GameState + phase state machine
│   ├── models.py             Dataclasses: PlayerSlot, GameState, Turn, NightAction
│   ├── llm_client.py         OpenAI-compatible HTTP client + thinking-strip + timeouts
│   ├── prompts.py            Prompt templates indexed by (phase, role, step)
│   ├── notes.py              Notes/gameskill read-inject-save (incl. 5-game compression)
│   ├── randomization.py      Seeded RNG for card deal + intro/round-start order
│   ├── tally.py              Vote resolution + win-condition checks
│   ├── logger.py             transcript.json + game_log.md writers
│   └── rules.md              Quick rule guide injected into model context
│
├── frontend/
│   ├── index.html            Single page
│   ├── app.js                Chat view, GM controls, settings modal
│   └── style.css
│
├── games/                    Gitignored; per-game working data
│   └── game_NNN/
│       ├── notes/{nickname}.md × 4
│       ├── afterthoughts/{nickname}.md × 4
│       ├── transcript.json   Every prompt + response, with timing
│       ├── game_log.md       Human-readable post-game record
│       └── seed.json         RNG seed, cast, swap record
│
└── gameskills/               Persistent across games
    ├── {nickname}.md         Live gameskill per player
    └── _history/
        └── {nickname}_game_NNN.md   Archive snapshots
```

## Configuration schema (`config.yaml`)

```yaml
slots:
  - nickname: Owen
    endpoint: http://localhost:11434/v1
    model: qwen3:8b
    system_prompt: |
      <optional override; defaults to template>
    timeout_seconds: 180        # optional; default 180
    inactivity_timeout_seconds: 45  # optional; default 45
    temperature: 0.7            # optional
  - nickname: Gemma
    endpoint: http://localhost:11434/v1
    model: gemma3:9b
    ...
  - nickname: Rowen
    ...
  - nickname: Jemma
    ...

game:
  hot_memory_last_k_turns: 20
  pause_points: []              # casual mode; [] = fully autonomous
  # research mode example: ["gameskill_commit"]
  # paranoid mode example: ["phase_transition", "gameskill_commit"]
  gameskill_auto_commit: true   # casual default; false for research review
  random_seed: null             # null = generate fresh; set integer for reproducibility
  gameskill_compress_after_games: 5
```

Validation at startup:

- All four nicknames present, unique, alphanumeric only, ≤12 chars.
- All four endpoints reachable (ping `/v1/models` on each at boot, fail loudly if any unreachable).
- All four model names exist on their respective endpoints (check via `/v1/models` listing).

## Data model (dataclasses)

```python
@dataclass
class PlayerSlot:
    nickname: str
    endpoint: str
    model: str
    system_prompt: str
    timeout: int
    inactivity_timeout: int
    sampler: dict        # temperature, top_p, etc.

@dataclass
class Turn:
    speaker: str         # nickname
    statement: str
    target: Optional[str]    # nickname they're questioning
    question: Optional[str]
    timestamp: float

@dataclass
class NightAction:
    role: str            # "werewolf" | "seer" | "troublemaker" | "villager"
    actor: str           # nickname
    action_type: str     # "peek" | "swap" | "none"
    params: dict         # {target: ...} or {target1, target2: ...}
    runner_result: Optional[str]  # what runner told the model (Seer reveal)

@dataclass
class GameState:
    game_id: str
    seed: int
    slots: dict[str, PlayerSlot]   # by nickname
    card_holder_pre_swap: dict[int, str]    # {1: "Gemma", ...}
    card_holder_post_swap: dict[int, str]
    swap_record: tuple[str, str]   # which two players were swapped
    public_chat: list[Turn]
    private_chats: dict[str, list[dict]]   # full message lists per nickname
    notes: dict[str, str]          # current notes content per nickname
    phase: Phase                    # enum
    round_index: int
    turn_index_in_round: int
    speaker_order: list[str]        # randomized per round
    pause_points: list[str]
    votes: dict[str, str]          # nickname → nickname voted
```

## Game flow / state machine

Phases dispatched by `GameState.advance()`:

1. `SETUP` — deal cards, shuffle orders, persist seed.json
2. `NIGHT_WEREWOLF` — handler builds prompt with card content + gameskill + rule guide; one inference call (notes only, no action). Werewolf is told they are alone.
3. `NIGHT_SEER` — three inferences: (a) pick a player to peek at, (b) reflect on what was revealed, (c) write notes. Runner reveals between (a) and (b).
4. `NIGHT_TROUBLEMAKER` — two inferences: (a) pick two players to swap (self-swap allowed), (b) write notes. Runner silently updates `card_holder_post_swap`; TM never learns the result.
5. `NIGHT_VILLAGER` — one inference (notes only).
6. `DAY_INTROS` — four turns in randomized order. Each turn: (a) statement, (b) notes. Two inferences per player.
7. `DAY_POPCORN_R1` — four turns. Starter is randomized (not card-derived). Each turn: (a) reply to question from previous turn (if any) + make statement, (b) pick next speaker, (c) formulate question, (d) write notes. Four inferences per turn.
8. `DAY_POPCORN_R2` — same shape as R1.
9. `DAY_REFLECTION` — four turns in randomized order. Single inference per player: write final reflection notes. Public chat gets a one-line templated ack ("noted, ready to vote"), not model-generated.
10. `VOTE` — four private inferences, one per player. Each model returns a nickname (validated: must be in play, not self).
11. `REVEAL` — runner-side, no inference. Tally computed, win condition determined, full state dump appended to each model's private chat.
12. `AFTERTHOUGHT` — one inference per model. Reflection on the gap between what they believed and what was true.
13. `GAMESKILL_UPDATE` — one inference per model. Appends new `## Lessons from Game N` section to their gameskill. On the 6th game (or first post-compression-threshold game), runs compression prompt instead.
14. `END` — finalize transcript and game_log.md.

State machine pauses at `pause_points` boundaries when configured. UI shows "Awaiting GM approval to continue" with a button.

## Sub-step prompting

Each handler builds prompts using templates from `prompts.py`. Pattern:

- **System message** (stable for whole game): nickname, win conditions, hard rules ("you may not vote for yourself", "swapped players are not told they were swapped"), no-think directive, "your dealt card may not be your current card" warning.
- **Synthetic seed exchange** (stable for whole game): user message containing the rule guide, the player's card content, and their gameskill from prior games; assistant ack "Understood."
- **Accumulated history** (grows over time): night phase exchanges, public chat turns from prior rounds, prior sub-step responses within current turn.
- **Current step prompt** (varies per call): the specific instruction for THIS sub-step, with goal-orientation reminder embedded.

The persistent prefix (system + synthetic seed) is sent on every call but prefix-cached by the engine, so cost is amortized to near-zero after the first call.

Validation between sub-steps: if a model picks an invalid nickname (not in play, self-target where prohibited), empty response, or fails to parse, the runner re-prompts with the error stated explicitly. Three failed attempts = surface to GM as intervention point.

## Output processing

Every response from `llm_client.complete()` passes through:

1. **Thinking-token strip** (applied to public statements and action outputs; NOT to notes/afterthoughts). Regex strips `<think>…</think>`, `<thinking>…</thinking>`, OpenAI `reasoning` field, Anthropic `thinking` content blocks. Best-effort, ~80% coverage acceptable.
2. **Validation** per sub-step type (nickname lookup, length checks, parseability).
3. **Save** to appropriate destination (public chat / private chat / notes file / cheat sheet).

No-think directive (`/no_think` for Qwen, `reasoning_effort: minimal` for compatible APIs) is always sent in system prompt. Strip pass is the defensive backup.

## Notes and gameskill management

**Notes** (`games/game_NNN/notes/{nickname}.md`):
- Per-game, recreated each game from template.
- Model writes by producing notes-content as response text; runner overwrites file with response (full new content each save).
- Runner injects current notes content into context on every subsequent turn.
- Template structure enforced by runner: "My goal" DO-NOT-MODIFY section always re-injected verbatim on save.

**Gameskills** (`gameskills/{nickname}.md`):
- Persistent across games.
- Update mechanism: append-mostly. Each game appends `## Lessons from Game N` section at bottom. No revision of prior content.
- Compression trigger: when a player's gameskill has accumulated `gameskill_compress_after_games` (default 5) appended sections, the next game's update step runs a compression prompt instead of an append. Model is asked to synthesize the appended lessons into the canonical sections, preserve any lesson appearing in multiple games, drop lessons contradicted by later games. Pre-compression version archived to `_history/`.
- Auto-commit (casual mode) or human-review-diff (research mode) per config.

## UI design

**Single-pane chat** showing all events in chronological order. Three semantic categories with two visual axes:

- **Alignment encodes source**: left-aligned for system-generated content (announcements, GM reveals); right-aligned for player-generated content (public statements/questions, private notes/actions).
- **Color encodes audience**: announcement color (broadcast system messages), public color (broadcast player content), private color (per-player private content, including GM reveals which use private color with "GM:" prefix).

Bubble labels:

- Announcements: no name label. E.g., "Round 1 begins — Rowen speaks first"
- GM reveals: `GM → Owen: Gemma is currently holding card 4`
- Public statements: `Owen: <statement>`
- Public questions: `Owen → Gemma: <question>` (or just `Owen: <question text>` with target inferred from layout)
- Private notes: `Owen (notes) ▶` (collapsed by default, click to expand)
- Private actions: `Owen (action): Peek Gemma` or `Owen (swap): Rowen, Jemma`
- Private votes: `Owen (vote): Jemma`

**Collapse behavior**: all private bubbles collapsed by default to single-line summary (`Owen (notes) ▶`). Click to expand inline. No previews, no auto-expand rules, no power-user toggles. Public bubbles always fully rendered.

**Streaming**: responses stream token-by-token as they arrive. Watcher sees the AI thinking in real time. UI also shows per-slot status indicator: idle / generating / waiting on swap / stuck.

**GM controls panel** (collapsible side panel):

- Current phase indicator
- "Advance" button (only visible when paused at a pause-point)
- Cheat sheet toggle (shows current `card_holder_post_swap` mapping)
- Per-turn intervention buttons when failures occur: Retry / Skip / Abort
- Pre-game setup screen: four slot cards with endpoint/model/nickname/system-prompt inputs, "Validate Endpoints" button, "Start Game" button.

**Post-game screen**: vote tally, win outcome, link to game log, gameskill diff review (research mode only).

## Failure handling

**Timeout model**: two layers per request.

- **Inactivity timeout** (default 45s): resets on every streamed token. Triggers if no token for N seconds. Catches engine crashes, silent connection drops, stuck models.
- **Hard timeout** (default 180s): total wall-clock from request start. Backstop for "engine is alive but generating forever."

Configurable per slot in config.yaml.

**Three-button intervention** when a step fails:

- **Retry**: resend the same request, abandon any in-flight generation. Same prompt, same context.
- **Skip**: advance past this step. For notes/afterthought, save empty or template-default content. For action steps, error and force re-roll (you can't really skip a vote).
- **Abort game**: persist partial state to `games/game_NNN/`, mark game as aborted in log, return to setup screen.

**Error classes**:

- **Slow engine** (still streaming, hard timeout hit): "Owen's response timed out at 180s. Retry / Skip / Abort?"
- **Stuck engine** (no tokens, inactivity timeout hit): "Owen has been silent for 45s. Retry / Skip / Abort?"
- **Connection refused**: "Endpoint at <url> is unreachable. Start your engine and click Retry." Differentiated copy because the fix is different.
- **Parse failure** (e.g., model picked invalid nickname 3x in a row): "Owen failed validation 3 times on 'pick a target.' Retry / Skip / Abort?"

All failures + interventions logged to `transcript.json` with full context.

## Randomization

Game-scoped seeded RNG controls three independent shuffles, all logged to `seed.json`:

1. **Card deal**: shuffle `[1,2,3,4]` against the four nicknames.
2. **Round 0 intro order**: shuffle nicknames.
3. **Round 1 starter** and **Round 2 starter**: random pick from nicknames (independent of card holdings — explicitly *not* "current card 1 holder" to prevent positional info leakage across games).

Popcorn nominations within a round are player-driven so they self-randomize. Night phase canonical order (WW → Seer → TM → Villager) is fixed by mechanic.

## Engine recommendations

**README quickstart leads with Ollama:**

```bash
# Install Ollama (https://ollama.ai)
ollama pull qwen3:8b
ollama pull gemma3:9b
ollama pull mistral-small:24b
ollama pull <fourth model>

git clone <repo>
cd werewolf
pip install -r requirements.txt
cp config.yaml.example config.yaml
# edit config.yaml with your four nicknames + model names
python run.py
```

**"Power user setups" section** (short subsections, ~3 lines each):

- **vLLM**: launch four instances on four ports (`--port 8000`, etc.), each serving one model. Config slot endpoints point at the respective ports.
- **llama-server**: same pattern as vLLM, four instances on four ports.
- **LM Studio**: enable Developer → Local Server. Single endpoint, JIT loading handles multi-model.
- **Online APIs**: works with any OpenAI-compatible URL. Set `endpoint: https://api.openai.com/v1` or `https://openrouter.ai/api/v1` etc. with appropriate API key in `Authorization` header (config supports `api_key` per slot).

## Logging and artifacts

Per game, `games/game_NNN/` accumulates:

- `seed.json`: RNG seed, cast assignment, swap record.
- `notes/{nickname}.md` × 4: final notes content.
- `afterthoughts/{nickname}.md` × 4: post-game reflections.
- `transcript.json`: every inference call with prompt, response, timing, slot info. Full replay layer.
- `game_log.md`: human-readable record. Cast, swap, public chat, vote tally, outcome, afterthoughts as appendices, gameskill diffs as appendices.

Persistent across games:

- `gameskills/{nickname}.md`: live gameskill, append-mostly with periodic compression.
- `gameskills/_history/{nickname}_game_NNN.md`: archive of pre-update snapshots and pre-compression snapshots.

## Out of scope (v1)

Explicitly deferred:

- Spoiler-free filter (hide private bubbles until reveal). Default-on full transparency for v1.
- Embedding-based gameskill retrieval. Plain markdown injection works fine at this scale.
- Rule-based gameskill critique pass. Wait for 5+ games of accumulated diffs to inform what rules actually matter.
- Importance scoring on gameskill lessons. Inject full file every game; revisit when files grow problematic.
- Cross-agent gameskill sharing. Each model only sees its own.
- Real-time intervention during streaming (you can only intervene at sub-step boundaries, not mid-token).
- Multi-game tournament mode / leaderboard. Build the single-game runner first.

## Open questions (decide before coding)

1. **Reflection context for the Seer's "reflect on peek" step**: does the Seer's reflection prompt see only the peek result, or also the broader night context? Lean toward minimal: just the peek result + persistent prefix.
2. **Notes-update timing in popcorn rounds**: does each speaker write notes immediately after their turn (current StoryUI rhythm), or after the answer-back from whoever they questioned next turn (reflects on outcome)? Lean toward immediate post-turn for v1, simpler.
3. **System prompt template authorship**: ship a default system prompt template, or require user to provide one in config? Lean toward ship-default with override capability.
4. **Frontend framework**: vanilla JS confirmed, but should the chat view use a virtual scroll for long games, or just naive append? Naive is fine for <500 messages; revisit if it gets sluggish.

## Estimate

~1500–2000 lines Python backend, ~500 lines frontend. Realistic build target: one focused weekend for autonomous-mode MVP (config → run → game completes → log written), second weekend for GM controls and the gameskill-review UI. Polish + README + power-user docs another half day.
