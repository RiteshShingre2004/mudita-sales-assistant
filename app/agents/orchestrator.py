"""Orchestration of Intake -> Planning <-> Review, on persisted shared context.

Guarantees:
- Every step is a row keyed by (run, agent, context version, round). A completed step is
  never executed again: resuming a failed run reuses completed outputs and continues
  from the failed step.
- Every handoff is validated before it is stored and passed on (see agents.py / schemas.py),
  and each handoff message is stored once per step.
- Review is bounded: at most max_review_rounds Planning/Review rounds; anything still
  wrong is surfaced as unresolved issues.
- Code has the final word on the hard rules: the plan is only "approved" when the
  rule checker finds nothing AND the Review Agent approves.
- A user correction creates a new context version. Only Planning and Review rerun (Intake
  output is not affected), and they read the new version; older steps stay visible as history.
- Runs belong to a session; nothing from one session is ever read while running another.
"""
import logging
import threading
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.agents import AgentOutputInvalid, IntakeAgent, PlanningAgent, ReviewAgent
from app.agents.models import AgContext, AgEvent, AgMessage, AgRun, AgSession, AgStep
from app.agents.rules import CompanyRules, check_plan, parse_rules
from app.db import SessionLocal
from app.llm import LLMError

log = logging.getLogger("agents")

INTAKE, PLANNING, REVIEW = IntakeAgent(), PlanningAgent(), ReviewAgent()
_run_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class SimulatedFailure(Exception):
    """Raised by the labelled demo toggle 'simulate a model failure'."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def event(db: Session, run: AgRun, text: str, level: str = "info") -> None:
    db.add(AgEvent(run_id=run.id, text=text, level=level))


# ---------------------------------------------------------------- sessions and runs

def create_session(db: Session, name: str) -> AgSession:
    s = AgSession(name=name.strip()[:80] or "Session")
    db.add(s)
    db.commit()
    return s


def create_run(db: Session, session_id: str, title: str, transcript: str, rules_yaml: str,
               fail_next: str | None = None, seed_violation: bool = False) -> AgRun:
    parse_rules(rules_yaml)  # raises RulesError with readable messages
    if not transcript.strip():
        raise ValueError("The transcript is empty")
    run = AgRun(session_id=session_id, title=title.strip()[:200] or "Untitled run", transcript=transcript,
                rules_yaml=rules_yaml, status="pending",
                settings={"fail_next": fail_next, "seed_violation": bool(seed_violation)})
    db.add(run)
    db.flush()
    event(db, run, f"Run {run.id} created")
    if fail_next:
        event(db, run, f"DEMO: the next {fail_next} model call will fail (simulated fault)", "warn")
    if seed_violation:
        event(db, run, "DEMO: a rule violation will be injected into the first plan draft (simulated fault)", "warn")
    db.commit()
    return run


def reset_session(db: Session, session_id: str) -> int:
    runs = list(db.scalars(select(AgRun).where(AgRun.session_id == session_id)))
    for run in runs:
        for model in (AgEvent, AgMessage, AgStep, AgContext):
            db.query(model).filter(model.run_id == run.id).delete(synchronize_session=False)
        db.delete(run)
    sess = db.get(AgSession, session_id)
    if sess:
        sess.private_notes = ""
    db.commit()
    return len(runs)


def current_context(db: Session, run: AgRun) -> AgContext | None:
    if not run.context_version:
        return None
    return db.scalar(select(AgContext).where(AgContext.run_id == run.id, AgContext.version == run.context_version))


def context_dict(ctx: AgContext) -> dict:
    return {"version": ctx.version, "facts": ctx.facts, "conflicts": ctx.conflicts, "missing": ctx.missing}


# ---------------------------------------------------------------- corrections

def correct_fact(db: Session, run: AgRun, fact_id: str, text: str | None, owner: str | None,
                 deadline: str | None, reason: str, actor: str) -> AgContext:
    if run.status == "running":
        raise ValueError("The run is still working. Wait for it to finish, then correct the fact.")
    ctx = current_context(db, run)
    if ctx is None:
        raise ValueError("There are no facts yet. Run Intake first.")
    facts = [dict(f) for f in ctx.facts]
    target = next((f for f in facts if f["id"] == fact_id), None)
    if target is None:
        raise ValueError(f"Unknown fact {fact_id}")
    if deadline:
        date.fromisoformat(deadline)  # ValueError -> 422
    before = {k: target.get(k) for k in ("text", "owner", "deadline")}
    if text is not None and text.strip():
        target["text"] = text.strip()
    target["owner"] = (owner or "").strip() or None
    target["deadline"] = deadline or None
    after = {k: target.get(k) for k in ("text", "owner", "deadline")}
    if before == after:
        raise ValueError("Nothing changed")
    target["origin"] = "corrected"
    target["previous"] = before
    new = AgContext(run_id=run.id, version=ctx.version + 1, facts=facts, conflicts=ctx.conflicts,
                    missing=ctx.missing, reason=f"{fact_id} corrected by {actor}: {reason or 'no reason given'}",
                    created_by=actor)
    db.add(new)
    run.context_version = new.version
    run.status, run.error, run.result = "pending", "", {}
    changed = ", ".join(f"{k}: {before[k]!r} -> {after[k]!r}" for k in before if before[k] != after[k])
    event(db, run, f"Context v{new.version}: {fact_id} corrected ({changed}). Planning and Review will rerun "
                   f"on v{new.version}; Intake output is unaffected and is reused.")
    db.commit()
    return new


# ---------------------------------------------------------------- execution

def start_in_background(run_id: str) -> bool:
    """Start (or resume) a run in a worker thread. Returns False if it is already running."""
    with _locks_guard:
        lock = _run_locks.setdefault(run_id, threading.Lock())
    if not lock.acquire(blocking=False):
        return False

    def worker():
        try:
            execute(run_id)
        finally:
            lock.release()

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
    return True


def _step(db: Session, run: AgRun, agent: str, version: int, rnd: int) -> AgStep:
    step = db.scalar(select(AgStep).where(AgStep.run_id == run.id, AgStep.agent == agent,
                                          AgStep.context_version == version, AgStep.round == rnd))
    if step is None:
        step = AgStep(run_id=run.id, agent=agent, context_version=version, round=rnd, status="pending")
        db.add(step)
        db.flush()
    return step


def _message(db: Session, run: AgRun, step: AgStep, frm: str, to: str, kind: str, summary: str,
             payload: dict) -> None:
    exists = db.scalar(select(AgMessage).where(AgMessage.step_id == step.id, AgMessage.kind == kind))
    if exists is None:
        db.add(AgMessage(run_id=run.id, step_id=step.id, from_agent=frm, to_agent=to, kind=kind,
                         context_version=step.context_version, round=step.round, summary=summary,
                         payload=payload))


def _maybe_fail(db: Session, run: AgRun, agent: str) -> None:
    if (run.settings or {}).get("fail_next") == agent:
        run.settings = {**run.settings, "fail_next": None}   # consumed: a resume will not fail again
        db.commit()
        raise SimulatedFailure(f"SIMULATED model failure in the {agent} agent (demo toggle)")


def _execute_step(db: Session, run: AgRun, step: AgStep, inp: dict, fn) -> AgStep:
    """Run one agent step unless it already succeeded. Persists input, output, usage."""
    if step.status == "succeeded":
        return step
    step.status, step.attempts, step.started_at, step.error = "running", step.attempts + 1, utcnow(), ""
    step.input = inp
    db.commit()
    try:
        _maybe_fail(db, run, step.agent)
        result = fn()
    except (LLMError, AgentOutputInvalid, SimulatedFailure) as exc:
        reason = getattr(exc, "reason", None) or str(exc)
        step.status, step.error, step.finished_at = "failed", reason, utcnow()
        step.usage = [*(step.usage or []), *getattr(exc, "usage", [])]
        db.commit()   # keep the failed step (and what it cost) before the run stops
        raise
    step.output = result.output
    step.usage = [*(step.usage or []), *result.usage]
    step.status, step.finished_at = "succeeded", utcnow()
    for note in result.notes:
        event(db, run, f"Intake check: {note}", "warn")
    db.commit()
    return step


def _seed_violation(plan: dict, rules: CompanyRules) -> tuple[dict, str]:
    """DEMO fault: move one deadline onto a Saturday so the review loop has to catch it."""
    plan = {**plan, "tasks": [dict(t) for t in plan["tasks"]]}
    d = rules.meeting_date
    saturday = d + timedelta(days=(5 - d.weekday()) % 7 or 7)
    if saturday > rules.contract_go_live:
        saturday = rules.contract_go_live - timedelta(days=(rules.contract_go_live.weekday() + 2) % 7)
    t = plan["tasks"][0]
    t["deadline"], t["deadline_basis"] = saturday.isoformat(), "recommendation"
    return plan, f"DEMO: injected a weekend deadline ({saturday:%a %d %b}) into {t['id']} of the first draft"


def _merge(review: dict, findings: list[dict]) -> list[dict]:
    merged = [{**c, "from": "review_agent"} for c in review.get("corrections", [])]
    for f in findings:
        already = any(c.get("task_id") == f["task_id"] and f["rule"] in (c.get("reference") or "")
                      for c in merged)
        if not already:
            merged.append({"task_id": f["task_id"], "problem": f["problem"], "fix": f["fix"],
                           "reference": f["rule"], "from": "rule_checker"})
    return merged


def execute(run_id: str) -> None:
    with SessionLocal() as db:
        run = db.get(AgRun, run_id)
        if run is None or run.status == "completed":
            return
        sess = db.get(AgSession, run.session_id)
        run.status, run.error = "running", ""
        event(db, run, "Run started" if not run.context_version else f"Run resumed on context v{run.context_version}")
        db.commit()
        try:
            _execute(db, run, sess)
        except (LLMError, AgentOutputInvalid, SimulatedFailure) as exc:
            db.rollback()
            run = db.get(AgRun, run_id)
            reason = getattr(exc, "reason", None) or str(exc)
            run.status, run.error = "failed", reason
            event(db, run, f"Stopped: {reason}. Completed steps are kept; Resume continues from here.", "error")
            db.commit()
        except Exception as exc:  # unexpected: keep the run resumable and visible
            log.exception("run %s crashed", run_id)
            db.rollback()
            run = db.get(AgRun, run_id)
            run.status, run.error = "failed", f"Unexpected error: {exc.__class__.__name__}: {exc}"
            event(db, run, run.error, "error")
            db.commit()
        finally:
            _update_cost(db, run_id)


def _execute(db: Session, run: AgRun, sess: AgSession) -> None:
    rules = parse_rules(run.rules_yaml)

    # 1. Intake (once per run; its output is context v1)
    if not run.context_version:
        step = _step(db, run, "intake", 1, 1)
        inp = INTAKE.build_input(run.transcript, rules)
        event(db, run, "Intake Agent: extracting decisions, requirements and constraints")
        _execute_step(db, run, step, inp, lambda: INTAKE.run(db, inp, run.transcript))
        out = step.output
        db.add(AgContext(run_id=run.id, version=1, facts=out["facts"], conflicts=out["conflicts"],
                         missing=out["missing"], reason="Extracted by the Intake Agent", created_by="intake"))
        run.context_version = 1
        _message(db, run, step, "intake", "planning", "handoff",
                 f"{len(out['facts'])} facts, {len(out['conflicts'])} conflicts, {len(out['missing'])} gaps (v1)",
                 {"context_version": 1, "fact_ids": [f["id"] for f in out["facts"]]})
        db.commit()

    ctx = current_context(db, run)
    context = context_dict(ctx)
    version = ctx.version
    previous_plan, corrections = None, []
    plan, findings, review, rnd = {}, [], {}, 0

    # 2. Planning <-> Review, bounded
    for rnd in range(1, rules.max_review_rounds + 1):
        p_step = _step(db, run, "planning", version, rnd)
        if p_step.status != "succeeded":
            p_inp = PLANNING.build_input(context, rules, sess.private_notes if sess else "", previous_plan, corrections)
            event(db, run, f"Planning Agent: {'drafting' if rnd == 1 else 'revising'} the plan "
                           f"(round {rnd}, context v{version})")
            _execute_step(db, run, p_step, p_inp, lambda: PLANNING.run(db, p_inp))
            if rnd == 1 and (run.settings or {}).get("seed_violation"):
                seeded, note = _seed_violation(p_step.output, rules)
                p_step.output = {**seeded, "_demo_note": note}
                run.settings = {**run.settings, "seed_violation": False}
                event(db, run, note, "warn")
                db.commit()
        plan = {k: v for k, v in p_step.output.items() if not k.startswith("_")}
        _message(db, run, p_step, "planning", "review", "handoff",
                 f"Plan round {rnd}: {len(plan['tasks'])} tasks, {len(plan.get('open_questions', []))} open questions",
                 {"context_version": version, "round": rnd, "task_ids": [t["id"] for t in plan["tasks"]]})
        db.commit()

        findings = check_plan(plan, context["facts"], rules)
        r_step = _step(db, run, "review", version, rnd)
        if r_step.status != "succeeded":
            r_inp = REVIEW.build_input(run.transcript, context, rules, plan, findings)
            event(db, run, f"Review Agent: checking round {rnd} ({len(findings)} rule-checker finding(s))")
            _execute_step(db, run, r_step, r_inp, lambda: REVIEW.run(db, r_inp))
        review = r_step.output
        corrections = _merge(review, findings)
        approved = review["verdict"] == "approve" and not findings and not review.get("corrections")

        if approved:
            _message(db, run, r_step, "review", "coordinator", "approval",
                     f"Round {rnd}: approved", {"summary": review.get("summary", "")})
            event(db, run, f"Review Agent approved the plan in round {rnd}")
            break
        if rnd == rules.max_review_rounds:
            _message(db, run, r_step, "review", "coordinator", "final",
                     f"Round {rnd}: {len(corrections)} issue(s) still open after the last allowed round",
                     {"unresolved": corrections})
            event(db, run, f"Stopped after {rnd} review rounds with {len(corrections)} unresolved issue(s)", "warn")
            break
        _message(db, run, r_step, "review", "planning", "feedback",
                 f"Round {rnd}: {len(corrections)} correction(s) sent back", {"corrections": corrections})
        event(db, run, f"Review Agent sent {len(corrections)} correction(s) back to Planning")
        db.commit()
        previous_plan = plan

    unresolved = [] if (review.get("verdict") == "approve" and not findings and not review.get("corrections")) \
        else corrections
    run.result = {"plan": plan, "context_version": version, "rounds": rnd, "approved": not unresolved,
                  "unresolved": unresolved, "review_summary": review.get("summary", "")}
    run.status = "completed" if not unresolved else "needs_review"
    event(db, run, "Final plan ready" if not unresolved else "Final plan ready, with unresolved issues for a person")
    db.commit()


def _update_cost(db: Session, run_id: str) -> None:
    run = db.get(AgRun, run_id)
    if run is None:
        return
    total = 0.0
    for step in db.scalars(select(AgStep).where(AgStep.run_id == run_id)):
        total += sum(u.get("cost_usd", 0) for u in (step.usage or []))
    run.cost_usd = round(total, 6)
    db.commit()


def mark_interrupted_runs() -> None:
    """On start-up: a run left 'running' by a restart becomes resumable."""
    with SessionLocal() as db:
        for run in db.scalars(select(AgRun).where(AgRun.status == "running")):
            run.status, run.error = "interrupted", "The server restarted during this run. Resume continues it."
            for step in db.scalars(select(AgStep).where(AgStep.run_id == run.id, AgStep.status == "running")):
                step.status, step.error = "failed", "interrupted by a restart"
        db.commit()
