"""Data access layer: upsert helpers for bulk market/financial ingestion.

All writes go through repositories so business/UI layers never touch SQL.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.db.engine import make_session_factory
from src.db import models


def _session_factory(engine: Engine):
    return make_session_factory(engine)


def dialect_insert(engine: Engine, table):
    """Return the dialect-specific INSERT supporting ON CONFLICT upserts."""
    if engine.dialect.name == "sqlite":
        return sqlite_insert(table)
    if engine.dialect.name == "postgresql":
        return postgresql_insert(table)
    raise ValueError(f"Unsupported database dialect for upsert: {engine.dialect.name}")


# --------------------------------------------------------------------------- #
# Securities
# --------------------------------------------------------------------------- #
class SecurityRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._factory = _session_factory(engine)

    def upsert(self, values: dict[str, Any]) -> models.Security:
        """Insert or update a security by unique symbol. Returns the ORM row."""
        with self._factory() as session:
            stmt = (
                dialect_insert(self._engine, models.Security)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[models.Security.symbol],
                    set_={k: v for k, v in values.items() if k != "symbol"},
                )
            )
            session.execute(stmt)
            session.commit()
            obj = session.scalars(
                select(models.Security).where(models.Security.symbol == values["symbol"])
            ).one()
            return obj

    def get_by_symbol(self, symbol: str) -> models.Security | None:
        with self._factory() as session:
            return session.scalars(
                select(models.Security).where(models.Security.symbol == symbol)
            ).first()

    def get(self, security_id: int) -> models.Security | None:
        with self._factory() as session:
            return session.get(models.Security, security_id)

    def get_or_create(self, values: dict[str, Any]) -> models.Security:
        existing = self.get_by_symbol(values["symbol"])
        return existing if existing is not None else self.upsert(values)


# --------------------------------------------------------------------------- #
# Daily market
# --------------------------------------------------------------------------- #
class MarketRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._factory = _session_factory(engine)

    def upsert_bulk(self, rows: Iterable[dict[str, Any]]) -> int:
        """Bulk upsert daily_market rows keyed by (security_id, trade_date). Returns row count."""
        items = list(rows)
        if not items:
            return 0
        with self._factory() as session:
            stmt = dialect_insert(self._engine, models.DailyMarket).values(items)
            stmt = stmt.on_conflict_do_update(
                index_elements=["security_id", "trade_date"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "pre_close": stmt.excluded.pre_close,
                    "pct_change": stmt.excluded.pct_change,
                    "volume": stmt.excluded.volume,
                    "amount": stmt.excluded.amount,
                    "pe": stmt.excluded.pe,
                    "pe_ttm": stmt.excluded.pe_ttm,
                    "pb": stmt.excluded.pb,
                    "ps_ttm": stmt.excluded.ps_ttm,
                    "dv_ratio": stmt.excluded.dv_ratio,
                    "total_mv": stmt.excluded.total_mv,
                    "circ_mv": stmt.excluded.circ_mv,
                    "source": stmt.excluded.source,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            session.execute(stmt)
            session.commit()
            return len(items)

    def latest_trade_date(self, security_id: int) -> date | None:
        with self._factory() as session:
            row = session.execute(
                select(models.DailyMarket.trade_date)
                .where(models.DailyMarket.security_id == security_id)
                .order_by(models.DailyMarket.trade_date.desc())
                .limit(1)
            ).first()
            return row[0] if row else None

    def count_for(self, security_id: int) -> int:
        with self._factory() as session:
            return session.scalars(
                select(models.DailyMarket.id).where(
                    models.DailyMarket.security_id == security_id
                )
            ).all().__len__()

    def days(self, security_id: int) -> Sequence[models.DailyMarket]:
        with self._factory() as session:
            return list(
                session.scalars(
                    select(models.DailyMarket)
                    .where(models.DailyMarket.security_id == security_id)
                    .order_by(models.DailyMarket.trade_date)
                ).all()
            )


# --------------------------------------------------------------------------- #
# Financial reports
# --------------------------------------------------------------------------- #
class FinancialRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._factory = _session_factory(engine)

    def upsert_bulk(self, rows: Iterable[dict[str, Any]]) -> int:
        items = list(rows)
        if not items:
            return 0
        with self._factory() as session:
            stmt = dialect_insert(self._engine, models.FinancialReport).values(items)
            stmt = stmt.on_conflict_do_update(
                index_elements=["security_id", "report_period", "announcement_date"],
                set_={
                    "report_type": stmt.excluded.report_type,
                    "revenue": stmt.excluded.revenue,
                    "revenue_yoy": stmt.excluded.revenue_yoy,
                    "net_profit": stmt.excluded.net_profit,
                    "net_profit_yoy": stmt.excluded.net_profit_yoy,
                    "deducted_net_profit": stmt.excluded.deducted_net_profit,
                    "operating_cashflow": stmt.excluded.operating_cashflow,
                    "free_cashflow": stmt.excluded.free_cashflow,
                    "total_assets": stmt.excluded.total_assets,
                    "total_equity": stmt.excluded.total_equity,
                    "total_debt": stmt.excluded.total_debt,
                    "eps": stmt.excluded.eps,
                    "roe": stmt.excluded.roe,
                    "roa": stmt.excluded.roa,
                    "gross_margin": stmt.excluded.gross_margin,
                    "net_margin": stmt.excluded.net_margin,
                    "debt_ratio": stmt.excluded.debt_ratio,
                    "current_ratio": stmt.excluded.current_ratio,
                    "source": stmt.excluded.source,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            session.execute(stmt)
            session.commit()
            return len(items)

    def count_for(self, security_id: int) -> int:
        with self._factory() as session:
            return len(
                session.scalars(
                    select(models.FinancialReport.id).where(
                        models.FinancialReport.security_id == security_id
                    )
                ).all()
            )

    def latest_announcement(self, security_id: int) -> models.FinancialReport | None:
        with self._factory() as session:
            return session.scalars(
                select(models.FinancialReport)
                .where(models.FinancialReport.security_id == security_id)
                .order_by(models.FinancialReport.announcement_date.desc())
                .limit(1)
            ).first()


# --------------------------------------------------------------------------- #
# Industry metrics
# --------------------------------------------------------------------------- #
class IndustryMetricRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._factory = _session_factory(engine)

    def upsert_bulk(self, rows: Iterable[dict[str, Any]]) -> int:
        items = list(rows)
        if not items:
            return 0
        with self._factory() as session:
            stmt = dialect_insert(self._engine, models.IndustryMetric).values(items)
            stmt = stmt.on_conflict_do_update(
                index_elements=["security_id", "metric_date", "metric_name"],
                set_={
                    "metric_value": stmt.excluded.metric_value,
                    "unit": stmt.excluded.unit,
                    "source": stmt.excluded.source,
                    "fetched_at": stmt.excluded.fetched_at,
                },
            )
            session.execute(stmt)
            session.commit()
            return len(items)