"""Reply wording.

Code decides WHAT the reply does (ask for X, decline for reason Y, introduce
rep Z). The LLM only words it, from a short list of allowed facts. Every
draft is then checked; if it breaks a rule it is thrown away and a fixed
template is sent instead. The template alone is always a correct reply.
"""
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import settings
from app.llm import LLMError, chat
from app.policy import Policy
from app.rules import FIELD_WORDS


@dataclass
class ReplyPlan:
    kind: str                     # ask | decline | qualified | handoff
    channel: str
    lead_name: str = ""
    ask_for: list[str] = field(default_factory=list)
    decline_reason: str = ""      # out_of_scope | intent | budget | timeline | role
    rep_name: str = ""
    service_name: str = ""


def _first_name(name: str) -> str:
    name = (name or "").strip()
    if not name or "@" in name or name.startswith("+") or name.isdigit():
        return ""
    return name.split()[0][:30]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def template(plan: ReplyPlan, policy: Policy) -> str:
    hi = f"Hi {_first_name(plan.lead_name)}," if _first_name(plan.lead_name) else "Hi,"
    services = _join([s.name for s in policy.services])
    q = policy.qualification
    if plan.kind == "ask":
        wants = _join([FIELD_WORDS.get(f, f.replace("_", " ")) for f in plan.ask_for])
        about = f" about {plan.service_name}" if plan.service_name else ""
        body = (f"Thanks for getting in touch{about}. So I can match you with the right person, "
                f"could you tell me {wants}?")
    elif plan.kind == "decline":
        body = {
            "out_of_scope": f"Thanks for reaching out. We focus on {services}, so this isn't something we can help with.",
            "budget": (f"Thanks for the details. Our projects start at ${q.min_budget_usd:,.0f}, so we're not the "
                       "right fit for this one right now."),
            "timeline": (f"Thanks for the details. We plan new projects up to {q.max_start_within_months} months "
                         "ahead, so feel free to get back in touch closer to your start date."),
            "role": ("Thanks for the details. We usually start with whoever owns the decision, so feel free to "
                     "loop them in and they can reach out to us."),
            "intent": f"Thanks for your message. This inbox handles client inquiries about {services} only.",
        }.get(plan.decline_reason, "Thanks for reaching out. This isn't something we can help with.")
    elif plan.kind == "qualified":
        about = f" for {plan.service_name}" if plan.service_name else ""
        body = (f"Thanks, that's everything I need. You sound like a great fit{about}, and "
                f"{plan.rep_name} from our team will be your contact.")
    else:  # handoff
        body = "Thanks for your patience. A member of our team will reply to you personally."
    if plan.channel == "email":
        sign = "" if plan.kind == "qualified" else f"\n\n{policy.company_name}"  # qualified: slots follow
        return f"{hi}\n\n{body}{sign}"
    return f"{hi} {body}"


_WRITER = """You write one short reply for {company}'s sales inbox on {channel}.
Say what the TASK says, in a warm, plain, professional tone. {style}
Use only the FACTS below. Do not mention any other people, prices, dates, links,
email addresses, phone numbers or promises. Do not say a meeting is booked,
scheduled or confirmed. Do not answer questions the task does not cover.
Reply with the message text only."""


def _writer_task(plan: ReplyPlan, policy: Policy) -> tuple[str, list[str]]:
    facts = [f"Company: {policy.company_name}", "Services: " + _join([s.name for s in policy.services])]
    if plan.lead_name and _first_name(plan.lead_name):
        facts.append(f"Lead's first name: {_first_name(plan.lead_name)}")
    if plan.service_name:
        facts.append(f"They are interested in: {plan.service_name}")
    if plan.kind == "ask":
        task = "Thank them and ask, in one friendly question, for: " + _join(
            [FIELD_WORDS.get(f, f) for f in plan.ask_for]) + ". Ask nothing else."
    elif plan.kind == "decline":
        detail = {
            "budget": f"Our projects start at ${policy.qualification.min_budget_usd:,.0f}.",
            "timeline": f"We plan projects up to {policy.qualification.max_start_within_months} months ahead.",
            "out_of_scope": "What they asked for is not one of our services.",
            "role": "We start with the person who owns the decision; they can ask that person to reach out.",
            "intent": "This inbox only handles client inquiries.",
        }.get(plan.decline_reason, "")
        facts.append(detail)
        task = "Politely say we are not the right fit, for the reason in FACTS. Do not offer a call."
    elif plan.kind == "qualified":
        facts.append(f"Their contact at our company: {plan.rep_name}")
        task = ("Thank them, say they look like a great fit, and name their contact. Do not mention any times "
                "or dates; available times are added below your message automatically.")
    else:
        task = "Say a member of the team will reply personally. Nothing else."
    return task, facts


_BOOKED = re.compile(r"\b(booked|scheduled|confirmed|calendar invite|invite (?:has|was) been sent)\b", re.I)
_CONTACT = re.compile(r"https?://|www\.|\S+@\S+\.\w+|\+?\d[\d\s\-]{8,}\d")
_TITLES = re.compile(r"\b(ceo|cto|founder|co-founder|managing director|president)\b", re.I)
_MONEY = re.compile(r"(?:\$|usd\s?|₹|inr\s?|€|£)\s?([\d,]+(?:\.\d+)?)\s?(k)?", re.I)


def check_draft(text: str, plan: ReplyPlan, policy: Policy) -> str | None:
    """Return why a drafted reply is unsafe, or None if it is fine."""
    if not text or len(text) > 1200:
        return "empty or too long"
    if _BOOKED.search(text):
        return "claims a meeting is booked"
    if _CONTACT.search(text):
        return "contains a link, email address or phone number"
    if _TITLES.search(text):
        return "mentions a job title such as CEO or founder"
    other_reps = [r.name for r in policy.reps if r.name != plan.rep_name]
    for name in other_reps:
        if name.lower() in text.lower() or name.split()[0].lower() in re.findall(r"[a-z]+", text.lower()):
            return f"mentions {name}, who is not this lead's contact"
    if plan.kind == "qualified" and plan.rep_name and plan.rep_name.split()[0].lower() not in text.lower():
        return "does not name the assigned contact"
    allowed_money = {float(policy.qualification.min_budget_usd)}
    for m in _MONEY.finditer(text):
        value = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        if value not in allowed_money:
            return f"states an amount ({m.group(0).strip()}) that is not in the policy"
    if plan.kind == "ask" and "?" not in text:
        return "does not ask a question"
    if plan.kind in ("decline", "handoff") and re.search(r"\b(call|meeting)\b", text, re.I):
        return "offers a call when it should not"
    return None


def compose(session: Session, plan: ReplyPlan, policy: Policy) -> tuple[str, dict]:
    """Return (text, info). info says whether the LLM draft or the template was used."""
    fallback = template(plan, policy)
    task, facts = _writer_task(plan, policy)
    if plan.channel != "email":
        style = "Keep it under 50 words, no sign-off."
    elif plan.kind == "qualified":
        style = "Start with 'Hi <first name>,' (or 'Hi,'), keep it under 60 words, no sign-off."
    else:
        style = (f"Start with 'Hi <first name>,' (or 'Hi,'), keep it under 90 words, and end with a line "
                 f"'{policy.company_name}'.")
    try:
        result = chat([
            {"role": "system", "content": _WRITER.format(company=policy.company_name, channel=plan.channel, style=style)},
            {"role": "user", "content": "TASK: " + task + "\nFACTS:\n- " + "\n- ".join(facts)},
        ], model=settings.groq_reply_model, session=session, temperature=0.4, max_tokens=800)
    except LLMError as exc:
        return fallback, {"source": "template", "why": f"writer unavailable: {exc.reason}"}
    draft = result.content.strip().strip('"')
    problem = check_draft(draft, plan, policy)
    if problem:
        return fallback, {"source": "template", "why": f"draft rejected: {problem}", "usage": result.usage(),
                          "rejected_draft": draft[:600]}
    return draft, {"source": "llm", "usage": result.usage()}
