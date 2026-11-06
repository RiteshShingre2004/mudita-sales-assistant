"""Company rules: parsing and a deterministic checker.

The Review Agent (an LLM) reads the plan against the transcript and rules, but the
checker below is plain code and has the final word on the hard rules: a plan with
open rule violations is never marked approved, whatever the model says.
"""
from datetime import date

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class TeamMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    role: str = ""
    tags: list[str] = []


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str


class CompanyRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str
    meeting_date: date
    contract_go_live: date
    max_tasks_per_owner: int = Field(default=3, ge=1, le=20)
    max_review_rounds: int = Field(default=3, ge=1, le=6)
    team: list[TeamMember] = Field(min_length=1)
    rules: list[Rule] = Field(min_length=1)

    @field_validator("contract_go_live")
    @classmethod
    def _after_meeting(cls, v, info):
        md = info.data.get("meeting_date")
        if md and v < md:
            raise ValueError("contract_go_live must be after meeting_date")
        return v

    def member(self, name: str | None) -> TeamMember | None:
        if not name:
            return None
        n = name.strip().lower()
        for m in self.team:
            if m.name.lower() == n:
                return m
        firsts = [m for m in self.team if m.name.split()[0].lower() == n.split()[0]]
        return firsts[0] if len(firsts) == 1 and len(n.split()) == 1 else None

    def with_tag(self, tag: str) -> list[TeamMember]:
        return [m for m in self.team if tag in m.tags]


class RulesError(ValueError):
    pass


def parse_rules(text: str) -> CompanyRules:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RulesError(f"Rules are not valid YAML: {exc}")
    if not isinstance(raw, dict):
        raise RulesError("Rules must be a YAML mapping")
    try:
        return CompanyRules.model_validate(raw)
    except ValidationError as exc:
        raise RulesError("; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))


# ---------------------------------------------------------------- checker

def _d(value) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def check_plan(plan: dict, facts: list[dict], rules: CompanyRules) -> list[dict]:
    """Return rule findings as {rule, task_id, problem, fix}. Empty list = compliant."""
    tasks = plan.get("tasks", [])
    by_id = {t["id"]: t for t in tasks}
    fact_by_id = {f["id"]: f for f in facts}
    out: list[dict] = []

    def add(rule, task_id, problem, fix):
        out.append({"rule": rule, "task_id": task_id, "problem": problem, "fix": fix})

    pm = {m.name for m in rules.with_tag("pm")}
    sec = {m.name for m in rules.with_tag("security")}
    counts: dict[str, int] = {}

    for t in tasks:
        tid = t["id"]
        owner = rules.member(t.get("owner"))
        # R1: one owner from the team list
        if not t.get("owner"):
            add("R1", tid, "Task has no owner.", "Assign a team member (mark it as a recommendation if not "
                                                 "stated in the meeting).")
        elif owner is None:
            add("R1", tid, f"Owner '{t['owner']}' is not on the team list.",
                "Assign a team member; track what is needed from the client as an open question.")
        else:
            counts[owner.name] = counts.get(owner.name, 0) + 1

        # grounding of owner/deadline claims
        cited = [fact_by_id[s] for s in t.get("sources", []) if s in fact_by_id]
        for s in t.get("sources", []):
            if s not in fact_by_id:
                add("GROUNDING", tid, f"Cites '{s}', which is not a source fact.", "Cite existing fact ids only.")
        if t.get("owner_basis") == "fact" and owner is not None:
            if not any(rules.member(f.get("owner")) == owner for f in cited):
                add("GROUNDING", tid, f"Owner {owner.name} is marked as stated in the meeting, but no cited "
                                      "fact names them.", "Cite the fact that names the owner, or mark the "
                                                          "owner as a recommendation.")
        due = _d(t.get("deadline"))
        if t.get("deadline") and due is None:
            add("R6", tid, f"Deadline '{t['deadline']}' is not a date (YYYY-MM-DD).", "Use YYYY-MM-DD.")
        if due and t.get("deadline_basis") == "fact":
            if not any(f.get("deadline") == t["deadline"] for f in cited):
                add("GROUNDING", tid, f"Deadline {due} is marked as stated in the meeting, but no cited fact "
                                      "states it.", "Cite the fact with this date, or mark it as a recommendation.")

        # R2: working days
        if due and due.weekday() >= 5:
            add("R2", tid, f"Deadline {due:%a %d %b} is a weekend.", "Move it to a working day (Mon-Fri).")
        # R6: window
        if due and not (rules.meeting_date <= due <= rules.contract_go_live):
            add("R6", tid, f"Deadline {due} is outside {rules.meeting_date} to {rules.contract_go_live}.",
                "Keep deadlines between the meeting date and the contract go-live.")

        # dependencies exist, and come first
        for dep in t.get("depends_on", []):
            if dep not in by_id:
                add("STRUCTURE", tid, f"Depends on '{dep}', which does not exist.", "Remove or fix the dependency.")
            elif dep == tid:
                add("STRUCTURE", tid, "Depends on itself.", "Remove the self-dependency.")
            else:
                dd = _d(by_id[dep].get("deadline"))
                if due and dd and due < dd:
                    add("R6", tid, f"Due {due} but depends on {dep}, due later ({dd}).",
                        "Move this deadline on or after its dependency's deadline.")

        # R3: customer data needs a security review dependency
        if t.get("touches_customer_data") and not t.get("is_security_review"):
            ok = any(by_id.get(d, {}).get("is_security_review") and
                     (rules.member(by_id[d].get("owner")) or TeamMember(name="?")).name in sec
                     for d in t.get("depends_on", []))
            if not ok:
                add("R3", tid, "Touches customer data but does not depend on a security review owned by the "
                               "security lead.", "Add a security review task owned by the security lead and make "
                                                 "this task depend on it.")
        if t.get("is_security_review") and owner and owner.name not in sec:
            add("R3", tid, f"Security review is owned by {owner.name}, not the security lead.",
                "Assign the security lead.")

        # R4: client-facing deliverables need a PM sign-off that depends on them
        if t.get("client_facing") and not t.get("is_signoff"):
            ok = any(o.get("is_signoff") and tid in o.get("depends_on", []) and
                     (rules.member(o.get("owner")) or TeamMember(name="?")).name in pm for o in tasks)
            if not ok:
                add("R4", tid, "Client-facing deliverable without a PM sign-off task that depends on it.",
                    "Add (or extend) a sign-off task owned by the project manager that depends on this task.")

    # R5: capacity
    for name, n in counts.items():
        if n > rules.max_tasks_per_owner:
            add("R5", None, f"{name} owns {n} tasks (max {rules.max_tasks_per_owner}).",
                "Rebalance tasks across the team.")

    # cycles
    seen: dict[str, int] = {}

    def visit(tid, stack):
        if seen.get(tid) == 1:
            add("STRUCTURE", tid, "Dependency cycle: " + " -> ".join([*stack, tid]), "Break the cycle.")
            return
        if seen.get(tid) == 2 or tid not in by_id:
            return
        seen[tid] = 1
        for d in by_id[tid].get("depends_on", []):
            visit(d, [*stack, tid])
        seen[tid] = 2

    for tid in by_id:
        visit(tid, [])
    return out
