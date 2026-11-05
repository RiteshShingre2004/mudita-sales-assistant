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

async function load() {
  const p = await api("/api/policy");
  saved = p.yaml;
  ta.value = p.yaml;
  versionEl.textContent = `Version ${p.version}, saved by ${p.created_by}`;
  renderSummary(p.policy);
}

document.getElementById("save").addEventListener("click", async () => {
  errorsEl.innerHTML = "";
  statusEl.textContent = "Saving…";
  try {
    const r = await api("/api/policy", { method: "PUT", body: JSON.stringify({ yaml: ta.value }) });
    statusEl.textContent = `Saved as version ${r.version}.`;
    await load();
  } catch (e) {
    statusEl.textContent = "Not saved. Fix the problems below; the previous version is still active.";
    const errs = e.data?.detail?.errors || [e.message];
    errorsEl.innerHTML = errs.map((x) => `<li>${esc(x)}</li>`).join("");
  }
});
document.getElementById("revert").addEventListener("click", () => {
  ta.value = saved;
  errorsEl.innerHTML = "";
  statusEl.textContent = "Edits discarded.";
});

load().catch((e) => { statusEl.textContent = `Could not load the policy: ${e.message}`; });
