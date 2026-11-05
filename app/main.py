from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api import router as api_router
from app.auth import require_operator
from app.db import SessionLocal, init_db
from app.policy import get_active_policy_row

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    with SessionLocal() as s:
        get_active_policy_row(s)  # seeds policy v1 + reps on first boot
    yield


app = FastAPI(title="Lumen sales assistant", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(api_router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request, _: str = Depends(require_operator)):
    return templates.TemplateResponse(request, "inbox.html", {"page": "inbox"})


@app.get("/policy", response_class=HTMLResponse)
def policy_page(request: Request, _: str = Depends(require_operator)):
    return templates.TemplateResponse(request, "policy.html", {"page": "policy"})
