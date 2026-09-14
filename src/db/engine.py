"""SQLAlchemy engine / session factories. Databases are created in-memory in tests."""
from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.config.settings import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def make_engine(db_url: str, echo: bool = False) -> Engine:
    """Create an engine. For sqlite relative paths, resolve against project root."""
    url = db_url
    if db_url.startswith("sqlite://"):
        raw_path = db_url[len("sqlite:///") :] if db_url.startswith("sqlite:///") else ""
        if raw_path == ":memory:" or db_url == "sqlite://":
            url = "sqlite://"
        else:
            p = Path(raw_path)
            if not p.is_absolute():
                p = PROJECT_ROOT / raw_path
            p.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{p.as_posix()}"
    kwargs: dict = {"echo": echo}
    if url.startswith("sqlite"):
        if url == "sqlite://":
            # StaticPool => in-memory databases survive per-connection sessions
            kwargs["poolclass"] = StaticPool
            kwargs["connect_args"] = {"check_same_thread": False, "uri": True}
        else:
            kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def _enable_foreign_keys(dbapi_connection, connection_record) -> None:  # pragma: no cover
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@lru_cache(maxsize=1)
def _default_engine() -> Engine:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    if engine.dialect.name == "sqlite":
        event.listens_for(engine, "connect")(_enable_foreign_keys)
    return engine


def get_engine() -> Engine:
    return _default_engine()


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional session context: commits on success, rolls back on error."""
    eng = engine or get_engine()
    factory = make_session_factory(eng)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database(engine: Engine | None = None) -> None:
    """Create all tables (and apply V1.1 column migrations for existing DBs)."""
    from src.db import models  # noqa: F401  (register models)

    eng = engine or get_engine()
    models.Base.metadata.create_all(eng)
    from src.db.migrations import migrate

    migrate(eng)