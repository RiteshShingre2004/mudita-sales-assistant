"""Gmail inbox channel: polling for new mail and threaded replies.

Why polling (users.history.list) rather than Gmail push (Pub/Sub watch):
push needs a Google Cloud Pub/Sub topic, a push subscription and a public
endpoint, and still requires a history.list call to learn what changed.
Polling the history cursor every ~20 s gives the same result with fewer
moving parts, and the cursor makes it exactly-once with our dedupe rule.

Reliability rules:
- The history cursor only advances after every new message in the batch is
  stored. If a fetch fails midway, the next poll repeats the batch; already
  stored messages are dropped by the (channel, message id) uniqueness rule.
- Our own sent mail, drafts, newsletters, mailing lists and auto-replies are
  skipped so the assistant never answers a robot or itself.
"""
import base64
import html
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr

from sqlalchemy.orm import Session

from app.actions import log_action
from app.google_oauth import GoogleAPIError, get_row, google_request, set_status
from app.inbound import InboundMessage, already_stored, record_inbound
from app.models import Contact, Conversation, Message

API = "https://gmail.googleapis.com/gmail/v1/users/me"
FALLBACK_QUERY = "in:inbox newer_than:2d"
ROBOT_SENDERS = re.compile(r"^(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?)@", re.I)


# ---------- parsing ----------

def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _find_part(payload: dict, mime: str) -> str | None:
    if payload.get("mimeType") == mime and payload.get("body", {}).get("data"):
        return _b64(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        found = _find_part(part, mime)
        if found:
            return found
    return None


def _html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", raw)
    return html.unescape(re.sub(r"<[^>]+>", "", raw))


_WROTE = re.compile(r"^\s*On\b.*\bwrote:\s*$", re.I)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*Original Message", re.I)


def strip_quoted_reply(text: str) -> str:
    """Keep only the new part of a reply: cut at 'On ... wrote:' (which Gmail
    sometimes wraps over two lines) or '--- Original Message', drop '>' lines."""
    lines = text.replace("\r\n", "\n").split("\n")
    kept = []
    for i, line in enumerate(lines):
        two_lines = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")
        if _WROTE.match(line) or _ORIGINAL.match(line) or (line.strip().startswith("On ") and _WROTE.match(two_lines)):
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    out = "\n".join(kept).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out or text.strip()


def skip_reason(msg: dict, headers: dict, sender: str, own_address: str) -> str | None:
    labels = set(msg.get("labelIds", []))
    if "SENT" in labels or "DRAFT" in labels or sender == own_address:
        return "sent by this inbox"
    if "INBOX" not in labels:
        return "not in inbox"
    if "SPAM" in labels:
        return "spam"
    if headers.get("auto-submitted", "no").lower() != "no":
        return "auto-reply"
    if headers.get("precedence", "").lower() in ("bulk", "list", "junk"):
        return "bulk mail"
    if "list-id" in headers or "list-unsubscribe" in headers:
        return "mailing list"
    if ROBOT_SENDERS.match(sender):
        return "automated sender"
    return None


def parse_message(msg: dict, own_address: str) -> InboundMessage | str:
    """Return an InboundMessage, or a string reason why it was skipped."""
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
    name, addr = parseaddr(headers.get("from", ""))
    addr = addr.lower()
    reason = skip_reason(msg, headers, addr, own_address)
    if reason:
        return reason
    if not addr:
        return "no sender address"

    body = _find_part(payload, "text/plain")
    if body is None:
        html_body = _find_part(payload, "text/html")
        body = _html_to_text(html_body) if html_body else msg.get("snippet", "")
    body = strip_quoted_reply(body)

    sent_at = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc) \
        if msg.get("internalDate") else None
    return InboundMessage(
        channel="email",
        external_id=addr,
        display_name=name or addr,
        channel_message_id=msg["id"],
        body=body[:20000],
        thread_ref=msg.get("threadId", ""),
        subject=headers.get("subject", "")[:500],
        sent_at=sent_at,
        meta={"subject": headers.get("subject", ""), "rfc_message_id": headers.get("message-id", ""),
              "references": headers.get("references", ""), "thread_id": msg.get("threadId", "")},
    )


# ---------- polling ----------

def _get(session: Session, url: str, **params) -> dict:
    resp = google_request(session, "GET", url, params=params)
    if resp.status_code != 200:
        raise GoogleAPIError(resp.status_code, resp.text[:300])
    return resp.json()


def _new_message_ids(session: Session, start_history_id: str) -> tuple[list[str], str]:
    """Message ids added to INBOX since the cursor, and the new cursor."""
    ids: list[str] = []
    page_token = None
    latest = start_history_id
    while True:
        params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "labelId": "INBOX"}
        if page_token:
            params["pageToken"] = page_token
        data = _get(session, f"{API}/history", **params)
        for h in data.get("history", []):
            for added in h.get("messagesAdded", []):
                mid = added["message"]["id"]
                if mid not in ids:
                    ids.append(mid)
        latest = data.get("historyId", latest)
        page_token = data.get("nextPageToken")
        if not page_token:
            return ids, str(latest)


def poll_once(session: Session, actor: str = "poller") -> dict:
    """Fetch and store new inbox mail. Safe to call repeatedly."""
    row = get_row(session)
    own = row.account
    state = dict(row.state or {})
    cursor = state.get("gmail_history_id")
    result = {"new": 0, "skipped": 0, "duplicates": 0}

    try:
        if cursor:
            ids, new_cursor = _new_message_ids(session, cursor)
        else:
            raise GoogleAPIError(404, "no cursor")
    except GoogleAPIError as exc:
        if exc.status_code != 404:
            raise
        # Cursor too old or missing: fall back to recent inbox mail, then restart the cursor.
        listed = _get(session, f"{API}/messages", q=FALLBACK_QUERY, maxResults=25)
        ids = [m["id"] for m in listed.get("messages", [])]
        new_cursor = str(_get(session, f"{API}/profile")["historyId"])
        log_action(session, actor=actor, action="gmail_cursor_reset", level="warn",
                   summary="Gmail history cursor was too old; re-read the last 2 days of inbox mail")

    skipped_ids = set(state.get("skipped", []))
    fetched = []
    for mid in ids:
        if mid in skipped_ids or already_stored(session, "email", mid):
            result["duplicates"] += 1
            continue
        fetched.append(_get(session, f"{API}/messages/{mid}", format="full"))
    fetched.sort(key=lambda m: int(m.get("internalDate", "0")))

    for raw in fetched:
        parsed = parse_message(raw, own)
        if isinstance(parsed, str):
            result["skipped"] += 1
            skipped_ids.add(raw["id"])
            log_action(session, actor=actor, action="email_skipped",
                       summary=f"Ignored an email ({parsed})", detail={"gmail_id": raw["id"]})
            continue
        stored = record_inbound(session, parsed, actor=actor)
        if stored is None:
            result["duplicates"] += 1
            continue
        result["new"] += 1
        _hand_to_pipeline(session, *stored)

    # Only now is it safe to move the cursor forward.
    row = get_row(session)
    row.state = {**(row.state or {}), "gmail_history_id": new_cursor, "skipped": list(skipped_ids)[-300:]}
    set_status(session, "ok")
    return result


def _hand_to_pipeline(session: Session, conv: Conversation, message: Message) -> None:
    from app.pipeline import handle_inbound  # Step 3 fills this in

    handle_inbound(session, conv, message)


# ---------- sending ----------

def send_email(session: Session, conv: Conversation, text: str) -> str:
    """Reply in the same Gmail thread. Returns Gmail's message id."""
    row = get_row(session)
    contact = session.get(Contact, conv.contact_id)
    last_in = next((m for m in reversed(conv.messages) if m.direction == "in" and m.meta), None)
    meta = last_in.meta if last_in else {}

    subject = conv.subject or "Your inquiry with Lumen Analytics"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    mail = EmailMessage()
    mail["To"] = contact.external_id
    mail["From"] = row.account
    mail["Subject"] = subject
    if meta.get("rfc_message_id"):
        mail["In-Reply-To"] = meta["rfc_message_id"]
        refs = (meta.get("references", "") + " " + meta["rfc_message_id"]).strip()
        mail["References"] = refs
    mail.set_content(text)

    raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
    body = {"raw": raw}
    if conv.thread_ref:
        body["threadId"] = conv.thread_ref
    resp = google_request(session, "POST", f"{API}/messages/send", json=body)
    if resp.status_code != 200:
        raise GoogleAPIError(resp.status_code, resp.text[:300])
    return resp.json()["id"]
