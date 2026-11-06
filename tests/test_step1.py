import pytest
from sqlalchemy.exc import IntegrityError

from app.guards import AutomationPaused, require_automation_allowed
from app.models import ActionLog, Contact, Conversation, Message, Rep
from app.policy import DEFAULT_POLICY_PATH, PolicyError, get_active_policy, parse_policy

DEFAULT = DEFAULT_POLICY_PATH.read_text()


# ---------- policy validation ----------

def test_default_policy_is_valid():
    p = parse_policy(DEFAULT)
    assert {r.id for r in p.reps} == {"priya", "daniel"}


def test_routing_to_unknown_rep_is_rejected():
    bad = DEFAULT.replace("rep: priya", "rep: ceo", 1)
    with pytest.raises(PolicyError) as e:
        parse_policy(bad)
    assert "unknown rep 'ceo'" in str(e.value)


def test_unknown_specialty_and_bad_timezone_are_rejected():
    with pytest.raises(PolicyError):
        parse_policy(DEFAULT.replace("specialties: [bi_dashboards]", "specialties: [web_design]"))
    with pytest.raises(PolicyError):
        parse_policy(DEFAULT.replace("timezone: Asia/Kolkata", "timezone: Mars/Olympus"))


def test_yaml_syntax_error_is_readable():
    with pytest.raises(PolicyError) as e:
        parse_policy("company_name: [unclosed")
    assert e.value.errors[0].startswith("YAML syntax error at line 1")


def test_misspelled_policy_key_is_rejected_not_ignored():
    with pytest.raises(PolicyError) as e:
        parse_policy(DEFAULT.replace("min_budget_usd: 5000", "min_budjet_usd: 5000"))
    assert any("min_budjet_usd" in err for err in e.value.errors)


def test_validate_endpoint_does_not_save(client):
    r = client.post("/api/policy/validate", json={"yaml": DEFAULT.replace("rep: priya", "rep: ceo", 1)})
    assert r.json()["valid"] is False
    assert client.get("/api/policy").json()["version"] == 1


def test_first_boot_seeds_policy_and_reps(session):
    policy, version = get_active_policy(session)
    assert version == 1
    assert {r.id for r in session.query(Rep)} == {"priya", "daniel"}


# ---------- API ----------

def test_policy_update_versions_and_logs(client, session):
    r = client.put("/api/policy", json={"yaml": DEFAULT.replace("min_budget_usd: 5000", "min_budget_usd: 8000")})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert client.get("/api/policy").json()["policy"]["qualification"]["min_budget_usd"] == 8000
    assert session.query(ActionLog).filter_by(action="policy_saved").count() == 2


def test_invalid_policy_keeps_previous_version(client):
    r = client.put("/api/policy", json={"yaml": DEFAULT.replace("fallback: least_open_leads", "fallback: nobody")})
    assert r.status_code == 422
    assert r.json()["detail"]["errors"]
    assert client.get("/api/policy").json()["version"] == 1


def _make_conv(session, channel="email", ext="a@example.com"):
    c = Contact(channel=channel, external_id=ext, display_name="A")
    session.add(c); session.flush()
    cv = Conversation(contact_id=c.id, channel=channel)
    session.add(cv); session.commit()
    return cv


def test_takeover_blocks_automation_and_is_logged(client, session):
    cv = _make_conv(session)
    require_automation_allowed(session, cv.id, "send_reply")  # allowed before takeover

    assert client.post(f"/api/conversations/{cv.id}/takeover", json={"paused": True}).json()["automation_paused"]
    with pytest.raises(AutomationPaused):
        require_automation_allowed(session, cv.id, "send_reply")

    actions = [e["action"] for e in client.get(f"/api/conversations/{cv.id}").json()["log"]]
    assert "takeover_on" in actions and "automation_blocked" in actions


# ---------- identity and dedupe constraints ----------

def test_same_person_on_two_channels_stays_separate(session):
    _make_conv(session, "email", "sam@example.com")
    _make_conv(session, "whatsapp", "sam@example.com")
    assert session.query(Contact).count() == 2


def test_same_contact_on_same_channel_is_unique(session):
    _make_conv(session, "email", "sam@example.com")
    with pytest.raises(IntegrityError):
        _make_conv(session, "email", "sam@example.com")


def test_duplicate_channel_message_id_is_rejected(session):
    cv = _make_conv(session)
    session.add(Message(conversation_id=cv.id, channel="email", channel_message_id="m1", direction="in", author="lead"))
    session.commit()
    session.add(Message(conversation_id=cv.id, channel="email", channel_message_id="m1", direction="in", author="lead"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_dashboard_pages_render(client):
    assert client.get("/").status_code == 200
    assert client.get("/policy").status_code == 200
    assert client.get("/healthz").json() == {"ok": True, "database": "ready"}
    conns = {c["provider"]: c["status"] for c in client.get("/api/connections").json()}
    assert conns["instagram"] == "not_configured"
