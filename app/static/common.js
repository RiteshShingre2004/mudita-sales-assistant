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
    const err = new Error((data && (data.detail?.errors?.join("; ") || data.detail)) || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

let BUSINESS_TZ = "Asia/Kolkata";
let POLICY = null;

function fmtTime(iso, opts = {}) {
  if (!iso) return "";
  const d = new Date(iso);
  return new Intl.DateTimeFormat("en-IN", { timeZone: BUSINESS_TZ, hour: "2-digit", minute: "2-digit", ...opts }).format(d);
}
function fmtDateTime(iso) {
  return fmtTime(iso, { weekday: "short", day: "numeric", month: "short" });
}
function fmtRelative(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? fmtTime(iso) : fmtTime(iso, { day: "numeric", month: "short", hour: undefined, minute: undefined });
}

const STATUS_LABEL = { new: "New", needs_info: "Needs info", qualified: "Qualified", not_qualified: "Not qualified" };
const CHANNEL_LABEL = { email: "Email", whatsapp: "WhatsApp", instagram: "Instagram" };
const CONN_LABEL = { ok: "connected", not_connected: "not connected", not_configured: "not configured", expired: "sign-in expired", error: "error" };

async function loadConnections() {
  const ul = document.getElementById("connections");
  if (!ul) return;
  try {
    const rows = await api("/api/connections");
    ul.innerHTML = rows.map((c) =>
      `<li data-status="${esc(c.status)}" title="${esc(c.error || CONN_LABEL[c.status] || c.status)}">${esc(c.label)}: ${esc(CONN_LABEL[c.status] || c.status)}</li>`
    ).join("");
  } catch (e) {
    ul.innerHTML = `<li data-status="error">Status unavailable</li>`;
  }
}

async function loadBusinessTz() {
  try {
    const p = await api("/api/policy");
    POLICY = p.policy;
    BUSINESS_TZ = p.policy.working_hours.timezone || BUSINESS_TZ;
  } catch { /* keep default */ }
}

loadConnections();
setInterval(loadConnections, 30000);
