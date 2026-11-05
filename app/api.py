"""Dashboard JSON API. All routes require an operator login."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.actions import log_action
from app.auth import require_operator
from app.config import settings
from app.db import get_session
from app.models import ActionLog, Booking, Connection, Contact, Conversation, Lead, SlotOffer
from app.policy import PolicyError, get_active_policy_row, save_policy

router = APIRouter(prefix="/api", dependencies=[Depends(require_operator)])


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:  # SQLite drops tz info; we always store UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _log_out(e: ActionLog) -> dict:
    return {
        "id": e.id,
        "conversation_id": e.conversation_id,
        "actor": e.actor,
        "action": e.action,
        "level": e.level,
        "summary": e.summary,
        "detail": e.detail,
        "at": _iso(e.created_at),
    }


def _lead_out(lead: Lead | None) -> dict | None:
    if lead is None:
        return None
    return {
        "status": lead.status,
        "fields": lead.fields,
        "evidence": lead.evidence,
        "reasons": lead.reasons,
        "assigned_rep": (
            {"id": lead.assigned_rep.id, "name": lead.assigned_rep.name, "email": lead.assigned_rep.email}
            if lead.assigned_rep
            else None
        ),
        "assignment_reason": lead.assignment_reason,
        "policy_version": lead.policy_version,
        "updated_at": _iso(lead.updated_at),
    }


# ---------- conversations ----------

@router.get("/conversations")
def list_conversations(session: Session = Depends(get_session)):
    convs = session.scalars(
        select(Conversation)
        .options(selectinload(Conversation.contact), selectinload(Conversation.lead).selectinload(Lead.assigned_rep),
                 selectinload(Conversation.messages))
        .order_by(Conversation.last_message_at.desc())
    ).all()
    out = []
    for c in convs:
        last = c.messages[-1] if c.messages else None
        out.append({
            "id": c.id,
            "channel": c.channel,
            "contact": {"name": c.contact.display_name, "external_id": c.contact.external_id,
                        "is_demo": c.contact.is_demo},
            "last_message": {"body": last.body[:140], "author": last.author, "at": _iso(last.created_at)} if last else None,
            "lead_status": c.lead.status if c.lead else "new",
            "assigned_rep": c.lead.assigned_rep.name if c.lead and c.lead.assigned_rep else None,
            "automation_paused": c.automation_paused,
            "last_message_at": _iso(c.last_message_at),
        })
    return out


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, session: Session = Depends(get_session)):
    c = session.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(404, "Conversation not found")
    bookings = session.scalars(
        select(Booking).where(Booking.conversation_id == c.id).order_by(Booking.created_at)
    ).all()
    offers = session.scalars(
        select(SlotOffer).where(SlotOffer.conversation_id == c.id, SlotOffer.status == "active").order_by(SlotOffer.start)
    ).all()
    log = session.scalars(
        select(ActionLog).where(ActionLog.conversation_id == c.id).order_by(ActionLog.created_at.desc()).limit(100)
    ).all()
    return {
        "id": c.id,
        "channel": c.channel,
        "contact": {"name": c.contact.display_name, "external_id": c.contact.external_id,
                    "is_demo": c.contact.is_demo},
        "automation_paused": c.automation_paused,
        "paused_by": c.paused_by,
        "paused_at": _iso(c.paused_at),
        "messages": [
            {"id": m.id, "direction": m.direction, "author": m.author, "body": m.body,
             "send_status": m.send_status, "error": m.error, "at": _iso(m.created_at)}
            for m in c.messages
        ],
        "lead": _lead_out(c.lead),
        "slot_offers": [
            {"label": o.label, "start": _iso(o.start), "end": _iso(o.end), "rep_id": o.rep_id} for o in offers
        ],
        "bookings": [
            {"id": b.id, "status": b.status, "start": _iso(b.start), "end": _iso(b.end), "rep_id": b.rep_id,
             "calendar_event_id": b.calendar_event_id, "calendar_link": b.calendar_link, "error": b.error}
            for b in bookings
        ],
        "log": [_log_out(e) for e in log],
    }


class TakeoverIn(BaseModel):
    paused: bool


@router.post("/conversations/{conversation_id}/takeover")
def set_takeover(conversation_id: int, body: TakeoverIn, session: Session = Depends(get_session),
                 operator: str = Depends(require_operator)):
    c = session.get(Conversation, conversation_id)
    if c is None:
        raise HTTPException(404, "Conversation not found")
    if c.automation_paused == body.paused:
        return {"automation_paused": c.automation_paused}
    c.automation_paused = body.paused
    c.paused_by = operator if body.paused else ""
    c.paused_at = datetime.now(timezone.utc) if body.paused else None
    log_action(
        session,
        actor=f"human:{operator}",
        action="takeover_on" if body.paused else "takeover_off",
        summary=("Human took over. Automatic replies and bookings are stopped."
                 if body.paused else "Automation resumed by operator."),
        conversation_id=c.id,
        level="warn" if body.paused else "info",
    )
    session.commit()
    return {"automation_paused": c.automation_paused}


# ---------- global log ----------

@router.get("/log")
def global_log(limit: int = 50, session: Session = Depends(get_session)):
    rows = session.scalars(select(ActionLog).order_by(ActionLog.created_at.desc()).limit(min(limit, 200))).all()
    return [_log_out(e) for e in rows]


# ---------- policy ----------

@router.get("/policy")
def read_policy(session: Session = Depends(get_session)):
    row = get_active_policy_row(session)
    return {"version": row.version, "yaml": row.source_yaml, "created_by": row.created_by,
            "created_at": _iso(row.created_at), "policy": row.data}


class PolicyIn(BaseModel):
    yaml: str


@router.put("/policy")
def update_policy(body: PolicyIn, session: Session = Depends(get_session),
                  operator: str = Depends(require_operator)):
    try:
        row = save_policy(session, body.yaml, actor=f"human:{operator}")
    except PolicyError as exc:
        raise HTTPException(422, {"errors": exc.errors})
    return {"version": row.version}


# ---------- connections ----------

_PROVIDERS = {
    "email": ("Gmail inbox", lambda: bool(settings.google_client_id and settings.google_client_secret)),
    "calendar": ("Google Calendar", lambda: bool(settings.google_client_id and settings.google_client_secret)),
    "whatsapp": ("WhatsApp", lambda: bool(settings.whatsapp_token and settings.whatsapp_phone_number_id)),
    "instagram": ("Instagram", lambda: bool(settings.instagram_token)),
    "llm": ("Groq LLM", lambda: bool(settings.groq_api_key)),
}


@router.get("/connections")
def connections(session: Session = Depends(get_session)):
    rows = {r.provider: r for r in session.scalars(select(Connection))}
    out = []
    for key, (label, configured) in _PROVIDERS.items():
        row = rows.get(key)
        if not configured():
            status, err = "not_configured", "Environment variables are missing."
        elif row is None:
            status, err = ("ok" if key == "llm" else "not_connected"), ""
        else:
            status, err = row.status, row.last_error
        out.append({"provider": key, "label": label, "status": status, "error": err,
                    "account": row.account if row else "", "last_checked_at": _iso(row.last_checked_at) if row else None})
    return out
