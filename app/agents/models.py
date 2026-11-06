"""Shared context for the three-agent coordinator.

- AgSession   a private workspace; runs and private notes never cross sessions
- AgRun       one coordinator run (run id, transcript, rules, status, settings)
- AgContext   versioned source facts. Intake creates v1; every user correction creates
              a new version; planning/review always say which version they used
- AgStep      one agent execution, unique per (run, agent, context version, round), so a
              resumed run reuses completed steps instead of calling the model again
- AgMessage   handoff messages between agents (validated payloads)
- AgEvent     the orchestration log shown in the UI
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(n: int = 10) -> str:
    return uuid.uuid4().hex[:n]


class AgSession(Base):
    __tablename__ = "ag_sessions"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=lambda: new_id(12))
    name: Mapped[str] = mapped_column(String(80))
    private_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgRun(Base):
    __tablename__ = "ag_runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=lambda: new_id(10))
    session_id: Mapped[str] = mapped_column(ForeignKey("ag_sessions.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    transcript: Mapped[str] = mapped_column(Text)
    rules_yaml: Mapped[str] = mapped_column(Text)
    # pending | running | needs_review | completed | failed | interrupted
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    context_version: Mapped[int] = mapped_column(Integer, default=0)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)   # fail_next, seed_violation
    result: Mapped[dict] = mapped_column(JSON, default=dict)     # final plan + unresolved issues
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AgContext(Base):
    __tablename__ = "ag_contexts"
    __table_args__ = (UniqueConstraint("run_id", "version", name="uq_ag_context_version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ag_runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    facts: Mapped[list] = mapped_column(JSON, default=list)
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    missing: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(40), default="intake")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgStep(Base):
    __tablename__ = "ag_steps"
    __table_args__ = (UniqueConstraint("run_id", "agent", "context_version", "round", name="uq_ag_step"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ag_runs.id"), index=True)
    agent: Mapped[str] = mapped_column(String(20))           # intake | planning | review
    context_version: Mapped[int] = mapped_column(Integer)
    round: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending")   # running | succeeded | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgMessage(Base):
    __tablename__ = "ag_messages"
    __table_args__ = (UniqueConstraint("step_id", "kind", name="uq_ag_message_step_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ag_runs.id"), index=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("ag_steps.id"))
    from_agent: Mapped[str] = mapped_column(String(20))
    to_agent: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(20))   # handoff | feedback | approval | final
    context_version: Mapped[int] = mapped_column(Integer)
    round: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgEvent(Base):
    __tablename__ = "ag_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("ag_runs.id"), index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
