import os
import sys
from pathlib import Path

import pytest

# Isolated SQLite database per test run; must be set before app modules import.
_DB = Path(__file__).parent / "_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["APP_ENV"] = "test"
os.environ.pop("DASHBOARD_PASSWORD", None)
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"       # fake values; Google calls are mocked
os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"
os.environ["GROQ_API_KEY"] = "test-groq-key"  # fake; every Groq call goes to tests/fake_groq.py
os.environ["META_APP_SECRET"] = "test-app-secret"          # fake Meta app; Graph calls are mocked
os.environ["META_VERIFY_TOKEN"] = "test-verify-token"
os.environ["WHATSAPP_ACCESS_TOKEN"] = "test-wa-token"
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "1379451971922417"
os.environ["INSTAGRAM_ACCESS_TOKEN"] = "test-ig-token"
os.environ["INSTAGRAM_APP_SECRET"] = "test-ig-secret"
for _k in ("EMAIL_INBOX_ADDRESS",):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def groq(monkeypatch):
    """Every test gets a fake Groq. It starts 'down' so no test can reach a real model."""
    import httpx

    from app import llm
    from tests.fake_groq import FakeGroq

    fake = FakeGroq()
    monkeypatch.setattr(llm, "HTTP_TRANSPORT", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    return fake


@pytest.fixture()
def session():
    Base.metadata.drop_all(engine)
    init_db()
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def client(session):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
