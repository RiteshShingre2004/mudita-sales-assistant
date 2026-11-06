"""Instagram DMs (Instagram API with Instagram Login). graph.instagram.com is faked."""
import hashlib
import hmac
import json

import httpx
import pytest

from app import meta
from app.channels.instagram import _trim_bytes
from app.models import ActionLog, Connection, Contact, Conversation, Message
from tests.fake_groq import facts

OWN_ID = "17841400000000001"
LEAD = "7712345678901234"


class FakeInstagram:
    def __init__(self):
        self.sent: list[dict] = []
        self.subscribed: list[dict] = []
        self.send_error: dict | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "graph.instagram.com"
        assert request.headers["authorization"] == "Bearer test-ig-token"
        path = request.url.path
        if path == "/v23.0/me" and request.method == "GET":
            return httpx.Response(200, json={"user_id": OWN_ID, "username": "lumen.test", "id": "app-scoped-1"})
        if path == "/v23.0/me/subscribed_apps":
            self.subscribed.append(dict(request.url.params))
            return httpx.Response(200, json={"success": True})
        if path == "/v23.0/me/messages":
            if self.send_error:
                return httpx.Response(400, json={"error": self.send_error})
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(200, json={"recipient_id": body["recipient"]["id"], "message_id": f"m_out{len(self.sent)}"})
        if path == f"/v23.0/{LEAD}":
            return httpx.Response(200, json={"username": "studio.mira", "name": "Mira"})
        return httpx.Response(404, json={"error": {"message": f"unhandled {path}"}})


@pytest.fixture()
def ig(monkeypatch):
    fake = FakeInstagram()
    monkeypatch.setattr(meta, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


def dm(mid="m_in1", text="Hi there", sender=LEAD, echo=False, attachments=None):
    message = {"mid": mid}
    if text:
        message["text"] = text
    if echo:
        message["is_echo"] = True
    if attachments:
        message["attachments"] = attachments
    return {"object": "instagram", "entry": [{"time": 1762400000000, "id": OWN_ID, "messaging": [
        {"sender": {"id": sender}, "recipient": {"id": OWN_ID if sender != OWN_ID else LEAD},
         "timestamp": 1762400000000, "message": message}]}]}


def post(client, payload, secret=b"test-ig-secret"):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/instagram", content=raw,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


def test_verification_handshake(client):
    r = client.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token",
                                                   "hub.challenge": "777"})
    assert r.text == "777"


@pytest.mark.parametrize("secret, code", [(b"test-ig-secret", 200), (b"test-app-secret", 200), (b"nope", 401)])
def test_either_app_secret_is_accepted_others_rejected(client, session, ig, secret, code):
    assert post(client, dm(), secret=secret).status_code == code


def test_dm_is_stored_named_answered_and_deduped(client, session, ig, groq):
    groq.down = False
    groq.extract = facts(service=("forecasting_ml", "demand forecasting"))
    groq.writer = "Hi! Happy to help with forecasting. What budget and start date do you have in mind?"
    post(client, dm(text="Do you do demand forecasting?"))
    post(client, dm(text="Do you do demand forecasting?"))  # re-delivery

    session.expire_all()
    contact = session.query(Contact).one()
    assert (contact.channel, contact.external_id, contact.display_name) == ("instagram", LEAD, "@studio.mira")
    conv = session.query(Conversation).one()
    assert [m.author for m in conv.messages] == ["lead", "assistant"]
    assert ig.sent == [{"recipient": {"id": LEAD}, "message": {"text": groq.writer}}]
    assert conv.messages[1].channel_message_id == "m_out1"


def test_echoes_of_our_own_messages_are_ignored(client, session, ig):
    post(client, dm(mid="m_echo", text="our reply", sender=OWN_ID, echo=True))
    post(client, dm(mid="m_echo2", text="our reply", echo=True))
    assert session.query(Message).count() == 0


def test_photo_only_dm_is_noted(client, session, ig):
    post(client, dm(text="", attachments=[{"type": "image", "payload": {"url": "https://x"}}]))
    assert session.query(Message).one().body == "[Sent a photo; the assistant only reads text]"


def test_reply_outside_24h_window_is_explained(client, session, ig, groq):
    groq.down = False
    groq.writer = "Hi! What do you need help with?"
    ig.send_error = {"message": "This message is sent outside of allowed window.", "code": 10, "error_subcode": 2018278}
    post(client, dm())
    session.expire_all()
    conv = session.query(Conversation).one()
    reply = conv.messages[-1]
    assert reply.send_status == "failed" and "24 hours" in reply.error
    assert "not delivered" in conv.attention


def test_link_account_identifies_and_subscribes(client, session, ig):
    r = client.post("/api/connections/instagram/link")
    assert r.json()["account"] == "@lumen.test"
    assert ig.subscribed == [{"subscribed_fields": "messages"}]
    row = session.get(Connection, "instagram")
    session.refresh(row)
    assert row.state["ig_user_id"] == OWN_ID and row.status == "ok"
    assert session.query(ActionLog).filter_by(action="instagram_linked").count() == 1


def test_long_text_is_trimmed_to_instagram_limit():
    trimmed = _trim_bytes("é" * 900)
    assert len(trimmed.encode()) <= 1000 and trimmed.endswith("...")
