from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

if settings.database_url.startswith("sqlite"):
    _connect_args: dict = {"check_same_thread": False}
else:
    _connect_args = {"connect_timeout": 10}  # never hang forever on an unreachable database
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=_connect_args)


def describe_database() -> str:
    """Database kind and host, without credentials, for logs."""
    url = engine.url
    return f"{url.get_backend_name()} at {url.host or url.database}"
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    from app import models  # noqa: F401  (registers tables)
    from app.agents import models as agent_models  # noqa: F401
    from app.migrate import add_missing_columns

    Base.metadata.create_all(engine)
    add_missing_columns(engine, Base.metadata)
