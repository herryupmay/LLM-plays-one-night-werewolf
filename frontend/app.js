// Single-page UI for the One Night Werewolf runner.
// No build step -- plain ES modules, vanilla DOM.

const $ = (id) => document.getElementById(id);

const phasePill = $("phase-pill");
const gmPhase = $("gm-phase");
const gmPaused = $("gm-paused");
const btnAdvance = $("btn-advance");
const btnSaveValidate = $("btn-save-validate");
const btnStart = $("btn-start");
const validateOut = $("validate-output");
const slotEditor = $("slot-editor");
const setupScreen = $("setup-screen");
const gameScreen = $("game-screen");
const chat = $("chat");
const cheatSheet = $("cheat-sheet");
const gmPanel = $("gm-panel");
const gmToggle = $("gm-toggle");

const cfgPausePhase = $("cfg-pause-phase");
const cfgPauseGameskill = $("cfg-pause-gameskill");
const cfgAutoCommit = $("cfg-auto-commit");
const cfgSeed = $("cfg-seed");

// (per-slot model lists live on each card.dataset.models)
let activeStream = null;
const streamingBubbles = new Map();

const DEFAULT_NICKS = ["Owen", "Gemma", "Rowen", "Jemma"];

gmToggle.addEventListener("click", () => gmPanel.classList.toggle("hidden"));

// ---------------- setup: slot editor ----------------

function buildSlotEditor(initialSlots) {
  slotEditor.innerHTML = "";
  for (let i = 0; i < 4; i++) {
    const init = initialSlots[i] || {};
    const nick = init.nickname || DEFAULT_NICKS[i] || `Player${i+1}`;
    const ep = init.endpoint || "https://api.openai.com/v1";
    const card = document.createElement("div");
    card.className = "slot-card";
    card.dataset.index = i;
    card.dataset.models = "[]";  // per-slot model list, populated by per-slot fetch
    card.innerHTML = `
      <div class="slot-header-row">
        <span class="slot-player-label">Player ${i + 1}</span>
        <label class="slot-nick-inline-label">Nickname:</label>
        <input class="slot-nickname" type="text" maxlength="12" value="${escapeAttr(nick)}" placeholder="Nickname" />
      </div>
      <div class="field-row">
        <label>Endpoint</label>
        <input class="slot-endpoint" type="text" value="${escapeAttr(ep)}" placeholder="https://api.openai.com/v1" />
      </div>
      <div class="field-row">
        <label>API key</label>
        <input class="slot-api-key" type="password" placeholder="sk-... (blank for localhost)" />
      </div>
      <div class="field-row">
        <button class="slot-fetch-btn" type="button">Fetch models</button>
        <span class="slot-fetch-status muted"></span>
      </div>
      <select class="slot-model"><option value="">(fetch models first)</option></select>
      <details class="slot-advanced">
        <summary>Advanced</summary>
        <div class="field-row">
          <label>Temperature</label>
          <input class="slot-temp" type="number" step="0.1" min="0" max="2" value="${init.temperature ?? 0.7}" />
        </div>
        <div class="field-row">
          <label>Hard timeout (s)</label>
          <input class="slot-timeout" type="number" min="10" value="${init.timeout_seconds ?? 180}" />
        </div>
        <div class="field-row">
          <label>Inactivity timeout (s)</label>
          <input class="slot-inactivity" type="number" min="5" value="${init.inactivity_timeout_seconds ?? 45}" />
        </div>
      </details>
      <div class="status pending">unvalidated</div>
    `;
    slotEditor.appendChild(card);

    // If the loaded config already has a model name, show it as the current
    // option so it's visible even before fetching.
    if (init.model) {
      const sel = card.querySelector(".slot-model");
      sel.innerHTML = `<option value="${escapeAttr(init.model)}" selected>${escapeHtml(init.model)} (not yet verified)</option>`;
    }

    // Per-slot Fetch button.
    card.querySelector(".slot-fetch-btn").addEventListener("click", async () => {
      await fetchSlotModels(card);
    });
  }
}

async function fetchSlotModels(card) {
  const btn = card.querySelector(".slot-fetch-btn");
  const status = card.querySelector(".slot-fetch-status");
  const endpoint = card.querySelector(".slot-endpoint").value.trim();
  const apiKey = card.querySelector(".slot-api-key").value.trim() || null;
  btn.disabled = true;
  status.textContent = "Fetching...";
  status.classList.remove("bad");
  try {
    const resp = await fetch("/api/discover", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({endpoint, api_key: apiKey}),
    });
    const data = await resp.json();
    if (!data.ok) {
      status.textContent = `Error: ${data.error}`;
      status.classList.add("bad");
      return;
    }
    const models = data.models || [];
    card.dataset.models = JSON.stringify(models);
    status.textContent = `${models.length} models found.`;
    const currentSel = card.querySelector(".slot-model").value;
    populateSlotModelOptions(card, currentSel);
  } catch (e) {
    status.textContent = `Fetch failed: ${e.message}`;
    status.classList.add("bad");
  } finally {
    btn.disabled = false;
  }
}

function populateSlotModelOptions(card, selectedValue) {
  const sel = card.querySelector(".slot-model");
  let models = [];
  try { models = JSON.parse(card.dataset.models || "[]"); } catch (_) {}
  const opts = ['<option value="">(pick a model)</option>'];
  let foundSelected = false;
  for (const m of models) {
    const sel_attr = m === selectedValue ? " selected" : "";
    if (m === selectedValue) foundSelected = true;
    opts.push(`<option value="${escapeAttr(m)}"${sel_attr}>${escapeHtml(m)}</option>`);
  }
  // If the previously-selected value isn't in the new model list, keep it
  // visible at the top with a warning so the user can see it's stale.
  if (selectedValue && !foundSelected) {
    opts.splice(1, 0, `<option value="${escapeAttr(selectedValue)}" selected>${escapeHtml(selectedValue)} (not in list)</option>`);
  }
  sel.innerHTML = opts.join("");
}

// ---------------- backend: fetch + save + validate ----------------

function gatherConfig() {
  const slots = [];
  for (const card of slotEditor.querySelectorAll(".slot-card")) {
    const endpoint = card.querySelector(".slot-endpoint").value.trim();
    const apiKey = card.querySelector(".slot-api-key").value.trim() || null;
    slots.push({
      nickname: card.querySelector(".slot-nickname").value.trim(),
      endpoint,
      model: card.querySelector(".slot-model").value.trim(),
      api_key: apiKey,
      temperature: parseFloat(card.querySelector(".slot-temp").value),
      timeout_seconds: parseInt(card.querySelector(".slot-timeout").value, 10),
      inactivity_timeout_seconds: parseInt(card.querySelector(".slot-inactivity").value, 10),
    });
  }

  const pause_points = [];
  if (cfgPausePhase.checked) pause_points.push("phase_transition");
  if (cfgPauseGameskill.checked) pause_points.push("gameskill_commit");

  return {
    slots,
    game: {
      pause_points,
      gameskill_auto_commit: cfgAutoCommit.checked,
      random_seed: cfgSeed.value.trim() || null,
    },
  };
}

btnSaveValidate.addEventListener("click", async () => {
  btnSaveValidate.disabled = true;
  btnStart.disabled = true;
  validateOut.textContent = "Saving config...";

  const payload = gatherConfig();
  try {
    const saveResp = await fetch("/api/save_config", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    if (!saveResp.ok) {
      const j = await saveResp.json().catch(() => ({}));
      validateOut.textContent = `Save failed: ${j.detail || saveResp.statusText}`;
      return;
    }
  } catch (e) {
    validateOut.textContent = `Save request failed: ${e.message}`;
    return;
  } finally {
    btnSaveValidate.disabled = false;
  }

  validateOut.textContent = "Validating endpoints...";
  try {
    const resp = await fetch("/api/validate", { method: "POST" });
    const data = await resp.json();
    if (data.config_error) {
      validateOut.textContent = `Config error: ${data.config_error}`;
      return;
    }
    validateOut.textContent = JSON.stringify(data, null, 2);

    // Per-slot status indicators.
    let allOk = true;
    const cards = slotEditor.querySelectorAll(".slot-card");
    for (const r of data.endpoint_reports) {
      // Match by nickname (in slot order).
      const card = Array.from(cards).find(c => c.querySelector(".slot-nickname").value.trim() === r.nickname);
      if (!card) continue;
      const status = card.querySelector(".status");
      if (r.reachable && r.model_present) {
        status.textContent = "ok -- model listed";
        status.className = "status ok";
      } else if (r.reachable) {
        status.textContent = `reachable, model "${r.model}" not listed`;
        status.className = "status bad";
        allOk = false;
      } else {
        status.textContent = `unreachable: ${r.error || "unknown"}`;
        status.className = "status bad";
        allOk = false;
      }
    }
    btnStart.disabled = !allOk;
  } catch (e) {
    validateOut.textContent = `Validation failed: ${e.message}`;
  }
});

btnStart.addEventListener("click", async () => {
  btnStart.disabled = true;
  const resp = await fetch("/api/start", { method: "POST" });
  if (!resp.ok) {
    const j = await resp.json().catch(() => ({}));
    validateOut.textContent = `Start failed: ${j.detail || resp.statusText}`;
    btnStart.disabled = false;
    return;
  }
  setupScreen.classList.remove("visible");
  gameScreen.classList.add("visible");
  openStream();
  pollState();
});

btnAdvance.addEventListener("click", async () => {
  btnAdvance.disabled = true;
  await fetch("/api/advance", { method: "POST" });
});

// ---------------- initial load: pre-populate from existing config + resumable games ----------------

async function loadInitialConfig() {
  try {
    const resp = await fetch("/api/config");
    if (!resp.ok) {
      // No config yet -- build empty slot editor.
      buildSlotEditor([]);
      return;
    }
    const data = await resp.json();
    if (data.game) {
      const pp = data.game.pause_points || [];
      cfgPausePhase.checked = pp.includes("phase_transition");
      cfgPauseGameskill.checked = pp.includes("gameskill_commit");
      cfgAutoCommit.checked = data.game.gameskill_auto_commit !== false;
      cfgSeed.value = (data.game.random_seed === null || data.game.random_seed === undefined) ? "" : data.game.random_seed;
    }
    buildSlotEditor(data.slots || []);
  } catch (e) {
    console.warn("Could not load initial config:", e);
    buildSlotEditor([]);
  }
}

async function loadResumableGames() {
  try {
    const resp = await fetch("/api/games");
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.games || data.games.length === 0) return;

    const host = document.getElementById("resume-section-host");
    const section = document.createElement("div");
    section.className = "setup-card";
    const h = document.createElement("h2");
    h.textContent = `Resumable games (${data.games.length})`;
    section.appendChild(h);

    for (const g of data.games) {
      const row = document.createElement("div");
      row.className = "resume-row";
      row.innerHTML = `
        <button class="ghost resume-btn" data-game-id="${escapeAttr(g.game_id)}">Resume</button>
        <span class="resume-id">${escapeHtml(g.game_id)}</span>
        <span class="muted">last completed: ${escapeHtml(g.last_completed_phase || "(none)")}</span>
      `;
      section.appendChild(row);
    }
    host.appendChild(section);

    section.querySelectorAll(".resume-btn").forEach(b => {
      b.addEventListener("click", async () => {
        b.disabled = true;
        const gameId = b.dataset.gameId;
        const resp = await fetch("/api/resume", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({game_id: gameId}),
        });
        if (!resp.ok) {
          const j = await resp.json().catch(() => ({}));
          alert(`Resume failed: ${j.detail || resp.statusText}`);
          b.disabled = false;
          return;
        }
        setupScreen.classList.remove("visible");
        gameScreen.classList.add("visible");
        await renderGameHistory(gameId);
        openStream();
        pollState();
      });
    });
  } catch (e) {
    console.warn("Could not load resumable games:", e);
  }
}

// ---------------- game screen: SSE + bubbles (unchanged shape from prior version) ----------------

async function renderGameHistory(gameId) {
  // Backfill the chat pane with everything that already happened before the
  // resume. Pulls a synthesized state view from /api/game_state.
  try {
    const resp = await fetch(`/api/game_state/${encodeURIComponent(gameId)}`);
    if (!resp.ok) return;
    const st = await resp.json();

    appendBubble("system", `Resumed ${st.game_id} from phase ${st.last_completed_phase || "(none)"}.`);

    // GM-side cards summary so the watcher knows the truth.
    if (st.card_holder_pre_swap) {
      const cards = Object.entries(st.card_holder_pre_swap)
        .map(([c, n]) => `card ${c}=${n}`).join(", ");
      appendBubble("system", `[GM only] Deal: ${cards}.`);
    }
    if (st.swap_record) {
      appendBubble("system",
        `[GM only] Troublemaker swapped ${st.swap_record[0]} and ${st.swap_record[1]}.`);
    }
    // Night actions (Seer peeks, etc).
    for (const a of (st.night_actions || [])) {
      if (a.role === "seer" && a.runner_result) {
        appendBubble("gm-reveal",
          `(historical) ${a.runner_result}`,
          `GM -> ${a.actor}`);
      }
    }
    // Cheat sheet for the panel.
    if (st.card_holder_post_swap) {
      cheatSheet.textContent = Object.entries(st.card_holder_post_swap)
        .map(([c, n]) => `card ${c} -> ${n}`).join("\n");
    }

    // Public chat (the most important thing to replay).
    for (const t of (st.public_chat || [])) {
      appendBubble("public", t.statement, t.speaker);
      if (t.target && t.question) {
        appendBubble("public", `-> ${t.target}: ${t.question}`, `${t.speaker} asks`);
      }
    }

    // Each player's current notes as collapsed private bubbles (clickable to expand).
    for (const [nick, text] of Object.entries(st.notes || {})) {
      if (!text) continue;
      appendBubble("private", text, `${nick} (notes so far)`);
    }

    // Votes if any.
    for (const [voter, _target] of Object.entries(st.votes || {})) {
      appendBubble("system", `${voter} had voted.`);
    }

    appendBubble("system", "--- live updates resume below ---");
  } catch (e) {
    console.warn("Could not backfill history:", e);
  }
}

function openStream() {
  if (activeStream) activeStream.close();
  const es = new EventSource("/api/stream");
  activeStream = es;
  es.addEventListener("phase_entered", (e) => onPhaseEntered(JSON.parse(e.data)));
  es.addEventListener("setup_complete", (e) => onSetupComplete(JSON.parse(e.data)));
  es.addEventListener("announcement", (e) => onAnnouncement(JSON.parse(e.data)));
  es.addEventListener("gm_only_swap", (e) => onGmOnlySwap(JSON.parse(e.data)));
  es.addEventListener("private_action_started", (e) => onPrivateActionStarted(JSON.parse(e.data)));
  es.addEventListener("delta", (e) => onDelta(JSON.parse(e.data)));
  es.addEventListener("private_notes_updated", (e) => onPrivateNotesUpdated(JSON.parse(e.data)));
  es.addEventListener("public_turn_started", (e) => onPublicTurnStarted(JSON.parse(e.data)));
  es.addEventListener("public_turn", (e) => onPublicTurn(JSON.parse(e.data)));
  es.addEventListener("gm_reveal", (e) => onGmReveal(JSON.parse(e.data)));
  es.addEventListener("validation_retry", (e) => onValidationRetry(JSON.parse(e.data)));
  es.addEventListener("intervention_required", (e) => onInterventionRequired(JSON.parse(e.data)));
  es.addEventListener("llm_retry", (e) => onLlmRetry(JSON.parse(e.data)));
  es.addEventListener("vote_cast", (e) => onVoteCast(JSON.parse(e.data)));
  es.addEventListener("reveal", (e) => onReveal(JSON.parse(e.data)));
  es.addEventListener("afterthought_written", (e) => onAfterthought(JSON.parse(e.data)));
  es.addEventListener("gameskill_committed", (e) => onGameskill(JSON.parse(e.data), false));
  es.addEventListener("gameskill_staged", (e) => onGameskill(JSON.parse(e.data), true));
  es.addEventListener("game_log_written", () => appendBubble("system", "Game log written."));
  es.addEventListener("awaiting_gm_advance", (e) => onAwaitingAdvance(JSON.parse(e.data)));
  es.addEventListener("gm_advanced", () => { gmPaused.textContent = "no"; btnAdvance.disabled = true; });
  es.addEventListener("phase_not_implemented", (e) => {
    const d = JSON.parse(e.data);
    appendBubble("system", `Phase not implemented: ${d.phase}. ${d.message}`);
  });
  es.addEventListener("phase_error", (e) => {
    const d = JSON.parse(e.data);
    appendBubble("system", `Phase ${d.phase} errored: ${d.error}: ${d.message}`);
  });
  es.addEventListener("game_started", (e) => {
    const d = JSON.parse(e.data);
    if (d.resumed) appendBubble("system", `Resumed game ${d.game_id} after ${d.resumed_from_phase}.`);
  });
  es.addEventListener("game_ended", () => appendBubble("system", "Game ended."));
  es.onerror = () => {};
}

async function pollState() {
  try {
    const resp = await fetch("/api/state");
    if (!resp.ok) return;
    const st = await resp.json();
    if (st.running) {
      phasePill.textContent = st.phase;
      gmPhase.textContent = st.phase;
      gmPaused.textContent = st.awaiting_gm_advance ? `yes (${st.awaiting_reason})` : "no";
      btnAdvance.disabled = !st.awaiting_gm_advance;
      if (st.card_holders_post_swap) {
        cheatSheet.textContent = Object.entries(st.card_holders_post_swap)
          .map(([card, nick]) => `card ${card} -> ${nick}`)
          .join("\n");
      }
    }
  } finally {
    setTimeout(pollState, 1500);
  }
}

function onPhaseEntered(d) {
  appendBubble("system", `Phase: ${d.phase}`);
  phasePill.textContent = d.phase;
  gmPhase.textContent = d.phase;
}
function onSetupComplete(d) {
  appendBubble("system",
    `Intros: ${d.intro_order.join(" -> ")}. R1 starter: ${d.r1_starter}. R2 starter: ${d.r2_starter}.`);
}
function onAnnouncement(d) { appendBubble("system", d.text); }
function onGmOnlySwap(d) {
  appendBubble("system", `[GM only] Troublemaker swapped ${d.a} and ${d.b}. (Players were not told.)`);
}
function onPrivateActionStarted(d) {
  const key = `${d.nickname}|${d.kind || "stream"}`;
  const bubble = appendBubble("private", "", `${d.nickname} (${labelForKind(d.kind)})`);
  bubble.dataset.streamKey = key;
  streamingBubbles.set(key, bubble);
}
function onDelta(d) {
  const key = `${d.nickname}|${d.kind}`;
  let bubble = streamingBubbles.get(key);
  if (!bubble) {
    bubble = appendBubble("private", "", `${d.nickname} (${labelForKind(d.kind)})`);
    bubble.dataset.streamKey = key;
    streamingBubbles.set(key, bubble);
  }
  bubble.classList.remove("collapsed");
  bubble.querySelector(".body").textContent += d.text;
  scrollToBottom();
}
function onPrivateNotesUpdated(d) {
  for (const [key, bubble] of streamingBubbles.entries()) {
    if (key.startsWith(d.nickname + "|")) {
      bubble.classList.add("collapsed");
      streamingBubbles.delete(key);
    }
  }
}
function onPublicTurnStarted(d) {
  const key = `${d.nickname}|${d.kind}`;
  const bubble = appendBubble("public", "", `${d.nickname}`);
  bubble.dataset.streamKey = key;
  streamingBubbles.set(key, bubble);
}
function onPublicTurn(d) {
  let bubble = null;
  for (const [key, b] of streamingBubbles.entries()) {
    if (key.startsWith(d.speaker + "|") && b.classList.contains("public")) {
      bubble = b;
      streamingBubbles.delete(key);
      break;
    }
  }
  if (!bubble) {
    bubble = appendBubble("public", d.statement, d.speaker);
  } else {
    const body = bubble.querySelector(".body");
    if (!body.textContent.trim()) body.textContent = d.statement;
  }
  if (d.target && d.question) {
    appendBubble("public", `-> ${d.target}: ${d.question}`, `${d.speaker} asks`);
  }
}
function onGmReveal(d) { appendBubble("gm-reveal", d.text, `GM -> ${d.nickname}`); }
function onValidationRetry(d) {
  appendBubble("system", `re-prompt: ${d.nickname} failed ${d.kind} (attempt ${d.attempt}): ${d.error}`);
}
function onInterventionRequired(d) {
  appendBubble("system", `INTERVENTION REQUIRED: ${d.nickname} failed ${d.kind} -- ${d.reason}. Last response: ${d.last_response}`);
}
function onLlmRetry(d) {
  appendBubble("system", `LLM retry (attempt ${d.attempt}) for ${d.nickname} on ${d.step}: ${d.error}`);
}
function onVoteCast(d) { appendBubble("system", `${d.voter} has voted.`); }
function onReveal(d) {
  let s = `REVEAL\n\n`;
  s += `Pre-swap: ${formatHolders(d.pre_swap)}\n`;
  s += `Swap: ${d.swap_record ? d.swap_record.join(" <-> ") : "(none)"}\n`;
  s += `Post-swap: ${formatHolders(d.post_swap)}\n\n`;
  s += `Votes:\n`;
  for (const [voter, target] of Object.entries(d.votes)) {
    s += `  ${voter} -> ${target}\n`;
  }
  s += `\nTop-voted: ${d.top_voted.join(", ")}\n`;
  s += `Werewolf at vote time: ${d.werewolf_holder}\n\n`;
  s += d.village_wins ? "VILLAGE WINS" : "WEREWOLF WINS";
  appendBubble("system", s);
}
function onAfterthought(d) {
  // The streaming bubble (created by private_action_started + deltas) already
  // contains the full afterthought. We just leave it expanded so the watcher
  // can read it without clicking.
  for (const [key, bubble] of streamingBubbles.entries()) {
    if (key === `${d.nickname}|afterthought`) {
      bubble.classList.remove("collapsed");
      streamingBubbles.delete(key);
      return;
    }
  }
  // Fallback: if for some reason no streaming bubble exists, render the preview.
  appendBubble("private", d.preview, `${d.nickname} (afterthought)`).classList.remove("collapsed");
}
function onGameskill(d, staged) {
  const flag = d.compressed ? " (compressed)" : "";
  const action = staged ? "staged for review" : "committed";
  appendBubble("system", `${d.nickname} gameskill ${action}${flag}.`);
}
function onAwaitingAdvance(d) {
  gmPaused.textContent = `yes (after ${d.after_phase})`;
  btnAdvance.disabled = false;
  appendBubble("system", `Paused for GM review (after ${d.after_phase}). Click Advance to continue.`);
}

function formatHolders(holders) {
  const cardNames = { 1: "WW", 2: "Seer", 3: "TM", 4: "Vill" };
  return Object.entries(holders)
    .map(([c, n]) => `${cardNames[c] || c}=${n}`)
    .join(", ");
}
function labelForKind(kind) {
  if (!kind) return "stream";
  return kind.replace(/_/g, " ");
}
function appendBubble(kind, text, label) {
  const div = document.createElement("div");
  div.className = `bubble ${kind}`;
  if (kind === "private") div.classList.add("collapsed");
  if (label) {
    const l = document.createElement("div");
    l.className = "label";
    l.textContent = label;
    div.appendChild(l);
    if (kind === "private") {
      l.addEventListener("click", () => div.classList.toggle("collapsed"));
    }
  }
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = text;
  div.appendChild(body);
  chat.appendChild(div);
  scrollToBottom();
  return div;
}
function scrollToBottom() {
  chat.parentElement.scrollTop = chat.parentElement.scrollHeight;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

loadInitialConfig();
loadResumableGames();
