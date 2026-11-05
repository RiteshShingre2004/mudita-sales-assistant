"""Google OAuth for the test Gmail inbox and test calendar.

One consent screen grants both Gmail and Calendar scopes for the same test
account. Tokens live in the `connections` row "google" and never leave the
server. When a refresh fails with invalid_grant (revoked, or the 7-day
expiry that applies to apps in Testing mode), the connection is marked
"expired" and the dashboard shows a reconnect action. Nothing fails silently.
"""
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.actions import log_action
from app.config import settings
from app.models import Connection

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",      # read inbox, send replies
    "https://www.googleapis.com/auth/calendar.events",   # create the confirmed meeting
    "https://www.googleapis.com/auth/calendar.freebusy", # read availability
]
PROVIDER = "google"

# Tests replace this with httpx.MockTransport; production uses the real network.
HTTP_TRANSPORT: httpx.BaseTransport | None = None


def http_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), transport=HTTP_TRANSPORT)


class GoogleNotConnected(Exception):
    """No usable credentials. `status` is not_connected or expired."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


class GoogleAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Google API {status_code}: {message}")
        self.status_code = status_code


def redirect_uri() -> str:
    return settings.public_base_url.rstrip("/") + "/oauth/google/callback"


def get_row(session: Session) -> Connection:
    row = session.get(Connection, PROVIDER)
    if row is None:
        row = Connection(provider=PROVIDER, status="not_connected", credentials={}, state={})
        session.add(row)
        session.flush()
    return row


def set_status(session: Session, status: str, error: str = "", actor: str = "system") -> None:
    row = get_row(session)
    changed = row.status != status
    row.status, row.last_error = status, error
    row.last_checked_at = datetime.now(timezone.utc)
    if changed and status in ("expired", "error"):
        log_action(session, actor=actor, action=f"google_{status}", level="error",
                   summary=f"Google connection {status}: {error}")
    session.commit()


def start_url(session: Session) -> str:
    if not (settings.google_client_id and settings.google_client_secret):
        raise GoogleNotConnected("not_configured", "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set")
    row = get_row(session)
    state = secrets.token_urlsafe(24)
    row.state = {**(row.state or {}), "oauth_state": state, "oauth_started": time.time()}
    session.commit()
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",           # always return a refresh token
        "include_granted_scopes": "true",
        "state": state,
    })


def handle_callback(session: Session, code: str, state: str, actor: str) -> str:
    row = get_row(session)
    expected = (row.state or {}).get("oauth_state")
    started = (row.state or {}).get("oauth_started", 0)
    if not expected or not secrets.compare_digest(expected, state) or time.time() - started > 600:
        raise GoogleAPIError(400, "Sign-in link expired or was not started here. Start again from the dashboard.")

    with http_client() as http:
        resp = http.post(TOKEN_URL, data={
            "code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri(), "grant_type": "authorization_code",
        })
        if resp.status_code != 200:
            raise GoogleAPIError(resp.status_code, resp.text[:300])
        tok = resp.json()
        granted = set(tok.get("scope", "").split())
        missing = [s for s in SCOPES if s not in granted]
        if missing:
            raise GoogleAPIError(400, "Some permissions were not granted: " + ", ".join(s.rsplit("/", 1)[-1] for s in missing)
                                 + ". Connect again and tick every box.")
        if not tok.get("refresh_token"):
            raise GoogleAPIError(400, "Google returned no refresh token. Remove the app's access in your Google account and connect again.")

        profile = http.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                           headers={"Authorization": f"Bearer {tok['access_token']}"})
        if profile.status_code != 200:
            raise GoogleAPIError(profile.status_code, profile.text[:300])
        prof = profile.json()

    row.credentials = {
        "access_token": tok["access_token"],
        "refresh_token": tok["refresh_token"],
        "expires_at": time.time() + int(tok.get("expires_in", 3600)),
        "scopes": sorted(granted),
    }
    row.account = prof["emailAddress"].lower()
    # Start reading the inbox from "now"; older mail is not imported.
    row.state = {"gmail_history_id": str(prof["historyId"]), "skipped": []}
    row.status, row.last_error = "ok", ""
    row.last_checked_at = datetime.now(timezone.utc)
    log_action(session, actor=actor, action="google_connected",
               summary=f"Connected Google account {row.account} (Gmail + Calendar)")
    session.commit()
    return row.account


def access_token(session: Session, force_refresh: bool = False) -> str:
    row = get_row(session)
    creds = row.credentials or {}
    if not creds.get("refresh_token"):
        raise GoogleNotConnected(row.status if row.status == "expired" else "not_connected",
                                 "Google is not connected. Connect it from the Connections menu.")
    if not force_refresh and creds.get("expires_at", 0) - 60 > time.time():
        return creds["access_token"]

    try:
        with http_client() as http:
            resp = http.post(TOKEN_URL, data={
                "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                "refresh_token": creds["refresh_token"], "grant_type": "refresh_token",
            })
    except httpx.HTTPError as exc:
        set_status(session, "error", f"Could not reach Google to refresh sign-in: {exc.__class__.__name__}")
        raise GoogleAPIError(0, "network error while refreshing token")

    if resp.status_code in (400, 401) and "invalid_grant" in resp.text:
        set_status(session, "expired", "Google sign-in expired or was revoked. Reconnect Google.")
        raise GoogleNotConnected("expired", "Google sign-in expired or was revoked")
    if resp.status_code != 200:
        set_status(session, "error", f"Token refresh failed ({resp.status_code})")
        raise GoogleAPIError(resp.status_code, resp.text[:300])

    tok = resp.json()
    row.credentials = {**creds, "access_token": tok["access_token"],
                       "expires_at": time.time() + int(tok.get("expires_in", 3600))}
    if row.status != "ok":
        row.status, row.last_error = "ok", ""
    session.commit()
    return row.credentials["access_token"]


def google_request(session: Session, method: str, url: str, *, retry_on_401: bool = True, **kwargs) -> httpx.Response:
    """Authorised call with one refresh-and-retry on 401 (a 401 means the
    request was rejected before Google did anything, so retrying is safe)."""
    token = access_token(session)
    with http_client() as http:
        resp = http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if resp.status_code == 401 and retry_on_401:
            token = access_token(session, force_refresh=True)
            resp = http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
    return resp


def disconnect(session: Session, actor: str) -> None:
    row = get_row(session)
    token = (row.credentials or {}).get("refresh_token")
    if token:
        try:
            with http_client() as http:
                http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            pass  # local removal still happens
    row.credentials, row.state, row.account = {}, {}, ""
    row.status, row.last_error = "not_connected", ""
    log_action(session, actor=actor, action="google_disconnected", summary="Google disconnected", level="warn")
    session.commit()
