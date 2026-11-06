"""Assign a qualified lead to a rep, using only reps in the policy."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Lead, LeadStatus
from app.policy import Policy


def open_lead_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Lead.assigned_rep_id, func.count()).where(Lead.status == LeadStatus.QUALIFIED,
                                                         Lead.assigned_rep_id.is_not(None))
        .group_by(Lead.assigned_rep_id)
    ).all()
    return {rep: n for rep, n in rows}


def choose_rep(session: Session, policy: Policy, service: str | None,
               current_rep_id: str | None = None) -> tuple[str, str]:
    """Returns (rep_id, human-readable reason)."""
    active = {r.id: r for r in policy.reps if r.active}
    if current_rep_id in active:
        return current_rep_id, f"Already assigned to {active[current_rep_id].name}"

    for i, rule in enumerate(policy.routing.rules, start=1):
        if rule.service == service:
            if rule.rep in active:
                return rule.rep, (f"Routing rule {i}: {policy.service(service).name} goes to {active[rule.rep].name}")
            break  # matching rule points at an inactive rep: fall back

    fallback = policy.routing.fallback
    if fallback in active:
        return fallback, f"No active rule matched; policy fallback is {active[fallback].name}"
    counts = open_lead_counts(session)
    rep_id = min(sorted(active), key=lambda r: counts.get(r, 0))
    return rep_id, f"No active rule matched; {active[rep_id].name} has the fewest open leads ({counts.get(rep_id, 0)})"
