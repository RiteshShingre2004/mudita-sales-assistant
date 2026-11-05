// Inbox: all state lives on the server. The selected conversation is kept in
// the URL hash, so a page refresh returns to the same place.

const els = {
  desk: document.getElementById("desk"),
  list: document.getElementById("conv-list"),
  filter: document.getElementById("channel-filter"),
  title: document.getElementById("thread-title"),
  thread: document.getElementById("thread"),
  lead: document.getElementById("lead-pane"),
  back: document.getElementById("back"),
  control: document.getElementById("control"),
  banner: document.getElementById("paused-banner"),
};

const ROLE_LABEL = { decision_maker: "Decision maker", influencer: "Influencer", end_user: "End user", unknown: "Unknown" };

function displayValue(key, v) {
  if (key === "service") return POLICY?.services.find((s) => s.id === v)?.name || `${v} (not a listed service)`;
  if (key === "budget_usd") return `$${Number(v).toLocaleString("en-US")}`;
  if (key === "start_within_months") return Number(v) === 0 ? "Immediately" : `${v} month${Number(v) === 1 ? "" : "s"}`;
  if (key === "role") return ROLE_LABEL[v] || v;
  if (key === "intent") return String(v).replaceAll("_", " ");
  return v;
}

function renderControl(c) {
  els.banner.hidden = !c.automation_paused;
  els.control.innerHTML = c.automation_paused
    ? `<button type="button" id="takeover-btn" data-paused="false">Hand back to assistant</button>`
    : `<button type="button" class="primary" id="takeover-btn" data-paused="true">Take over</button>`;
  document.getElementById("takeover-btn").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      await api(`/api/conversations/${c.id}/takeover`, { method: "POST", body: JSON.stringify({ paused: btn.dataset.paused === "true" }) });
      lastDetailJson = "";
      await refresh();
    } catch (e) {
      btn.disabled = false;
      alert(`Could not change control: ${e.message}`);
    }
  });
}

let conversations = [];
let selectedId = Number(location.hash.replace("#c", "")) || null;
let lastDetailJson = "";

function renderList() {
  const ch = els.filter.value;
  const rows = conversations.filter((c) => !ch || c.channel === ch);
  if (!rows.length) {
    els.list.innerHTML = `<li class="empty"><strong>No inquiries yet</strong>New messages to the connected inbox, WhatsApp number or Instagram account appear here within a few seconds.</li>`;
    return;
  }
  els.list.innerHTML = rows.map((c) => `
    <li><button type="button" data-id="${c.id}" aria-current="${c.id === selectedId}">
      <span class="conv-name">${esc(c.contact.name || c.contact.external_id)}</span>
      <span class="conv-time">${esc(fmtRelative(c.last_message_at))}</span>
      <span class="conv-preview">${esc(c.last_message ? c.last_message.body : "")}</span>
      <span class="conv-meta">
        <span class="channel" data-ch="${esc(c.channel)}">${esc(CHANNEL_LABEL[c.channel] || c.channel)}</span>
        <span class="status" data-st="${esc(c.lead_status)}">${esc(STATUS_LABEL[c.lead_status] || c.lead_status)}</span>
        ${c.assigned_rep ? `<span class="tag">${esc(c.assigned_rep)}</span>` : ""}
        ${c.automation_paused ? `<span class="tag paused">Human in control</span>` : ""}
        ${c.contact.is_demo ? `<span class="tag demo">Sample data</span>` : ""}
      </span>
    </button></li>`).join("");
}

const AUTHOR_LABEL = { lead: "Lead", assistant: "Assistant", human: "Operator" };

function renderThread(c) {
  els.title.innerHTML = `
    <div class="thread-title-name">${esc(c.contact.name || c.contact.external_id)}</div>
    <div class="thread-title-sub"><span class="channel" data-ch="${esc(c.channel)}">${esc(CHANNEL_LABEL[c.channel])}</span>
      ${esc(c.contact.external_id)}${c.contact.is_demo ? " (sample data, not from a live channel)" : ""}</div>`;
  if (!c.messages.length) {
    els.thread.innerHTML = `<li class="empty">No messages in this conversation.</li>`;
    return;
  }
  els.thread.innerHTML = c.messages.map((m) => `
    <li class="msg ${m.direction === "out" ? "out" : "in"} ${esc(m.author)}">
      <div class="msg-meta">${esc(AUTHOR_LABEL[m.author] || m.author)}, ${esc(fmtDateTime(m.at))}</div>
      <div class="msg-body">${esc(m.body)}</div>
      ${m.send_status === "failed" ? `<div class="msg-error">Not delivered: ${esc(m.error || "unknown error")}</div>` : ""}
    </li>`).join("");
  els.thread.parentElement.scrollTop = els.thread.parentElement.scrollHeight;
}

const FIELD_LABEL = {
  service: "Service", budget_usd: "Budget (USD)", start_within_months: "Start within (months)",
  role: "Role", company: "Company", intent: "Intent",
};

function renderLead(c) {
  const lead = c.lead;
  const st = lead ? lead.status : "new";
  const fields = lead ? lead.fields : {};
  const evidence = lead ? lead.evidence : {};
  const keys = Object.keys(FIELD_LABEL);

  const factsRows = keys.map((k) => {
    const v = fields[k];
    const q = evidence[k];
    return `<tr><th scope="row">${FIELD_LABEL[k]}</th><td>${
      v === undefined || v === null || v === "" ? `<span class="unknown">Not stated</span>` : esc(displayValue(k, v))
    }${q ? `<span class="quote">"${esc(q)}"</span>` : ""}</td></tr>`;
  }).join("");

  const booking = c.bookings.length ? c.bookings[c.bookings.length - 1] : null;
  const meeting = booking
    ? `<p><strong>${esc(booking.status)}</strong>, ${esc(fmtDateTime(booking.start))}</p>
       ${booking.error ? `<p class="msg-error">${esc(booking.error)}</p>` : ""}
       ${booking.calendar_link ? `<p><a href="${esc(booking.calendar_link)}" target="_blank" rel="noopener">Open in Google Calendar</a></p>` : ""}`
    : c.slot_offers.length
      ? `<p class="section-note">Slots offered, waiting for the lead to pick one:</p><ul>${c.slot_offers.map((o) => `<li>${esc(o.label)}. ${esc(fmtDateTime(o.start))}</li>`).join("")}</ul>`
      : `<p class="section-note">No meeting yet. Slots are offered once the lead is qualified.</p>`;

  els.lead.innerHTML = `
    <div class="verdict" data-st="${esc(st)}">
      <div class="verdict-word">${esc(STATUS_LABEL[st] || st)}</div>
      <p class="verdict-sub">${lead && lead.policy_version ? `Decided by policy rules, version ${lead.policy_version}` : "Not assessed yet"}</p>
      ${lead && lead.reasons.length ? `<ul class="reasons">${lead.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    </div>

    <div class="section">
      <h2>What the lead told us</h2>
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
      ${c.log.length
        ? `<ol class="log">${c.log.map((e) => `<li data-level="${esc(e.level)}"><time datetime="${esc(e.at)}">${esc(fmtTime(e.at))}</time>
            <div><div class="what">${esc(e.summary)}</div><div class="actor">${esc(e.actor)}</div></div></li>`).join("")}</ol>`
        : `<p class="section-note">Nothing has happened in this conversation yet.</p>`}
    </div>`;
}

function renderNothingSelected() {
  els.title.innerHTML = "";
  els.control.innerHTML = "";
  els.banner.hidden = true;
  els.thread.innerHTML = `<li class="empty"><strong>Select an inquiry</strong>Its conversation, qualification and booking appear here.</li>`;
  els.lead.innerHTML = "";
}

async function refresh() {
  try {
    conversations = await api("/api/conversations");
  } catch (e) {
    els.list.innerHTML = `<li class="empty"><strong>Could not load inquiries</strong>${esc(e.message)}. Retrying in a few seconds.</li>`;
    return;
  }
  renderList();
  els.desk.classList.toggle("has-selection", Boolean(selectedId));
  if (!selectedId) return renderNothingSelected();
  try {
    const detail = await api(`/api/conversations/${selectedId}`);
    const json = JSON.stringify(detail);
    if (json === lastDetailJson) return; // avoid re-rendering (keeps scroll and focus)
    lastDetailJson = json;
    renderThread(detail);
    renderControl(detail);
    renderLead(detail);
  } catch (e) {
    if (e.status === 404) { selectedId = null; history.replaceState(null, "", "/"); renderNothingSelected(); }
  }
}

els.list.addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-id]");
  if (!btn) return;
  selectedId = Number(btn.dataset.id);
  history.replaceState(null, "", `#c${selectedId}`);
  lastDetailJson = "";
  refresh();
});
els.filter.addEventListener("change", renderList);
window.addEventListener("hashchange", () => {
  selectedId = Number(location.hash.replace("#c", "")) || null;
  lastDetailJson = "";
  refresh();
});
els.back.addEventListener("click", () => {
  selectedId = null;
  history.replaceState(null, "", "/");
  els.desk.classList.remove("has-selection");
  renderList();
});

loadBusinessTz().then(refresh);
setInterval(refresh, 4000);
