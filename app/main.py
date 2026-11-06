import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.auth import require_operator
from app.config import settings
from app.db import SessionLocal, describe_database, init_db
from app.oauth_routes import router as oauth_router
from app.policy import get_active_policy_row
from app.templating import templates
from app.webhooks import router as webhook_router

BASE = Path(__file__).parent


log = logging.getLogger("uvicorn.error")  # shows in Render's logs
DB_STATE = {"ready": False, "error": ""}


def _prepare_database() -> None:
    init_db()
    with SessionLocal() as s:
        get_active_policy_row(s)  # seeds policy v1 + reps on first boot


async def _connect_database(attempts: int) -> bool:
    for i in range(1, attempts + 1):
        try:
            await asyncio.to_thread(_prepare_database)
        except Exception as exc:
            DB_STATE["error"] = f"{exc.__class__.__name__}: {str(exc).splitlines()[0][:200]}"
            log.error("Database not ready (attempt %d/%d, %s): %s", i, attempts, describe_database(), DB_STATE["error"])
            if i < attempts:
                await asyncio.sleep(3)
        else:
            DB_STATE.update(ready=True, error="")
            log.info("Database ready (%s)", describe_database())
            return True
    return False


def _link_instagram() -> None:
    from app.channels.instagram import link_account
    from app.meta import MetaAPIError

    with SessionLocal() as s:
        try:
            log.info("Instagram linked: %s", link_account(s))
        except MetaAPIError as exc:
            log.error("Instagram link failed: %s", exc.reason)


async def _keep_trying_then_start(start_background) -> None:
    while not await _connect_database(attempts=1):
        await asyncio.sleep(15)
    start_background()


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.info("Starting Lumen sales assistant (env=%s)", settings.app_env)
    tasks: list[asyncio.Task] = []

    def start_background() -> None:
        if settings.app_env != "test":
            from app.poller import run_forever

            tasks.append(asyncio.create_task(run_forever()))
            log.info("Inbox poller started")
            if settings.instagram_token:
                tasks.append(asyncio.create_task(asyncio.to_thread(_link_instagram)))

    # Bounded wait so the port always opens: a slow or missing database shows up
    # in the logs and on /healthz instead of as a silent deploy time-out.
    if await _connect_database(attempts=3):
        start_background()
    else:
        log.error("Starting without a database; retrying every 15 s. Check DATABASE_URL and the database's status.")
        tasks.append(asyncio.create_task(_keep_trying_then_start(start_background)))
    yield
    for t in tasks:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t


app = FastAPI(title="Lumen sales assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(api_router)
app.include_router(oauth_router)
app.include_router(webhook_router)


@app.api_route("/healthz", methods=["GET", "HEAD"])  # uptime monitors often use HEAD
def healthz():
    # 200 while the process is up, so Render can finish the deploy; the body says whether
    # the database is reachable.
    return {"ok": True, "database": "ready" if DB_STATE["ready"] else f"unavailable: {DB_STATE['error']}"}


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request, _: str = Depends(require_operator)):
    return templates.TemplateResponse(request, "inbox.html", {"page": "inbox"})


@app.get("/policy", response_class=HTMLResponse)
def policy_page(request: Request, _: str = Depends(require_operator)):
    return templates.TemplateResponse(request, "policy.html", {"page": "policy"})


# Public pages required to publish the Meta app (no login).
@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return templates.TemplateResponse(request, "privacy.html", {})


@app.get("/data-deletion", response_class=HTMLResponse)
def data_deletion(request: Request):
    return templates.TemplateResponse(request, "data_deletion.html", {})


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return templates.TemplateResponse(request, "about.html", {})


@app.get("/terms", response_class=HTMLResponse)
def terms(request: Request):
    return templates.TemplateResponse(request, "terms.html", {})
