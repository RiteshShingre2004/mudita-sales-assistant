const ta = document.getElementById("policy-yaml");
const errorsEl = document.getElementById("errors");
const statusEl = document.getElementById("save-status");
const versionEl = document.getElementById("version");
const summaryEl = document.getElementById("summary");
let saved = "";

function renderSummary(p) {
  const svc = Object.fromEntries(p.services.map((s) => [s.id, s.name]));
  const reps = Object.fromEntries(p.reps.map((r) => [r.id, r.name]));
  const q = p.qualification, wh = p.working_hours, m = p.meeting;
  summaryEl.innerHTML = `
    <p><strong>${esc(p.company_name)}</strong></p>
    <p class="hint">${esc(p.description)}</p>
    <h3>Services</h3>
    <ul>${p.services.map((s) => `<li>${esc(s.name)}</li>`).join("")}</ul>
    <h3>A lead is qualified when</h3>
    <ul>
      <li>they want one of the services above</li>
      <li>budget is at least $${Number(q.min_budget_usd).toLocaleString("en-US")}</li>
      <li>they want to start within ${q.max_start_within_months} months</li>
      <li>their role is ${q.eligible_roles.map((r) => r.replace("_", " ")).join(" or ")}</li>
    </ul>
    <p class="hint">Missing any of: ${q.required_fields.map((f) => f.replaceAll("_", " ")).join(", ")} means "needs info". These are never qualified: ${q.disqualifying_intents.map((i) => i.replaceAll("_", " ")).join(", ")}.</p>
    <h3>Reps and routing</h3>
    <ul>${p.routing.rules.map((r) => `<li>${esc(svc[r.service])} goes to ${esc(reps[r.rep])}</li>`).join("")}
      <li>Anything else goes to ${p.routing.fallback === "least_open_leads" ? "the rep with the fewest open leads" : esc(reps[p.routing.fallback])}</li></ul>
    <p class="hint">${p.reps.map((r) => `${esc(r.name)} (${r.active ? "active" : "inactive"})`).join(", ")}</p>
    <h3>Meetings</h3>
    <p>${m.duration_minutes}-minute calls, ${wh.days.join(", ")}, ${wh.start} to ${wh.end} ${esc(wh.timezone)}.</p>
    <p class="hint">At least ${m.min_notice_hours} hours' notice, up to ${m.horizon_days} days ahead, ${m.slots_to_offer} slots offered at a time.</p>`;
}

const validityEl = document.getElementById("validity");
const saveBtn = document.getElementById("save");
let versionText = "";

function markDirty() {
  const dirty = ta.value !== saved;
  versionEl.innerHTML = dirty ? `<span class="dirty-dot">Unsaved edits</span>, based on ${esc(versionText)}` : esc(versionText);
  saveBtn.disabled = !dirty;
}

let validateTimer = null;
async function validateNow() {
  if (ta.value === saved) { validityEl.textContent = ""; errorsEl.innerHTML = ""; return; }
  try {
    const r = await api("/api/policy/validate", { method: "POST", body: JSON.stringify({ yaml: ta.value }) });
    validityEl.dataset.valid = String(r.valid);
    validityEl.textContent = r.valid ? "Valid. Ready to save." : `${r.errors.length} problem${r.errors.length > 1 ? "s" : ""} to fix`;
    errorsEl.innerHTML = r.errors.map((x) => `<li>${esc(x)}</li>`).join("");
  } catch { validityEl.textContent = ""; }
}

async function load() {
  const p = await api("/api/policy");
  saved = p.yaml;
  ta.value = p.yaml;
  versionText = `Version ${p.version}, saved by ${p.created_by}`;
  renderSummary(p.policy);
  markDirty();
  validityEl.textContent = "";
}

async function save() {
  if (ta.value === saved) return;
  errorsEl.innerHTML = "";
  statusEl.textContent = "Saving…";
  try {
    const r = await api("/api/policy", { method: "PUT", body: JSON.stringify({ yaml: ta.value }) });
    statusEl.textContent = "";
    toast(`Policy saved as version ${r.version}. New decisions use it from now on.`, "ok");
    await load();
  } catch (e) {
    statusEl.textContent = "Not saved. The previous version is still active.";
    const errs = e.data?.detail?.errors || [e.message];
    errorsEl.innerHTML = errs.map((x) => `<li>${esc(x)}</li>`).join("");
  }
}

ta.addEventListener("input", () => {
  markDirty();
  statusEl.textContent = "";
  clearTimeout(validateTimer);
  validateTimer = setTimeout(validateNow, 400);
});
let tabReleased = false; // Escape, then Tab, moves focus out (no keyboard trap)
ta.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { tabReleased = true; return; }
  if (e.key !== "Tab") { tabReleased = false; return; }
  if (!tabReleased && !e.shiftKey && !e.ctrlKey && !e.metaKey) { // indent instead of leaving the editor
    e.preventDefault();
    ta.setRangeText("  ", ta.selectionStart, ta.selectionEnd, "end");
    ta.dispatchEvent(new Event("input"));
  }
});
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
});
window.addEventListener("beforeunload", (e) => { if (ta.value !== saved) e.preventDefault(); });
saveBtn.addEventListener("click", save);
document.getElementById("revert").addEventListener("click", () => {
  ta.value = saved;
  errorsEl.innerHTML = "";
  validityEl.textContent = "";
  markDirty();
  toast("Edits discarded.");
});

load().catch((e) => { statusEl.textContent = `Could not load the policy: ${e.message}`; });
