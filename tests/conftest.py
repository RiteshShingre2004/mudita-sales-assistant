import os
import sys
from pathlib import Path

import pytest

# Isolated SQLite database per test run; must be set before app modules import.
_DB = Path(__file__).parent / "_test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["APP_ENV"] = "test"
os.environ.pop("DASHBOARD_PASSWORD", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402


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
