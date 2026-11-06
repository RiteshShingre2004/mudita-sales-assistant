"""Input/output contracts for the three agents.

Each agent answers in strict JSON schema mode (Groq structured outputs), and every
answer is then validated again here with pydantic plus cross-reference checks before
it may be handed to the next agent. An invalid answer is sent back to the same agent
once with the exact errors; if it is still invalid the step fails visibly.
"""
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FACT_KINDS = ["decision", "requirement", "constraint", "deliverable", "open_question"]


def _s(t="string"):
    return {"type": t}


def _ns(t="string"):
    return {"type": [t, "null"]}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


# ---------------------------------------------------------------- Intake

INTAKE_SCHEMA = {"name": "intake_output", "schema": _obj({
    "items": _arr(_obj({
        "kind": {"type": "string", "enum": FACT_KINDS},
        "text": _s(),
        "source_lines": _arr(_s("integer")),
        "quote": _s(),
        "owner": _ns(),
        "owner_quote": _ns(),
        "deadline": _ns(),
        "deadline_quote": _ns(),
    })),
    "conflicts": _arr(_obj({"text": _s(), "source_lines": _arr(_s("integer"))})),
    "missing": _arr(_obj({"text": _s(), "source_lines": _arr(_s("integer"))})),
})}


class IntakeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["decision", "requirement", "constraint", "deliverable", "open_question"]
    text: str = Field(min_length=3, max_length=400)
    source_lines: list[int] = Field(min_length=1)
    quote: str = Field(min_length=3)
    owner: str | None = None
    owner_quote: str | None = None
    deadline: str | None = None
    deadline_quote: str | None = None


class IntakeNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=3, max_length=400)
    source_lines: list[int] = Field(min_length=1)


class IntakeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[IntakeItem] = Field(min_length=1)
    conflicts: list[IntakeNote] = []
    missing: list[IntakeNote] = []


# ---------------------------------------------------------------- Planning

BASIS = ["fact", "recommendation", "none"]

PLAN_SCHEMA = {"name": "plan_output", "schema": _obj({
    "tasks": _arr(_obj({
        "id": _s(),
        "title": _s(),
        "owner": _ns(),
        "owner_basis": {"type": "string", "enum": BASIS},
        "deadline": _ns(),
        "deadline_basis": {"type": "string", "enum": BASIS},
        "depends_on": _arr(_s()),
        "sources": _arr(_s()),
        "touches_customer_data": _s("boolean"),
        "client_facing": _s("boolean"),
        "is_signoff": _s("boolean"),
        "is_security_review": _s("boolean"),
        "rationale": _s(),
    })),
    "recommendations": _arr(_obj({"text": _s(), "why": _s()})),
    "open_questions": _arr(_obj({"text": _s(), "sources": _arr(_s())})),
})}


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str = Field(min_length=3, max_length=200)
    owner: str | None = None
    owner_basis: Literal["fact", "recommendation", "none"]
    deadline: str | None = None
    deadline_basis: Literal["fact", "recommendation", "none"]
    depends_on: list[str] = []
    sources: list[str] = []
    touches_customer_data: bool = False
    client_facing: bool = False
    is_signoff: bool = False
    is_security_review: bool = False
    rationale: str = ""

    @field_validator("id")
    @classmethod
    def _tid(cls, v):
        if not re.fullmatch(r"T\d{1,3}", v):
            raise ValueError("task ids look like T1, T2, ...")
        return v

    @field_validator("deadline")
    @classmethod
    def _date(cls, v):
        if v is not None:
            date.fromisoformat(v)  # raises ValueError with a clear message
        return v


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    why: str


class OpenQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    sources: list[str] = []


class PlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[Task] = Field(min_length=1, max_length=25)
    recommendations: list[Recommendation] = []
    open_questions: list[OpenQuestion] = []


def plan_cross_check(plan: PlanOutput, fact_ids: set[str]) -> list[str]:
    """Structural problems that make a plan unusable as a handoff (not rule violations)."""
    errors = []
    ids = [t.id for t in plan.tasks]
    if len(set(ids)) != len(ids):
        errors.append("task ids must be unique")
    for t in plan.tasks:
        if t.owner_basis != "none" and not t.owner:
            errors.append(f"{t.id}: owner_basis is '{t.owner_basis}' but owner is empty")
        if t.deadline_basis != "none" and not t.deadline:
            errors.append(f"{t.id}: deadline_basis is '{t.deadline_basis}' but deadline is empty")
        if t.owner_basis == "fact" or t.deadline_basis == "fact":
            if not any(s in fact_ids for s in t.sources):
                errors.append(f"{t.id}: claims a fact basis but cites no existing fact id")
    for q in plan.open_questions:
        for s in q.sources:
            if s not in fact_ids:
                errors.append(f"open question cites unknown fact '{s}'")
    return errors


# ---------------------------------------------------------------- Review

REVIEW_SCHEMA = {"name": "review_output", "schema": _obj({
    "verdict": {"type": "string", "enum": ["approve", "revise"]},
    "corrections": _arr(_obj({
        "task_id": _ns(),
        "problem": _s(),
        "fix": _s(),
        "reference": _s(),
    })),
    "summary": _s(),
})}


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str | None = None
    problem: str = Field(min_length=3)
    fix: str = Field(min_length=3)
    reference: str = ""


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: Literal["approve", "revise"]
    corrections: list[Correction] = []
    summary: str = ""
