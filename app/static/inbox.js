// Inbox. All state lives on the server; the open conversation is kept in the
// URL hash, so a refresh returns to the same place.

const els = {
  desk: document.getElementById("desk"),
  list: document.getElementById("conv-list"),
  channel: document.getElementById("channel-filter"),
  search: document.getElementById("search"),
  statusFilters: document.getElementById("status-filters"),
  title: document.getElementById("thread-title"),
  threadScroll: document.getElementById("thread-scroll"),
  thread: document.getElementById("thread"),
  lead: document.getElementById("lead-pane"),
  back: document.getElementById("back"),
  control: document.getElementById("control"),
  banner: document.getElementById("paused-banner"),
  composer: document.getElementById("composer"),
  replyText: document.getElementById("reply-text"),
  sendBtn: document.getElementById("send-btn"),
  composerNote: document.getElementById("composer-note"),
};

let conversations = [];
let current = null;                 // detail of the open conversation
let selectedId = Number(location.hash.replace("#c", "")) || null;
let lastDetailJson = "";
let statusFilter = "";
let seenStamps = null;              // conversation id -> last_message_at, for "new message" flashes
const drafts = {};                  // unsent operator replies per conversation

const ROLE_LABEL = { decision_maker: "Decision maker", influencer: "Influencer", end_user: "End user", unknown: "Unknown" };
const AUTHOR_LABEL = { lead: "Lead", assistant: "Assistant", human: "Operator" };
const FIELD_LABEL = {
  service: "Service", budget_usd: "Budget", start_within_months: "Start",
  role: "Role", company: "Company", intent: "Intent",
};
const STATUS_ORDER = [["", "All"], ["needs_info", "Needs info"], ["qualified", "Qualified"], ["not_qualified", "Not qualified"], ["paused", "Human in control"]];

function displayValue(key, v) {
  if (key === "service") return POLICY?.services.find((s) => s.id === v)?.name || `${v} (not a listed service)`;
  if (key === "budget_usd") return `$${Number(v).toLocaleString("en-US")}`;
  if (key === "start_within_months") return Number(v) === 0 ? "Immediately" : `Within ${v} month${Number(v) === 1 ? "" : "s"}`;
  if (key === "role") return ROLE_LABEL[v] || v;
  if (key === "intent") return String(v).replaceAll("_", " ");
  return v;
}
const evidenceText = (e) => (e && typeof e === "object" ? e.quote : e) || "";

// ---------- list ----------

function matches(c) {
  if (els.channel.value && c.channel !== els.channel.value) return false;
  if (statusFilter === "paused" && !c.automation_paused) return false;
  if (statusFilter && statusFilter !== "paused" && c.lead_status !== statusFilter) return false;
  const q = els.search.value.trim().toLowerCase();
  if (!q) return true;
  return [c.contact.name, c.contact.external_id, c.last_message?.body, c.assigned_rep].some((s) => (s || "").toLowerCase().includes(q));
}
const visible = () => conversations.filter(matches);

function renderStatusFilters() {
  const count = (key) => conversations.filter((c) =>
    (!els.channel.value || c.channel === els.channel.value) &&
    (key === "" || (key === "paused" ? c.automation_paused : c.lead_status === key))).length;
  els.statusFilters.innerHTML = STATUS_ORDER.map(([key, label]) =>
    `<button type="button" data-filter="${key}" aria-pressed="${statusFilter === key}">${label} <span>${count(key)}</span></button>`).join("");
}

function renderList(flashIds = new Set()) {
  renderStatusFilters();
  const rows = visible();
  if (!conversations.length) {
    els.list.innerHTML = `<li class="empty"><strong>No inquiries yet</strong>New messages to the connected inbox, WhatsApp number or Instagram account appear here within a few seconds. Open Connections at the top to connect them.</li>`;
    return;
  }
  if (!rows.length) {
    els.list.innerHTML = `<li class="empty"><strong>Nothing matches</strong><button type="button" class="link" id="clear-filters">Clear search and filters</button></li>`;
    document.getElementById("clear-filters").onclick = () => { els.search.value = ""; statusFilter = ""; els.channel.value = ""; renderList(); };
    return;
  }
  els.list.innerHTML = rows.map((c) => `
    <li><button type="button" data-id="${c.id}" aria-current="${c.id === selectedId}" class="${flashIds.has(c.id) ? "flash" : ""}">
      <span class="conv-name">${esc(c.contact.name || c.contact.external_id)}</span>
      <span class="conv-time">${esc(fmtRelative(c.last_message_at))}</span>
      <span class="conv-preview">${c.last_message && c.last_message.author !== "lead" ? `<span class="you">${c.last_message.author === "human" ? "You" : "Assistant"}:</span> ` : ""}${esc(c.last_message ? c.last_message.body : "")}</span>
      <span class="conv-meta">
        <span class="channel" data-ch="${esc(c.channel)}">${esc(CHANNEL_LABEL[c.channel] || c.channel)}</span>
        <span class="status" data-st="${esc(c.lead_status)}">${esc(STATUS_LABEL[c.lead_status] || c.lead_status)}</span>
        ${c.assigned_rep ? `<span class="tag">${esc(c.assigned_rep)}</span>` : ""}
        ${c.automation_paused ? `<span class="tag paused">Human in control</span>` : ""}
        ${c.contact.is_demo ? `<span class="tag demo">Sample data</span>` : ""}
      </span>
    </button></li>`).join("");
}

// ---------- thread ----------

function renderThread(c) {
  els.title.innerHTML = `
    <div class="thread-title-name">${esc(c.contact.name || c.contact.external_id)}</div>
    <div class="thread-title-sub"><span class="channel" data-ch="${esc(c.channel)}">${esc(CHANNEL_LABEL[c.channel])}</span>
      ${esc(c.contact.external_id)}${c.subject ? `, “${esc(c.subject)}”` : ""}${c.contact.is_demo ? " (sample data, not from a live channel)" : ""}</div>`;
  const nearBottom = els.threadScroll.scrollHeight - els.threadScroll.scrollTop - els.threadScroll.clientHeight < 80;
  if (!c.messages.length) {
    els.thread.innerHTML = `<li class="empty">No messages in this conversation.</li>`;
    return;
  }
  els.thread.innerHTML = c.messages.map((m) => `
    <li class="msg ${m.direction === "out" ? "out" : "in"} ${esc(m.author)}" id="m${m.id}">
      <div class="msg-meta">${esc(AUTHOR_LABEL[m.author] || m.author)}, ${esc(fmtDateTime(m.at))}${m.send_status === "sending" ? ", sending" : ""}</div>
      <div class="msg-body">${esc(m.body)}</div>
      ${m.send_status === "failed" ? `<div class="msg-error">Not delivered: ${esc(m.error || "unknown error")}</div>` : ""}
      ${m.send_status === "uncertain" ? `<div class="msg-error">Delivery unknown: ${esc(m.error)}</div>` : ""}
    </li>`).join("");
  if (nearBottom || els.threadScroll.dataset.for !== String(c.id)) els.threadScroll.scrollTop = els.threadScroll.scrollHeight;
  els.threadScroll.dataset.for = String(c.id);
}

function highlightEvidence(field) {
  if (!current?.lead) return;
  const ev = current.lead.evidence[field];
  const quote = evidenceText(ev).trim();
  if (!quote) return;
  let target = ev && ev.message_id ? document.getElementById(`m${ev.message_id}`) : null;
  if (!target) {
    target = [...els.thread.querySelectorAll(".msg.in")].find((li) =>
      li.querySelector(".msg-body").textContent.toLowerCase().includes(quote.toLowerCase()));
  }
  if (!target) { toast("That quote is not in the visible messages."); return; }
  const bodyEl = target.querySelector(".msg-body");
  const text = bodyEl.textContent;
  const i = text.toLowerCase().indexOf(quote.toLowerCase());
  if (i >= 0) bodyEl.innerHTML = `${esc(text.slice(0, i))}<mark>${esc(text.slice(i, i + quote.length))}</mark>${esc(text.slice(i + quote.length))}`;
  els.thread.querySelectorAll(".msg.spot").forEach((n) => n.classList.remove("spot"));
  target.classList.add("spot");
  target.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  lastDetailJson = ""; // next refresh may redraw; that's fine once the person has seen it
}

// ---------- control + composer ----------

function renderControl(c) {
  els.banner.hidden = !c.automation_paused;
  els.control.innerHTML = c.automation_paused
    ? `<button type="button" id="takeover-btn" data-paused="false">Hand back to assistant</button>`
    : `<button type="button" class="primary" id="takeover-btn" data-paused="true">Take over</button>`;
  document.getElementById("takeover-btn").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const pausing = btn.dataset.paused === "true";
    btn.disabled = true;
    try {
      await api(`/api/conversations/${c.id}/takeover`, { method: "POST", body: JSON.stringify({ paused: pausing }) });
      toast(pausing ? "You have taken over. The assistant is stopped here." : "Handed back. The assistant will handle new messages.", pausing ? "warn" : "ok");
      lastDetailJson = "";
      await refresh();
      if (pausing) els.replyText.focus();
    } catch (e) {
      btn.disabled = false;
      toast(`Could not change control: ${e.message}`, "error");
    }
  });

  els.composer.hidden = false;
  const demo = c.contact.is_demo;
  els.replyText.disabled = demo;
  els.sendBtn.disabled = demo;
  els.replyText.placeholder = demo ? "Replies are off for sample data" : `Write a reply as yourself, sent by ${CHANNEL_LABEL[c.channel]}`;
  els.composerNote.textContent = demo ? "" : c.automation_paused
    ? "Ctrl + Enter to send."
    : "The assistant is still active. Take over first if you want to handle this yourself.";
  if (els.composer.dataset.for !== String(c.id)) {
    els.replyText.value = drafts[c.id] || "";
    els.composer.dataset.for = String(c.id);
  }
}

els.replyText.addEventListener("input", () => { if (selectedId) drafts[selectedId] = els.replyText.value; });
els.replyText.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); els.composer.requestSubmit(); }
});
els.composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = els.replyText.value.trim();
  if (!text || !selectedId) return;
  els.sendBtn.disabled = true;
  els.sendBtn.textContent = "Sending…";
  try {
    const r = await api(`/api/conversations/${selectedId}/reply`, { method: "POST", body: JSON.stringify({ text }) });
    if (r.send_status === "sent") {
      toast("Reply sent.", "ok");
      els.replyText.value = "";
      delete drafts[selectedId];
    } else {
      toast(`Reply ${r.send_status}: ${r.error}`, "error", 8000);
    }
  } catch (err) {
    toast(err.message, "error");
  } finally {
    els.sendBtn.disabled = false;
    els.sendBtn.textContent = "Send reply";
    lastDetailJson = "";
    refresh();
  }
});

// ---------- lead file ----------

function renderLead(c) {
  const lead = c.lead;
  const st = lead ? lead.status : "new";
  const fields = lead ? lead.fields : {};
  const evidence = lead ? lead.evidence : {};

  const factsRows = Object.keys(FIELD_LABEL).map((k) => {
    const v = fields[k];
    const q = evidenceText(evidence[k]);
    const known = !(v === undefined || v === null || v === "");
    return `<tr><th scope="row">${FIELD_LABEL[k]}</th><td>${
      known ? esc(displayValue(k, v)) : `<span class="unknown">Not stated</span>`
    }${q ? `<button type="button" class="quote" data-field="${k}" title="Show this in the conversation">“${esc(q)}”</button>` : ""}</td></tr>`;
  }).join("");

  const booking = c.bookings.length ? c.bookings[c.bookings.length - 1] : null;
  const meeting = booking
    ? `<p><strong>${esc(booking.status)}</strong>, ${esc(fmtDateTime(booking.start))}</p>
       ${booking.error ? `<p class="msg-error">${esc(booking.error)}</p>` : ""}
       ${booking.calendar_link ? `<p><a href="${esc(booking.calendar_link)}" target="_blank" rel="noopener">Open in Google Calendar</a></p>` : ""}`
    : c.slot_offers.length
      ? `<p class="section-note">Slots offered, waiting for the lead to pick one:</p><ul>${c.slot_offers.map((o) => `<li>${esc(o.label)}. ${esc(fmtDateTime(o.start))}</li>`).join("")}</ul>`
      : `<p class="section-note">No meeting yet. Slots are offered once the lead is qualified.</p>`;

  const logItems = c.log.map((e) => {
    const hasDetail = e.detail && Object.keys(e.detail).length;
    const head = `<time datetime="${esc(e.at)}">${esc(fmtTime(e.at))}</time><div><div class="what">${esc(e.summary)}</div><div class="actor">${esc(e.actor)}</div></div>`;
    return hasDetail
      ? `<li data-level="${esc(e.level)}"><details><summary>${head}</summary><pre>${esc(JSON.stringify(e.detail, null, 2))}</pre></details></li>`
      : `<li data-level="${esc(e.level)}"><div class="log-row">${head}</div></li>`;
  }).join("");

  els.lead.innerHTML = `
    <div class="verdict" data-st="${esc(st)}">
      <div class="verdict-word">${esc(STATUS_LABEL[st] || st)}</div>
      <p class="verdict-sub">${lead && lead.policy_version ? `Decided by policy rules, version ${lead.policy_version}` : "Not assessed yet"}</p>
      ${lead && lead.reasons.length ? `<ul class="reasons">${lead.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    </div>
    <div class="section">
      <h2>What the lead told us</h2>
      <p class="section-note">Select a quote to see it in the conversation.</p>
      <table class="facts"><tbody>${factsRows}</tbody></table>
    </div>
    <div class="section">
      <h2>Assigned rep</h2>
      ${lead && lead.assigned_rep
        ? `<p class="rep-name">${esc(lead.assigned_rep.name)}</p><p class="section-note">${esc(lead.assignment_reason)}</p>`
        : `<p class="section-note">Not assigned. A rep is assigned once the lead is qualified.</p>`}
    </div>
    <div class="section">
      <h2>Meeting</h2>
      ${meeting}
    </div>
    <div class="section">
      <h2>Action log</h2>
      ${c.log.length ? `<ol class="log">${logItems}</ol>` : `<p class="section-note">Nothing has happened in this conversation yet.</p>`}
    </div>`;
}

els.lead.addEventListener("click", (e) => {
  const q = e.target.closest("button.quote");
  if (q) highlightEvidence(q.dataset.field);
});

function renderNothingSelected() {
  current = null;
  els.title.innerHTML = "";
  els.control.innerHTML = "";
  els.banner.hidden = true;
  els.composer.hidden = true;
  els.thread.innerHTML = `<li class="empty"><strong>Select an inquiry</strong>Its conversation, qualification and booking appear here.</li>`;
  els.lead.innerHTML = "";
}

// ---------- data ----------

async function refresh() {
  let list;
  try {
    list = await api("/api/conversations");
  } catch (e) {
    els.list.innerHTML = `<li class="empty"><strong>Could not load inquiries</strong>${esc(e.message)}. Retrying in a few seconds.</li>`;
    return;
  }
  const flash = new Set();
  if (seenStamps) {
    for (const c of list) {
      if (seenStamps[c.id] !== c.last_message_at && c.last_message?.author === "lead") flash.add(c.id);
    }
    if (flash.size) toast(flash.size === 1 ? `New message from ${list.find((c) => flash.has(c.id)).contact.name}` : `${flash.size} new messages`);
  }
  seenStamps = Object.fromEntries(list.map((c) => [c.id, c.last_message_at]));
  conversations = list;
  renderList(flash);
  document.title = `${list.filter((c) => c.lead_status === "needs_info" && !c.automation_paused).length ? "● " : ""}Lumen sales desk`;

  els.desk.classList.toggle("has-selection", Boolean(selectedId));
  if (!selectedId) return renderNothingSelected();
  try {
    const detail = await api(`/api/conversations/${selectedId}`);
    const json = JSON.stringify(detail);
    if (json === lastDetailJson) return; // nothing changed: keep scroll, focus and highlights
    lastDetailJson = json;
    current = detail;
    renderThread(detail);
    renderControl(detail);
    renderLead(detail);
  } catch (e) {
    if (e.status === 404) { select(null); toast("That conversation no longer exists."); }
  }
}

function select(id) {
  selectedId = id;
  history.replaceState(null, "", id ? `#c${id}` : location.pathname);
  lastDetailJson = "";
  if (!id) { els.desk.classList.remove("has-selection"); renderList(); renderNothingSelected(); return; }
  renderList();
  refresh();
}

els.list.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-id]");
  if (btn) select(Number(btn.dataset.id));
});
els.statusFilters.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-filter]");
  if (!btn) return;
  statusFilter = statusFilter === btn.dataset.filter ? "" : btn.dataset.filter;
  renderList();
});
els.channel.addEventListener("change", () => renderList());
els.search.addEventListener("input", () => renderList());
els.back.addEventListener("click", () => select(null));
window.addEventListener("hashchange", () => {
  const id = Number(location.hash.replace("#c", "")) || null;
  if (id !== selectedId) select(id);
});
document.addEventListener("data-changed", () => { lastDetailJson = ""; refresh(); });

document.addEventListener("keydown", (e) => {
  const typing = e.target.closest("input, textarea, select");
  if (e.key === "/" && !typing) { e.preventDefault(); els.search.focus(); return; }
  if (e.key === "Escape" && e.target === els.search) { els.search.value = ""; els.search.blur(); renderList(); return; }
  if (typing || !["ArrowDown", "ArrowUp", "j", "k"].includes(e.key)) return;
  const rows = visible();
  if (!rows.length) return;
  e.preventDefault();
  const i = rows.findIndex((c) => c.id === selectedId);
  const next = ["ArrowDown", "j"].includes(e.key) ? Math.min(rows.length - 1, i + 1) : Math.max(0, i - 1);
  select(rows[i === -1 ? 0 : next].id);
  els.list.querySelector(`button[data-id="${rows[i === -1 ? 0 : next].id}"]`)?.focus({ preventScroll: false });
});

loadBusinessTz().then(refresh);
setInterval(refresh, 4000);
setInterval(() => renderList(), 60000); // keep "5 min ago" labels fresh
