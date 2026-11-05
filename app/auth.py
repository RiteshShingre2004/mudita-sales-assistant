"""Dashboard login (HTTP Basic). Webhook routes are not behind this; they
verify the provider's signature instead (Step 5/6)."""
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

_basic = HTTPBasic(auto_error=False)


def require_operator(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    if not settings.dashboard_password:
        if settings.is_production:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "DASHBOARD_PASSWORD is not set")
        return "local-dev"  # open locally, never in production
    ok = credentials is not None and (
        secrets.compare_digest(credentials.username, settings.dashboard_user)
        and secrets.compare_digest(credentials.password, settings.dashboard_password)
    )
    if not ok:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "Sign in required", headers={"WWW-Authenticate": "Basic"}
        )
    return credentials.username
