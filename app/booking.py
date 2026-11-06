"""Offering slots and booking a meeting, without duplicates or conflicts.

Guarantees:
- Slots are real free times, listed by code (the LLM never writes times).
- A booking happens only after the lead picks one specific offered slot.
- Availability is re-checked immediately before booking; if the slot was taken
  in the meantime, fresh slots are offered instead (no conflicting booking).
- The calendar event id is derived from (conversation, slot), and a booking
  row is unique on it. A repeated confirmation finds the existing booking;
  even if two requests raced, Google rejects the second event with 409.
- If the create call times out, the booking is "uncertain": we look the event
  up by its id before doing anything else, and only create again once we
  have confirmed it is not there.
- The takeover guard runs right before the booking and before every message.
"""
import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.actions import log_action
from app.config import settings
import httpx

from app.gcal import (NOT_SENT_ERRORS, TIMEOUT_ERRORS, busy_intervals, candidate_slots, format_slot, get_event, insert_event,
                      is_free)
from app.google_oauth import GoogleAPIError, GoogleNotConnected
from app.guards import require_automation_allowed
from app.models import Booking, BookingStatus, Contact, Conversation, Lead, SlotOffer
from app.policy import Policy


class CalendarUnavailable(Exception):
    pass


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def active_offers(session: Session, conv: Conversation) -> list[SlotOffer]:
    return list(session.scalars(select(SlotOffer).where(SlotOffer.conversation_id == conv.id,
                                                        SlotOffer.status == "active").order_by(SlotOffer.label)))


def confirmed_booking(session: Session, conv: Conversation) -> Booking | None:
    return session.scalar(select(Booking).where(Booking.conversation_id == conv.id,
                                                Booking.status == BookingStatus.CONFIRMED))


def open_booking(session: Session, conv: Conversation) -> Booking | None:
    """A booking whose outcome is not settled yet (pending or uncertain)."""
    return session.scalar(select(Booking).where(Booking.conversation_id == conv.id,
                                                Booking.status.in_([BookingStatus.PENDING, BookingStatus.UNCERTAIN])))


# ---------- offering ----------

def offer_slots(session: Session, conv: Conversation, lead: Lead, policy: Policy,
                now: datetime | None = None) -> list[SlotOffer]:
    now = now or datetime.now(timezone.utc)
    try:
        busy = busy_intervals(session, now, now + timedelta(days=policy.meeting.horizon_days + 1))
    except (GoogleAPIError, GoogleNotConnected, httpx.HTTPError) as exc:
        raise CalendarUnavailable(str(exc) or exc.__class__.__name__)
    # Never offer a time that is on offer to someone else, or one this lead already saw.
    held = [o.start for o in session.scalars(select(SlotOffer).where(
        (SlotOffer.status == "active") | (SlotOffer.conversation_id == conv.id)))]
    slots = candidate_slots(policy, now, busy, held)
    for old in active_offers(session, conv):
        old.status = "superseded"
    offers = [SlotOffer(conversation_id=conv.id, rep_id=lead.assigned_rep_id, label=str(i), start=s, end=e)
              for i, (s, e) in enumerate(slots, start=1)]
    session.add_all(offers)
    log_action(session, actor="calendar", action="slots_offered", conversation_id=conv.id,
               summary=("Offered " + "; ".join(f"{o.label}) {format_slot(o.start, policy)}" for o in offers))
               if offers else "No free slots in the booking window",
               detail={"slots": [o.start.isoformat() for o in offers]})
    session.commit()
    return offers


def slot_list_text(offers: list[SlotOffer], policy: Policy) -> str:
    lines = [f"{o.label}) {format_slot(o.start, policy)}" for o in offers]
    n = len(offers)
    pick = "1" if n == 1 else ", ".join(str(i) for i in range(1, n)) + f" or {n}"
    return ("Here are the next available times for a "
            f"{policy.meeting.duration_minutes}-minute call:\n" + "\n".join(lines) +
            f"\n\nReply with {pick} to confirm a time.")


# ---------- reading the lead's choice ----------

_ORDINALS = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5", "1st": "1", "2nd": "2",
             "3rd": "3"}
_NONE = re.compile(r"\b(none|neither|no(?:ne)? of (?:these|them|those)|doesn'?t work|don'?t work|not available|"
                   r"other times?|different time|another time)\b", re.I)


def parse_choice(text: str, labels: list[str]) -> str | None:
    """Deterministic read of a reply such as '2', 'option 2', 'the second one', '#3 please'.
    Returns a label, 'none_work', or None when the reply does not clearly pick one slot."""
    t = text.strip().lower()
    t = re.sub(r"[^\w\s#']", " ", t)
    words = t.split()
    if _NONE.search(text):
        return "none_work"
    numbers = re.findall(r"(?:^|\s|#)([1-9])(?=\s|$)", " " + t + " ")
    ords = [_ORDINALS[w] for w in words if w in _ORDINALS]
    picks = set(numbers) | set(ords)
    if len(picks) == 1:
        (pick,) = picks
        # Only short replies count; "I have 2 offices and 3 teams" is not a choice.
        if pick in labels and len(words) <= 8:
            return pick
    return None


# ---------- booking ----------

def idempotency_key(conv: Conversation, start: datetime) -> str:
    # Google event ids allow a-v and 0-9; hex digits are a safe subset.
    digest = hashlib.sha256(f"{conv.id}|{_aware(start).isoformat()}".encode()).hexdigest()[:40]
    return "lm" + digest


def _event_body(conv: Conversation, offer: SlotOffer, policy: Policy, key: str) -> dict:
    contact: Contact = conv.contact
    rep = policy.rep(offer.rep_id)
    tz = policy.working_hours.timezone
    return {
        "id": key,
        "summary": f"{policy.meeting.title}: {contact.display_name}",
        "description": (f"Lead: {contact.display_name} ({conv.channel}: {contact.external_id})\n"
                        f"Rep: {rep.name if rep else offer.rep_id} <{rep.email if rep else ''}>\n"
                        f"Conversation: {settings.public_base_url.rstrip('/')}/#c{conv.id}\n"
                        "Booked automatically by the Lumen sales assistant prototype (test calendar)."),
        "start": {"dateTime": _aware(offer.start).isoformat(), "timeZone": tz},
        "end": {"dateTime": _aware(offer.end).isoformat(), "timeZone": tz},
        "extendedProperties": {"private": {"lumen_conversation": str(conv.id), "lumen_key": key}},
    }


def _confirm(session: Session, booking: Booking, event: dict | None, how: str) -> Booking:
    booking.status = BookingStatus.CONFIRMED
    booking.calendar_event_id = booking.idempotency_key
    booking.calendar_link = (event or {}).get("htmlLink", booking.calendar_link)
    booking.error = ""
    for o in session.scalars(select(SlotOffer).where(SlotOffer.conversation_id == booking.conversation_id,
                                                     SlotOffer.status == "active")):
        o.status = "booked" if _aware(o.start) == _aware(booking.start) else "superseded"
    log_action(session, actor="calendar", action="booking_confirmed", conversation_id=booking.conversation_id,
               summary=f"Meeting booked ({how})", detail={"event_id": booking.idempotency_key})
    session.commit()
    return booking


def reconcile(session: Session, booking: Booking, conv: Conversation, policy: Policy) -> Booking:
    """Settle a pending/uncertain booking by looking the event up, never by blind re-creation."""
    event = get_event(session, booking.idempotency_key)
    if event is not None:
        return _confirm(session, booking, event, "found the event after an uncertain create")
    # Verified absent: creating with the same id is now safe (and Google would still reject a duplicate).
    offer = SlotOffer(conversation_id=conv.id, rep_id=booking.rep_id, label="-", start=booking.start, end=booking.end)
    outcome, event = insert_event(session, _event_body(conv, offer, policy, booking.idempotency_key))
    if outcome == "exists":
        event = get_event(session, booking.idempotency_key)
    return _confirm(session, booking, event, "created after confirming the first attempt had not gone through")


def book(session: Session, conv: Conversation, offer: SlotOffer, policy: Policy) -> tuple[str, Booking | None]:
    """Returns (outcome, booking). Outcomes: confirmed, already_booked, slot_taken, uncertain, failed."""
    key = idempotency_key(conv, offer.start)

    existing = session.scalar(select(Booking).where(Booking.idempotency_key == key))
    if existing and existing.status == BookingStatus.CONFIRMED:
        log_action(session, actor="calendar", action="booking_repeat", conversation_id=conv.id,
                   summary="Repeated confirmation: this slot is already booked, nothing new created")
        session.commit()
        return "already_booked", existing
    other = confirmed_booking(session, conv)
    if other:
        return "already_booked", other

    settled = existing is not None and existing.status in (BookingStatus.PENDING, BookingStatus.UNCERTAIN)
    # Re-check availability right before booking (skipped only while settling our own
    # earlier attempt, whose event would itself show up as busy).
    if not settled and not is_free(session, offer.start, offer.end):
        offer.status = "taken"
        log_action(session, actor="calendar", action="slot_taken", conversation_id=conv.id, level="warn",
                   summary=f"Slot {format_slot(offer.start, policy)} was taken since it was offered; not booked")
        session.commit()
        return "slot_taken", None

    require_automation_allowed(session, conv.id, "book_meeting")

    booking = existing
    if booking is None:
        booking = Booking(conversation_id=conv.id, rep_id=offer.rep_id, idempotency_key=key,
                          start=offer.start, end=offer.end, status=BookingStatus.PENDING)
        session.add(booking)
        try:
            session.commit()   # the row exists before the provider call, so a crash leaves a trace
        except IntegrityError:  # a concurrent confirmation got here first
            session.rollback()
            return "already_booked", session.scalar(select(Booking).where(Booking.idempotency_key == key))
    elif settled:
        try:
            return "confirmed", reconcile(session, booking, conv, policy)
        except TIMEOUT_ERRORS:
            return "uncertain", booking
    else:  # an earlier attempt failed outright; nothing was created, so try again on the same row
        booking.status, booking.error = BookingStatus.PENDING, ""
        session.commit()

    try:
        outcome, event = insert_event(session, _event_body(conv, offer, policy, key))
    except TIMEOUT_ERRORS:
        booking.status = BookingStatus.UNCERTAIN
        booking.error = "Google Calendar did not answer in time; checking whether the event was created."
        log_action(session, actor="calendar", action="booking_uncertain", conversation_id=conv.id, level="warn",
                   summary="Calendar create timed out; looking the event up before doing anything else")
        session.commit()
        try:
            return "confirmed", reconcile(session, booking, conv, policy)
        except TIMEOUT_ERRORS:
            return "uncertain", booking
        except (GoogleAPIError, GoogleNotConnected) as exc:
            booking.error = f"Could not confirm whether the meeting was created: {exc}"
            session.commit()
            return "uncertain", booking
    except (GoogleAPIError, GoogleNotConnected, *NOT_SENT_ERRORS) as exc:
        booking.status, booking.error = BookingStatus.FAILED, str(exc) or exc.__class__.__name__
        log_action(session, actor="calendar", action="booking_failed", conversation_id=conv.id, level="error",
                   summary=f"Calendar booking failed: {exc}")
        session.commit()
        return "failed", booking

    if outcome == "exists":
        event = get_event(session, key)
        return "confirmed", _confirm(session, booking, event, "event already existed with this id")
    return "confirmed", _confirm(session, booking, event, "event created")
