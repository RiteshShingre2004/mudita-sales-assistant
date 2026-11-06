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
import re
import threading

import httpx
from collections import defaultdict

from sqlalchemy.orm import Session

from app.actions import log_action
from app.booking import (CalendarUnavailable, active_offers, book, confirmed_booking, offer_slots, open_booking,
                         parse_choice, slot_list_text)
from app.extraction import extract
from app.gcal import format_slot
from app.google_oauth import GoogleAPIError, GoogleNotConnected
from app.guards import AutomationPaused
from app.llm import LLMError
from app.models import Conversation, Lead, LeadStatus, Message, SlotOffer
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

    offers = active_offers(session, conv)
    labels = [o.label for o in offers]

    # 2. extract + verify
    try:
        ext = extract(session, policy, list(conv.messages), slot_labels=labels or None)
    except LLMError as exc:
        session.rollback()
        flag(session, conv, f"The assistant could not read the latest message ({exc.reason}). "
                            "Reply yourself, or use Retry once the problem is fixed.", level="error")
        return
    for note in ext.dropped:
        log_action(session, actor="system", action="evidence_rejected", level="warn", conversation_id=conv.id,
                   summary=note)

    slot_choice = ext.fields.pop("slot_choice", None)
    ext.evidence.pop("slot_choice", None)

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
    if decision.status == LeadStatus.QUALIFIED and lead.assigned_rep_id:
        latest = next((m for m in reversed(conv.messages) if m.direction == "in"), None)
        choice = parse_choice(latest.body if latest else "", labels) or slot_choice
        _booking_flow(session, conv, lead, policy, offers, choice, latest.body if latest else "")
        return
    for o in offers:  # no longer qualified: withdraw offered times
        o.status = "superseded"
    session.commit()
    plan = _plan(conv, lead, decision, previous_status, policy)
    if plan is None:
        return
    text, info = compose(session, plan, policy)
    if info["source"] == "template":
        log_action(session, actor="system", action="reply_template", conversation_id=conv.id, level="info",
                   summary=f"Used the safe template reply ({info['why']})", detail=info)
        session.commit()

    # 6. send (the takeover guard runs again inside send_message, right before the send)
    sent = _send(session, conv, text, {"kind": plan.kind, "asked_for": plan.ask_for, "reply_source": info["source"],
                                       "llm_usage": info.get("usage")})
    if sent and plan.kind == "handoff":
        flag(session, conv, f"The lead still hasn't given everything needed after "
                            f"{policy.conversation.max_follow_up_questions} questions. Please take over.")


def _send(session: Session, conv: Conversation, text: str, meta: dict) -> bool:
    """Send an automatic message; returns True if delivered to the channel."""
    try:
        sent = send_message(session, conv, text, author="assistant", actor="assistant", meta=meta)
    except AutomationPaused:
        return False
    if sent.send_status == "sent":
        if conv.attention:
            conv.attention = ""
            session.commit()
        return True
    flag(session, conv, f"The assistant's reply was not delivered ({sent.send_status}: {sent.error})", level="error")
    return False


def _sign(conv: Conversation, policy) -> str:
    return f"\n\n{policy.company_name}" if conv.channel == "email" else ""


_AFFIRM = re.compile(r"\b(yes|yep|yeah|ok|okay|confirm(ed)?|sounds good|great|perfect|works|thanks|thank you)\b", re.I)


def _booking_flow(session: Session, conv: Conversation, lead: Lead, policy, offers: list[SlotOffer],
                  choice: str | None, latest_text: str = "") -> None:
    """Qualified lead: offer times, read the choice, book exactly once."""
    rep = policy.rep(lead.assigned_rep_id)
    rep_name = rep.name if rep else "our team"
    sign = _sign(conv, policy)

    booked = confirmed_booking(session, conv)
    if booked:
        reconfirm = parse_choice(latest_text, [str(i) for i in range(1, 10)]) not in (None, "none_work") \
            or bool(_AFFIRM.search(latest_text))
        if reconfirm:   # e.g. the lead confirms again: remind, never book again
            _send(session, conv, f"You're all set: your call with {rep_name} is on "
                                 f"{format_slot(booked.start, policy)}. Nothing else needed.{sign}",
                  {"kind": "booking_reminder"})
        else:
            flag(session, conv, "The lead wrote again after the meeting was booked.")
        return

    pending = open_booking(session, conv)
    if pending:  # an earlier attempt's outcome is unknown: settle it first, never book twice
        offer = SlotOffer(conversation_id=conv.id, rep_id=pending.rep_id, label="-", start=pending.start, end=pending.end)
        _do_book(session, conv, offer, policy, rep_name)
        return

    if not offers:
        _first_offer(session, conv, lead, policy, rep_name)
        return

    if choice and choice != "none_work":
        offer = next((o for o in offers if o.label == choice), None)
        if offer:
            _do_book(session, conv, offer, policy, rep_name)
            return

    if choice == "none_work":
        rounds = len(_assistant_messages(conv, "slots"))
        if rounds >= 2:
            flag(session, conv, "None of the offered times suit the lead after several tries. Please arrange a time.")
            return
        _reoffer(session, conv, lead, policy, "No problem, here are some other times.")
        return

    # Unclear reply: never guess a slot.
    if len(_assistant_messages(conv, "pick_slot")) >= 2:
        flag(session, conv, "The lead hasn't picked one of the offered times. Please follow up.")
        return
    _send(session, conv, "Which of these times works best for you?\n\n" + slot_list_text(offers, policy) + sign,
          {"kind": "pick_slot"})


def _first_offer(session: Session, conv: Conversation, lead: Lead, policy, rep_name: str) -> None:
    service = policy.service(lead.fields.get("service") or "")
    plan = ReplyPlan(kind="qualified", channel=conv.channel, lead_name=conv.contact.display_name,
                     rep_name=rep_name, service_name=service.name if service else "")
    if _assistant_messages(conv, "qualified"):
        # Already introduced (e.g. qualified before booking existed, or the calendar was down then):
        # offer times now rather than leaving the lead waiting.
        _reoffer(session, conv, lead, policy, f"Here are times for your call with {rep_name}.")
        return
    intro, info = compose(session, plan, policy)
    if info["source"] == "template":
        log_action(session, actor="system", action="reply_template", conversation_id=conv.id,
                   summary=f"Used the safe template reply ({info['why']})", detail=info)
        session.commit()
    try:
        new = offer_slots(session, conv, lead, policy)
    except CalendarUnavailable as exc:
        _send(session, conv, intro + "\n\nWe'll follow up shortly with times for a call." + _sign(conv, policy),
              {"kind": "qualified", "reply_source": info["source"]})
        flag(session, conv, f"Qualified, but the calendar could not be read ({exc}). Offer times yourself or fix the "
                            "Google connection and use Retry.", level="error")
        return
    if not new:
        _send(session, conv, intro + "\n\nWe'll follow up shortly with times for a call." + _sign(conv, policy),
              {"kind": "qualified", "reply_source": info["source"]})
        flag(session, conv, "Qualified, but there are no free times in the booking window. Please offer times.")
        return
    _send(session, conv, intro + "\n\n" + slot_list_text(new, policy) + _sign(conv, policy),
          {"kind": "qualified", "reply_source": info["source"], "llm_usage": info.get("usage")})


def _reoffer(session: Session, conv: Conversation, lead: Lead, policy, opening: str) -> None:
    try:
        new = offer_slots(session, conv, lead, policy)
    except CalendarUnavailable as exc:
        flag(session, conv, f"Could not read the calendar to offer new times ({exc}).", level="error")
        return
    if not new:
        flag(session, conv, "No other free times in the booking window. Please arrange a time.")
        return
    _send(session, conv, opening + "\n\n" + slot_list_text(new, policy) + _sign(conv, policy), {"kind": "slots"})


def _do_book(session: Session, conv: Conversation, offer: SlotOffer, policy, rep_name: str) -> None:
    try:
        outcome, booking = book(session, conv, offer, policy)
    except AutomationPaused:
        return
    except (GoogleAPIError, GoogleNotConnected, httpx.HTTPError) as exc:
        flag(session, conv, f"Could not check the calendar before booking ({exc or exc.__class__.__name__}). "
                            "Nothing was booked.", level="error")
        return
    when = format_slot(offer.start, policy)
    if outcome in ("confirmed", "already_booked"):
        when = format_slot(booking.start, policy)
        text = (f"Confirmed: your {policy.meeting.duration_minutes}-minute call with {rep_name} is booked for "
                f"{when}. {rep_name.split()[0]} will be in touch with the details." if outcome == "confirmed"
                else f"You're all set: your call with {rep_name} is on {when}. Nothing else needed.")
        _send(session, conv, text + _sign(conv, policy),
              {"kind": "booking_confirmed" if outcome == "confirmed" else "booking_reminder"})
    elif outcome == "slot_taken":
        _reoffer(session, conv, conv.lead, policy, f"Sorry, {when} was just taken.")
    elif outcome == "uncertain":
        flag(session, conv, f"Booking for {when} is uncertain: the calendar did not answer in time. "
                            "Use Retry to check it; nothing will be booked twice.", level="error")
    else:
        flag(session, conv, f"Booking for {when} failed: {booking.error if booking else 'unknown error'}. "
                            "Nothing was booked.", level="error")


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
            flag(_session_of(conv), conv, "The lead replied after being qualified.")
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
