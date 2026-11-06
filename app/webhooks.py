"""Public webhook endpoints (no dashboard login; Meta's signature is the check)."""
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse, Response

from app.actions import log_action
from app.config import settings
from app.db import SessionLocal
from app.meta import set_health, signature_ok

router = APIRouter(prefix="/webhooks")
log = logging.getLogger("webhooks")


def _verify(request: Request, provider: str) -> Response:
    p = request.query_params
    if p.get("hub.mode") == "subscribe" and settings.meta_verify_token and \
            p.get("hub.verify_token") == settings.meta_verify_token:
        with SessionLocal() as s:
            log_action(s, actor="webhook", action=f"{provider}_webhook_verified",
                       summary=f"Meta verified the {provider} webhook URL")
            s.commit()
        return PlainTextResponse(p.get("hub.challenge", ""))
    return PlainTextResponse("verification failed", status_code=403)


def run_pipeline(conversation_ids: list[int]) -> None:
    from app.models import Conversation
    from app.pipeline import flag, handle_inbound

    for cid in conversation_ids:
        with SessionLocal() as s:
            conv = s.get(Conversation, cid)
            try:
                handle_inbound(s, conv)
            except Exception as exc:  # stored already; make sure a person sees it wasn't handled
                log.exception("pipeline failed for conversation %s", cid)
                s.rollback()
                flag(s, s.get(Conversation, cid), f"The assistant hit an unexpected error "
                                                  f"({exc.__class__.__name__}). Reply yourself or use Retry.",
                     level="error")


def _mark_webhook_seen(session, provider: str) -> None:
    from datetime import datetime, timezone

    from app.models import Connection

    row = session.get(Connection, provider)
    if row is None:
        row = Connection(provider=provider, status="ok", credentials={}, state={})
        session.add(row)
    row.state = {**(row.state or {}), "last_webhook_at": datetime.now(timezone.utc).isoformat()}
    session.commit()


def _secrets_for(provider: str) -> list[str]:
    # Instagram Login apps may sign with the Instagram app secret; accept either.
    if provider == "instagram":
        return [settings.instagram_app_secret, settings.meta_app_secret]
    return [settings.meta_app_secret]


async def _receive(request: Request, background: BackgroundTasks, provider: str, store) -> Response:
    raw = await request.body()
    secrets = [x for x in _secrets_for(provider) if x]
    if not secrets:
        with SessionLocal() as s:
            set_health(s, provider, "error", "No app secret is set, so incoming messages can't be verified")
        return PlainTextResponse("not configured", status_code=503)
    if not signature_ok(raw, request.headers.get("x-hub-signature-256"), secrets):
        with SessionLocal() as s:
            log_action(s, actor="webhook", action="webhook_rejected", level="warn",
                       summary=f"Rejected a {provider} webhook call with a missing or wrong signature")
            s.commit()
            # Show it in the Connections menu: a wrong META_APP_SECRET otherwise looks like silence.
            set_health(s, provider, "error", "A message arrived but its signature did not match. Check that "
                                             + ("INSTAGRAM_APP_SECRET" if provider == "instagram" else "META_APP_SECRET")
                                             + " is this app's secret.")
        return PlainTextResponse("bad signature", status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return PlainTextResponse("bad json", status_code=400)

    with SessionLocal() as s:
        _mark_webhook_seen(s, provider)
        to_process = store(s, payload)
    if to_process:
        background.add_task(run_pipeline, to_process)
    return PlainTextResponse("ok")


@router.get("/whatsapp")
def whatsapp_verify(request: Request):
    return _verify(request, "whatsapp")


@router.post("/whatsapp")
async def whatsapp_receive(request: Request, background: BackgroundTasks):
    from app.channels.whatsapp import store_webhook

    return await _receive(request, background, "whatsapp", store_webhook)


@router.get("/instagram")
def instagram_verify(request: Request):
    return _verify(request, "instagram")


@router.post("/instagram")
async def instagram_receive(request: Request, background: BackgroundTasks):
    from app.channels.instagram import store_webhook

    return await _receive(request, background, "instagram", store_webhook)
