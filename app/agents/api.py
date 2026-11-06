"""JSON API for the three-agent coordinator. Every run is addressed through its session,
and a run is only visible from the session that owns it."""
from datetime import timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.agents import number_lines
from app.agents.models import AgContext, AgEvent, AgMessage, AgRun, AgSession, AgStep
from app.agents.orchestrator import correct_fact, create_run, create_session, reset_session, start_in_background
from app.agents.rules import RulesError
from app.agents.sample import SAMPLE_RULES, SAMPLE_TITLE, SAMPLE_TRANSCRIPT
from app.auth import require_operator
from app.db import get_session

router = APIRouter(prefix="/api/agents", dependencies=[Depends(require_operator)])
AGENTS = ("intake", "planning", "review")


def _iso(dt):
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _session(db: Session, sid: str) -> AgSession:
    s = db.get(AgSession, sid)
    if s is None:
        raise HTTPException(404, "Session not found")
    return s


def _run(db: Session, sid: str, rid: str) -> AgRun:
    run = db.get(AgRun, rid)
    if run is None or run.session_id != sid:   # another session's run is simply not there
        raise HTTPException(404, "Run not found in this session")
    return run


@router.get("/sample")
def sample():
    return {"title": SAMPLE_TITLE, "transcript": SAMPLE_TRANSCRIPT, "rules_yaml": SAMPLE_RULES}


# ---------------------------------------------------------------- sessions

class SessionIn(BaseModel):
    name: str


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_session)):
    rows = db.scalars(select(AgSession).order_by(AgSession.created_at)).all()
    counts = dict(db.execute(select(AgRun.session_id, func.count()).group_by(AgRun.session_id)).all())
    return [{"id": s.id, "name": s.name, "runs": counts.get(s.id, 0), "has_notes": bool(s.private_notes),
             "created_at": _iso(s.created_at)} for s in rows]


@router.post("/sessions")
def new_session(body: SessionIn, db: Session = Depends(get_session)):
    s = create_session(db, body.name)
    return {"id": s.id, "name": s.name}


@router.get("/sessions/{sid}")
def get_session_detail(sid: str, db: Session = Depends(get_session)):
    s = _session(db, sid)
    runs = db.scalars(select(AgRun).where(AgRun.session_id == sid).order_by(AgRun.created_at.desc())).all()
    return {"id": s.id, "name": s.name, "private_notes": s.private_notes,
            "runs": [{"id": r.id, "title": r.title, "status": r.status, "context_version": r.context_version,
                      "cost_usd": r.cost_usd, "created_at": _iso(r.created_at)} for r in runs]}


class NotesIn(BaseModel):
    notes: str


@router.put("/sessions/{sid}/notes")
def set_notes(sid: str, body: NotesIn, db: Session = Depends(get_session)):
    s = _session(db, sid)
    s.private_notes = body.notes[:4000]
    db.commit()
    return {"ok": True}


@router.post("/sessions/{sid}/reset")
def reset(sid: str, db: Session = Depends(get_session)):
    _session(db, sid)
    return {"deleted_runs": reset_session(db, sid)}


# ---------------------------------------------------------------- runs

class RunIn(BaseModel):
    title: str = ""
    transcript: str
    rules_yaml: str
    simulate_failure: str | None = None     # "intake" | "planning" | "review" (demo)
    seed_violation: bool = False            # demo


@router.post("/sessions/{sid}/runs")
def new_run(sid: str, body: RunIn, db: Session = Depends(get_session)):
    _session(db, sid)
    if body.simulate_failure and body.simulate_failure not in AGENTS:
        raise HTTPException(422, "simulate_failure must be intake, planning or review")
    try:
        run = create_run(db, sid, body.title, body.transcript, body.rules_yaml, body.simulate_failure,
                         body.seed_violation)
    except (RulesError, ValueError) as exc:
        raise HTTPException(422, str(exc))
    start_in_background(run.id)
    return {"id": run.id}


@router.post("/sessions/{sid}/runs/{rid}/resume")
def resume(sid: str, rid: str, db: Session = Depends(get_session)):
    run = _run(db, sid, rid)
    if run.status == "completed":
        raise HTTPException(409, "This run is already complete")
    if not start_in_background(run.id):
        raise HTTPException(409, "This run is already working")
    return {"ok": True}


class FailIn(BaseModel):
    agent: str


@router.post("/sessions/{sid}/runs/{rid}/simulate-failure")
def simulate_failure(sid: str, rid: str, body: FailIn, db: Session = Depends(get_session)):
    run = _run(db, sid, rid)
    if body.agent not in AGENTS:
        raise HTTPException(422, "agent must be intake, planning or review")
    run.settings = {**(run.settings or {}), "fail_next": body.agent}
    db.add(AgEvent(run_id=run.id, level="warn",
                   text=f"DEMO: the next {body.agent} model call will fail (simulated fault)"))
    db.commit()
    return {"ok": True}


class CorrectionIn(BaseModel):
    text: str | None = None
    owner: str | None = None
    deadline: str | None = None
    reason: str = ""


@router.post("/sessions/{sid}/runs/{rid}/facts/{fid}")
def correct(sid: str, rid: str, fid: str, body: CorrectionIn, db: Session = Depends(get_session),
            operator: str = Depends(require_operator)):
    run = _run(db, sid, rid)
    try:
        ctx = correct_fact(db, run, fid, body.text, body.owner, body.deadline, body.reason, f"human:{operator}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    start_in_background(run.id)
    return {"context_version": ctx.version}


@router.get("/sessions/{sid}/runs/{rid}")
def run_detail(sid: str, rid: str, db: Session = Depends(get_session)):
    run = _run(db, sid, rid)
    contexts = db.scalars(select(AgContext).where(AgContext.run_id == rid).order_by(AgContext.version)).all()
    steps = db.scalars(select(AgStep).where(AgStep.run_id == rid).order_by(AgStep.id)).all()
    messages = db.scalars(select(AgMessage).where(AgMessage.run_id == rid).order_by(AgMessage.id)).all()
    events = db.scalars(select(AgEvent).where(AgEvent.run_id == rid).order_by(AgEvent.id.desc()).limit(200)).all()
    return {
        "id": run.id, "title": run.title, "status": run.status, "error": run.error,
        "context_version": run.context_version, "settings": run.settings, "result": run.result,
        "cost_usd": run.cost_usd, "created_at": _iso(run.created_at),
        "transcript_lines": [{"n": n, "text": t} for n, t in number_lines(run.transcript)],
        "rules_yaml": run.rules_yaml,
        "contexts": [{"version": c.version, "facts": c.facts, "conflicts": c.conflicts, "missing": c.missing,
                      "reason": c.reason, "created_by": c.created_by, "created_at": _iso(c.created_at)}
                     for c in contexts],
        "steps": [{"id": s.id, "agent": s.agent, "context_version": s.context_version, "round": s.round,
                   "status": s.status, "attempts": s.attempts, "error": s.error, "input": s.input,
                   "output": s.output, "usage": s.usage, "started_at": _iso(s.started_at),
                   "finished_at": _iso(s.finished_at), "stale": s.context_version < run.context_version
                   and s.agent != "intake"} for s in steps],
        "messages": [{"id": m.id, "from": m.from_agent, "to": m.to_agent, "kind": m.kind,
                      "context_version": m.context_version, "round": m.round, "summary": m.summary,
                      "payload": m.payload, "at": _iso(m.created_at)} for m in messages],
        "events": [{"level": e.level, "text": e.text, "at": _iso(e.created_at)} for e in events],
    }
