"""Conversation pipeline: what happens after an inbound message is stored.

Step 2: no automatic reply yet; messages are stored and shown.
Step 3 adds LLM extraction, the qualification rules, routing and follow-ups.
"""
from sqlalchemy.orm import Session

from app.models import Conversation, Message


def handle_inbound(session: Session, conv: Conversation, message: Message) -> None:
    return None
