"""Editable business policy: schema, validation, versioned storage.

The policy is the single source of truth for services, qualification
criteria, staff, routing, hours and meeting rules. It is edited by a human
in the dashboard. Inbound messages and LLM output never modify it.
"""
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.actions import log_action
from app.models import PolicyVersion, Rep

DEFAULT_POLICY_PATH = Path(__file__).parent / "default_policy.yaml"

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
KNOWN_FIELDS = {"service", "budget_usd", "start_within_months", "role", "company"}
KNOWN_ROLES = {"decision_maker", "influencer", "end_user", "unknown"}
KNOWN_INTENTS = {"job_application", "student_project", "vendor_pitch", "spam"}
SLUG = r"^[a-z][a-z0-9_]{1,40}$"


class Strict(BaseModel):
    """Unknown keys are errors, so a typo cannot silently fall back to a default."""

    model_config = ConfigDict(extra="forbid")


class Service(Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=2)
    description: str = ""


class Qualification(Strict):
    required_fields: list[str]
    min_budget_usd: int = Field(ge=0)
    max_start_within_months: int = Field(ge=0, le=36)
    eligible_roles: list[str]
    disqualifying_intents: list[str] = []

    @field_validator("required_fields")
    @classmethod
    def _fields_known(cls, v: list[str]) -> list[str]:
        unknown = set(v) - KNOWN_FIELDS
        if unknown:
            raise ValueError(f"unknown required field(s) {sorted(unknown)}; allowed: {sorted(KNOWN_FIELDS)}")
        return v

    @field_validator("eligible_roles")
    @classmethod
    def _roles_known(cls, v: list[str]) -> list[str]:
        unknown = set(v) - KNOWN_ROLES
        if unknown:
            raise ValueError(f"unknown role(s) {sorted(unknown)}; allowed: {sorted(KNOWN_ROLES)}")
        return v

    @field_validator("disqualifying_intents")
    @classmethod
    def _intents_known(cls, v: list[str]) -> list[str]:
        unknown = set(v) - KNOWN_INTENTS
        if unknown:
            raise ValueError(f"unknown intent(s) {sorted(unknown)}; allowed: {sorted(KNOWN_INTENTS)}")
        return v


class RepPolicy(Strict):
    id: str = Field(pattern=SLUG)
    name: str = Field(min_length=2)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    specialties: list[str] = []
    active: bool = True


class RoutingRule(Strict):
    service: str
    rep: str


class Routing(Strict):
    rules: list[RoutingRule] = []
    fallback: str = "least_open_leads"


class WorkingHours(Strict):
    timezone: str
    days: list[str]
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"'{v}' is not a valid IANA timezone, e.g. Asia/Kolkata")
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        bad = [d for d in v if d not in DAY_NAMES]
        if bad or not v:
            raise ValueError(f"days must be a non-empty list of {DAY_NAMES}")
        return v

    @model_validator(mode="after")
    def _order(self) -> "WorkingHours":
        if self.start >= self.end:
            raise ValueError("working_hours.start must be before working_hours.end")
        return self


class Meeting(Strict):
    title: str = "Intro call"
    duration_minutes: int = Field(ge=15, le=120)
    min_notice_hours: int = Field(ge=0, le=72)
    horizon_days: int = Field(ge=1, le=30)
    slots_to_offer: int = Field(ge=1, le=5)


class ConversationRules(Strict):
    max_follow_up_questions: int = Field(default=4, ge=1, le=10)


class Currency(Strict):
    """Fixed conversion rates, used only to compare a stated budget with the minimum."""

    rates_to_usd: dict[str, float] = Field(default_factory=lambda: {"INR": 0.012, "EUR": 1.08, "GBP": 1.27})

    @field_validator("rates_to_usd")
    @classmethod
    def _codes(cls, v: dict[str, float]) -> dict[str, float]:
        for code, rate in v.items():
            if len(code) != 3 or not code.isupper() or not code.isalpha():
                raise ValueError(f"'{code}' is not a 3-letter currency code like INR")
            if rate <= 0:
                raise ValueError(f"rate for {code} must be above 0")
        return v

    def to_usd(self, amount: float, code: str) -> float | None:
        code = (code or "USD").upper()
        if code == "USD":
            return amount
        rate = self.rates_to_usd.get(code)
        return amount * rate if rate else None


class Policy(Strict):
    company_name: str = Field(min_length=2)
    description: str = ""
    services: list[Service] = Field(min_length=1)
    qualification: Qualification
    reps: list[RepPolicy] = Field(min_length=1)
    routing: Routing
    working_hours: WorkingHours
    meeting: Meeting
    conversation: ConversationRules = ConversationRules()
    currency: Currency = Currency()

    @model_validator(mode="after")
    def _cross_refs(self) -> "Policy":
        service_ids = [s.id for s in self.services]
        rep_ids = [r.id for r in self.reps]
        if len(set(service_ids)) != len(service_ids):
            raise ValueError("service ids must be unique")
        if len(set(rep_ids)) != len(rep_ids):
            raise ValueError("rep ids must be unique")
        if not any(r.active for r in self.reps):
            raise ValueError("at least one rep must be active")
        for rep in self.reps:
            for s in rep.specialties:
                if s not in service_ids:
                    raise ValueError(f"rep '{rep.id}' has unknown specialty '{s}'")
        for i, rule in enumerate(self.routing.rules):
            if rule.service not in service_ids:
                raise ValueError(f"routing rule {i + 1} uses unknown service '{rule.service}'")
            if rule.rep not in rep_ids:
                raise ValueError(f"routing rule {i + 1} uses unknown rep '{rule.rep}'")
        if self.routing.fallback != "least_open_leads" and self.routing.fallback not in rep_ids:
            raise ValueError("routing.fallback must be 'least_open_leads' or an existing rep id")
        return self

    # helpers used by later steps
    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)

    def rep(self, rep_id: str) -> RepPolicy | None:
        return next((r for r in self.reps if r.id == rep_id), None)


class PolicyError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def parse_policy(source_yaml: str) -> Policy:
    """Parse and validate YAML text. Raises PolicyError with readable messages."""
    try:
        raw = yaml.safe_load(source_yaml)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}, column {mark.column + 1}: " if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise PolicyError([f"YAML syntax error at {where}{problem}"])
    if not isinstance(raw, dict):
        raise PolicyError(["policy must be a YAML mapping at the top level"])
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        errors = []
        for e in exc.errors():
            loc = ".".join(str(p) for p in e["loc"]) or "policy"
            errors.append(f"{loc}: {e['msg'].removeprefix('Value error, ')}")
        raise PolicyError(errors)


def _sync_reps(session: Session, policy: Policy) -> None:
    """Keep the reps table in step with the policy. Reps removed from the
    policy are deactivated, not deleted, so past assignments stay readable."""
    existing = {r.id: r for r in session.scalars(select(Rep))}
    for rp in policy.reps:
        row = existing.pop(rp.id, None) or Rep(id=rp.id)
        row.name, row.email, row.active = rp.name, rp.email, rp.active
        session.add(row)
    for leftover in existing.values():
        leftover.active = False


def save_policy(session: Session, source_yaml: str, actor: str) -> PolicyVersion:
    policy = parse_policy(source_yaml)
    latest = session.scalar(select(PolicyVersion).order_by(PolicyVersion.version.desc()).limit(1))
    version = PolicyVersion(
        version=(latest.version + 1) if latest else 1,
        source_yaml=source_yaml,
        data=policy.model_dump(),
        created_by=actor,
    )
    session.add(version)
    _sync_reps(session, policy)
    log_action(
        session,
        actor=actor,
        action="policy_saved",
        summary=f"Policy version {version.version} saved",
        detail={"version": version.version},
    )
    session.commit()
    return version


def get_active_policy_row(session: Session) -> PolicyVersion:
    row = session.scalar(select(PolicyVersion).order_by(PolicyVersion.version.desc()).limit(1))
    if row is None:
        row = save_policy(session, DEFAULT_POLICY_PATH.read_text(), actor="system")
    return row


def get_active_policy(session: Session) -> tuple[Policy, int]:
    row = get_active_policy_row(session)
    return Policy.model_validate(row.data), row.version
