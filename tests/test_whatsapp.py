"""WhatsApp channel: webhook verification and signatures, inbound, delivery
receipts, sending, and readable errors. Meta's Graph API is faked."""
import hashlib
import hmac
import json

import httpx
import pytest

from app import meta
from app.models import ActionLog, Contact, Conversation, Message
from tests.fake_groq import facts

PHONE_ID = "1379451971922417"
SECRET = b"test-app-secret"


class FakeGraph:
    def __init__(self):
        self.sent: list[dict] = []
        self.error: dict | None = None   # e.g. {"code": 131030, "message": "..."}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v23.0/{PHONE_ID}/messages"
        assert request.headers["authorization"] == "Bearer test-wa-token"
        if self.error:
            return httpx.Response(400, json={"error": self.error})
        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out{len(self.sent)}"}]})


@pytest.fixture()
def graph(monkeypatch):
    fake = FakeGraph()
    monkeypatch.setattr(meta, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


def wa_payload(wamid="wamid.in1", text="Hello", wa_id="919800000010", name="Rohit", phone_id=PHONE_ID,
               msg_type="text", statuses=None):
    msg = {"from": wa_id, "id": wamid, "timestamp": "1762400000", "type": msg_type}
    if msg_type == "text":
        msg["text"] = {"body": text}
    else:
        msg[msg_type] = {"id": "media1", "caption": text}
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_id}}
    if statuses is None:
        value["contacts"] = [{"profile": {"name": name}, "wa_id": wa_id}]
        value["messages"] = [msg]
    else:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}]}


def post(client, payload, secret=SECRET, signature=None):
    raw = json.dumps(payload).encode()
    sig = signature if signature is not None else "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    return client.post("/webhooks/whatsapp", content=raw,
                       headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})


# ---------- webhook verification ----------

def test_verification_handshake(client):
    ok = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "test-verify-token",
                                                  "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345"
    bad = client.get("/webhooks/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                                   "hub.challenge": "12345"})
    assert bad.status_code == 403


@pytest.mark.parametrize("kwargs", [{"signature": ""}, {"secret": b"wrong-secret"}])
def test_unsigned_or_forged_calls_are_rejected(client, session, graph, kwargs):
    r = post(client, wa_payload(), **kwargs)
    assert r.status_code == 401
    assert session.query(Message).count() == 0
    assert session.query(ActionLog).filter_by(action="webhook_rejected").count() == 1


# ---------- inbound + reply ----------

def test_message_is_stored_answered_and_deduped(client, session, graph, groq):
    groq.down = False
    groq.extract = facts(service=("forecasting_ml", "do you do forecasting"))
    groq.writer = "Hi Rohit! Yes, we do forecasting. What budget and timeline do you have in mind?"

    assert post(client, wa_payload(text="hello, do you do forecasting?")).status_code == 200
    assert post(client, wa_payload(text="hello, do you do forecasting?")).status_code == 200  # Meta re-delivery

    session.expire_all()
    contact = session.query(Contact).one()
    assert (contact.channel, contact.external_id, contact.display_name) == ("whatsapp", "919800000010", "Rohit")
    conv = session.query(Conversation).one()
    assert [m.author for m in conv.messages] == ["lead", "assistant"]
    assert len(graph.sent) == 1
    sent = graph.sent[0]
    assert sent["to"] == "919800000010" and sent["type"] == "text" and "budget" in sent["text"]["body"]
    assert conv.messages[1].channel_message_id == "wamid.out1"


def test_messages_for_another_number_are_ignored(client, session, graph):
    post(client, wa_payload(phone_id="999"))
    assert session.query(Message).count() == 0


def test_photo_is_stored_with_a_note(client, session, graph):
    post(client, wa_payload(msg_type="image", text="our current dashboard"))
    body = session.query(Message).one().body
    assert body.startswith("[Sent a photo; the assistant only reads text]") and "our current dashboard" in body


# ---------- delivery receipts ----------

def _sent_conversation(client, session, graph, groq):
    groq.down = False
    groq.writer = "Hi Rohit! What would you like help with?"
    post(client, wa_payload())
    session.expire_all()
    return session.query(Conversation).one()


def test_delivery_receipts_update_the_message(client, session, graph, groq):
    conv = _sent_conversation(client, session, graph, groq)
    post(client, wa_payload(statuses=[{"id": "wamid.out1", "status": "read", "recipient_id": "919800000010"}]))
    session.expire_all()
    assert session.get(Conversation, conv.id).messages[-1].meta["delivery"] == "read"


def test_failed_delivery_is_flagged_with_a_reason(client, session, graph, groq):
    conv = _sent_conversation(client, session, graph, groq)
    post(client, wa_payload(statuses=[{"id": "wamid.out1", "status": "failed", "recipient_id": "919800000010",
                                       "errors": [{"code": 131047, "title": "Re-engagement message"}]}]))
    session.expire_all()
    conv = session.get(Conversation, conv.id)
    assert conv.messages[-1].send_status == "failed" and "24 hours" in conv.messages[-1].error
    assert "did not deliver" in conv.attention


# ---------- sending errors ----------

def test_recipient_not_on_allowed_list_is_explained(client, session, graph, groq):
    graph.error = {"code": 131030, "message": "Recipient phone number not in allowed list"}
    conv = _sent_conversation(client, session, graph, groq)
    reply = conv.messages[-1]
    assert reply.send_status == "failed" and "allowed recipient list" in reply.error
    assert "not delivered" in conv.attention


def test_expired_token_marks_whatsapp_red(client, session, graph, groq):
    graph.error = {"code": 190, "message": "Error validating access token"}
    _sent_conversation(client, session, graph, groq)
    status = {c["provider"]: c for c in client.get("/api/connections").json()}["whatsapp"]
    assert status["status"] == "expired" and "token" in status["error"]


def test_operator_reply_goes_out_on_whatsapp(client, session, graph, groq):
    conv = _sent_conversation(client, session, graph, groq)
    r = client.post(f"/api/conversations/{conv.id}/reply", json={"text": "Hi Rohit, Priya here."})
    assert r.json()["send_status"] == "sent"
    assert graph.sent[-1]["text"]["body"] == "Hi Rohit, Priya here."


# ---------- making silent failures visible ----------

def test_wrong_app_secret_turns_whatsapp_red(client, session, graph):
    post(client, wa_payload(), secret=b"not-the-app-secret")
    status = {c["provider"]: c for c in client.get("/api/connections").json()}["whatsapp"]
    assert status["status"] == "error" and "META_APP_SECRET" in status["error"]


def test_wrong_phone_number_id_is_explained(client, session, graph):
    post(client, wa_payload(phone_id="555000111"))
    status = {c["provider"]: c for c in client.get("/api/connections").json()}["whatsapp"]
    assert status["status"] == "error" and "555000111" in status["error"]


def test_last_webhook_time_is_shown(client, session, graph):
    before = {c["provider"]: c for c in client.get("/api/connections").json()}["whatsapp"]
    assert before["last_webhook_at"] is None
    post(client, wa_payload())
    after = {c["provider"]: c for c in client.get("/api/connections").json()}["whatsapp"]
    assert after["last_webhook_at"] and after["status"] == "ok"
