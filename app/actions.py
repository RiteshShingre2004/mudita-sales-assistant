"""Action log: a concise, append-only record of everything the system did."""
from sqlalchemy.orm import Session

from app.models import ActionLog


def log_action(
    session: Session,
    *,
    actor: str,
    action: str,
    summary: str,
    conversation_id: int | None = None,
    level: str = "info",
    detail: dict | None = None,
) -> ActionLog:
    entry = ActionLog(
        conversation_id=conversation_id,
        actor=actor,
        action=action,
        level=level,
        summary=summary,
        detail=detail or {},
    )
    session.add(entry)
    return entry
