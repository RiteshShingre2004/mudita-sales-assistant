"""Instagram Direct channel (Instagram API with Instagram Login).

Inbound: Meta calls POST /webhooks/instagram with `object: "instagram"` and a
`messaging` array. Our own sent messages come back as echoes (`is_echo`)
and are skipped. Messages are deduped by `mid`.

Outbound: POST graph.instagram.com/<ver>/me/messages with the lead's
Instagram-scoped id (IGSID). Replies are only allowed within 24 hours of the
lead's last message; outside it the error is explained in plain words.

`link_account` reads which professional account the token belongs to and
subscribes it to message webhooks, so no manual Graph API call is needed.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.actions import log_action
from app.config import settings
from app.inbound import InboundMessage, record_inbound
from app.meta import MetaAPIError, graph_post, set_health
from app.models import Connection, Contact, Conversation

PROVIDER = "instagram"
BASE = "https://graph.instagram.com"


def _account(session: Session) -> Connection:
    row = session.get(Connection, PROVIDER)
    if row is None:
        row = Connection(provider=PROVIDER, status="not_connected", credentials={}, state={})
        session.add(row)
        session.flush()
    return row


def link_account(session: Session, actor: str = "system") -> str:
    """Identify the professional account and subscribe it to message webhooks."""
    if not settings.instagram_token:
        raise MetaAPIError("Instagram is not set up on the server (INSTAGRAM_ACCESS_TOKEN missing)")
    me = graph_post(session, PROVIDER, "me", settings.instagram_token, None, base=BASE, method="GET",
                    params={"fields": "user_id,username"})
    graph_post(session, PROVIDER, "me/subscribed_apps", settings.instagram_token, None, base=BASE,
               params={"subscribed_fields": "messages"})
    row = _account(session)
    row.account = "@" + me.get("username", "")
    row.state = {**(row.state or {}), "ig_user_id": str(me.get("user_id") or me.get("id", ""))}
    log_action(session, actor=actor, action="instagram_linked",
               summary=f"Instagram {row.account} linked; message webhooks subscribed")
    session.commit()
    set_health(session, PROVIDER, "ok")
    return row.account


def _own_ids(session: Session) -> set[str]:
    row = session.get(Connection, PROVIDER)
    own = (row.state or {}).get("ig_user_id") if row else None
    return {own} if own else set()


def _body_of(message: dict) -> str:
    text = message.get("text") or ""
    attachments = message.get("attachments") or []
    if attachments and not text:
        kinds = {a.get("type", "file") for a in attachments}
        what = "a photo" if kinds <= {"image"} else "an attachment"
        return f"[Sent {what}; the assistant only reads text]"
    return text


def parse_webhook(payload: dict, own_ids: set[str]) -> list[InboundMessage]:
    out: list[InboundMessage] = []
    if payload.get("object") != "instagram":
        return out
    for entry in payload.get("entry", []):
        account_id = str(entry.get("id", ""))
        for ev in entry.get("messaging", []):
            message = ev.get("message")
            if not message or message.get("is_echo") or message.get("is_deleted") or message.get("is_unsupported"):
                continue  # our own sent messages, deletions, read receipts, reactions
            sender = str(ev.get("sender", {}).get("id", ""))
            if not sender or sender in own_ids or sender == account_id:
                continue
            ts = ev.get("timestamp")
            out.append(InboundMessage(
                channel="instagram", external_id=sender, display_name="",
                channel_message_id=message["mid"], body=_body_of(message)[:4000],
                sent_at=datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc) if ts else None,
                meta={"account_id": account_id},
            ))
    return out


def _lookup_username(session: Session, igsid: str) -> str:
    try:
        data = graph_post(session, PROVIDER, igsid, settings.instagram_token, None, base=BASE, method="GET",
                          params={"fields": "username,name"})
    except Exception:
        return ""
    return data.get("username") and "@" + data["username"] or data.get("name", "")


def store_webhook(session: Session, payload: dict) -> list[int]:
    to_process: list[int] = []
    messages = parse_webhook(payload, _own_ids(session))
    if messages:
        set_health(session, PROVIDER, "ok")
    for inbound in messages:
        stored = record_inbound(session, inbound, actor="webhook")
        if stored is None:
            continue
        conv, _ = stored
        contact = session.get(Contact, conv.contact_id)
        if contact.display_name == contact.external_id and settings.instagram_token:
            name = _lookup_username(session, contact.external_id)
            if name:
                contact.display_name = name
                session.commit()
        if conv.id not in to_process:
            to_process.append(conv.id)
    return to_process


def _trim_bytes(text: str, limit: int = 1000) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[: limit - 3].decode("utf-8", errors="ignore").rstrip() + "..."


def send_instagram(session: Session, conv: Conversation, text: str) -> str:
    if not settings.instagram_token:
        raise MetaAPIError("Instagram is not set up on the server (INSTAGRAM_ACCESS_TOKEN missing)")
    contact = session.get(Contact, conv.contact_id)
    data = graph_post(session, PROVIDER, "me/messages", settings.instagram_token,
                      {"recipient": {"id": contact.external_id}, "message": {"text": _trim_bytes(text)}}, base=BASE)
    return data["message_id"]
