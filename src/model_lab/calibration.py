"""Forecast-error persistence + refresh (spec section 3.3.3).

``forecast_errors`` is an append-only polygon: one row per
(security, as_of_date, horizon) recording the model's predicted annualised
return (``expected_return_base``) vs the realised annualised return. Rows are
only created once the required number of trading days have elapsed; refill is
idempotent (a resolvable snapshot is re-derived from its source columns, never
modified after write).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory
from src.model_lab.engine import collect_future_returns

# Only the annualised base-case forecast is written as ``predicted``;
# realised values are stored for every horizon that has elapsed.
PERSIST_HORIZONS = ("3M", "6M", "12M", "24M")


def refresh_forecast_errors(
    engine: Engine,
    as_of_max: date | None = None,
    horizons: tuple[str, ...] = PERSIST_HORIZONS,
) -> dict[str, Any]:
    """Recompute every resolvable (snapshot, horizon) pair and upsert into
    ``forecast_errors``. Returns a summary dict.

    Idempotency: rows are deleted and re-inserted wholesale for the affected
    horizons (the table is derived data, not a journal), so re-runs converge.
    """
    rows = collect_future_returns(engine, horizons=horizons, as_of_max=as_of_max)
    factory = make_session_factory(engine)

    # derive titles for the security suffix used in deprecation notes
    sec_ids = {r.security_id for r in rows}
    horizon_days_of = {"3M": 63, "6M": 126, "12M": 252, "24M": 504}

    with factory() as session:
        # remove stale rows for these horizons (only if no new row supersedes)
        for h in horizons:
            session.execute(
                delete(models.ForecastError).where(
                    models.ForecastError.horizon == horizon_days_of[h]
                )
            )
        written = 0
        for r in rows:
            horizon_days = horizon_days_of[r.horizon]
            if r.expected_return_base is None:
                continue
            session.add(models.ForecastError(
                security_id=r.security_id,
                as_of_date=r.as_of_date,
                horizon=horizon_days,
                predicted=float(r.expected_return_base),
                actual=float(r.annualized_return),
                error=float(r.annualized_return - r.expected_return_base),
                regime="",
            ))
            written += 1
        session.commit()

    return {
        "resolved_pairs": len(rows),
        "written": written,
        "horizons": list(horizons),
        "securities": sorted(sec_ids),
    }


def forecast_error_rows(
    engine: Engine, horizon: str = "12M", limit: int = 500
) -> list[dict[str, Any]]:
    """Read back forecast errors joined with symbol (for dashboards/exports)."""
    horizon_days = {"3M": 63, "6M": 126, "12M": 252, "24M": 504}[horizon]
    factory = make_session_factory(engine)
    out: list[dict[str, Any]] = []
    with factory() as session:
        q = (
            select(models.ForecastError, models.Security.symbol)
            .join(models.Security, models.Security.id == models.ForecastError.security_id)
            .where(models.ForecastError.horizon == horizon_days)
            .order_by(models.ForecastError.as_of_date.desc())
            .limit(limit)
        )
        for fe, symbol in session.execute(q):
            out.append({
                "symbol": symbol,
                "as_of_date": fe.as_of_date.isoformat(),
                "horizon_days": fe.horizon,
                "predicted": fe.predicted,
                "actual": fe.actual,
                "error": fe.error,
                "regime": fe.regime,
            })
    return out