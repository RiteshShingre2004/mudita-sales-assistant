"""Qualification: plain code applying the business policy to verified facts.

The LLM never decides a status. This function is deterministic: the same
facts and policy always give the same answer, with a reason for each step.
Order matters: a failed rule is decisive (not qualified) even if other
fields are still missing; otherwise any missing field means "needs info".
"""
from dataclasses import dataclass, field

from app.models import LeadStatus
from app.policy import Policy

FIELD_WORDS = {
    "service": "what you need help with",
    "budget_usd": "your approximate budget",
    "start_within_months": "when you'd like to start",
    "role": "your role in the decision",
    "company": "your company name",
}


@dataclass
class Decision:
    status: str
    reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: str = ""  # out_of_scope | intent | budget | timeline | role


def _required_key(name: str) -> str:
    return "budget_usd" if name == "budget_usd" else name


def decide(policy: Policy, fields: dict, notes: list[str] | None = None) -> Decision:
    q = policy.qualification
    reasons = list(notes or [])

    intent = fields.get("intent")
    if intent in q.disqualifying_intents:
        return Decision(LeadStatus.NOT_QUALIFIED, reasons + [f"Intent '{intent.replace('_', ' ')}' is never qualified under the policy"],
                        failed="intent")

    service = fields.get("service")
    if service == "other":
        return Decision(LeadStatus.NOT_QUALIFIED, reasons + ["They want a service we do not offer"], failed="out_of_scope")
    if service:
        reasons.append(f"Service '{policy.service(service).name}' is offered")

    budget = fields.get("budget_usd")
    if budget is not None:
        if budget < q.min_budget_usd:
            return Decision(LeadStatus.NOT_QUALIFIED,
                            reasons + [f"Budget ${budget:,.0f} is below the ${q.min_budget_usd:,.0f} minimum"], failed="budget")
        reasons.append(f"Budget ${budget:,.0f} meets the ${q.min_budget_usd:,.0f} minimum")

    start = fields.get("start_within_months")
    if start is not None:
        if start > q.max_start_within_months:
            return Decision(LeadStatus.NOT_QUALIFIED,
                            reasons + [f"Start in {start:g} months is later than {q.max_start_within_months}"], failed="timeline")
        reasons.append(f"Start in {start:g} month{'s' if start != 1 else ''} is within {q.max_start_within_months}")

    role = fields.get("role")
    if role is not None:
        if role not in q.eligible_roles:
            return Decision(LeadStatus.NOT_QUALIFIED,
                            reasons + [f"Role '{role.replace('_', ' ')}' is not one the policy qualifies"], failed="role")
        reasons.append(f"Role '{role.replace('_', ' ')}' is eligible")

    missing = [f for f in q.required_fields if fields.get(_required_key(f)) in (None, "")]
    if "budget_usd" in missing and fields.get("budget_original"):
        reasons.append(f"Budget {fields['budget_original']} cannot be converted; asking for an amount in USD")
    if missing:
        return Decision(LeadStatus.NEEDS_INFO,
                        reasons + ["Missing: " + ", ".join(m.replace("_", " ") for m in missing)], missing=missing)
    return Decision(LeadStatus.QUALIFIED, reasons)
