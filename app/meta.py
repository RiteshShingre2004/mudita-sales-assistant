"""Shared helpers for Meta webhooks and the Graph API (WhatsApp now, Instagram next).

- Webhook POSTs must carry X-Hub-Signature-256, an HMAC-SHA256 of the raw body
  with the app secret. Anything else is rejected and logged.
- Graph API errors are turned into readable reasons, and token problems mark
  the connection "expired" so the dashboard shows it.
"""
import hashlib
import hmac
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.actions import log_action
from app.config import settings
from app.models import Connection

GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v23.0")

# Tests replace this with httpx.MockTransport. The running app uses the real API.
HTTP_TRANSPORT: httpx.BaseTransport | None = None


class MetaAPIError(Exception):
    def __init__(self, reason: str, code: int | None = None, status: int = 0):
        super().__init__(reason)
        self.reason, self.code, self.status = reason, code, status


def signature_ok(raw_body: bytes, header: str | None) -> bool:
    if not settings.meta_app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(settings.meta_app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


def set_health(session: Session, provider: str, status: str, error: str = "", account: str | None = None) -> None:
    row = session.get(Connection, provider)
    if row is None:
        row = Connection(provider=provider, credentials={}, state={})
        session.add(row)
    changed = row.status != status or row.last_error != error
    row.status, row.last_error = status, error
    if account is not None:
        row.account = account
    row.last_checked_at = datetime.now(timezone.utc)
    if changed and status in ("expired", "error"):
        log_action(session, actor="system", action=f"{provider}_{status}", level="error",
                   summary=f"{provider.capitalize()} connection {status}: {error}")
    session.commit()


# Graph error codes worth explaining in plain words.
_EXPLAIN = {
    190: "The access token is invalid or expired. Create a new permanent token.",
    131047: "More than 24 hours have passed since the lead's last message, so WhatsApp only allows "
            "approved template messages now.",
    131030: "This phone number is not on the test number's allowed recipient list "
            "(WhatsApp > API Setup > To > Manage phone number list).",
    131031: "Meta has locked or restricted this WhatsApp Business Account. Check WhatsApp Manager "
            "(business.facebook.com/wa/manage) and Business Support Home for the reason.",
    131026: "WhatsApp could not deliver to this number (not on WhatsApp, or an old app version).",
    131056: "Too many messages to this number in a short time. Try again shortly.",
    130429: "WhatsApp rate limit reached. Try again shortly.",
    10: "The app does not have permission for this. Check the token's permissions.",
    200: "The app does not have permission for this. Check the token's permissions.",
}


def explain(code: int | None, fallback: str) -> str:
    return _EXPLAIN.get(code or -1, fallback)


def graph_post(session: Session, provider: str, path: str, token: str, payload: dict) -> dict:
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{path.lstrip('/')}"
    try:
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), transport=HTTP_TRANSPORT) as http:
            resp = http.post(url, json=payload, headers={"Authorization": f"Bearer {token}"})
    except httpx.TimeoutException:
        raise  # caller records "uncertain": the message may have gone out
    except httpx.HTTPError as exc:
        set_health(session, provider, "error", f"Could not reach Meta ({exc.__class__.__name__})")
        raise MetaAPIError(f"could not reach Meta ({exc.__class__.__name__})")

    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code == 200:
        set_health(session, provider, "ok")
        return data

    err = data.get("error", {}) if isinstance(data, dict) else {}
    code = err.get("code")
    detail = (err.get("error_data") or {}).get("details") or err.get("message") or f"HTTP {resp.status_code}"
    reason = explain(code, detail)
    if code == 190 or resp.status_code == 401:
        set_health(session, provider, "expired", reason)
    elif code in (10, 200):
        set_health(session, provider, "error", reason)
    raise MetaAPIError(reason, code=code, status=resp.status_code)
