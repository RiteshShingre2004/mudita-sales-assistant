import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.auth import require_operator
from app.config import settings
from app.db import SessionLocal, init_db
from app.oauth_routes import router as oauth_router
from app.policy import get_active_policy_row
from app.templating import templates
from app.webhooks import router as webhook_router

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as s:
        get_active_policy_row(s)  # seeds policy v1 + reps on first boot
    poller = None
    if settings.app_env != "test":
        from app.poller import run_forever

        poller = asyncio.create_task(run_forever())
    yield
    if poller:
        poller.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poller


app = FastAPI(title="Lumen sales assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(api_router)
app.include_router(oauth_router)
app.include_router(webhook_router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request, _: str = Depends(require_operator)):
    return templates.TemplateResponse(request, "inbox.html", {"page": "inbox"})


@app.get("/policy", response_class=HTMLResponse)
def policy_page(request: Request, _: str = Depends(require_operator)):
    return templates.TemplateResponse(request, "policy.html", {"page": "policy"})
