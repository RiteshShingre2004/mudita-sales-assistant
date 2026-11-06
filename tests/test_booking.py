"""Calendar booking: offers, explicit choice, and the brief's required cases
(repeated confirmation, newly occupied slot). Google is faked."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import google_oauth
from app.booking import parse_choice
from app.channels.gmail import poll_once
from app.gcal import candidate_slots
from app.models import ActionLog, Booking, Conversation, SlotOffer
from app.policy import DEFAULT_POLICY_PATH, parse_policy
from tests.fake_google import FakeGoogle, gmail_message
from tests.fake_groq import facts
from tests.test_gmail import connect

POLICY = parse_policy(DEFAULT_POLICY_PATH.read_text())
QUALIFIED = facts(service=("bi_dashboards", "Power BI dashboards"), budget=(12000, "USD", "budget is $12k"),
                  start=(1, "start next month"), role=("decision_maker", "I'm the founder"))
FIRST = "I'm the founder. We need Power BI dashboards, budget is $12k, start next month."


# ---------- pure: slot generation and choice parsing ----------

def test_slots_respect_hours_days_notice_and_busy_and_spread_across_days():
    now = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)  # Fri 14:30 IST
    busy = [(datetime(2026, 11, 9, 4, 30, tzinfo=timezone.utc), datetime(2026, 11, 9, 6, 0, tzinfo=timezone.utc))]
    slots = candidate_slots(POLICY, now, busy)
    assert len(slots) == 3
    ist = [s.astimezone(timezone(timedelta(hours=5, minutes=30))) for s, _ in slots]
    assert all(t.weekday() < 5 and 10 <= t.hour < 18 for t in ist)          # Mon-Fri, 10-18 IST
    assert all(s >= now + timedelta(hours=4) for s, _ in slots)             # 4 h notice
    assert len({t.date() for t in ist}) == 3                                # one per day
    # Friday is skipped (14:30 + 4 h notice is past 18:00); Mon 10:00-11:30 was busy.
    assert [t.strftime("%a %H:%M") for t in ist] == ["Mon 11:30", "Tue 10:00", "Wed 10:00"]


def test_held_slots_are_not_offered_twice():
    now = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
    first = candidate_slots(POLICY, now, [])
    again = candidate_slots(POLICY, now, [], held=[s for s, _ in first])
    assert not {s for s, _ in first} & {s for s, _ in again}


@pytest.mark.parametrize("text, expected", [
    ("2", "2"), ("Option 3 please", "3"), ("the second one works", "2"), ("#1", "1"),
    ("yes sounds good", None), ("1 or 2 are both fine", None), ("We have 2 offices and 3 teams across India", None),
    ("none of these work for me", "none_work"), ("5", None),
])
def test_choice_parsing_never_guesses(text, expected):
    assert parse_choice(text, ["1", "2", "3"]) == expected


# ---------- end to end through email ----------

@pytest.fixture()
def google(session, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(google_oauth, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    connect(session, fake)
    return fake


def say(session, google, mid, body):
    google.deliver(gmail_message(mid, "Anika Rao <anika@example.com>", body, thread="t-book",
                                 ts=1_700_000_000_000 + len(google.history)))
    poll_once(session)
    session.expire_all()
    return session.query(Conversation).one()


def last_reply(conv):
    return [m for m in conv.messages if m.author == "assistant"][-1]


@pytest.fixture()
def offered(session, google, groq):
    groq.down = False
    groq.extract = QUALIFIED
    groq.writer = "Hi Anika,\n\nThanks! You look like a great fit, and Priya Nair will be your contact."
    conv = say(session, google, "b1", FIRST)
    return conv


def active(session, conv):
    return session.query(SlotOffer).filter_by(conversation_id=conv.id, status="active").order_by(SlotOffer.label).all()


def test_qualified_lead_gets_three_real_slots(session, google, offered):
    conv = offered
    reply = last_reply(conv)
    assert reply.meta["kind"] == "qualified" and "Priya Nair" in reply.body
    assert "Reply with 1, 2 or 3 to confirm a time." in reply.body
    assert len(active(session, conv)) == 3 and google.inserts == []


def test_REQUIRED_full_run_books_a_confirmed_test_calendar_event(session, google, offered):
    """REQUIRED 1 (booking half): qualified -> routed -> confirmed calendar booking."""
    conv = say(session, google, "b2", "2")
    booking = session.query(Booking).one()
    assert booking.status == "confirmed" and booking.calendar_event_id in google.events
    event = google.events[booking.calendar_event_id]
    assert "Priya Nair" in event["description"] and event["start"]["timeZone"] == "Asia/Kolkata"
    assert last_reply(conv).meta["kind"] == "booking_confirmed" and "Confirmed" in last_reply(conv).body
    assert active(session, conv) == []


def test_REQUIRED_repeated_confirmation_creates_no_duplicate(session, google, offered):
    """REQUIRED 3a: confirming twice gives one booking and one event."""
    say(session, google, "r1", "2")
    conv = say(session, google, "r2", "2, confirmed!")
    assert len(google.events) == 1 and len(google.inserts) == 1
    assert session.query(Booking).count() == 1
    assert last_reply(conv).meta["kind"] == "booking_reminder"


def test_REQUIRED_newly_occupied_slot_is_not_booked(session, google, offered):
    """REQUIRED 3b: the chosen slot gets taken after it was offered -> no conflicting booking,
    the lead gets fresh times."""
    slot2 = active(session, offered)[1]

    def someone_else_books_it(fake):
        fake.busy.append((slot2.start.replace(tzinfo=timezone.utc), slot2.end.replace(tzinfo=timezone.utc)))

    google.busy_hook = someone_else_books_it
    conv = say(session, google, "o1", "2")
    assert google.inserts == [] and session.query(Booking).count() == 0
    reply = last_reply(conv)
    assert reply.meta["kind"] == "slots" and "was just taken" in reply.body
    new = active(session, conv)
    assert len(new) == 3 and all(o.start != slot2.start for o in new)
    assert session.query(ActionLog).filter_by(action="slot_taken").count() == 1


def test_vague_yes_asks_which_slot_and_books_nothing(session, google, offered, groq):
    conv = say(session, google, "v1", "yes sounds good")
    assert google.inserts == [] and last_reply(conv).meta["kind"] == "pick_slot"


def test_model_can_read_a_natural_choice_but_only_with_a_quote(session, google, offered, groq):
    groq.extract = {**QUALIFIED, "slot_choice": {"value": "3", "quote": "the last time works for me"}}
    conv = say(session, google, "n1", "Hmm, the last time works for me.")
    assert session.query(Booking).one().status == "confirmed"
    groq.extract = {**QUALIFIED, "slot_choice": {"value": "1", "quote": "invented words"}}
    assert last_reply(conv).meta["kind"] == "booking_confirmed"


def test_none_work_offers_different_times(session, google, offered):
    first = {o.start for o in active(session, offered)}
    conv = say(session, google, "nw", "none of these work, sorry")
    assert last_reply(conv).meta["kind"] == "slots"
    assert not first & {o.start for o in active(session, conv)}


def test_timeout_after_create_is_reconciled_without_a_second_event(session, google, offered):
    google.insert_timeouts, google.timeout_after_create = 1, True
    conv = say(session, google, "t1", "1")
    booking = session.query(Booking).one()
    assert booking.status == "confirmed" and len(google.inserts) == 1     # looked up, not re-created
    assert session.query(ActionLog).filter_by(action="booking_uncertain").count() == 1
    assert last_reply(conv).meta["kind"] == "booking_confirmed"


def test_timeout_before_create_is_created_once_after_checking(session, google, offered):
    google.insert_timeouts, google.timeout_after_create = 1, False
    say(session, google, "t2", "1")
    booking = session.query(Booking).one()
    assert booking.status == "confirmed" and len(google.events) == 1
    assert google.inserts == [booking.idempotency_key] * 2                 # same id both times


def test_takeover_before_booking_stops_it(session, google, offered):
    conv = offered
    conv.automation_paused = True
    session.commit()
    say(session, google, "tk", "1")
    assert google.inserts == [] and session.query(Booking).count() == 0


def test_calendar_disconnected_still_replies_and_flags(session, google, groq):
    groq.down = False
    groq.extract = QUALIFIED
    groq.writer = "Hi Anika,\n\nThanks! You look like a great fit, and Priya Nair will be your contact."
    google.busy_hook = lambda fake: (_ for _ in ()).throw(httpx.ConnectError("calendar unreachable"))
    google.deliver(gmail_message("cd1", "Anika Rao <anika@example.com>", FIRST, thread="t-cd"))
    poll_once(session)
    session.expire_all()
    conv = session.query(Conversation).one()
    assert "follow up shortly with times" in last_reply(conv).body
    assert "calendar could not be read" in conv.attention


def test_lead_qualified_before_times_were_offered_gets_times_on_next_message(session, google, groq):
    """Seen live: a lead qualified while the calendar was unavailable (or before booking existed)
    must get times when they write again, not be left waiting."""
    groq.down = False
    groq.extract = QUALIFIED
    groq.writer = "Hi Anika,\n\nThanks! You look like a great fit, and Priya Nair will be your contact."
    google.busy_hook = lambda fake: (_ for _ in ()).throw(httpx.ConnectError("calendar down"))
    conv = say(session, google, "late1", FIRST)
    assert "follow up shortly with times" in last_reply(conv).body
    google.busy_hook = None
    conv = say(session, google, "late2", "Any update on the call?")
    reply = last_reply(conv)
    assert reply.meta["kind"] == "slots" and "Reply with 1, 2 or 3" in reply.body
    conv = say(session, google, "late3", "2")
    assert session.query(Booking).one().status == "confirmed"
