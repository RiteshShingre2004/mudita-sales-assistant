"""Google Calendar: free/busy and event creation for the test calendar.

Availability comes from one calendar (GOOGLE_CALENDAR_ID, default the
connected account's primary calendar), which stands in for the team's
calendar; the fictional reps do not have real calendars.
"""
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.google_oauth import GoogleAPIError, google_request
from app.policy import DAY_NAMES, Policy

API = "https://www.googleapis.com/calendar/v3"


def calendar_id() -> str:
    return os.environ.get("GOOGLE_CALENDAR_ID", "primary").strip() or "primary"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def busy_intervals(session: Session, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    cal = calendar_id()
    resp = google_request(session, "POST", f"{API}/freeBusy", json={
        "timeMin": start.astimezone(timezone.utc).isoformat(), "timeMax": end.astimezone(timezone.utc).isoformat(),
        "items": [{"id": cal}],
    })
    if resp.status_code != 200:
        raise GoogleAPIError(resp.status_code, resp.text[:300])
    info = resp.json().get("calendars", {}).get(cal, {})
    if info.get("errors"):
        raise GoogleAPIError(403, f"cannot read calendar '{cal}': {info['errors'][0].get('reason')}")
    return [(_parse(b["start"]), _parse(b["end"])) for b in info.get("busy", [])]


def candidate_slots(policy: Policy, now: datetime, busy: list[tuple[datetime, datetime]],
                    held: list[datetime] | None = None) -> list[tuple[datetime, datetime]]:
    """Free slots inside working hours, after the notice period, spread across days."""
    wh, m = policy.working_hours, policy.meeting
    tz = ZoneInfo(wh.timezone)
    length = timedelta(minutes=m.duration_minutes)
    earliest = _aware(now) + timedelta(hours=m.min_notice_hours)
    held_set = {_aware(h) for h in (held or [])}
    sh, sm = map(int, wh.start.split(":"))
    eh, em = map(int, wh.end.split(":"))

    by_day: dict[str, list[tuple[datetime, datetime]]] = {}
    local_today = _aware(now).astimezone(tz).date()
    for offset in range(m.horizon_days + 1):
        day = local_today + timedelta(days=offset)
        if DAY_NAMES[day.weekday()] not in wh.days:
            continue
        t = datetime(day.year, day.month, day.day, sh, sm, tzinfo=tz)
        day_end = datetime(day.year, day.month, day.day, eh, em, tzinfo=tz)
        while t + length <= day_end:
            s, e = t.astimezone(timezone.utc), (t + length).astimezone(timezone.utc)
            clash = any(s < _aware(be) and _aware(bs) < e for bs, be in busy)
            if s >= earliest and not clash and s not in held_set:
                by_day.setdefault(day.isoformat(), []).append((s, e))
            t += timedelta(minutes=30)

    # One slot per day first (morning-ish, the first free one), then fill from the earliest days.
    picked: list[tuple[datetime, datetime]] = []
    for day in sorted(by_day):
        if len(picked) == m.slots_to_offer:
            break
        picked.append(by_day[day][0])
    for day in sorted(by_day):
        for slot in by_day[day][1:]:
            if len(picked) >= m.slots_to_offer:
                break
            picked.append(slot)
    return sorted(picked)[: m.slots_to_offer]


def is_free(session: Session, start: datetime, end: datetime) -> bool:
    return not busy_intervals(session, start, end)


def get_event(session: Session, event_id: str) -> dict | None:
    resp = google_request(session, "GET", f"{API}/calendars/{calendar_id()}/events/{event_id}")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise GoogleAPIError(resp.status_code, resp.text[:300])
    event = resp.json()
    return None if event.get("status") == "cancelled" else event


def insert_event(session: Session, body: dict) -> tuple[str, dict | None]:
    """Returns ("created", event) or ("exists", None). Timeouts propagate (outcome unknown)."""
    resp = google_request(session, "POST", f"{API}/calendars/{calendar_id()}/events",
                          params={"sendUpdates": "none"}, json=body)
    if resp.status_code in (200, 201):
        return "created", resp.json()
    if resp.status_code == 409:
        return "exists", None
    raise GoogleAPIError(resp.status_code, resp.text[:300])


def format_slot(start: datetime, policy: Policy) -> str:
    local = _aware(start).astimezone(ZoneInfo(policy.working_hours.timezone))
    return local.strftime("%a %-d %b, %-I:%M %p ") + local.tzname()


# Errors after which a create request MAY have reached Google: outcome unknown.
TIMEOUT_ERRORS = (httpx.TimeoutException, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)
# Errors where nothing reached Google (safe to treat as "not done").
NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ProxyError)
