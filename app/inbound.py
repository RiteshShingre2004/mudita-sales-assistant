"""Shared entry point for every inbound message, whatever the channel.

Each channel adapter (Gmail poller, WhatsApp webhook, Instagram webhook)
normalises its payload and calls `record_inbound`. That function:
- finds or creates the contact by (channel, external_id); never across channels
- drops a message it has already stored (webhook re-delivery, re-poll)
- appends to the contact's conversation on that channel
- logs the event and hands the message to the conversation pipeline
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actions import log_action
from app.models import Contact, Conversation, Message


@dataclass
class InboundMessage:
    channel: str
    external_id: str          # sender identity on that channel
    channel_message_id: str   # provider's message id, used for dedupe
    body: str
    display_name: str = ""
    thread_ref: str = ""
    subject: str = ""
    sent_at: datetime | None = None
    meta: dict = field(default_factory=dict)


def already_stored(session: Session, channel: str, channel_message_id: str) -> bool:
    return session.scalar(
        select(Message.id).where(Message.channel == channel, Message.channel_message_id == channel_message_id)
    ) is not None


def record_inbound(session: Session, msg: InboundMessage, actor: str) -> tuple[Conversation, Message] | None:
    """Store the message. Returns None if it was a duplicate."""
    if already_stored(session, msg.channel, msg.channel_message_id):
        return None

    contact = session.scalar(
        select(Contact).where(Contact.channel == msg.channel, Contact.external_id == msg.external_id)
    )
    if contact is None:
        contact = Contact(channel=msg.channel, external_id=msg.external_id,
                          display_name=msg.display_name or msg.external_id)
        session.add(contact)
        session.flush()
    elif msg.display_name and contact.display_name == contact.external_id:
        contact.display_name = msg.display_name

    conv = session.scalar(
        select(Conversation).where(Conversation.contact_id == contact.id, Conversation.channel == msg.channel)
        .order_by(Conversation.created_at.desc()).limit(1)
    )
    is_new = conv is None
    if is_new:
        conv = Conversation(contact_id=contact.id, channel=msg.channel)
        session.add(conv)
        session.flush()
    if msg.thread_ref:
        conv.thread_ref = msg.thread_ref
    if msg.subject and not conv.subject:
        conv.subject = msg.subject

    at = msg.sent_at or datetime.now(timezone.utc)
    message = Message(conversation_id=conv.id, channel=msg.channel, channel_message_id=msg.channel_message_id,
                      direction="in", author="lead", body=msg.body, send_status="received",
                      meta=msg.meta, created_at=at)
    session.add(message)
    conv.last_message_at = max(_aware(conv.last_message_at), at) if conv.last_message_at else at
    try:
        session.flush()
    except IntegrityError:  # lost a race with a concurrent delivery of the same message
        session.rollback()
        return None

    log_action(session, actor=actor, action="message_received", conversation_id=conv.id,
               summary=("New inquiry" if is_new else "New message") + f" from {contact.display_name} via {msg.channel}",
               detail={"channel_message_id": msg.channel_message_id})
    session.commit()
    return conv, message


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
