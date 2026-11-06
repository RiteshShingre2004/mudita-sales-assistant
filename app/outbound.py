"""Send a message on the conversation's own channel.

- Automatic messages (author="assistant") pass the takeover guard first.
- The message row is written as "sending" before the provider call, so a
  crash mid-send leaves a visible trace instead of nothing.
- A timeout leaves the message "uncertain": it may or may not have been
  delivered, so it is never automatically re-sent.
"""
import uuid

import httpx
from sqlalchemy.orm import Session

from app.actions import log_action
from app.google_oauth import GoogleAPIError, GoogleNotConnected
from app.guards import require_automation_allowed
from app.models import Conversation, Message, utcnow


class ChannelNotAvailable(Exception):
    pass


def _send_on_channel(session: Session, conv: Conversation, text: str) -> str:
    if conv.channel == "email":
        from app.channels.gmail import send_email

        return send_email(session, conv, text)
    raise ChannelNotAvailable(f"Sending on {conv.channel} is not connected yet")


def send_message(session: Session, conv: Conversation, text: str, *, author: str, actor: str,
                 meta: dict | None = None) -> Message:
    if author == "assistant":
        require_automation_allowed(session, conv.id, "send_reply")

    msg = Message(conversation_id=conv.id, channel=conv.channel, channel_message_id=f"pending-{uuid.uuid4()}",
                  direction="out", author=author, body=text, send_status="sending", meta=meta or {})
    session.add(msg)
    session.commit()

    try:
        provider_id = _send_on_channel(session, conv, text)
    except httpx.TimeoutException:
        msg.send_status, msg.error = "uncertain", "The channel did not answer in time. It may have been delivered; check before re-sending."
    except (GoogleNotConnected, GoogleAPIError, ChannelNotAvailable, httpx.HTTPError) as exc:
        msg.send_status, msg.error = "failed", str(exc)
    else:
        msg.channel_message_id, msg.send_status = provider_id, "sent"

    conv.last_message_at = utcnow()
    if author == "human" and msg.send_status == "sent":
        conv.attention = ""  # a person has answered
    who = "Operator" if author == "human" else "Assistant"
    if msg.send_status == "sent":
        log_action(session, actor=actor, action="message_sent", conversation_id=conv.id,
                   summary=f"{who} reply sent via {conv.channel}")
    else:
        log_action(session, actor=actor, action=f"message_{msg.send_status}", conversation_id=conv.id,
                   level="error", summary=f"{who} reply {msg.send_status}: {msg.error}")
    session.commit()
    return msg
