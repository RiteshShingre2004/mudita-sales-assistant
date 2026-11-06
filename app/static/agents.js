// Three-agent coordinator UI. State lives on the server; the selected session and run are
// kept in the URL hash so a refresh returns to the same place.

const $ = (id) => document.getElementById(id);
const AGENTS = [
  { key: "intake", name: "Intake", job: "Writes down what was said, with line numbers" },
  { key: "planning", name: "Planning", job: "Turns the facts and rules into tasks" },
  { key: "review", name: "Review", job: "Checks the plan and sends fixes back" },
];
const AGENT_NAME = { intake: "Intake", planning: "Planning", review: "Review", coordinator: "Coordinator" };
const RUN_STATUS = {
  pending: "Queued", running: "Working", completed: "Approved", needs_review: "Needs a person",
  failed: "Stopped", interrupted: "Interrupted",
};
const BUSY = ["running", "pending"];

let sessions = [];
let sid = null, rid = null;
let run = null, lastJson = "";
let tab = "plan", userPickedTab = false, planView = "list", focusTask = null;
let drawerStep = null, ioMode = "output";
let factsVersion = null, highlightLines = new Set(), highlightFact = null, editingFact = null;
let sample = null, pollTimer = null;

const plural = (n, w) => `${n} ${w}${n === 1 ? "" : /(s|x|ch|sh)$/.test(w) ? "es" : "s"}`;
const isBusy = () => run && BUSY.includes(run.status);

// ---------------------------------------------------------------- url state
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  return { s: p.get("s"), r: p.get("r") };
}
function writeHash() {
  const p = new URLSearchParams();
  if (sid) p.set("s", sid);
  if (rid) p.set("r", rid);
  history.replaceState(null, "", "#" + p.toString());
}
function selectRun(id) {
  rid = id; run = null; lastJson = ""; factsVersion = null; focusTask = null;
  highlightLines = new Set(); highlightFact = null; editingFact = null; userPickedTab = false;
  closeDrawer(); writeHash();
}

// ---------------------------------------------------------------- sessions
async function loadSessions() {
  sessions = await api("/api/agents/sessions");
  if (!sessions.length) {
    await api("/api/agents/sessions", { method: "POST", body: JSON.stringify({ name: "Session A" }) });
    sessions = await api("/api/agents/sessions");
  }
  if (!sid || !sessions.some((s) => s.id === sid)) sid = sessions[0].id;
  $("session-select").innerHTML = sessions.map((s) =>
    `<option value="${esc(s.id)}" ${s.id === sid ? "selected" : ""}>${esc(s.name)} · ${plural(s.runs, "run")}</option>`).join("");
}

async function loadSession() {
  const s = await api(`/api/agents/sessions/${sid}`);
  if (document.activeElement !== $("notes")) $("notes").value = s.private_notes || "";
  $("notes-state").textContent = s.private_notes ? "Saved" : "";
  $("runs").innerHTML = s.runs.length ? s.runs.map((r) => `
    <li><button type="button" data-run="${esc(r.id)}" aria-current="${r.id === rid}">
      <span class="dot" data-st="${esc(r.status)}" aria-hidden="true"></span>
      <span class="rr-title">${esc(r.title)}</span>
      <span class="rr-sub">${esc(RUN_STATUS[r.status] || r.status)}${r.context_version > 1 ? ` · v${r.context_version}` : ""}${r.cost_usd ? ` · $${r.cost_usd.toFixed(4)}` : ""}</span>
    </button></li>`).join("")
    : `<li class="rail-empty">No runs yet. Start one with the sample meeting.</li>`;
  if (rid && !s.runs.some((r) => r.id === rid)) rid = null;
  return s;
}

$("session-select").addEventListener("change", async (e) => {
  sid = e.target.value; selectRun(null); await refreshAll();
});
$("new-session").addEventListener("click", async () => {
  const name = `Session ${String.fromCharCode(65 + sessions.length)}`;
  const s = await api("/api/agents/sessions", { method: "POST", body: JSON.stringify({ name }) });
  sid = s.id; selectRun(null);
  toast(`${name} created. Its runs and notes are private to it.`, "ok");
  await refreshAll();
});
$("save-notes").addEventListener("click", async () => {
  await api(`/api/agents/sessions/${sid}/notes`, { method: "PUT", body: JSON.stringify({ notes: $("notes").value }) });
  $("notes-state").textContent = "Saved";
  toast("Notes saved. Only this session's Planning Agent will see them.", "ok");
});
$("notes").addEventListener("input", () => { $("notes-state").textContent = "Unsaved"; });
$("reset").addEventListener("click", async () => {
  if (!confirm("Delete every run and the private notes in this session? Other sessions are not touched.")) return;
  const r = await api(`/api/agents/sessions/${sid}/reset`, { method: "POST" });
  selectRun(null); toast(`Session reset: ${plural(r.deleted_runs, "run")} deleted.`, "ok");
  await refreshAll();
});
$("runs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-run]");
  if (!b) return;
  selectRun(b.dataset.run); refreshAll();
});

// ---------------------------------------------------------------- new run form
$("new-run").addEventListener("click", () => { selectRun(null); render(); loadSession(); });
$("load-sample").addEventListener("click", fillSample);
async function fillSample() {
  sample = sample || await api("/api/agents/sample");
  $("f-title").value = sample.title;
  $("f-transcript").value = sample.transcript;
  $("f-rules").value = sample.rules_yaml;
}
$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("form-error").textContent = "";
  const btn = e.submitter; btn.disabled = true; btn.textContent = "Starting…";
  try {
    const r = await api(`/api/agents/sessions/${sid}/runs`, {
      method: "POST", body: JSON.stringify({
        title: $("f-title").value, transcript: $("f-transcript").value, rules_yaml: $("f-rules").value,
        simulate_failure: $("f-fail").value || null, seed_violation: $("f-seed").checked,
      }),
    });
    selectRun(r.id);
    toast("Run started. Watch the agents work at the top.", "ok");
    await refreshAll();
  } catch (err) {
    $("form-error").textContent = err.message;
  } finally {
    btn.disabled = false; btn.textContent = "Start run";
  }
});

// ---------------------------------------------------------------- helpers over the run
function stepsAt(agent) {
  if (!run) return [];
  const v = agent === "intake" ? 1 : run.context_version;
  return run.steps.filter((s) => s.agent === agent && s.context_version === v).sort((a, b) => a.round - b.round);
}
const latestStep = (agent) => stepsAt(agent).at(-1);
function ruleValue(key) {
  const m = new RegExp(`^${key}:\\s*["']?([^"'\\n#]+)`, "m").exec(run?.rules_yaml || "");
  return m ? m[1].trim() : null;
}
const maxRounds = () => Number(ruleValue("max_review_rounds") || 3);
function tokens(step) {
  return (step?.usage || []).reduce((a, x) => a + (x.prompt_tokens || 0) + (x.completion_tokens || 0), 0);
}
function fmtDay(iso) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}
const msgFor = (step, kinds) => run.messages.find((m) => m.from === step.agent && m.round === step.round &&
  m.context_version === step.context_version && kinds.includes(m.kind));

// ---------------------------------------------------------------- header
function renderHead() {
  $("run-title").textContent = run.title;
  const tok = run.steps.reduce((a, s) => a + tokens(s), 0);
  $("run-meta").innerHTML = `<span class="pill" data-st="${esc(run.status)}">${esc(RUN_STATUS[run.status] || run.status)}</span>
    <span>context v${run.context_version || "–"}</span><span>${plural(run.steps.length, "step")}</span>
    <span>${tok.toLocaleString()} tokens</span><span>$${(run.cost_usd || 0).toFixed(4)}</span>
    <code title="Run ID">${esc(run.id)}</code>`;
  const canResume = ["failed", "interrupted"].includes(run.status);
  $("run-actions").innerHTML = `
    ${canResume ? `<button type="button" class="primary" id="resume">Resume</button>` : ""}
    <details class="menu" ${isBusy() ? "data-disabled" : ""}>
      <summary title="Demo tools">Demo</summary>
      <div class="menu-pop">
        <p class="menu-title">Simulate a failure</p>
        <p class="hint">The next model call of the agent fails, clearly labelled. Correct a fact to trigger it, then Resume.</p>
        <button type="button" data-sim="planning" ${isBusy() ? "disabled" : ""}>Fail Planning's next call</button>
        <button type="button" data-sim="review" ${isBusy() ? "disabled" : ""}>Fail Review's next call</button>
      </div>
    </details>`;
  const err = $("run-error");
  err.hidden = !run.error;
  const sentence = (t) => /[.!?)]$/.test(t) ? t : t + ".";
  err.innerHTML = run.error ? `<b>Stopped.</b> ${esc(sentence(run.error))}${canResume ? " Finished steps are kept, and Resume picks up from the failed one." : ""}` : "";
  if (canResume) $("resume").onclick = async (e) => {
    e.target.disabled = true;
    try {
      await api(`/api/agents/sessions/${sid}/runs/${rid}/resume`, { method: "POST" });
      toast("Resuming from the failed step. Finished steps are reused.", "ok");
      userPickedTab = false; lastJson = ""; refreshRun();
    } catch (err2) { toast(err2.message, "error"); e.target.disabled = false; }
  };
}

// ---------------------------------------------------------------- the agent strip
function renderFlow() {
  const parts = [];
  AGENTS.forEach((a, i) => {
    const st = latestStep(a.key);
    const state = !st ? "waiting" : st.status === "succeeded" ? "done" : st.status === "running" ? "working" : st.status;
    const label = { waiting: "Waiting", done: "Done", working: "Working", failed: "Failed", pending: "Queued" }[state] || state;
    const ok = st && st.status === "succeeded";
    const sub = !st ? a.job : a.key === "intake" ? (ok ? plural(st.output.facts.length, "fact") : "Reading the transcript")
      : `Round ${st.round} of ${maxRounds()}${a.key === "review" && ok ? ` · ${st.output.verdict === "approve" ? "approve" : "revise"}` : ""}`;
    parts.push(`
      <li class="node" data-agent="${a.key}" data-state="${state}">
        <button type="button" ${st ? `data-step="${st.id}"` : "disabled"} title="${st ? "Open this step" : a.job}">
          <span class="node-top"><span class="node-name">${a.name}</span><span class="node-state">${label}</span></span>
          <span class="node-sub">${esc(sub)}</span>
          ${st && st.status === "failed" ? `<span class="node-err">${esc(st.error)}</span>` : ""}
          ${st ? `<span class="node-meta">${tokens(st).toLocaleString()} tokens${st.attempts > 1 ? ` · attempt ${st.attempts}` : ""}</span>` : ""}
        </button>
      </li>`);
    if (i < AGENTS.length - 1) parts.push(`<li class="link" aria-hidden="true">${linkLabel(a.key, AGENTS[i + 1].key)}</li>`);
  });
  $("flow").innerHTML = parts.join("");
}
function linkLabel(from, to) {
  const msgs = run.messages.filter((m) => m.context_version === (from === "intake" ? 1 : run.context_version));
  const fwd = msgs.filter((m) => m.from === from && m.to === to).at(-1);
  const back = msgs.filter((m) => m.from === to && m.to === from).at(-1);
  const f = !fwd ? "" : from === "intake" ? plural(fwd.payload.fact_ids.length, "fact") : `plan, round ${fwd.round}`;
  const b = !back ? "" : plural(back.payload.corrections.length, "fix");
  return `<span class="wire ${fwd ? "on" : ""}"><span class="wire-label">${esc(f)}</span></span>
    ${b ? `<span class="wire wire-back on"><span class="wire-label">${esc(b)} back</span></span>` : ""}`;
}

// ---------------------------------------------------------------- outcome
function renderOutcome() {
  const res = run.result || {};
  const el = $("outcome");
  if (isBusy()) {
    const active = run.steps.find((s) => s.status === "running");
    el.innerHTML = `<div class="outcome working"><span class="spinner" aria-hidden="true"></span>
      <div><b>${active ? `${AGENT_NAME[active.agent]} Agent is working` : "Starting"}</b>
      <p>${active && active.agent !== "intake" ? `Round ${active.round} of ${maxRounds()}, context v${active.context_version}. ` : ""}This page updates live.</p></div></div>`;
    return;
  }
  if (!res.plan) { el.innerHTML = ""; return; }
  const over = res.overruled || [];
  const tasks = res.plan.tasks || [];
  const owners = new Set(tasks.map((t) => t.owner).filter(Boolean));
  const stats = `<ul class="stats">
      <li><b>${tasks.length}</b> tasks</li><li><b>${owners.size}</b> owners</li>
      <li><b>${res.rounds}</b> of ${maxRounds()} rounds</li><li><b>${(res.plan.open_questions || []).length}</b> open questions</li></ul>`;
  if (res.approved) {
    el.innerHTML = `<div class="outcome ok"><span class="badge-big" aria-hidden="true">✓</span>
      <div><b>Plan approved</b>
      <p>Approved in round ${res.rounds} with no rule-checker findings, on context v${res.context_version}.${over.length ? ` The rule checker overruled ${plural(over.length, "false alarm")} from the Review Agent; see below.` : ""}</p>
      ${stats}</div></div>
      ${over.length ? `<ul class="corr">${over.map(corrItem).join("")}</ul>` : ""}`;
  } else {
    el.innerHTML = `<div class="outcome warn"><span class="badge-big" aria-hidden="true">!</span>
      <div><b>Needs a person</b>
      <p>${plural(res.unresolved.length, "issue")} still open after ${plural(res.rounds, "round")}. The plan below is the last draft.</p>
      ${stats}</div></div>
      <ul class="corr">${res.unresolved.map(corrItem).join("")}${over.map(corrItem).join("")}</ul>`;
  }
}

function corrItem(c) {
  const head = `<span class="ref">${esc(c.reference || "")}</span>${c.task_id ? `<button type="button" class="chip-task" data-task="${esc(c.task_id)}">${esc(c.task_id)}</button>` : ""}`;
  if (c.overruled) {
    return `<li class="overruled">${head}<s>${esc(c.problem)}</s>
      <span class="fix">Overruled by the rule checker: ${esc(c.overruled)}.</span><span class="who">Review Agent claim, not applied</span></li>`;
  }
  return `<li>${head}${esc(c.problem)}
    <span class="fix">Fix: ${esc(c.fix)}</span>${c.from ? `<span class="who">from the ${c.from === "rule_checker" ? "rule checker" : "Review Agent"}</span>` : ""}</li>`;
}

// ---------------------------------------------------------------- plan tab
function basisChip(basis) {
  if (basis === "fact") return `<span class="basis fact" title="Said in the meeting">stated</span>`;
  if (basis === "recommendation") return `<span class="basis rec" title="Proposed by the Planning Agent">suggested</span>`;
  return "";
}
const initials = (name) => (name || "?").split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
const factChips = (ids) => (ids || []).map((f) => `<button type="button" class="chip-fact" data-fact="${esc(f)}">${esc(f)}</button>`).join("");

function related(tasks, id) {
  if (!id) return { deps: new Set(), users: new Set() };
  const t = tasks.find((x) => x.id === id);
  return { deps: new Set(t?.depends_on || []), users: new Set(tasks.filter((x) => (x.depends_on || []).includes(id)).map((x) => x.id)) };
}

function planList(p) {
  const tasks = p.tasks || [];
  const rel = related(tasks, focusTask);
  return `<ol class="tasks">${tasks.map((t) => {
    const cls = t.id === focusTask ? "focus" : rel.deps.has(t.id) ? "dep" : rel.users.has(t.id) ? "user" : focusTask ? "dim" : "";
    return `<li class="task ${cls}" data-task-row="${esc(t.id)}" tabindex="0">
      <span class="t-id">${esc(t.id)}</span>
      <div class="t-main">
        <div class="t-title">${esc(t.title)}</div>
        <div class="t-tags">${t.is_security_review ? "<span>security review</span>" : ""}${t.touches_customer_data ? "<span>customer data</span>" : ""}${t.client_facing ? "<span>client-facing</span>" : ""}${t.is_signoff ? "<span>sign-off</span>" : ""}
          ${(t.depends_on || []).length ? `<span class="after">after ${t.depends_on.map(esc).join(", ")}</span>` : ""}</div>
        ${t.rationale ? `<div class="t-why">${esc(t.rationale)}</div>` : ""}
      </div>
      <div class="t-owner"><span class="avatar">${esc(initials(t.owner))}</span><span>${esc(t.owner || "No owner")}<br>${basisChip(t.owner_basis)}</span></div>
      <div class="t-due"><b>${t.deadline ? esc(fmtDay(t.deadline)) : "No date"}</b><br>${basisChip(t.deadline_basis)}</div>
      <div class="t-src">${factChips(t.sources)}</div>
    </li>`;
  }).join("")}</ol>`;
}

function planSchedule(p) {
  const start = ruleValue("meeting_date"), end = ruleValue("contract_go_live");
  if (!start || !end) return `<p class="hint">The schedule needs meeting_date and contract_go_live in the rules.</p>`;
  const day0 = new Date(start + "T00:00:00"), dayN = new Date(end + "T00:00:00");
  const days = [];
  for (let d = new Date(day0); d <= dayN; d.setDate(d.getDate() + 1)) days.push(new Date(d));
  const idx = (iso) => iso ? Math.round((new Date(iso + "T00:00:00") - day0) / 864e5) : null;
  const tasks = p.tasks || [];
  const byId = Object.fromEntries(tasks.map((t) => [t.id, t]));
  const rel = related(tasks, focusTask);
  const cols = `grid-template-columns: minmax(140px, 220px) repeat(${days.length}, minmax(18px, 1fr))`;
  const head = days.map((d) => {
    const we = d.getDay() === 0 || d.getDay() === 6;
    return `<span class="sc-day ${we ? "we" : ""}" title="${d.toDateString()}">${d.getDate()}<small>${"SMTWTFS"[d.getDay()]}</small></span>`;
  }).join("");
  const rows = tasks.map((t) => {
    const due = idx(t.deadline);
    const depEnd = Math.max(0, ...(t.depends_on || []).map((d) => idx(byId[d]?.deadline) ?? 0));
    const from = due == null ? null : Math.min(depEnd, due);
    const cls = t.id === focusTask ? "focus" : rel.deps.has(t.id) ? "dep" : rel.users.has(t.id) ? "user" : focusTask ? "dim" : "";
    const cells = days.map((d, i) => `<span class="sc-cell ${d.getDay() % 6 === 0 ? "we" : ""}" style="grid-column:${i + 2}"></span>`).join("");
    const bar = due == null || due < 0 || due >= days.length ? "" :
      `<span class="sc-bar" data-agent-basis="${esc(t.deadline_basis)}" style="grid-column:${from + 2} / ${due + 3}" title="${esc(t.id)} due ${esc(fmtDay(t.deadline))}"><span class="sc-due"></span></span>`;
    return `<div class="sc-row ${cls}" data-task-row="${esc(t.id)}" style="${cols}">
      <span class="sc-label"><b>${esc(t.id)}</b> ${esc(t.owner ? t.owner.split(" ")[0] : "")}<small>${esc(t.title)}</small></span>${cells}${bar}</div>`;
  }).join("");
  return `<div class="sched"><div class="sc-row sc-head" style="${cols}"><span class="sc-label"><small>${esc(fmtDay(start))} → go-live ${esc(fmtDay(end))}</small></span>${head}</div>${rows}</div>
    <p class="hint legend">Each bar runs from when the task's dependencies are due to its own deadline. Shaded columns are weekends (rule R2). Click a task to see what it depends on.</p>`;
}

function renderPlanTab() {
  const res = run.result || {};
  const done = stepsAt("planning").filter((x) => x.status === "succeeded").at(-1);
  const p = res.plan || done?.output;
  if (!p) { $("tab-plan").innerHTML = `<p class="empty">The plan appears here once the Planning Agent has a draft.</p>`; return; }
  const draft = !res.plan;
  $("tab-plan").innerHTML = `
    <div class="tab-bar">
      <p class="hint">${draft ? `Draft from round ${done.round}, still under review. ` : ""}
        <span class="basis fact">stated</span> said in the meeting · <span class="basis rec">suggested</span> proposed by Planning</p>
      <div class="seg small" role="tablist">
        <button type="button" data-view="list" aria-selected="${planView === "list"}">List</button>
        <button type="button" data-view="schedule" aria-selected="${planView === "schedule"}">Schedule</button>
      </div>
    </div>
    ${planView === "list" ? planList(p) : planSchedule(p)}
    <div class="extras">
      ${(p.open_questions || []).length ? `<div class="extra"><h3>Open questions</h3><ul>${p.open_questions.map((q) => `<li>${esc(q.text)} ${factChips(q.sources)}</li>`).join("")}</ul></div>` : ""}
      ${(p.recommendations || []).length ? `<div class="extra"><h3>Recommendations</h3><ul>${p.recommendations.map((r) => `<li>${esc(r.text)} <span class="sub">${esc(r.why)}</span></li>`).join("")}</ul></div>` : ""}
    </div>`;
}

// ---------------------------------------------------------------- story tab (collaboration)
function storyEntry(s) {
  const o = s.output || {};
  let title = "", body = "";
  if (s.agent === "intake") {
    title = "Wrote down what was said";
    body = s.status === "succeeded" ? `${plural(o.facts.length, "fact")}, ${plural(o.conflicts.length, "conflict")}, ${plural(o.missing.length, "gap")}, each tied to transcript lines.` : "";
  } else if (s.agent === "planning") {
    title = s.round === 1 ? "Drafted the plan" : `Revised the plan (round ${s.round})`;
    body = s.status === "succeeded" ? `${plural(o.tasks.length, "task")}, ${plural((o.open_questions || []).length, "open question")}.` : "";
    if (o._demo_note) body += ` <span class="demo">${esc(o._demo_note)}</span>`;
  } else {
    const m = msgFor(s, ["feedback", "final", "approval"]);
    const sent = m?.kind === "feedback" ? m.payload.corrections : m?.kind === "final" ? m.payload.unresolved : [];
    const over = m?.payload?.overruled || [];
    title = s.status !== "succeeded" ? `Checking round ${s.round}`
      : m?.kind === "approval" ? `Approved round ${s.round}` : m?.kind === "final" ? `Round ${s.round}: issues left for a person` : `Sent ${plural(sent.length, "fix")} back`;
    body = s.status === "succeeded" ? `<span class="verdict" data-v="${esc(o.verdict)}">${o.verdict === "approve" ? "approve" : "revise"}</span> ${esc(o.summary || "")}` : "";
    if (sent.length || over.length) body += `<ul class="corr">${sent.map(corrItem).join("")}${over.map(corrItem).join("")}</ul>`;
  }
  const state = s.status === "succeeded" ? "" : s.status === "running" ? `<span class="pill" data-st="running">working</span>` : `<span class="pill" data-st="failed">${esc(s.status)}</span>`;
  return `<li class="ev" data-agent="${s.agent}">
    <span class="ev-dot" aria-hidden="true"></span>
    <div class="ev-card">
      <div class="ev-head"><span class="ev-who">${AGENT_NAME[s.agent]}</span> <b>${esc(title)}</b> ${state}
        <button type="button" class="link-btn" data-step="${s.id}">Details</button></div>
      ${s.status === "failed" ? `<p class="err-text">${esc(s.error)}</p>` : ""}
      ${body ? `<div class="ev-body">${body}</div>` : ""}
      <div class="ev-meta">${s.agent === "intake" ? "context v1" : `round ${s.round} · context v${s.context_version}`} · ${tokens(s).toLocaleString()} tokens${s.attempts > 1 ? ` · attempt ${s.attempts}` : ""}</div>
    </div></li>`;
}

function renderStory() {
  if (!run.steps.length) { $("tab-story").innerHTML = `<p class="empty">The agents' handoffs appear here as they happen.</p>`; return; }
  const versions = [...new Set(run.steps.map((s) => s.context_version))].sort((a, b) => b - a);
  const intake = run.steps.filter((s) => s.agent === "intake");
  $("tab-story").innerHTML = versions.map((v) => {
    const ctx = run.contexts.find((c) => c.version === v);
    const steps = run.steps.filter((s) => s.context_version === v && (s.agent !== "intake")).sort((a, b) => a.id - b.id);
    const list = (v === 1 ? [...intake, ...steps] : steps).map(storyEntry).join("");
    const current = v === (run.context_version || 1);
    const head = `<span class="ctx-v">context v${v}</span> ${esc(ctx?.reason || "")} ${current ? `<span class="pill" data-st="completed">current</span>` : `<span class="hint">superseded</span>`}`;
    return current
      ? `<section class="ctx"><h3 class="ctx-head">${head}</h3><ol class="story">${v !== 1 ? intake.map(storyEntry).join("") : ""}${list}</ol></section>`
      : `<details class="ctx old"><summary class="ctx-head">${head}</summary><ol class="story">${list}</ol></details>`;
  }).join("");
}

// ---------------------------------------------------------------- facts / transcript / log
function lineChips(lines) {
  return (lines || []).map((n) => `<button type="button" class="chip-line" data-line="${n}">L${n}</button>`).join("");
}
function renderFactsTab() {
  const versions = run.contexts;
  if (!versions.length) { $("tab-facts").innerHTML = `<p class="empty">Facts appear when the Intake Agent finishes.</p>`; return; }
  const v = factsVersion || run.context_version;
  const ctx = versions.find((c) => c.version === v) || versions.at(-1);
  const isCurrent = ctx.version === run.context_version;
  $("tab-facts").innerHTML = `
    <div class="tab-bar">
      <p class="hint">${esc(ctx.reason)}. ${isCurrent && !isBusy() ? "Correct a fact and Planning and Review rerun on the new version; Intake is reused." : ""}</p>
      <div class="seg small" role="tablist">${versions.map((c) => `<button type="button" data-ver="${c.version}" aria-selected="${c.version === ctx.version}">v${c.version}</button>`).join("")}</div>
    </div>
    <ul class="facts">${ctx.facts.map((f) => `
      <li id="fact-${f.id}" class="${highlightFact === f.id ? "hl" : ""} ${f.origin === "corrected" ? "corrected" : ""}">
        <div class="f-head"><b>${f.id}</b><span class="kind">${esc(f.kind.replace("_", " "))}</span>${lineChips(f.source_lines)}
          ${isCurrent && !isBusy() && editingFact !== f.id ? `<button type="button" class="link-btn" data-edit="${f.id}">Correct</button>` : ""}</div>
        <div>${esc(f.text)}</div>
        ${f.owner || f.deadline ? `<div class="sub">${f.owner ? `owner ${esc(f.owner)}` : ""}${f.owner && f.deadline ? " · " : ""}${f.deadline ? `date ${esc(fmtDay(f.deadline))}` : ""}</div>` : ""}
        ${f.origin === "corrected" ? `<div class="sub corrected-note">Corrected by a person. Before: ${esc(previousText(f))}</div>` : ""}
        ${editingFact === f.id ? factForm(f) : ""}
      </li>`).join("")}</ul>
    <div class="extras">
      ${ctx.conflicts.length ? `<div class="extra"><h3>Conflicts</h3><ul>${ctx.conflicts.map((c) => `<li>${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul></div>` : ""}
      ${ctx.missing.length ? `<div class="extra"><h3>Missing</h3><ul>${ctx.missing.map((c) => `<li>${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul></div>` : ""}
    </div>`;
}
function previousText(f) {
  const p = f.previous || {};
  const parts = [];
  if (p.text !== f.text) parts.push(`“${p.text}”`);
  if ((p.owner || null) !== (f.owner || null)) parts.push(`owner ${p.owner || "none"}`);
  if ((p.deadline || null) !== (f.deadline || null)) parts.push(`date ${p.deadline || "none"}`);
  return parts.join(", ") || "unchanged";
}
function factForm(f) {
  return `<form class="fact-form" data-fact-form="${f.id}">
    <label>Text<textarea name="text" rows="2">${esc(f.text)}</textarea></label>
    <div class="ff-row"><label>Owner<input name="owner" value="${esc(f.owner || "")}"></label>
      <label>Date<input name="deadline" type="date" value="${esc(f.deadline || "")}"></label></div>
    <label>Why<input name="reason" placeholder="e.g. client confirmed the earlier go-live"></label>
    <div class="ff-row end"><button type="button" data-cancel>Cancel</button>
      <button type="submit" class="primary">Save as v${run.context_version + 1} and rerun</button></div>
  </form>`;
}
function renderTranscriptTab() {
  $("tab-transcript").innerHTML = `<ol class="transcript">${run.transcript_lines.map((l) => {
    const m = /^([^:]{1,40}):\s*(.*)$/.exec(l.text);
    return `<li id="line-${l.n}" class="${highlightLines.has(l.n) ? "hl" : ""}"><span class="ln">L${l.n}</span>
      <span>${m ? `<b class="spk">${esc(m[1])}</b> ${esc(m[2])}` : esc(l.text)}</span></li>`;
  }).join("")}</ol>`;
}
function renderLog() {
  $("tab-log").innerHTML = `<ol class="log">${run.events.map((e) => `<li data-level="${esc(e.level)}"><time>${esc(fmtTime(e.at, { second: "2-digit" }))}</time><span>${esc(e.text)}</span></li>`).join("")}</ol>`;
}

function setTab(name, byUser = false) {
  tab = name;
  if (byUser) userPickedTab = true;
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  ["plan", "story", "facts", "transcript", "log"].forEach((t) => { $(`tab-${t}`).hidden = t !== name; });
}

// ---------------------------------------------------------------- drawer (one step's input and output)
function openDrawer(stepId) {
  drawerStep = stepId; renderDrawer();
  $("drawer").classList.add("open"); $("drawer").setAttribute("aria-hidden", "false"); $("scrim").hidden = false;
  $("drawer-close").focus();
}
function closeDrawer() {
  drawerStep = null;
  $("drawer").classList.remove("open"); $("drawer").setAttribute("aria-hidden", "true"); $("scrim").hidden = true;
}
function renderDrawer() {
  if (drawerStep == null || !run) return;
  const s = run.steps.find((x) => x.id === drawerStep);
  if (!s) { closeDrawer(); return; }
  $("drawer").dataset.agent = s.agent;
  $("drawer-kicker").textContent = `${s.agent === "intake" ? "" : `Round ${s.round} · `}context v${s.context_version}${s.stale ? " · superseded" : ""}`;
  $("drawer-title").textContent = `${AGENT_NAME[s.agent]} Agent`;
  document.querySelectorAll("[data-io]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.io === ioMode)));
  let body = s.status === "failed" ? `<p class="err-text">${esc(s.error)}</p>` : "";
  body += ioMode === "output" ? (s.status === "succeeded" ? stepOutput(s) : `<p class="hint">No output yet.</p>`) : stepInput(s);
  body += `<p class="hint">${tokens(s).toLocaleString()} tokens · ${plural(s.attempts, "attempt")}</p>
    <details class="raw"><summary>Raw JSON</summary><pre>${esc(JSON.stringify(ioMode === "output" ? s.output : s.input, null, 2))}</pre></details>`;
  $("drawer-body").innerHTML = body;
}
function stepOutput(s) {
  const o = s.output;
  if (s.agent === "intake") {
    return `<ul class="facts">${o.facts.map((f) => `<li><div class="f-head"><b>${f.id}</b><span class="kind">${esc(f.kind.replace("_", " "))}</span>${lineChips(f.source_lines)}</div>
      ${esc(f.text)}${f.owner || f.deadline ? `<div class="sub">${f.owner ? `owner ${esc(f.owner)}` : ""} ${f.deadline ? `date ${esc(f.deadline)}` : ""}</div>` : ""}</li>`).join("")}</ul>
      ${o.conflicts.length ? `<h3>Conflicts</h3><ul>${o.conflicts.map((c) => `<li>${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul>` : ""}
      ${o.missing.length ? `<h3>Missing</h3><ul>${o.missing.map((c) => `<li>${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul>` : ""}`;
  }
  if (s.agent === "planning") return (o._demo_note ? `<p class="demo">${esc(o._demo_note)}</p>` : "") + planList(o);
  return `<p><span class="verdict" data-v="${esc(o.verdict)}">${o.verdict === "approve" ? "approve" : "revise"}</span> ${esc(o.summary || "")}</p>
    ${o.corrections.length ? `<ul class="corr">${o.corrections.map(corrItem).join("")}</ul>` : `<p class="hint">No corrections.</p>`}`;
}
function stepInput(s) {
  const i = s.input || {};
  if (s.agent === "intake") return `<p>The numbered transcript (${run.transcript_lines.length} lines) and the meeting date ${esc(i.meeting_date || "")}. Nothing else.</p>`;
  if (s.agent === "planning") {
    return `<dl class="io">
      <dt>Source facts, v${i.context_version}</dt><dd>${(i.facts || []).map((f) => `<button type="button" class="chip-fact" data-fact="${f.id}">${f.id}${f.origin === "corrected" ? "*" : ""}</button>`).join("")}</dd>
      <dt>Conflicts / gaps</dt><dd>${(i.conflicts || []).length} / ${(i.missing || []).length}</dd>
      <dt>Company rules</dt><dd>${(i.rules?.rules || []).map((r) => r.id).join(", ")}; a team of ${(i.rules?.team || []).length}</dd>
      <dt>This session's private notes</dt><dd>${i.session_notes ? esc(i.session_notes) : "none"}</dd>
      <dt>Previous plan</dt><dd>${i.previous_plan ? plural(i.previous_plan.tasks.length, "task") : "none (first draft)"}</dd>
      <dt>Fixes to make</dt><dd>${(i.corrections || []).length ? `<ul class="corr">${i.corrections.map(corrItem).join("")}</ul>` : "none"}</dd></dl>
      <p class="hint">Planning never sees the transcript, only verified facts.</p>`;
  }
  return `<dl class="io">
    <dt>Sources</dt><dd>Transcript (${run.transcript_lines.length} lines), facts v${i.context_version}, company rules</dd>
    <dt>Plan to check</dt><dd>${(i.plan?.tasks || []).map((t) => t.id).join(", ")}</dd>
    <dt>Rule-checker findings</dt><dd>${(i.checker_findings || []).length ? `<ul class="corr">${i.checker_findings.map((f) => corrItem({ ...f, reference: f.rule, from: "rule_checker" })).join("")}</ul>` : "none"}</dd></dl>`;
}

// ---------------------------------------------------------------- events (delegated)
document.addEventListener("click", async (e) => {
  const t = (sel) => e.target.closest(sel);
  if (t("#drawer-close") || t("#scrim")) { closeDrawer(); return; }
  const step = t("[data-step]");
  if (step && step.dataset.step) { ioMode = "output"; openDrawer(Number(step.dataset.step)); return; }
  const io = t("[data-io]");
  if (io) { ioMode = io.dataset.io; renderDrawer(); return; }
  const tb = t("[data-tab]");
  if (tb) { setTab(tb.dataset.tab, true); return; }
  const view = t("[data-view]");
  if (view) { planView = view.dataset.view; renderPlanTab(); return; }
  const ver = t("[data-ver]");
  if (ver) { factsVersion = Number(ver.dataset.ver); renderFactsTab(); return; }
  const sim = t("[data-sim]");
  if (sim) {
    await api(`/api/agents/sessions/${sid}/runs/${rid}/simulate-failure`, { method: "POST", body: JSON.stringify({ agent: sim.dataset.sim }) });
    sim.closest("details").open = false;
    toast(`Demo fault armed: ${AGENT_NAME[sim.dataset.sim]}'s next model call will fail. Correct a fact to trigger it.`, "warn", 7000);
    lastJson = ""; refreshRun(); return;
  }
  const line = t("[data-line]");
  if (line) {
    closeDrawer();
    highlightLines = new Set([Number(line.dataset.line)]);
    renderTranscriptTab(); setTab("transcript", true);
    $(`line-${line.dataset.line}`)?.scrollIntoView({ block: "center", behavior: "smooth" }); return;
  }
  const fact = t("[data-fact]");
  if (fact) {
    closeDrawer();
    highlightFact = fact.dataset.fact; factsVersion = null;
    renderFactsTab(); setTab("facts", true);
    $(`fact-${fact.dataset.fact}`)?.scrollIntoView({ block: "center", behavior: "smooth" }); return;
  }
  const task = t("[data-task]");
  if (task) {
    closeDrawer();
    focusTask = task.dataset.task; renderPlanTab(); setTab("plan", true);
    document.querySelector(`[data-task-row="${CSS.escape(focusTask)}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }); return;
  }
  const row = t("[data-task-row]");
  if (row && $("tab-plan").contains(row)) {
    focusTask = focusTask === row.dataset.taskRow ? null : row.dataset.taskRow; renderPlanTab(); return;
  }
  const edit = t("[data-edit]");
  if (edit) { editingFact = edit.dataset.edit; renderFactsTab(); return; }
  if (t("[data-cancel]")) { editingFact = null; renderFactsTab(); return; }
  // close the demo menu when clicking elsewhere
  document.querySelectorAll("details.menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { closeDrawer(); if (focusTask) { focusTask = null; renderPlanTab(); } }
  if (e.key === "Enter" && e.target.matches?.("[data-task-row]")) e.target.click();
});
document.addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-fact-form]");
  if (!form) return;
  e.preventDefault();
  const fd = new FormData(form);
  const btn = form.querySelector("button[type=submit]"); btn.disabled = true;
  try {
    const r = await api(`/api/agents/sessions/${sid}/runs/${rid}/facts/${form.dataset.factForm}`, {
      method: "POST", body: JSON.stringify({ text: fd.get("text"), owner: fd.get("owner"), deadline: fd.get("deadline") || null, reason: fd.get("reason") }),
    });
    editingFact = null; factsVersion = null; userPickedTab = false; lastJson = "";
    toast(`Saved as context v${r.context_version}. Planning and Review rerun on it; Intake is reused.`, "ok", 6000);
    refreshRun();
  } catch (err) { toast(err.message, "error"); btn.disabled = false; }
});

// ---------------------------------------------------------------- render + polling
function render() {
  const hasRun = Boolean(rid && run);
  $("run-form").hidden = Boolean(rid);
  $("run-view").hidden = !hasRun;
  if (!rid && !$("f-transcript").value) fillSample();
  if (!hasRun) return;
  if (!userPickedTab) tab = isBusy() ? "story" : run.result?.plan ? "plan" : "story";
  renderHead(); renderFlow(); renderOutcome();
  renderPlanTab(); renderStory(); if (editingFact === null) renderFactsTab(); renderTranscriptTab(); renderLog();
  setTab(tab); renderDrawer();
}

async function refreshRun() {
  if (!rid) return;
  try {
    const d = await api(`/api/agents/sessions/${sid}/runs/${rid}`);
    const json = JSON.stringify(d);
    if (json === lastJson) return;
    const finishedNow = run && BUSY.includes(run.status) && !BUSY.includes(d.status);
    lastJson = json; run = d;
    render();
    if (finishedNow) {
      toast(run.status === "completed" ? "Plan approved." : run.status === "needs_review" ? "Plan ready, with issues for a person." : "Run stopped. See the reason at the top.",
        run.status === "completed" ? "ok" : "warn", 6000);
      loadSession();
    }
  } catch (e) {
    if (e.status === 404) { selectRun(null); render(); } else console.error(e);
  }
}

async function refreshAll() {
  await loadSessions();
  await loadSession();
  if (rid) await refreshRun(); else render();
}

function schedule() {
  clearTimeout(pollTimer);
  const busy = isBusy();
  pollTimer = setTimeout(async () => { await refreshRun(); if (busy) loadSession(); schedule(); }, busy ? 1200 : 5000);
}

(async function init() {
  const h = readHash(); sid = h.s; rid = h.r;
  await loadBusinessTz();
  await refreshAll();
  writeHash();
  schedule();
})();
