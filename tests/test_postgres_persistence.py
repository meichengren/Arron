from sqlalchemy import create_engine

from src.db.engine import normalize_database_url
from src.db import migrations
from src.db.repositories import dialect_insert
from src.db import models


def test_normalizes_render_postgres_url_for_psycopg():
    assert normalize_database_url(
        "postgres://user:password@host/db?sslmode=require"
    ) == "postgresql+psycopg://user:password@host/db?sslmode=require"


def test_dialect_insert_selects_postgresql_statement():
    engine = create_engine("postgresql+psycopg://user:password@host/db")
    assert dialect_insert(engine, models.Security.__table__).dialect.name == "postgresql"


def test_existing_columns_uses_sqlalchemy_inspector(monkeypatch):
    class Inspector:
        def get_columns(self, table):
            assert table == "portfolio_snapshots"
            return [{"name": "id"}, {"name": "risk_level"}]

    monkeypatch.setattr(migrations, "inspect", lambda engine: Inspector())
    assert migrations._existing_columns(object(), "portfolio_snapshots") == {"id", "risk_level"}


def test_transfer_requires_a_postgresql_target():
    from types import SimpleNamespace

    from src.db.transfer import copy_sqlite_database

    sqlite = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    try:
        copy_sqlite_database(sqlite, sqlite)
    except ValueError as exc:
        assert str(exc) == "The migration target must be PostgreSQL."
    else:
        raise AssertionError("SQLite targets must be rejected")
