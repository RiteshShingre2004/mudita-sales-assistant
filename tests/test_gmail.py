import base64
import email
import time

import httpx
import pytest

from app import google_oauth
from app.channels.gmail import poll_once, strip_quoted_reply
from app.guards import AutomationPaused
from app.models import ActionLog, Contact, Conversation, Message
from app.outbound import send_message
from tests.fake_google import FakeGoogle, gmail_message


@pytest.fixture()
def google(session, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(google_oauth, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


def connect(session, fake):
    row = google_oauth.get_row(session)
    row.credentials = {"access_token": "at", "refresh_token": "rt", "expires_at": time.time() + 3600}
    row.account = "desk@lumen-test.example"
    row.state = {"gmail_history_id": str(fake.history_id)}
    row.status = "ok"
    session.commit()


# ---------- OAuth ----------

def test_oauth_callback_rejects_unknown_state(session, google):
    google_oauth.start_url(session)
    with pytest.raises(google_oauth.GoogleAPIError):
        google_oauth.handle_callback(session, "code", "forged-state", actor="human:t")


def test_oauth_callback_stores_tokens_and_starts_cursor(session, google):
    url = google_oauth.start_url(session)
    state = dict(p.split("=") for p in url.split("?")[1].split("&"))["state"]
    account = google_oauth.handle_callback(session, "code", state, actor="human:t")
    row = google_oauth.get_row(session)
    assert account == "desk@lumen-test.example"
    assert row.credentials["refresh_token"] == "rt-1" and row.status == "ok"
    assert row.state["gmail_history_id"] == str(google.history_id)


# ---------- polling ----------

def test_new_inquiry_is_stored_once(session, google):
    connect(session, google)
    google.deliver(gmail_message("m1", "Anika Rao <Anika@Example.com>", "We need dashboards.", subject="Dashboards"))
    assert poll_once(session)["new"] == 1
    assert poll_once(session)["new"] == 0  # re-poll: nothing new

    contact = session.query(Contact).one()
    assert contact.external_id == "anika@example.com" and contact.display_name == "Anika Rao"
    conv = session.query(Conversation).one()
    assert conv.thread_ref == "t-m1" and conv.subject == "Dashboards"
    assert session.query(Message).count() == 1


def test_follow_up_lands_in_same_conversation(session, google):
    connect(session, google)
    google.deliver(gmail_message("m1", "a@example.com", "Hello", ts=1))
    poll_once(session)
    google.deliver(gmail_message("m2", "a@example.com", "Budget is 10k", ts=2))
    poll_once(session)
    assert session.query(Conversation).count() == 1
    assert [m.body for m in session.query(Conversation).one().messages] == ["Hello", "Budget is 10k"]


@pytest.mark.parametrize("msg, reason", [
    (gmail_message("a1", "x@example.com", "Out of office", headers={"Auto-Submitted": "auto-replied"}), "auto-reply"),
    (gmail_message("a2", "news@shop.example", "Sale!", headers={"List-Unsubscribe": "<mailto:u@shop.example>"}), "mailing list"),
    (gmail_message("a3", "desk@lumen-test.example", "our own reply", labels=("SENT", "INBOX")), "sent by this inbox"),
    (gmail_message("a4", "no-reply@service.example", "Your receipt"), "automated sender"),
])
def test_robots_and_own_mail_are_ignored(session, google, msg, reason):
    connect(session, google)
    google.deliver(msg)
    assert poll_once(session)["skipped"] == 1
    assert session.query(Message).count() == 0
    assert reason in session.query(ActionLog).filter_by(action="email_skipped").one().summary


def test_failed_fetch_does_not_advance_cursor_and_recovers_without_duplicates(session, google):
    connect(session, google)
    start = google_oauth.get_row(session).state["gmail_history_id"]
    google.deliver(gmail_message("ok1", "a@example.com", "first", ts=1))
    google.deliver(gmail_message("bad", "b@example.com", "second", ts=2))
    google.fail_get_for = {"bad"}
    with pytest.raises(google_oauth.GoogleAPIError):
        poll_once(session)
    session.rollback()
    assert google_oauth.get_row(session).state["gmail_history_id"] == start  # cursor unchanged

    google.fail_get_for = set()
    result = poll_once(session)
    assert result["new"] == 2 and session.query(Message).count() == 2


def test_stale_cursor_falls_back_to_recent_inbox(session, google):
    connect(session, google)
    google.deliver(gmail_message("m1", "a@example.com", "hi"))
    google.history_404 = True
    assert poll_once(session)["new"] == 1
    assert session.query(ActionLog).filter_by(action="gmail_cursor_reset").count() == 1


def test_expired_sign_in_is_visible(session, google, client):
    connect(session, google)
    row = google_oauth.get_row(session)
    row.credentials = {**row.credentials, "expires_at": 0}  # force a refresh
    session.commit()
    google.refresh_fails = True
    with pytest.raises(google_oauth.GoogleNotConnected):
        poll_once(session)
    status = {c["provider"]: c for c in client.get("/api/connections").json()}["email"]
    assert status["status"] == "expired" and "Reconnect" in status["error"]


def test_quoted_reply_is_stripped():
    text = "Yes, 15k budget.\n\nOn Thu, 5 Nov 2026 at 10:00, Lumen <desk@x.example>\nwrote:\n> earlier text"
    assert strip_quoted_reply(text) == "Yes, 15k budget."
    assert strip_quoted_reply("Thanks\n> quoted\nOk") == "Thanks\nOk"


# ---------- sending ----------

def _inbound_conv(session, google):
    connect(session, google)
    google.deliver(gmail_message("m1", "a@example.com", "Hello", subject="Need help"))
    poll_once(session)
    return session.query(Conversation).one()


def test_operator_reply_is_threaded(session, google):
    conv = _inbound_conv(session, google)
    msg = send_message(session, conv, "Thanks, what is your budget?", author="human", actor="human:t")
    assert msg.send_status == "sent" and msg.channel_message_id == "sent-1"

    sent = google.sent[0]
    assert sent["threadId"] == "t-m1"
    mime = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"]))
    assert mime["To"] == "a@example.com"
    assert mime["Subject"] == "Re: Need help"
    assert mime["In-Reply-To"] == "<m1@mail.example>"


def test_send_failure_is_recorded_not_hidden(session, google):
    conv = _inbound_conv(session, google)
    google_oauth.disconnect(session, actor="human:t")
    msg = send_message(session, conv, "hello?", author="human", actor="human:t")
    assert msg.send_status == "failed" and "not connected" in msg.error
    assert session.query(ActionLog).filter_by(action="message_failed").count() == 1


def test_assistant_cannot_send_during_takeover(session, google):
    conv = _inbound_conv(session, google)
    conv.automation_paused = True
    session.commit()
    with pytest.raises(AutomationPaused):
        send_message(session, conv, "auto reply", author="assistant", actor="assistant")
    assert google.sent == []


# ---------- personal mailbox safety (EMAIL_INBOX_ADDRESS) ----------

@pytest.fixture()
def plus_address(monkeypatch):
    import dataclasses

    from app.channels import gmail

    monkeypatch.setattr(gmail, "settings", dataclasses.replace(gmail.settings, email_inbox_address="me+lumen@gmail.com"))


def test_personal_mail_is_ignored_unread_and_unlogged(session, google, plus_address):
    connect(session, google)
    google.deliver(gmail_message("p1", "bank@bank.example", "Your statement", to="me@gmail.com"))
    result = poll_once(session)
    assert result["new"] == 0 and result["ignored"] == 1
    assert session.query(Message).count() == 0
    assert session.query(ActionLog).filter(ActionLog.action.in_(["email_skipped", "message_received"])).count() == 0
    assert "get p1 full" not in google.calls  # body never downloaded


def test_mail_to_assistant_address_and_thread_replies_are_handled(session, google, plus_address):
    connect(session, google)
    google.deliver(gmail_message("q1", "lead@example.com", "Need dashboards", to="Lumen <me+lumen@gmail.com>", ts=1))
    poll_once(session)
    # The lead replies to the plain address, but in the same thread: still handled.
    google.deliver(gmail_message("q2", "lead@example.com", "Budget 10k", to="me@gmail.com", thread="t-q1", ts=2))
    poll_once(session)
    assert [m.body for m in session.query(Conversation).one().messages] == ["Need dashboards", "Budget 10k"]


def test_replies_ask_leads_to_answer_the_assistant_address(session, google, plus_address):
    connect(session, google)
    google.deliver(gmail_message("r1", "lead@example.com", "Hi", to="me+lumen@gmail.com"))
    poll_once(session)
    send_message(session, session.query(Conversation).one(), "Hello!", author="human", actor="human:t")
    mime = email.message_from_bytes(base64.urlsafe_b64decode(google.sent[0]["raw"]))
    assert mime["Reply-To"] == "me+lumen@gmail.com"
