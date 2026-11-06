"""What happens after an inbound message is stored.

  1. Stop if a human has taken over (checked again right before sending).
  2. LLM extracts facts with quotes; code verifies every quote.
  3. Code applies the policy rules  ->  status + reasons.
  4. Code picks the rep (qualified leads only).
  5. Code decides the next move; the LLM only words it; the draft is checked.
  6. Send on the same channel, or flag the conversation for a person.

If any step fails, nothing is sent automatically and the conversation is
flagged "needs you" with the reason, so failures are never silent.
"""
import threading
from collections import defaultdict

from sqlalchemy.orm import Session

from app.actions import log_action
from app.extraction import extract
from app.guards import AutomationPaused
from app.llm import LLMError
from app.models import Conversation, Lead, LeadStatus, Message
from app.outbound import send_message
from app.policy import get_active_policy
from app.replies import ReplyPlan, compose
from app.routing import choose_rep
from app.rules import decide

_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
_locks_guard = threading.Lock()


def _lock_for(conversation_id: int) -> threading.Lock:
    with _locks_guard:
        return _locks[conversation_id]


def flag(session: Session, conv: Conversation, reason: str, *, level: str = "warn") -> None:
    conv.attention = reason
    log_action(session, actor="system", action="needs_attention", level=level, conversation_id=conv.id,
               summary=f"Needs a person: {reason}")
    session.commit()


def _assistant_messages(conv: Conversation, kind: str | None = None) -> list[Message]:
    return [m for m in conv.messages if m.author == "assistant" and m.send_status == "sent"
            and (kind is None or (m.meta or {}).get("kind") == kind)]


def handle_inbound(session: Session, conv: Conversation, message: Message | None = None) -> None:
    with _lock_for(conv.id):
        _run(session, conv)


def _run(session: Session, conv: Conversation) -> None:
    session.refresh(conv)
    if conv.contact.is_demo:
        return
    if conv.automation_paused:
        log_action(session, actor="system", action="assistant_quiet", conversation_id=conv.id,
                   summary="New message stored. The assistant stayed quiet because a human is in control.")
        session.commit()
        return

    policy, version = get_active_policy(session)
    lead = conv.lead
    if lead is None:
        lead = Lead(conversation_id=conv.id, status=LeadStatus.NEW, fields={}, evidence={}, reasons=[])
        session.add(lead)
    previous_status = lead.status
    # Never hold a transaction open across the (slow) model call: an operator's
    # "Take over" must be able to save while the assistant is thinking.
    session.commit()

    # 2. extract + verify
    try:
        ext = extract(session, policy, list(conv.messages))
    except LLMError as exc:
        session.rollback()
        flag(session, conv, f"The assistant could not read the latest message ({exc.reason}). "
                            "Reply yourself, or use Retry once the problem is fixed.", level="error")
        return
    for note in ext.dropped:
        log_action(session, actor="system", action="evidence_rejected", level="warn", conversation_id=conv.id,
                   summary=note)

    # 3. rules
    decision = decide(policy, ext.fields, ext.notes)
    lead.fields, lead.evidence = ext.fields, ext.evidence
    lead.status, lead.reasons, lead.policy_version = decision.status, decision.reasons, version

    # 4. routing (qualified only; an earlier assignment is kept unless that rep was deactivated)
    if decision.status == LeadStatus.QUALIFIED:
        rep_id, why = choose_rep(session, policy, ext.fields.get("service"), lead.assigned_rep_id)
        if rep_id != lead.assigned_rep_id:
            lead.assigned_rep_id, lead.assignment_reason = rep_id, why
            log_action(session, actor="rules", action="rep_assigned", conversation_id=conv.id,
                       summary=f"Assigned to {policy.rep(rep_id).name}. {why}")

    log_action(session, actor="rules", action="lead_assessed", conversation_id=conv.id,
               summary=(f"Status {decision.status.replace('_', ' ')}"
                        + (f" (was {previous_status.replace('_', ' ')})" if previous_status != decision.status else "")
                        + f": {decision.reasons[-1] if decision.reasons else 'no facts yet'}"),
               detail={"policy_version": version, "fields": ext.fields, "evidence": ext.evidence,
                       "dropped": ext.dropped, "llm_usage": ext.usage, "llm_raw": ext.raw})
    session.commit()

    # 5. next move (code decides)
    plan = _plan(conv, lead, decision, previous_status, policy)
    if plan is None:
        return
    text, info = compose(session, plan, policy)
    if info["source"] == "template":
        log_action(session, actor="system", action="reply_template", conversation_id=conv.id, level="info",
                   summary=f"Used the safe template reply ({info['why']})", detail=info)
        session.commit()

    # 6. send (the takeover guard runs again inside send_message, right before the send)
    try:
        sent = send_message(session, conv, text, author="assistant", actor="assistant",
                            meta={"kind": plan.kind, "asked_for": plan.ask_for, "reply_source": info["source"],
                                  "llm_usage": info.get("usage")})
    except AutomationPaused:
        return
    if sent.send_status == "sent":
        if conv.attention:
            conv.attention = ""
            session.commit()
        if plan.kind == "handoff":
            flag(session, conv, f"The lead still hasn't given everything needed after "
                                f"{policy.conversation.max_follow_up_questions} questions. Please take over.")
    else:
        flag(session, conv, f"The assistant's reply was not delivered ({sent.send_status}: {sent.error})", level="error")


def _plan(conv: Conversation, lead: Lead, decision, previous_status: str, policy) -> ReplyPlan | None:
    service = policy.service(lead.fields.get("service") or "")
    base = dict(channel=conv.channel, lead_name=conv.contact.display_name,
                service_name=service.name if service else "")

    if decision.status == LeadStatus.NOT_QUALIFIED:
        if _assistant_messages(conv, "decline"):
            flag(_session_of(conv), conv, "The lead wrote again after being declined.")
            return None
        return ReplyPlan(kind="decline", decline_reason=decision.failed, **base)

    if decision.status == LeadStatus.QUALIFIED:
        if _assistant_messages(conv, "qualified"):
            flag(_session_of(conv), conv, "The lead replied after being qualified. Booking is handled in a later step; reply yourself for now.")
            return None
        rep = policy.rep(lead.assigned_rep_id)
        return ReplyPlan(kind="qualified", rep_name=rep.name if rep else "", **base)

    # needs info
    asked = len(_assistant_messages(conv, "ask"))
    if asked >= policy.conversation.max_follow_up_questions:
        if _assistant_messages(conv, "handoff"):
            flag(_session_of(conv), conv, "The lead wrote again after being handed to the team.")
            return None
        return ReplyPlan(kind="handoff", **base)
    return ReplyPlan(kind="ask", ask_for=decision.missing[:2], **base)


def _session_of(obj) -> Session:
    from sqlalchemy.orm import object_session

    return object_session(obj)


def retry_latest(session: Session, conv: Conversation, actor: str) -> None:
    """Operator-triggered re-run after a failure (e.g. LLM outage)."""
    log_action(session, actor=actor, action="assistant_retry", conversation_id=conv.id,
               summary="Operator asked the assistant to try again")
    conv.attention = ""
    session.commit()
    handle_inbound(session, conv)
