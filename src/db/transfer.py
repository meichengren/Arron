"""Safe one-time transfer from the legacy SQLite database to PostgreSQL."""
from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import init_database
from src.db.migrations import migrate


def _table_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table.name: int(connection.scalar(select(func.count()).select_from(table)) or 0)
            for table in models.Base.metadata.sorted_tables
        }


def _reset_postgres_sequences(engine: Engine, copied: Mapping[str, int]) -> None:
    """Advance serial sequences after explicit primary-key copies."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as connection:
        for table in models.Base.metadata.sorted_tables:
            if not copied.get(table.name) or "id" not in table.c:
                continue
            # Table names come from SQLAlchemy metadata, never user input.
            connection.execute(
                text(
                    f"SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
                    f"(SELECT MAX(id) FROM {table.name}), true)"
                ),
                {"table_name": table.name},
            )


def copy_sqlite_database(source: Engine, target: Engine) -> dict[str, int]:
    """Copy all application tables once, refusing a non-empty target database."""
    if source.dialect.name != "sqlite":
        raise ValueError("The migration source must be SQLite.")
    if target.dialect.name != "postgresql":
        raise ValueError("The migration target must be PostgreSQL.")

    # Bring an older SQLite schema up to the current model before reading it.
    migrate(source)
    init_database(target)

    target_counts = _table_counts(target)
    populated = [name for name, count in target_counts.items() if count]
    if populated:
        raise RuntimeError(
            "Target PostgreSQL database is not empty; refusing to overwrite: "
            + ", ".join(populated)
        )

    source_counts = _table_counts(source)
    copied: dict[str, int] = {}
    with source.connect() as source_connection, target.begin() as target_connection:
        for table in models.Base.metadata.sorted_tables:
            rows = source_connection.execute(select(table)).mappings().all()
            if rows:
                target_connection.execute(table.insert(), [dict(row) for row in rows])
            copied[table.name] = len(rows)

    target_after = _table_counts(target)
    mismatches = [
        f"{name}: source={expected}, target={target_after[name]}"
        for name, expected in source_counts.items()
        if target_after[name] != expected
    ]
    if mismatches:
        raise RuntimeError("Post-copy verification failed: " + "; ".join(mismatches))

    _reset_postgres_sequences(target, copied)
    return copied
