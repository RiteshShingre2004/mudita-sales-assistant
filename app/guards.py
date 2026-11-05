"""Human-control guard.

Every automatic outbound action (LLM reply, slot offer, booking) must call
`require_automation_allowed` immediately before acting, reading the latest
row from the database. A takeover therefore stops work that is already
queued, not only future inbound messages.
"""
from sqlalchemy.orm import Session

from app.actions import log_action
from app.models import Conversation


class AutomationPaused(Exception):
    pass


def require_automation_allowed(session: Session, conversation_id: int, attempted: str) -> None:
    conv = session.get(Conversation, conversation_id, populate_existing=True)
    if conv is None:
        raise ValueError(f"conversation {conversation_id} not found")
    if conv.automation_paused:
        log_action(
            session,
            actor="system",
            action="automation_blocked",
            summary=f"Skipped '{attempted}': a human has taken over this conversation",
            conversation_id=conversation_id,
            level="warn",
            detail={"attempted": attempted, "paused_by": conv.paused_by},
        )
        session.commit()
        raise AutomationPaused(attempted)
