"""Model Lab orchestration: run all look-back analytics and export a report.

``run_model_lab`` -> dict that is directly JSON-serialisable, plus optional
CSV dump of the resolved forecast-error pairs (the "3M/6M/12M look-back stats
exportable" acceptance criterion).
"""
from __future__ import annotations

import csv
from datetime import date
from typing import Any

from sqlalchemy.engine import Engine

from src.model_lab.calibration import (
    forecast_error_rows,
    refresh_forecast_errors,
)
from src.model_lab.engine import (
    HORIZONS,
    buy_zone_irr,
    collect_future_returns,
    forecast_calibration,
    score_validity,
    walk_forward,
)

# order used for the report
ALL_HORIZONS = ("3M", "6M", "12M", "24M")


def _date_or_none(v: date | None) -> str | None:
    return v.isoformat() if v else None


def run_model_lab(
    engine: Engine,
    refresh: bool = True,
    as_of_max: date | None = None,
) -> dict[str, Any]:
    """Run the full Model Lab look-back and return an exportable dict."""
    if refresh:
        refresh_summary = refresh_forecast_errors(engine, as_of_max=as_of_max)
    else:
        refresh_summary = {"skipped": True}

    rows = collect_future_returns(engine, horizons=ALL_HORIZONS, as_of_max=as_of_max)

    return {
        "generated_at": date.today().isoformat(),
        "refresh": refresh_summary,
        "n_snapshots_resolved": len(rows),
        "score_validity": [
            score_validity(rows, h).to_dict() for h in ALL_HORIZONS
        ],
        "walk_forward": [
            walk_forward(rows, h) for h in ALL_HORIZONS
        ],
        "calibration": [
            forecast_calibration(engine, h, as_of_max=as_of_max).to_dict()
            for h in ALL_HORIZONS
        ],
        "buy_zone": [
            b.to_dict() for b in buy_zone_irr(rows)
        ],
        "forecast_error_rows": forecast_error_rows(engine, "12M", limit=1000),
    }


def export_forecast_errors_csv(
    engine: Engine, path: str, horizon: str = "12M"
) -> int:
    """Dump forecast-error pairs to CSV. Returns rows written."""
    rows = forecast_error_rows(engine, horizon, limit=100_000)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["symbol", "as_of_date", "horizon_days",
                            "predicted", "actual", "error", "regime"]
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return len(rows)