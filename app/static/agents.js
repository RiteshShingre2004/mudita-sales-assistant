// Three-agent coordinator UI. State lives on the server; the selected session and run are
// kept in the URL hash so a refresh returns to the same place.

const $ = (id) => document.getElementById(id);
const AGENTS = [
  { key: "intake", name: "Intake Agent", job: "Extracts facts with line references" },
  { key: "planning", name: "Planning Agent", job: "Turns facts and rules into a plan" },
  { key: "review", name: "Review Agent", job: "Checks the plan, sends corrections back" },
];
const AGENT_NAME = { intake: "Intake", planning: "Planning", review: "Review", coordinator: "Coordinator" };
const RUN_STATUS = {
  pending: "Queued", running: "Working", completed: "Approved", needs_review: "Needs a person",
  failed: "Stopped", interrupted: "Interrupted",
};

let sessions = [];
let sid = null, rid = null;
let run = null, lastJson = "";
let selectedStep = null, userPickedStep = false, ioMode = "output", rightTab = "plan", factsVersion = null;
let highlightLines = new Set(), highlightFact = null, editingFact = null;
let sample = null, pollTimer = null;

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

// ---------------------------------------------------------------- sessions
async function loadSessions() {
  sessions = await api("/api/agents/sessions");
  if (!sessions.length) {
    await api("/api/agents/sessions", { method: "POST", body: JSON.stringify({ name: "Session A" }) });
    sessions = await api("/api/agents/sessions");
  }
  if (!sid || !sessions.some((s) => s.id === sid)) sid = sessions[0].id;
  $("session-select").innerHTML = sessions.map((s) =>
    `<option value="${esc(s.id)}" ${s.id === sid ? "selected" : ""}>${esc(s.name)} (${s.runs} run${s.runs === 1 ? "" : "s"})</option>`).join("");
}

async function loadSession() {
  const s = await api(`/api/agents/sessions/${sid}`);
  if (document.activeElement !== $("notes")) $("notes").value = s.private_notes || "";
  $("notes-state").textContent = s.private_notes ? "Saved" : "";
  $("runs").innerHTML = s.runs.length ? s.runs.map((r) => `
    <li><button type="button" data-run="${esc(r.id)}" aria-current="${r.id === rid}">
      <span class="ag-run-title">${esc(r.title)}</span>
      <span class="ag-run-sub"><span class="ag-status" data-st="${esc(r.status)}">${esc(RUN_STATUS[r.status] || r.status)}</span>
        ${r.context_version ? `context v${r.context_version}` : ""} ${r.cost_usd ? `· $${r.cost_usd.toFixed(4)}` : ""}</span>
    </button></li>`).join("")
    : `<li class="empty">No runs in this session yet.</li>`;
  if (rid && !s.runs.some((r) => r.id === rid)) rid = null;
  return s;
}

$("session-select").addEventListener("change", async (e) => {
  sid = e.target.value; rid = null; run = null; lastJson = "";
  writeHash(); await refreshAll();
});
$("new-session").addEventListener("click", async () => {
  const name = `Session ${String.fromCharCode(65 + sessions.length)}`;
  const s = await api("/api/agents/sessions", { method: "POST", body: JSON.stringify({ name }) });
  sid = s.id; rid = null; run = null; lastJson = "";
  writeHash(); toast(`${name} created. Its runs and notes are private to it.`, "ok");
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
  rid = null; run = null; lastJson = "";
  writeHash(); toast(`Session reset: ${r.deleted_runs} run(s) deleted.`, "ok");
  await refreshAll();
});
$("runs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-run]");
  if (!b) return;
  rid = b.dataset.run; run = null; lastJson = ""; selectedStep = null; userPickedStep = false; factsVersion = null;
  highlightLines = new Set(); highlightFact = null; editingFact = null;
  writeHash(); refreshAll();
});

// ---------------------------------------------------------------- new run form
$("new-run").addEventListener("click", () => { rid = null; run = null; lastJson = ""; writeHash(); render(); });
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
    rid = r.id; run = null; lastJson = ""; selectedStep = null; factsVersion = null;
    writeHash(); toast("Run started. The agents' progress appears live.", "ok");
    await refreshAll();
  } catch (err) {
    $("form-error").textContent = err.message;
  } finally {
    btn.disabled = false; btn.textContent = "Start run";
  }
});

// ---------------------------------------------------------------- run view
function stepsAt(agent) {
  if (!run) return [];
  const v = agent === "intake" ? 1 : run.context_version;
  return run.steps.filter((s) => s.agent === agent && s.context_version === v).sort((a, b) => a.round - b.round);
}
function latestStep(agent) { const s = stepsAt(agent); return s[s.length - 1]; }
function rulesMaxRounds() {
  const m = /max_review_rounds:\s*(\d+)/.exec(run?.rules_yaml || ""); return m ? Number(m[1]) : 3;
}
function tokens(step) {
  const u = step?.usage || [];
  return u.reduce((a, x) => a + (x.prompt_tokens || 0) + (x.completion_tokens || 0), 0);
}

function renderPipeline() {
  const parts = [];
  AGENTS.forEach((a, i) => {
    const st = latestStep(a.key);
    const state = !st ? "waiting" : st.status === "succeeded" ? "done" : st.status === "running" ? "working" : st.status;
    const label = { waiting: "Waiting", done: "Done", working: "Working…", failed: "Failed", pending: "Queued" }[state] || state;
    const meta = !st ? "" : a.key === "intake" ? `context v1`
      : `round ${st.round} of ${rulesMaxRounds()} · context v${st.context_version}`;
    parts.push(`
      <li class="agent" data-agent="${a.key}" data-state="${state}">
        <button type="button" data-step="${st ? st.id : ""}" ${st ? "" : "disabled"} aria-pressed="${st && selectedStep === st.id}">
          <span class="agent-name">${a.name}</span>
          <span class="agent-state">${label}</span>
          <span class="agent-job">${a.job}</span>
          <span class="agent-meta">${meta}${st && st.attempts > 1 ? ` · attempt ${st.attempts}` : ""}${st && tokens(st) ? ` · ${tokens(st).toLocaleString()} tokens` : ""}</span>
          ${st && st.status === "failed" ? `<span class="agent-err">${esc(st.error)}</span>` : ""}
        </button>
      </li>`);
    if (i < AGENTS.length - 1) parts.push(`<li class="arrow" aria-hidden="true">${arrowLabel(a.key, AGENTS[i + 1].key)}</li>`);
  });
  $("pipeline").innerHTML = parts.join("");
}
function arrowLabel(from, to) {
  const msgs = run.messages.filter((m) => m.context_version === (from === "intake" ? 1 : run.context_version));
  const fwd = [...msgs].reverse().find((m) => m.from === from && m.to === to);
  const back = [...msgs].reverse().find((m) => m.from === to && m.to === from);
  return `<span class="arr-fwd">${fwd ? "→ " + esc(shortSummary(fwd)) : "→"}</span>${back ? `<span class="arr-back">← ${esc(shortSummary(back))}</span>` : ""}`;
}
function shortSummary(m) {
  if (m.kind === "feedback") return `${m.payload.corrections.length} correction${m.payload.corrections.length === 1 ? "" : "s"}`;
  if (m.from === "intake") return `${m.payload.fact_ids.length} facts`;
  if (m.from === "planning") return `plan r${m.round}`;
  return m.kind;
}

function stepChips() {
  return run.steps.map((s) => `<button type="button" class="chip-step ${s.stale ? "stale" : ""}" data-step="${s.id}"
      aria-pressed="${selectedStep === s.id}" data-st="${esc(s.status)}">${AGENT_NAME[s.agent]}${s.agent !== "intake" ? ` r${s.round}` : ""} · v${s.context_version}${s.stale ? " · stale" : ""}</button>`).join("");
}

function renderIO() {
  const s = run.steps.find((x) => x.id === selectedStep) || null;
  $("io-title").innerHTML = s
    ? `${AGENT_NAME[s.agent]} Agent ${s.agent !== "intake" ? `· round ${s.round}` : ""} · context v${s.context_version}
       ${s.stale ? `<span class="tag">stale: older context</span>` : ""}`
    : "Steps";
  const chips = `<div class="ag-chips">${stepChips()}</div>`;
  if (!s) { $("io-body").innerHTML = chips + `<p class="ag-hint">Select an agent above to see what it received and returned.</p>`; return; }
  let body = "";
  if (s.status === "failed") body += `<p class="ag-error-inline">${esc(s.error)}</p>`;
  if (ioMode === "output") body += s.status === "succeeded" ? renderOutput(s) : `<p class="ag-hint">No output yet.</p>`;
  else body += renderInput(s);
  const raw = ioMode === "output" ? s.output : s.input;
  body += `<details class="raw"><summary>Raw JSON ${ioMode}</summary><pre>${esc(JSON.stringify(raw, null, 2))}</pre></details>`;
  $("io-body").innerHTML = chips + body;
}

function renderOutput(s) {
  const o = s.output;
  if (s.agent === "intake") {
    return `<table class="ag-table"><thead><tr><th>Fact</th><th>What was said</th><th>Source</th></tr></thead><tbody>
      ${o.facts.map((f) => `<tr><td><b>${f.id}</b><br><span class="kind">${esc(f.kind.replace("_", " "))}</span></td>
        <td>${esc(f.text)}${f.owner ? `<br><span class="sub">owner: ${esc(f.owner)}</span>` : ""}${f.deadline ? `<br><span class="sub">date: ${esc(f.deadline)}</span>` : ""}</td>
        <td>${lineChips(f.source_lines)}</td></tr>`).join("")}</tbody></table>
      ${o.conflicts.length ? `<h3>Conflicts</h3><ul>${o.conflicts.map((c) => `<li><b>${c.id}</b> ${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul>` : ""}
      ${o.missing.length ? `<h3>Missing</h3><ul>${o.missing.map((c) => `<li><b>${c.id}</b> ${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul>` : ""}`;
  }
  if (s.agent === "planning") {
    return (o._demo_note ? `<p class="demo">${esc(o._demo_note)}</p>` : "") + planTable(o, false);
  }
  return `<p><span class="verdict" data-v="${esc(o.verdict)}">${o.verdict === "approve" ? "Approve" : "Revise"}</span> ${esc(o.summary || "")}</p>
    ${o.corrections.length ? `<ul class="corr">${o.corrections.map(corrItem).join("")}</ul>` : ""}`;
}

function renderInput(s) {
  const i = s.input || {};
  if (s.agent === "intake") return `<p>The numbered transcript (${run.transcript_lines.length} lines) and the meeting date ${esc(i.meeting_date || "")}. Nothing else.</p>`;
  if (s.agent === "planning") {
    return `<ul class="io-list">
      <li><b>Source facts, context v${i.context_version}:</b> ${(i.facts || []).map((f) => `<button type="button" class="chip-fact" data-fact="${f.id}">${f.id}${f.origin === "corrected" ? "*" : ""}</button>`).join(" ")}</li>
      <li><b>Conflicts / gaps:</b> ${(i.conflicts || []).length} / ${(i.missing || []).length}</li>
      <li><b>Company rules:</b> ${(i.rules?.rules || []).map((r) => r.id).join(", ")}; team of ${(i.rules?.team || []).length}</li>
      <li><b>Private notes of this session:</b> ${i.session_notes ? esc(i.session_notes) : "none"}</li>
      <li><b>Previous plan:</b> ${i.previous_plan ? `${i.previous_plan.tasks.length} tasks` : "none (first draft)"}</li>
      <li><b>Corrections to fix:</b> ${(i.corrections || []).length ? `<ul class="corr">${i.corrections.map(corrItem).join("")}</ul>` : "none"}</li></ul>
      <p class="ag-hint">The Planning Agent never sees the transcript: only verified facts.</p>`;
  }
  return `<ul class="io-list">
    <li><b>Transcript</b> (${run.transcript_lines.length} lines), <b>source facts</b> v${i.context_version}, <b>company rules</b></li>
    <li><b>Plan to check:</b> ${(i.plan?.tasks || []).map((t) => t.id).join(", ")}</li>
    <li><b>Rule-checker findings:</b> ${(i.checker_findings || []).length ? `<ul class="corr">${i.checker_findings.map((f) => corrItem({ ...f, reference: f.rule, from: "rule_checker" })).join("")}</ul>` : "none"}</li></ul>`;
}

function corrItem(c) {
  if (c.overruled) {
    return `<li class="overruled"><span class="ref">${esc(c.reference || "")}</span>${c.task_id ? `<b>${esc(c.task_id)}</b> ` : ""}<s>${esc(c.problem)}</s>
    <span class="fix">Overruled by the rule checker: ${esc(c.overruled)}.</span><span class="who">Review Agent claim, not applied</span></li>`;
  }
  return `<li><span class="ref">${esc(c.reference || "")}</span>${c.task_id ? `<b>${esc(c.task_id)}</b> ` : ""}${esc(c.problem)}
    <span class="fix">Fix: ${esc(c.fix)}</span>${c.from ? `<span class="who">${c.from === "rule_checker" ? "rule checker" : "Review Agent"}</span>` : ""}</li>`;
}

function lineChips(lines) {
  return (lines || []).map((n) => `<button type="button" class="chip-line" data-line="${n}">L${n}</button>`).join(" ");
}

function basisChip(basis) {
  if (basis === "fact") return `<span class="basis fact" title="Stated in the meeting">stated</span>`;
  if (basis === "recommendation") return `<span class="basis rec" title="Proposed by the Planning Agent">recommended</span>`;
  return "";
}

function planTable(p, final) {
  const tasks = p.tasks || [];
  return `<table class="ag-table plan"><thead><tr><th>Task</th><th>Owner</th><th>Due</th><th>Needs</th></tr></thead><tbody>
    ${tasks.map((t) => `<tr>
      <td><b>${esc(t.id)}</b> ${esc(t.title)}
        <div class="flags">${t.is_security_review ? "<span>security review</span>" : ""}${t.touches_customer_data ? "<span>customer data</span>" : ""}${t.client_facing ? "<span>client-facing</span>" : ""}${t.is_signoff ? "<span>sign-off</span>" : ""}</div>
        <div class="srcs">${(t.sources || []).map((f) => `<button type="button" class="chip-fact" data-fact="${esc(f)}">${esc(f)}</button>`).join(" ")}</div></td>
      <td>${esc(t.owner || "—")} ${basisChip(t.owner_basis)}</td>
      <td class="nowrap">${t.deadline ? esc(fmtDay(t.deadline)) : "—"} ${basisChip(t.deadline_basis)}</td>
      <td>${(t.depends_on || []).map(esc).join(", ") || "—"}</td></tr>`).join("")}
    </tbody></table>
    ${(p.recommendations || []).length ? `<h3>Recommendations</h3><ul>${p.recommendations.map((r) => `<li>${esc(r.text)} <span class="sub">${esc(r.why)}</span></li>`).join("")}</ul>` : ""}
    ${(p.open_questions || []).length ? `<h3>Open questions</h3><ul>${p.open_questions.map((q) => `<li>${esc(q.text)} ${(q.sources || []).map((f) => `<button type="button" class="chip-fact" data-fact="${esc(f)}">${esc(f)}</button>`).join(" ")}</li>`).join("")}</ul>` : ""}`;
}

function fmtDay(iso) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
}

function messageItem(m) {
  return `<li class="msg-${esc(m.kind)}">
      <div class="m-head"><span class="who-from" data-agent="${esc(m.from)}">${AGENT_NAME[m.from]}</span> → <span class="who-to" data-agent="${esc(m.to)}">${AGENT_NAME[m.to]}</span>
        <span class="kind-badge">${esc(m.kind)}</span><span class="sub">round ${m.round} · v${m.context_version}</span></div>
      <div>${esc(m.summary)}</div>
      ${m.kind === "feedback" ? `<ul class="corr">${m.payload.corrections.map(corrItem).join("")}</ul>` : ""}
      ${m.kind === "final" ? `<ul class="corr">${m.payload.unresolved.map(corrItem).join("")}</ul>` : ""}
      ${(m.payload.overruled || []).length ? `<ul class="corr">${m.payload.overruled.map(corrItem).join("")}</ul>` : ""}
    </li>`;
}

function renderMessages() {
  const v = run.context_version;
  const current = run.messages.filter((m) => m.context_version === v || (m.from === "intake"));
  const older = run.messages.filter((m) => !current.includes(m));
  const versions = [...new Set(older.map((m) => m.context_version))].sort((a, b) => b - a);
  $("messages").innerHTML = (current.length ? current.map(messageItem).join("")
    : `<li class="empty">Handoffs between agents appear here as they happen.</li>`) +
    versions.map((ver) => `<li class="older"><details><summary>Earlier: context v${ver} (${older.filter((m) => m.context_version === ver).length} messages, superseded)</summary>
      <ol class="ag-messages">${older.filter((m) => m.context_version === ver).map(messageItem).join("")}</ol></details></li>`).join("");
  $("events").innerHTML = run.events.map((e) => `<li data-level="${esc(e.level)}"><time>${esc(fmtTime(e.at, { second: "2-digit" }))}</time> ${esc(e.text)}</li>`).join("");
}

function renderHead() {
  $("run-title").textContent = run.title;
  $("run-meta").innerHTML = `<span class="ag-status" data-st="${esc(run.status)}">${esc(RUN_STATUS[run.status] || run.status)}</span>
    Run <code>${esc(run.id)}</code> · context v${run.context_version || "–"} · ${run.steps.length} step(s) · model cost $${(run.cost_usd || 0).toFixed(4)}`;
  const busy = run.status === "running" || run.status === "pending";
  const canResume = ["failed", "interrupted"].includes(run.status);
  $("run-actions").innerHTML = `
    ${canResume ? `<button type="button" class="primary" id="resume">Resume</button>` : ""}
    <label class="ag-demo">Simulate a failure (demo)
      <select id="sim" ${busy ? "disabled" : ""}><option value="">choose agent</option><option value="planning">Planning</option><option value="review">Review</option></select>
    </label>`;
  const err = $("run-error");
  err.hidden = !run.error;
  const sentence = (t) => /[.!?)]$/.test(t) ? t : t + ".";
  err.textContent = run.error ? `${sentence(run.error)}${canResume ? " Completed steps are kept; Resume continues from the failed step." : ""}` : "";
  if (canResume) $("resume").onclick = async () => {
    try {
      await api(`/api/agents/sessions/${sid}/runs/${rid}/resume`, { method: "POST" });
      toast("Resuming from the failed step. Completed steps are reused.", "ok");
      lastJson = ""; refreshRun();
    } catch (e) { toast(e.message, "error"); }
  };
  $("sim").onchange = async (e) => {
    if (!e.target.value) return;
    await api(`/api/agents/sessions/${sid}/runs/${rid}/simulate-failure`, { method: "POST", body: JSON.stringify({ agent: e.target.value }) });
    toast(`Demo fault armed: the next ${e.target.value} model call will fail. Correct a fact to trigger it.`, "warn", 7000);
    lastJson = ""; refreshRun();
  };
}

// ---------------------------------------------------------------- right panel
function renderPlanTab() {
  const res = run.result || {};
  if (!res.plan) {
    $("tab-plan").innerHTML = `<p class="empty">The final plan appears here when the review loop finishes.</p>`;
    return;
  }
  const over = res.overruled || [];
  const banner = res.approved
    ? `<div class="ag-banner ok"><b>Approved</b> in round ${res.rounds}, with no rule-checker findings${over.length ? ` (the rule checker overruled ${over.length} false alarm(s) from the Review Agent)` : " and the Review Agent's sign-off"}. Built on context v${res.context_version}.</div>`
    : `<div class="ag-banner warn"><b>Needs a person:</b> ${res.unresolved.length} issue(s) still open after ${res.rounds} round(s).</div>`;
  $("tab-plan").innerHTML = banner +
    (!res.approved ? `<ul class="corr">${res.unresolved.map(corrItem).join("")}</ul>` : "") +
    (over.length ? `<ul class="corr">${over.map(corrItem).join("")}</ul>` : "") +
    planTable(res.plan, true) +
    `<p class="ag-hint legend"><span class="basis fact">stated</span> said in the meeting (see its source fact) · <span class="basis rec">recommended</span> proposed by the Planning Agent</p>`;
}

function renderFactsTab() {
  const versions = run.contexts;
  if (!versions.length) { $("tab-facts").innerHTML = `<p class="empty">Facts appear when the Intake Agent finishes.</p>`; return; }
  const v = factsVersion || run.context_version;
  const ctx = versions.find((c) => c.version === v) || versions[versions.length - 1];
  const isCurrent = ctx.version === run.context_version;
  const busy = run.status === "running" || run.status === "pending";
  $("tab-facts").innerHTML = `
    <div class="ag-row"><label class="ag-label" for="vsel">Context version</label>
      <select id="vsel">${versions.map((c) => `<option value="${c.version}" ${c.version === ctx.version ? "selected" : ""}>v${c.version}${c.version === run.context_version ? " (current)" : ""}</option>`).join("")}</select></div>
    <p class="ag-hint">${esc(ctx.reason)}</p>
    <ul class="facts">${ctx.facts.map((f) => `
      <li id="fact-${f.id}" class="${highlightFact === f.id ? "hl" : ""} ${f.origin === "corrected" ? "corrected" : ""}">
        <div class="f-head"><b>${f.id}</b> <span class="kind">${esc(f.kind.replace("_", " "))}</span> ${lineChips(f.source_lines)}
          ${isCurrent && !busy ? `<button type="button" class="link" data-edit="${f.id}">Correct</button>` : ""}</div>
        <div>${esc(f.text)}</div>
        ${f.owner || f.deadline ? `<div class="sub">${f.owner ? `owner: ${esc(f.owner)}` : ""} ${f.deadline ? `date: ${esc(f.deadline)}` : ""}</div>` : ""}
        ${f.origin === "corrected" ? `<div class="sub corrected-note">Corrected by a person. Before: ${esc(previousText(f))}</div>` : ""}
        ${editingFact === f.id ? factForm(f) : ""}
      </li>`).join("")}</ul>
    ${ctx.conflicts.length ? `<h3>Conflicts</h3><ul>${ctx.conflicts.map((c) => `<li><b>${c.id}</b> ${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul>` : ""}
    ${ctx.missing.length ? `<h3>Missing</h3><ul>${ctx.missing.map((c) => `<li><b>${c.id}</b> ${esc(c.text)} ${lineChips(c.source_lines)}</li>`).join("")}</ul>` : ""}`;
  $("vsel").onchange = (e) => { factsVersion = Number(e.target.value); renderFactsTab(); };
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
    <div class="ag-row"><label>Owner<input name="owner" value="${esc(f.owner || "")}"></label>
      <label>Date<input name="deadline" type="date" value="${esc(f.deadline || "")}"></label></div>
    <label>Why<input name="reason" placeholder="e.g. client confirmed the earlier go-live"></label>
    <div class="ag-row ag-row-end"><button type="button" data-cancel>Cancel</button>
      <button type="submit" class="primary">Save as v${run.context_version + 1} and rerun</button></div>
  </form>`;
}

function renderTranscriptTab() {
  $("tab-transcript").innerHTML = `<ol class="transcript">${run.transcript_lines.map((l) =>
    `<li id="line-${l.n}" class="${highlightLines.has(l.n) ? "hl" : ""}"><span class="ln">L${l.n}</span>${esc(l.text)}</li>`).join("")}</ol>`;
}

function setTab(name) {
  rightTab = name;
  document.querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  ["plan", "facts", "transcript"].forEach((t) => { $(`tab-${t}`).hidden = t !== name; });
}

// ---------------------------------------------------------------- events (delegated)
document.addEventListener("click", (e) => {
  const step = e.target.closest("[data-step]");
  if (step && step.dataset.step) {
    selectedStep = Number(step.dataset.step); userPickedStep = true; renderPipeline(); renderIO(); return;
  }
  const io = e.target.closest("[data-io]");
  if (io) {
    ioMode = io.dataset.io;
    document.querySelectorAll("[data-io]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.io === ioMode)));
    renderIO(); return;
  }
  const tab = e.target.closest("[data-tab]");
  if (tab) { setTab(tab.dataset.tab); return; }
  const line = e.target.closest("[data-line]");
  if (line) {
    highlightLines = new Set([Number(line.dataset.line)]);
    renderTranscriptTab(); setTab("transcript");
    $(`line-${line.dataset.line}`)?.scrollIntoView({ block: "center" }); return;
  }
  const fact = e.target.closest("[data-fact]");
  if (fact) {
    highlightFact = fact.dataset.fact; factsVersion = null;
    renderFactsTab(); setTab("facts");
    $(`fact-${fact.dataset.fact}`)?.scrollIntoView({ block: "center" }); return;
  }
  const edit = e.target.closest("[data-edit]");
  if (edit) { editingFact = edit.dataset.edit; renderFactsTab(); return; }
  if (e.target.closest("[data-cancel]")) { editingFact = null; renderFactsTab(); }
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
    editingFact = null; factsVersion = null; selectedStep = null; userPickedStep = false; lastJson = "";
    toast(`Saved as context v${r.context_version}. Planning and Review rerun on it; Intake is reused.`, "ok", 6000);
    refreshRun();
  } catch (err) { toast(err.message, "error"); btn.disabled = false; }
});

// ---------------------------------------------------------------- render + polling
function render() {
  const hasRun = Boolean(rid && run);
  $("run-form").hidden = Boolean(rid);
  $("run-view").hidden = !hasRun;
  $("right").hidden = !hasRun;
  $("ag").classList.toggle("has-run", hasRun);
  $("empty").hidden = true;
  if (!rid && !$("f-transcript").value) fillSample();
  if (!hasRun) return;
  renderHead(); renderPipeline(); renderIO(); renderMessages();
  renderPlanTab(); if (editingFact === null) renderFactsTab(); renderTranscriptTab(); setTab(rightTab);
}

async function refreshRun() {
  if (!rid) return;
  try {
    const d = await api(`/api/agents/sessions/${sid}/runs/${rid}`);
    const json = JSON.stringify(d);
    if (json === lastJson) return;
    const finishedNow = run && ["running", "pending"].includes(run.status) && !["running", "pending"].includes(d.status);
    lastJson = json; run = d;
    if (!userPickedStep || !run.steps.some((s) => s.id === selectedStep)) {
      // follow the most recent step while the run moves on, until the person picks one
      const latest = [...run.steps].sort((a, b) => a.id - b.id).pop();
      selectedStep = latest ? latest.id : null;
      userPickedStep = false;
    }
    render();
    if (finishedNow) {
      toast(run.status === "completed" ? "Plan approved." : run.status === "needs_review" ? "Plan ready, with issues for a person." : "Run stopped. See the reason above.",
        run.status === "completed" ? "ok" : "warn", 6000);
      loadSession();
    }
  } catch (e) {
    if (e.status === 404) { rid = null; run = null; writeHash(); render(); }
  }
}

async function refreshAll() {
  await loadSessions();
  await loadSession();
  if (rid) await refreshRun(); else render();
}

function schedule() {
  clearTimeout(pollTimer);
  const busy = run && ["running", "pending"].includes(run.status);
  pollTimer = setTimeout(async () => { await refreshRun(); if (busy) loadSession(); schedule(); }, busy ? 1200 : 5000);
}

(async function init() {
  const h = readHash(); sid = h.s; rid = h.r;
  await loadBusinessTz();
  await refreshAll();
  writeHash();
  schedule();
})();
