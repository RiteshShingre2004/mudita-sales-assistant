from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_operator
from app.db import get_session
from app.google_oauth import GoogleAPIError, GoogleNotConnected, handle_callback, start_url

router = APIRouter()


def _problem(request: Request, message: str) -> HTMLResponse:
    from app.templating import templates

    return templates.TemplateResponse(request, "connect_result.html",
                                      {"page": "connect", "ok": False, "message": message}, status_code=400)


@router.get("/oauth/google/start")
def google_start(request: Request, session: Session = Depends(get_session), _: str = Depends(require_operator)):
    try:
        return RedirectResponse(start_url(session), status_code=302)
    except GoogleNotConnected as exc:
        return _problem(request, str(exc))


@router.get("/oauth/google/callback")
def google_callback(request: Request, code: str = "", state: str = "", error: str = "",
                    session: Session = Depends(get_session), operator: str = Depends(require_operator)):
    if error:
        return _problem(request, f"Google sign-in was cancelled or refused ({error}).")
    try:
        account = handle_callback(session, code, state, actor=f"human:{operator}")
    except (GoogleAPIError, GoogleNotConnected) as exc:
        return _problem(request, str(exc))
    return RedirectResponse(f"/?connected={account}", status_code=302)
