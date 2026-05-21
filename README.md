# LLM Plays One Night Werewolf

A standalone runner app that sits four LLM players around the table for
[One Night Ultimate Werewolf](https://en.wikipedia.org/wiki/Ultimate_Werewolf)
and watches them play. Engine-agnostic over any OpenAI-compatible API
(Ollama, LM Studio, llama.cpp / llama-server, vLLM, OpenAI, OpenRouter,
StoryUI, etc.). Autonomous once configured; pause-points + intervention
hooks if you're a control freak.

Built as an instrument for studying the rule-vs-self-belief reasoning
failures social-deduction games surface in LLMs. 
In short, see how LLMs interact with each other and confuse themselves 
about their identities. 

Full design spec lives in [`runner-app-spec.md`](runner-app-spec.md).

## What you get

- A FastAPI backend on `:8765` that walks the 14-phase game state machine
  (setup → 4 night phases → intros → 2 popcorn rounds → reflection → vote →
  reveal → afterthought → gameskill update → end).
- A single-page UI that streams every player's reasoning live, with public
  chat / private notes / GM reveals styled distinctly. Dark theme.
- Per-slot config: each of the four players can sit on a different
  endpoint, model, API key, and sampler config.
- Snapshot/resume: if the engine crashes mid-game, the runner can be
  restarted and the game resumed from the last completed phase.
- Append-and-compress gameskill: each player keeps a persistent
  notes-to-self file across games. Compression pass triggers after 5
  accumulated sections.
- Full transcript (`transcript.jsonl`) + human-readable `game_log.md`
  per game.
  
  In short, the game runner manages everything in the game. just sit and watch. 

## Quickstart -- OpenAI

If you just have an OpenAI API key:

```bash
git clone https://github.com/herryupmay/LLM-plays-one-night-werewolf.git
cd LLM-plays-one-night-werewolf
python3 -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 run.py
```

The browser opens at `http://localhost:8765`. In each of the four
**Player N** cards, paste your API key, click **Fetch models**, pick a
model (e.g. `gpt-4o-mini`), set a nickname. Click **Save & Validate**,
then **Start game**.

## Quickstart -- Ollama (local)

```bash
ollama pull qwen2.5:14b
ollama pull gemma2:9b
ollama pull mistral-small:24b
ollama pull llama3.1:8b

git clone https://github.com/herryupmay/LLM-plays-one-night-werewolf.git
cd LLM-plays-one-night-werewolf
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 run.py
```

In each slot, set **Endpoint** to `http://localhost:11434/v1`, leave **API
key** blank, **Fetch models**, pick from the list.

## Quickstart -- any other OpenAI-compatible engine

Works the same as Ollama. Point the endpoint at your engine's `/v1` URL
(llama-server: `http://localhost:8080/v1`, LM Studio Developer Server:
`http://localhost:1234/v1`, vLLM: whatever you launched with, online APIs
like OpenRouter: `https://openrouter.ai/api/v1`, etc.). Set the API key if
the engine requires one.

## How a game flows

After **Start game**:

1. **Setup** -- cards dealt, RNG seeded. `seed.json` written.
2. **Night** -- Werewolf, Seer (peek + reveal), Troublemaker (silent swap),
   Villager each take their phase in sequence. Notes streamed live.
3. **Day intros** -- four short intros in randomized order.
4. **Popcorn R1 + R2** -- four turns per round, each speaker nominates the
   next + asks a question. ~16 inferences per round.
5. **Reflection** -- silent notes update + templated "ready to vote" line.
6. **Vote** -- private nickname pick per player. Cannot self-vote.
7. **Reveal** -- card holdings, swap, vote tally, win condition.
8. **Afterthought** -- each player reflects on the gap between what they
   believed during play and what was actually true.
9. **Gameskill update** -- each player appends `## Lessons from game_NNN`
   to their persistent gameskill file. Compresses after 5 sections.
10. **End** -- `game_log.md` written.

Roughly 60 inferences per full game. ~10 minutes on local hardware,
faster on hosted APIs.

## What ends up on disk

- `games/game_NNN/` -- per-game artifacts:
  - `seed.json` -- RNG seed + initial deal + popcorn starters
  - `transcript.jsonl` -- every prompt + response + event, append-only
  - `notes/{nickname}.md` -- final notes per player
  - `afterthoughts/{nickname}.md` -- post-game reflections per player
  - `game_log.md` -- human-readable game log
  - `state.pickle` -- snapshot for resume (gitignored)
- `gameskills/{nickname}.md` -- persistent per-nickname learning across games
- `gameskills/_history/` -- pre-update / pre-compression snapshots
- `config.yaml` -- saved from the UI (or hand-edited)

`games/`, `gameskills/`, `config.yaml`, and `.venv/` are in `.gitignore`.

## Resuming a crashed game

After every phase boundary the runner pickles state to
`games/game_NNN/state.pickle`. On a phase error (engine crash, validation
timeout, etc.), the same snapshot is written before the runner exits.

To resume:

1. Fix whatever broke (restart your engine, etc.).
2. `python3 run.py` again.
3. The setup screen now shows a **Resumable games** section at the top.
4. Click **Resume** -- the runner replays the prior public chat and notes
   as bubbles, then picks up from the next phase.

## Configuration reference (`config.yaml`)

The UI writes this for you, but you can edit by hand. Each of four slots:

```yaml
slots:
  - nickname: Owen          # alphanumeric, 1-12 chars, unique
    endpoint: http://localhost:11434/v1
    model: qwen2.5:14b
    api_key: null           # optional; sent as Authorization: Bearer
    temperature: 0.7
    timeout_seconds: 180             # hard wall-clock per LLM call
    inactivity_timeout_seconds: 45   # resets on every streamed token

game:
  hot_memory_last_k_turns: 20
  pause_points: []          # [] = autonomous; "phase_transition" / "gameskill_commit"
  gameskill_auto_commit: true
  gameskill_compress_after_games: 5
  random_seed: null         # null = fresh; integer for reproducibility
```

## Architecture sketch

- `backend/server.py` -- FastAPI app (config, validate, discover,
  save_config, start, advance, state, stream, games, resume,
  game_state).
- `backend/game.py` -- `GameRunner` class, phase state machine, all phase
  handlers, snapshot/resume.
- `backend/llm_client.py` -- OpenAI-compatible streaming client with
  two-layer timeouts (inactivity + hard) and reasoning-field handling.
- `backend/prompts.py` -- templates indexed by (phase, role, step).
- `backend/models.py` -- `GameState`, `PlayerSlot`, `Turn`, `NightAction`,
  `Phase` enum.
- `backend/notes.py` -- file I/O for notes, gameskills, afterthoughts.
- `backend/logger.py` -- transcript + game_log writers.
- `backend/randomization.py`, `backend/tally.py` -- seeded shuffles and
  vote/win resolution.
- `frontend/` -- single page (index.html / app.js / style.css). No build
  step; ES modules + vanilla DOM. Live updates via SSE.

## Troubleshooting

- **"Model not listed" on validate.** Click **Fetch models** to see what
  the endpoint actually exposes. Backends sometimes rename models
  (`qwen3:8b` may appear as `Qwen3-8B`, etc.). Pick from the dropdown
  rather than typing.
- **Empty private bubbles, no streaming text.** Likely the engine is
  emitting everything to `delta.reasoning` instead of `delta.content`
  (some Gemma / DeepSeek setups). Fixed -- the runner now pipes reasoning
  to the UI in real time and falls back to it for nickname extraction.
- **Player calling itself by the model's name (e.g. Jemma says "I'm
  Gemma").** Identity anchoring is in the prompt prefix and system prompt
  but Gemma-family models sometimes drift. Mostly harmless; the runner
  uses the nickname for all game logic.
- **Player misreads the Seer's peek as post-swap.** The Seer acts BEFORE
  the Troublemaker, so the reveal is the dealt card -- it may move
  before vote time. This is one of the rule-vs-belief failures the spec
  is specifically built to surface; not a bug.
- **Context length warnings.** Local engines often load at 4096 tokens.
  Game inferences can hit 8-12k by R2 popcorn. Bump your engine's ctx to
  16k+ or set `hot_memory_last_k_turns` to ~6 in `game:`.
- **WSL `xdg-open: no method available`.** Harmless; the runner detects
  WSL and shells out to `cmd.exe /c start` to open the URL in your
  Windows browser. If that fails too, paste the URL manually.

## Status

What's implemented:

- All 14 phases of the game loop, end-to-end.
- In-app per-slot setup screen with model discovery.
- Snapshot every phase + resume.
- 3-strike nickname validation with auto-reprompt on bad model output.
- Auto-retry once on transient backend disconnects.
- Reasoning-field fallback for engines that stream only to
  `delta.reasoning`.

Known omissions (PRs welcome):

- GM intervention buttons (Retry / Skip / Abort on validation 3-strikes
  or phase errors) -- currently surfaces as a system bubble only.
- `trim_profile: low` preset for users with <16GB RAM.
- Tighter popcorn statement prompts (current statements run long).

## License

MIT License 

