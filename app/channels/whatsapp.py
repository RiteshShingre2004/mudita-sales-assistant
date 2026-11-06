"""WhatsApp Cloud API channel.

Inbound: Meta calls POST /webhooks/whatsapp. We check the signature, store
each new message (deduped by its wamid), answer 200 straight away, then run
the conversation pipeline in the background. Meta re-delivers a webhook it
did not get a 200 for; the dedupe rule makes that harmless.

Outbound: a text message to the lead's WhatsApp id via the Graph API.
Delivery receipts ("sent", "delivered", "read", "failed") update the message.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions import log_action
from app.config import settings
from app.inbound import InboundMessage, record_inbound
from app.meta import MetaAPIError, explain, graph_post, set_health
from app.models import Contact, Conversation, Message

PROVIDER = "whatsapp"

_NON_TEXT = {
    "image": "a photo", "video": "a video", "audio": "a voice message", "document": "a document",
    "sticker": "a sticker", "location": "a location", "contacts": "a contact card", "reaction": "a reaction",
}


def _body_of(msg: dict) -> str:
    kind = msg.get("type")
    if kind == "text":
        return msg.get("text", {}).get("body", "")
    if kind == "button":
        return msg.get("button", {}).get("text", "")
    if kind == "interactive":
        inter = msg.get("interactive", {})
        reply = inter.get("button_reply") or inter.get("list_reply") or {}
        return reply.get("title", "")
    caption = (msg.get(kind) or {}).get("caption", "") if isinstance(msg.get(kind), dict) else ""
    note = f"[Sent {_NON_TEXT.get(kind, 'an unsupported message')}; the assistant only reads text]"
    return f"{note}\n{caption}".strip()


def parse_webhook(payload: dict, other_numbers: list[str] | None = None) -> tuple[list[InboundMessage], list[dict]]:
    """Return (new messages, delivery statuses) addressed to our phone number.
    Phone number ids that are not ours are appended to `other_numbers`."""
    messages: list[InboundMessage] = []
    statuses: list[dict] = []
    if payload.get("object") != "whatsapp_business_account":
        return messages, statuses
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") != "messages":
                continue
            value = change.get("value", {})
            pid = value.get("metadata", {}).get("phone_number_id")
            if pid != settings.whatsapp_phone_number_id:
                if other_numbers is not None and pid:
                    other_numbers.append(pid)
                continue  # some other number on the same app
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                if m.get("type") == "reaction":
                    continue  # emoji reactions are not messages to answer
                wa_id = m.get("from", "")
                ts = m.get("timestamp")
                messages.append(InboundMessage(
                    channel="whatsapp", external_id=wa_id, display_name=names.get(wa_id) or f"+{wa_id}",
                    channel_message_id=m["id"], body=_body_of(m)[:4000],
                    sent_at=datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None,
                    meta={"type": m.get("type"), "context": m.get("context", {})},
                ))
            statuses.extend(value.get("statuses", []))
    return messages, statuses


def apply_status(session: Session, status: dict) -> None:
    msg = session.scalar(select(Message).where(Message.channel == "whatsapp",
                                               Message.channel_message_id == status.get("id")))
    if msg is None:
        return
    state = status.get("status")
    msg.meta = {**(msg.meta or {}), "delivery": state}
    if state == "failed":
        err = (status.get("errors") or [{}])[0]
        reason = explain(err.get("code"), (err.get("error_data") or {}).get("details") or err.get("title") or "failed")
        msg.send_status, msg.error = "failed", reason
        conv = session.get(Conversation, msg.conversation_id)
        conv.attention = f"WhatsApp did not deliver a reply: {reason}"
        log_action(session, actor="webhook", action="message_failed", level="error", conversation_id=conv.id,
                   summary=f"WhatsApp reported the reply as not delivered: {reason}",
                   detail={"code": err.get("code"), "wamid": status.get("id")})
    session.commit()


def store_webhook(session: Session, payload: dict) -> list[int]:
    """Store new messages and apply statuses. Returns conversation ids to process."""
    others: list[str] = []
    messages, statuses = parse_webhook(payload, others)
    if others and not messages and not statuses:
        set_health(session, PROVIDER, "error",
                   f"A message arrived for phone number ID {others[0]}, but WHATSAPP_PHONE_NUMBER_ID is "
                   f"{settings.whatsapp_phone_number_id or 'not set'}. Set it to the test number's Phone number ID.")
        return []
    if messages:
        set_health(session, PROVIDER, "ok")
    to_process: list[int] = []
    for inbound in messages:
        stored = record_inbound(session, inbound, actor="webhook")
        if stored is not None and stored[0].id not in to_process:
            to_process.append(stored[0].id)
    for st in statuses:
        apply_status(session, st)
    return to_process


def send_whatsapp(session: Session, conv: Conversation, text: str) -> str:
    if not (settings.whatsapp_token and settings.whatsapp_phone_number_id):
        raise MetaAPIError("WhatsApp is not set up on the server (token or phone number ID missing)")
    contact = session.get(Contact, conv.contact_id)
    data = graph_post(session, PROVIDER, f"{settings.whatsapp_phone_number_id}/messages", settings.whatsapp_token, {
        "messaging_product": "whatsapp", "recipient_type": "individual", "to": contact.external_id,
        "type": "text", "text": {"preview_url": False, "body": text[:4096]},
    })
    return data["messages"][0]["id"]
