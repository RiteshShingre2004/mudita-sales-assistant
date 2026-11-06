const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = (d && (d.errors?.join("; ") || (typeof d === "string" ? d : null))) || `Request failed (${res.status})`;
    const err = new Error(msg);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

// ---------- toasts ----------
function toast(message, kind = "info", ms = 4500) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 250); }, ms);
}

// ---------- time ----------
let BUSINESS_TZ = "Asia/Kolkata";
let POLICY = null;

function fmtTime(iso, opts = {}) {
  if (!iso) return "";
  return new Intl.DateTimeFormat("en-IN", { timeZone: BUSINESS_TZ, hour: "2-digit", minute: "2-digit", ...opts }).format(new Date(iso));
}
function fmtDateTime(iso) {
  return fmtTime(iso, { weekday: "short", day: "numeric", month: "short" });
}
function fmtRelative(iso) {
  if (!iso) return "";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)} min ago`;
  const d = new Date(iso), now = new Date();
  return d.toDateString() === now.toDateString()
    ? fmtTime(iso)
    : fmtTime(iso, { day: "numeric", month: "short", hour: undefined, minute: undefined });
}

const STATUS_LABEL = { new: "New", needs_info: "Needs info", qualified: "Qualified", not_qualified: "Not qualified" };
const CHANNEL_LABEL = { email: "Email", whatsapp: "WhatsApp", instagram: "Instagram" };
const CONN_LABEL = { ok: "Connected", not_connected: "Not connected", not_configured: "Not set up on server", expired: "Sign-in expired", error: "Error" };

// ---------- connections menu ----------
const connEls = {
  toggle: document.getElementById("conn-toggle"),
  panel: document.getElementById("conn-panel"),
  list: document.getElementById("conn-list"),
  dots: document.getElementById("conn-dots"),
  summary: document.getElementById("conn-summary"),
};

function connActions(c) {
  if (c.connect !== "oauth" || c.status === "not_configured") return "";
  const acts = [];
  if (c.status === "ok") {
    if (c.provider === "email") acts.push(`<button type="button" data-act="poll">Check inbox now</button>`);
    if (c.provider === "email") acts.push(`<button type="button" data-act="disconnect">Disconnect Google</button>`);
  } else {
    acts.push(`<a class="button primary" href="/oauth/google/start">${c.status === "expired" ? "Reconnect Google" : "Connect Google"}</a>`);
  }
  return acts.length ? `<div class="conn-actions">${acts.join("")}</div>` : "";
}

let lastConnProblem = "";
async function loadConnections() {
  if (!connEls.list) return;
  try {
    const rows = await api("/api/connections");
    connEls.dots.innerHTML = rows.map((c) => `<i data-status="${esc(c.status)}"></i>`).join("");
    const broken = rows.filter((c) => c.status === "expired" || c.status === "error");
    const ready = rows.filter((c) => c.status === "ok").length;
    connEls.summary.textContent = broken.length
      ? `${broken.length} connection${broken.length > 1 ? "s" : ""} need attention`
      : `${ready} of ${rows.length} connected`;
    connEls.toggle.classList.toggle("alert", broken.length > 0);
    connEls.list.innerHTML = rows.map((c) => `
      <li data-status="${esc(c.status)}">
        <div class="conn-row"><strong>${esc(c.label)}</strong><span class="conn-state">${esc(CONN_LABEL[c.status] || c.status)}</span></div>
        ${c.account ? `<div class="conn-sub">${esc(c.account)}</div>` : ""}
        ${c.error && c.status !== "ok" ? `<div class="conn-sub conn-err">${esc(c.error)}</div>` : ""}
        ${c.last_checked_at && c.status === "ok" && c.provider === "email" ? `<div class="conn-sub">Inbox checked ${esc(fmtRelative(c.last_checked_at))}</div>` : ""}
        ${c.webhook && c.status !== "not_configured" ? `<div class="conn-sub">${c.last_webhook_at
          ? `Last message from Meta ${esc(fmtRelative(c.last_webhook_at))}`
          : "No messages received from Meta yet. Check the webhook is verified and subscribed to messages."}</div>` : ""}
        ${connActions(c)}
      </li>`).join("");
    const problem = broken.map((b) => b.label + b.status).join();
    if (problem && problem !== lastConnProblem) toast(`${broken.map((b) => b.label).join(", ")}: ${broken[0].error || "needs attention"}`, "error", 8000);
    lastConnProblem = problem;
  } catch {
    connEls.summary.textContent = "Connection status unavailable";
  }
}

if (connEls.toggle) {
  connEls.toggle.addEventListener("click", () => {
    const open = connEls.panel.hidden;
    connEls.panel.hidden = !open;
    connEls.toggle.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#conn-menu")) { connEls.panel.hidden = true; connEls.toggle.setAttribute("aria-expanded", "false"); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !connEls.panel.hidden) { connEls.panel.hidden = true; connEls.toggle.focus(); }
  });
  connEls.list.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    btn.disabled = true;
    try {
      if (btn.dataset.act === "poll") {
        const r = await api("/api/channels/email/poll", { method: "POST" });
        if (r.error) toast(`Inbox check failed: ${r.error}`, "error");
        else if (r.busy) toast("An inbox check is already running.");
        else toast(r.new ? `${r.new} new email${r.new > 1 ? "s" : ""} received.` : "No new emails.", "ok");
        document.dispatchEvent(new CustomEvent("data-changed"));
      } else if (btn.dataset.act === "disconnect") {
        if (!confirm("Disconnect Google? The inbox and calendar stop working until you connect again.")) return;
        await api("/api/connections/google/disconnect", { method: "POST" });
        toast("Google disconnected.");
      }
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
      loadConnections();
    }
  });
}

async function loadBusinessTz() {
  try {
    const p = await api("/api/policy");
    POLICY = p.policy;
    BUSINESS_TZ = p.policy.working_hours.timezone || BUSINESS_TZ;
  } catch { /* keep default */ }
}

(function announceConnected() {
  const params = new URLSearchParams(location.search);
  if (params.get("connected")) {
    toast(`Connected Google account ${params.get("connected")}. New inbox mail now appears here.`, "ok", 7000);
    history.replaceState(null, "", location.pathname + location.hash);
  }
})();

loadConnections();
setInterval(loadConnections, 20000);
