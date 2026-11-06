"""Assignment 02: three connected agents. Groq is faked per agent (by its system prompt);
everything else (validation, rule checker, orchestration, persistence) is the real code.

Required cases are marked REQUIRED.
"""
import json
import re
from datetime import date, timedelta

import httpx
import pytest

from app import llm
from app.agents import api as agents_api
from app.agents.agents import verify_intake
from app.agents.models import AgContext, AgEvent, AgMessage, AgRun, AgStep
from app.agents.orchestrator import execute, mark_interrupted_runs
from app.agents.rules import check_plan, parse_rules
from app.agents.sample import SAMPLE_RULES, SAMPLE_TRANSCRIPT

RULES = parse_rules(SAMPLE_RULES)


def item(kind, text, lines, quote, owner=None, owner_quote=None, deadline=None, deadline_quote=None):
    return {"kind": kind, "text": text, "source_lines": lines, "quote": quote, "owner": owner,
            "owner_quote": owner_quote, "deadline": deadline, "deadline_quote": deadline_quote}


INTAKE_OUT = {
    "items": [
        item("requirement", "Daily sales dashboard for 40 stores, live before the Black Friday review", [2],
             "live before our Black Friday review on 27 November", deadline="2026-11-27",
             deadline_quote="on 27 November"),                                                        # F1
        item("decision", "Build in Power BI on a new Snowflake warehouse", [3],
             "we'll build it in Power BI, on top of a new Snowflake warehouse"),                         # F2
        item("deliverable", "Snowflake setup and nightly POS load", [4],
             "I can own the Snowflake setup and the nightly load", owner="Daniel",
             owner_quote="I can own the Snowflake setup"),                                              # F3
        item("constraint", "Customer data needs a security review first", [5, 6],
             "nothing touches production customer data before a security review"),                       # F4
        item("deliverable", "Dashboard design", [7], "I'll take the dashboard design", owner="Priya",
             owner_quote="I'll take the dashboard design"),                                             # F5
        item("deliverable", "Store hierarchy file from Northwind", [8],
             "Alex, will send the store hierarchy file", owner="Alex",
             owner_quote="Alex, will send the store hierarchy file"),                                   # F6
        item("deliverable", "Training session for store managers", [9],
             "We also need a training session for the store managers", owner="Meera",                  # invented
             owner_quote="We also need a training session"),                                            # F7
        item("requirement", "Dashboards load in under 3 seconds on tablets", [13],
             "Dashboards must load in under 3 seconds"),                                                # F8
        item("constraint", "Stay within the 40k budget", [14], "stay within our 40k budget"),            # F9
        item("decision", "Next check-in Thursday 12 November", [15], "Next check-in Thursday 12 November",
             deadline="2026-11-12", deadline_quote="Thursday 12 November"),                             # F10
        item("decision", "Client approved a 60k budget", [14], "approved a 60k budget"),                 # fabricated
    ],
    "conflicts": [{"text": "Go-live 27 Nov (contract) vs 20 Nov (client wish)", "source_lines": [2, 10, 11]}],
    "missing": [{"text": "No owner for store-manager training", "source_lines": [9]}],
}


def weekday_before(d: date, days: int) -> date:
    d = d - timedelta(days=days)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def plan_for(facts: list[dict], revising: bool) -> dict:
    """A plan written the way a planner would, from F1's go-live date. The first draft puts the
    migration on the weekend Daniel offered (Sat 14 Nov), which breaks rule R2."""
    golive = date.fromisoformat(next(f for f in facts if f["id"] == "F1")["deadline"])
    t3 = weekday_before(golive, 7)
    t2 = weekday_before(t3, 1) if revising else date(2026, 11, 14)

    def task(i, title, owner, ob, due, db, deps, src, **flags):
        return {"id": f"T{i}", "title": title, "owner": owner, "owner_basis": ob, "deadline": due.isoformat(),
                "deadline_basis": db, "depends_on": deps, "sources": src, "touches_customer_data": False,
                "client_facing": False, "is_signoff": False, "is_security_review": False, "rationale": "",
                **flags}
    return {
        "tasks": [
            task(1, "Security review of the POS customer data", "Arjun Rao", "recommendation", date(2026, 11, 10),
                 "recommendation", [], ["F4"], is_security_review=True),
            task(2, "Snowflake setup and nightly POS load", "Daniel Brooks", "fact", t2, "recommendation",
                 ["T1"], ["F3", "F4"], touches_customer_data=True),
            task(3, "Dashboard design", "Priya Nair", "fact", t3, "recommendation", ["T2"], ["F5", "F6"],
                 client_facing=True),
            task(4, "Store-manager training", "Meera Iyer", "recommendation", weekday_before(golive, 1),
                 "recommendation", ["T3"], ["F7"], client_facing=True),
            task(5, "PM sign-off before go-live", "Meera Iyer", "recommendation", golive, "fact", ["T3", "T4"],
                 ["F1"], is_signoff=True),
        ],
        "recommendations": [{"text": "Owner for training: Meera Iyer", "why": "No owner was named (M1)"}],
        "open_questions": [{"text": "Go-live 20 or 27 November?", "sources": ["F1"]},
                           {"text": "When will Alex send the store hierarchy file?", "sources": ["F6"]}],
    }


class FakeAgents:
    """Plays the three agents. Each agent is recognised by its own system prompt."""

    def __init__(self):
        self.calls: list[str] = []
        self.prompts: dict[str, list[str]] = {"intake": [], "planning": [], "review": []}
        self.down: set[str] = set()
        self.planning_override = None   # callable(user_text, revising) -> dict | str
        self.review_override = None     # callable(user_text) -> dict

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system, user = body["messages"][0]["content"], body["messages"][1]["content"]
        agent = re.match(r"You are the (\w+) Agent", system).group(1).lower()
        self.calls.append(agent)
        self.prompts[agent].append(user + "\n" + "\n".join(m["content"] for m in body["messages"][2:]))
        if agent in self.down:
            return httpx.Response(503, json={"error": {"message": "unavailable"}})
        if agent == "intake":
            out = INTAKE_OUT
        elif agent == "planning":
            revising = "Corrections from the Review Agent" in user
            facts = json.loads(user.split("):\n", 1)[1].split("\n\nConflicts:")[0])
            out = self.planning_override(user, revising) if self.planning_override else plan_for(facts, revising)
        else:
            n = int(re.search(r"Automatic rule checker findings \((\d+)\)", user).group(1))
            out = self.review_override(user) if self.review_override else (
                {"verdict": "approve", "corrections": [], "summary": "Plan follows the rules and the transcript."}
                if n == 0 else
                {"verdict": "revise", "summary": f"{n} problem(s)",
                 "corrections": [{"task_id": "T2", "problem": "Migration is scheduled on Saturday 14 Nov",
                                  "fix": "Move it to a weekday before the dashboard work", "reference": "R2"}]})
        content = out if isinstance(out, str) else json.dumps(out)
        return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": 1500,
                                                                            "completion_tokens": 400},
                                         "choices": [{"message": {"content": content}}]})


@pytest.fixture()
def agents(monkeypatch, client):
    fake = FakeAgents()
    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    # run the orchestrator synchronously in tests
    monkeypatch.setattr(agents_api, "start_in_background", lambda rid: (execute(rid), True)[1])
    return fake


def new_session(client, name="A"):
    return client.post("/api/agents/sessions", json={"name": name}).json()["id"]


def new_run(client, sid, **kw):
    body = {"title": "Kickoff", "transcript": SAMPLE_TRANSCRIPT, "rules_yaml": SAMPLE_RULES, **kw}
    r = client.post(f"/api/agents/sessions/{sid}/runs", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def detail(client, sid, rid):
    return client.get(f"/api/agents/sessions/{sid}/runs/{rid}").json()


# ---------------------------------------------------------------- the required cases

def test_REQUIRED_complete_run_with_a_rule_violation_sent_back(client, session, agents):
    """REQUIRED: one complete three-agent run, in which the Review Agent sends a real rule
    violation (weekend deadline) back to Planning and the revision is approved."""
    sid = new_session(client)
    rid = new_run(client, sid)
    d = detail(client, sid, rid)
    assert d["status"] == "completed" and d["result"]["approved"] and d["result"]["rounds"] == 2
    assert agents.calls == ["intake", "planning", "review", "planning", "review"]
    kinds = [(m["from"], m["to"], m["kind"]) for m in d["messages"]]
    assert kinds == [("intake", "planning", "handoff"), ("planning", "review", "handoff"),
                     ("review", "planning", "feedback"), ("planning", "review", "handoff"),
                     ("review", "coordinator", "approval")]
    feedback = d["messages"][2]["payload"]["corrections"]
    assert any(c["reference"] == "R2" and c["task_id"] == "T2" for c in feedback)
    t2 = next(t for t in d["result"]["plan"]["tasks"] if t["id"] == "T2")
    assert date.fromisoformat(t2["deadline"]).weekday() < 5
    assert "Corrections from the Review Agent" in agents.prompts["planning"][1]   # the feedback was delivered
    assert d["cost_usd"] > 0


def test_REQUIRED_changed_fact_propagates_without_stale_context(client, session, agents):
    """REQUIRED: correct a source fact; only Planning and Review rerun, on the new context version,
    and the plan follows the new fact."""
    sid = new_session(client)
    rid = new_run(client, sid)
    r = client.post(f"/api/agents/sessions/{sid}/runs/{rid}/facts/F1",
                    json={"deadline": "2026-11-20", "reason": "Client confirmed the earlier go-live"})
    assert r.json()["context_version"] == 2
    d = detail(client, sid, rid)
    assert d["status"] == "completed" and d["result"]["context_version"] == 2
    assert agents.calls.count("intake") == 1                          # intake not rerun
    signoff = next(t for t in d["result"]["plan"]["tasks"] if t["is_signoff"])
    assert signoff["deadline"] == "2026-11-20"
    v2_prompt = [p for p in agents.prompts["planning"] if "context version 2" in p][0]
    f1 = json.loads(v2_prompt.split("):\n", 1)[1].split("\n\nConflicts:")[0])[0]
    assert f1["deadline"] == "2026-11-20" and f1["origin"] == "corrected"
    assert "context version 1" not in v2_prompt                       # nothing from v1
    stale = [s for s in d["steps"] if s["stale"]]
    assert stale and all(s["context_version"] == 1 for s in stale)


def test_REQUIRED_recovery_after_simulated_failure_without_duplicating_work(client, session, agents):
    """REQUIRED: a (clearly labelled) simulated model failure stops the run; Resume continues from
    the failed step and does not call the model again for completed steps."""
    sid = new_session(client)
    rid = new_run(client, sid, simulate_failure="review")
    d = detail(client, sid, rid)
    assert d["status"] == "failed" and "SIMULATED" in d["error"]
    assert agents.calls == ["intake", "planning"]
    failed = [s for s in d["steps"] if s["status"] == "failed"]
    assert [(s["agent"], s["round"]) for s in failed] == [("review", 1)]
    assert any("DEMO" in e["text"] for e in d["events"])

    client.post(f"/api/agents/sessions/{sid}/runs/{rid}/resume")
    d = detail(client, sid, rid)
    assert d["status"] == "completed"
    assert agents.calls == ["intake", "planning", "review", "planning", "review"]   # intake/planning r1 reused
    assert session.query(AgMessage).filter_by(run_id=rid, kind="handoff", from_agent="intake").count() == 1


def test_REQUIRED_two_sessions_do_not_share_private_context(client, session, agents):
    a, b = new_session(client, "A"), new_session(client, "B")
    client.put(f"/api/agents/sessions/{a}/notes", json={"notes": "CONFIDENTIAL-A: client budget is really 38k"})
    run_a = new_run(client, a)
    run_b = new_run(client, b)
    a_prompts = [p for p in agents.prompts["planning"] if "CONFIDENTIAL-A" in p]
    assert len(a_prompts) == 2                     # both of session A's planning rounds saw A's notes
    assert len(agents.prompts["planning"]) == 4     # ...and none of session B's did
    assert client.get(f"/api/agents/sessions/{b}/runs/{run_a}").status_code == 404
    assert client.post(f"/api/agents/sessions/{b}/runs/{run_a}/resume").status_code == 404
    assert [r["id"] for r in client.get(f"/api/agents/sessions/{b}").json()["runs"]] == [run_b]


# ---------------------------------------------------------------- grounding and authority

def test_intake_removes_invented_owner_and_unsupported_fact():
    out, notes = verify_intake(INTAKE_OUT, SAMPLE_TRANSCRIPT)
    facts = {f["id"]: f for f in out["facts"]}
    assert len(facts) == 10                                         # fabricated 60k item dropped
    assert facts["F7"]["owner"] is None                             # Meera only mentioned the training
    assert facts["F3"]["owner"] == "Daniel" and facts["F6"]["owner"] == "Alex"
    assert facts["F1"]["deadline"] == "2026-11-27"
    assert any("60k" in n for n in notes) and any("Removed owner 'Meera'" in n for n in notes)


def test_review_cannot_approve_a_plan_that_breaks_a_rule(client, session, agents):
    agents.review_override = lambda user: {"verdict": "approve", "corrections": [], "summary": "Looks fine"}
    agents.planning_override = lambda user, revising: plan_for(
        json.loads(user.split("):\n", 1)[1].split("\n\nConflicts:")[0]), revising=False)   # never fixes it
    sid = new_session(client)
    rid = new_run(client, sid)
    d = detail(client, sid, rid)
    assert d["status"] == "needs_review" and not d["result"]["approved"]
    assert d["result"]["rounds"] == RULES.max_review_rounds              # bounded
    assert any(u["reference"] == "R2" and u["from"] == "rule_checker" for u in d["result"]["unresolved"])


def test_seeded_violation_demo_is_labelled_and_corrected(client, session, agents):
    agents.planning_override = lambda user, revising: plan_for(
        json.loads(user.split("):\n", 1)[1].split("\n\nConflicts:")[0]), revising=True)   # always compliant
    sid = new_session(client)
    rid = new_run(client, sid, seed_violation=True)
    d = detail(client, sid, rid)
    assert d["status"] == "completed" and d["result"]["rounds"] == 2
    assert any(e["text"].startswith("DEMO: injected a weekend deadline") for e in d["events"])
    first = next(s for s in d["steps"] if s["agent"] == "planning" and s["round"] == 1)
    assert "_demo_note" in first["output"]


def test_invalid_handoff_is_sent_back_once_then_fails_visibly(client, session, agents):
    attempts = {"n": 0}

    def bad_then_good(user, revising):
        attempts["n"] += 1
        if "failed validation" in agents.prompts["planning"][-1]:
            return plan_for(json.loads(user.split("):\n", 1)[1].split("\n\nConflicts:")[0]), True)
        plan = plan_for(json.loads(user.split("):\n", 1)[1].split("\n\nConflicts:")[0]), True)
        plan["tasks"][0]["id"] = "Task-one"
        return plan

    agents.planning_override = bad_then_good
    sid = new_session(client)
    rid = new_run(client, sid)
    d = detail(client, sid, rid)
    assert d["status"] == "completed"
    p1 = next(s for s in d["steps"] if s["agent"] == "planning")
    assert len(p1["usage"]) == 2                                     # one repair round-trip

    agents.planning_override = lambda user, revising: "{not json"
    rid2 = new_run(client, sid)
    d2 = detail(client, sid, rid2)
    assert d2["status"] == "failed" and "failed validation twice" in d2["error"]


def test_real_model_outage_then_resume(client, session, agents):
    agents.down = {"planning"}
    sid = new_session(client)
    rid = new_run(client, sid)
    d = detail(client, sid, rid)
    assert d["status"] == "failed" and d["context_version"] == 1     # intake output kept
    agents.down = set()
    client.post(f"/api/agents/sessions/{sid}/runs/{rid}/resume")
    assert detail(client, sid, rid)["status"] == "completed"
    assert agents.calls.count("intake") == 1


def test_restart_marks_running_runs_as_interrupted(client, session, agents):
    sid = new_session(client)
    rid = new_run(client, sid)
    run = session.get(AgRun, rid)
    run.status = "running"
    session.add(AgStep(run_id=rid, agent="review", context_version=9, round=1, status="running"))
    session.commit()
    mark_interrupted_runs()
    session.expire_all()
    assert session.get(AgRun, rid).status == "interrupted"


def test_reset_clears_only_that_session(client, session, agents):
    a, b = new_session(client, "A"), new_session(client, "B")
    new_run(client, a)
    run_b = new_run(client, b)
    assert client.post(f"/api/agents/sessions/{a}/reset").json() == {"deleted_runs": 1}
    assert session.query(AgRun).count() == 1 and session.get(AgRun, run_b)
    assert session.query(AgContext).count() == 1 and session.query(AgEvent).filter_by(run_id=run_b).count() > 0


def test_bad_rules_are_rejected_before_any_model_call(client, session, agents):
    sid = new_session(client)
    r = client.post(f"/api/agents/sessions/{sid}/runs",
                    json={"transcript": SAMPLE_TRANSCRIPT, "rules_yaml": "company: X\nteam: []"})
    assert r.status_code == 422 and agents.calls == []


# ---------------------------------------------------------------- rule checker unit tests

FACTS = verify_intake(INTAKE_OUT, SAMPLE_TRANSCRIPT)[0]["facts"]


def _rules(plan):
    return {f["rule"] for f in check_plan(plan, FACTS, RULES)}


def test_checker_accepts_the_corrected_plan():
    assert check_plan(plan_for(FACTS, revising=True), FACTS, RULES) == []


def test_checker_catches_each_rule():
    good = plan_for(FACTS, revising=True)

    def changed(fn):
        p = json.loads(json.dumps(good))
        fn(p["tasks"])
        return _rules(p)

    assert "R1" in changed(lambda t: t[2].update(owner="Alex"))                        # client owner
    assert "R2" in changed(lambda t: t[1].update(deadline="2026-11-15"))               # Sunday
    assert "R3" in changed(lambda t: t[1].update(depends_on=[]))                       # no security review
    assert "R4" in changed(lambda t: t[4].update(depends_on=["T3"]))                   # training not signed off
    assert "R5" in changed(lambda t: [x.update(owner="Meera Iyer") for x in t])        # capacity
    assert "R6" in changed(lambda t: t[4].update(deadline="2026-11-30"))               # after go-live
    assert "GROUNDING" in changed(lambda t: t[0].update(owner_basis="fact"))           # Arjun not in a fact
    assert "STRUCTURE" in changed(lambda t: t[0].update(depends_on=["T5"]))            # cycle


def test_agents_wait_out_a_groq_rate_limit(client, session, agents):
    """Seen live: Groq's free tier returned 429 (tokens per minute) on the Planning call.
    The agents now wait as Groq asks and retry, instead of stopping the run."""
    original = agents.handler
    state = {"limited": 2}

    def limited(request):
        body = json.loads(request.content)
        if "Planning Agent" in body["messages"][0]["content"] and state["limited"]:
            state["limited"] -= 1
            return httpx.Response(429, json={"error": {"message": "Rate limit reached for model on tokens per "
                                                       "minute (TPM): Limit 8000. Please try again in 1.9s."}})
        return original(request)

    import app.llm as llm_module
    monkeypatch_transport = httpx.MockTransport(limited)
    llm_module.HTTP_TRANSPORT = monkeypatch_transport
    sid = new_session(client)
    rid = new_run(client, sid)
    assert detail(client, sid, rid)["status"] == "completed"
    assert state["limited"] == 0


def test_checker_overrules_a_false_weekend_claim_from_the_review_agent(client, session, agents):
    """Seen live: the Review Agent said T2 fell on a weekend when it was due on a Thursday.
    R2 is a date fact the checker decides exactly, so the false alarm is overruled and shown."""
    def review(user):
        n = int(re.search(r"Automatic rule checker findings \((\d+)\)", user).group(1))
        if n:
            return {"verdict": "revise", "summary": "fix", "corrections": [
                {"task_id": "T2", "problem": "weekend", "fix": "weekday", "reference": "R2"}]}
        return {"verdict": "revise", "summary": "Only violation is T2's weekend deadline", "corrections": [
            {"task_id": "T2", "problem": "Deadline falls on a weekend", "fix": "Move to Monday", "reference": "R2"}]}
    agents.review_override = review
    sid = new_session(client)
    rid = new_run(client, sid)
    d = detail(client, sid, rid)
    assert d["status"] == "completed" and d["result"]["approved"]
    over = d["result"]["overruled"]
    assert len(over) == 1 and over[0]["task_id"] == "T2" and "false alarm" in over[0]["overruled"]
    assert any("overruled the Review Agent" in e["text"] for e in d["events"])


def test_review_agent_judgement_calls_are_not_overruled(client, session, agents):
    """Grounding / R3-style objections are not mechanical: the checker never dismisses them."""
    agents.review_override = lambda user: {"verdict": "revise", "summary": "x", "corrections": [
        {"task_id": "T2", "problem": "Owner not supported by the transcript", "fix": "Mark as recommendation",
         "reference": "GROUNDING"}]}
    sid = new_session(client)
    rid = new_run(client, sid)
    d = detail(client, sid, rid)
    assert d["status"] == "needs_review" and not d["result"]["overruled"]
