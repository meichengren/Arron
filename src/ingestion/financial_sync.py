"""Financial statement sync (spec Sections 6.1 step 6, 34: report_period + announcement_date).

Look-ahead safety: every row keeps report_period AND announcement_date; backtests
must only use rows where announcement_date <= backtest date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from src.db.engine import Engine
from src.db.repositories import FinancialRepository
from src.providers.provider_router import ProviderRouter


@dataclass
class FinancialSyncResult:
    security_id: int
    symbol: str
    rows_upserted: int
    latest_report_period: date | None
    provider: str


class FinancialSyncService:
    def __init__(self, engine: Engine, router: ProviderRouter) -> None:
        self._engine = engine
        self._router = router
        self._repo = FinancialRepository(engine)

    def sync(
        self,
        security_id: int,
        symbol: str,
        years: int = 5,
        end_date: date | None = None,
    ) -> FinancialSyncResult:
        df, source = self._router.get_financial_reports(symbol)
        today = end_date or date.today()
        cutoff = date(today.year - years, today.month, today.day)
        if df is None or df.empty:
            return FinancialSyncResult(security_id, symbol, 0, None, source)

        # keep reports whose report period ends within the window
        df = df[
            pd.to_datetime(df["report_period"], errors="coerce").dt.date >= cutoff
        ].copy()

        rows: list[dict] = []
        now = datetime.now()
        for _, r in df.iterrows():
            rows.append(
                {
                    "security_id": security_id,
                    "report_period": r.get("report_period"),
                    "announcement_date": r.get("announcement_date"),
                    "report_type": _report_type(r.get("report_period")),
                    "revenue": _f(r.get("revenue")),
                    "revenue_yoy": _f(r.get("revenue_yoy")),
                    "net_profit": _f(r.get("net_profit")),
                    "net_profit_yoy": _f(r.get("net_profit_yoy")),
                    "deducted_net_profit": _f(r.get("deducted_net_profit")),
                    "operating_cashflow": _f(r.get("operating_cashflow")),
                    "free_cashflow": _f(r.get("free_cashflow")),
                    "total_assets": _f(r.get("total_assets")),
                    "total_equity": _f(r.get("total_equity")),
                    "total_debt": _f(r.get("total_debt")),
                    "eps": _f(r.get("eps")),
                    "roe": _f(r.get("roe")),
                    "roa": _f(r.get("roa")),
                    "gross_margin": _f(r.get("gross_margin")),
                    "net_margin": _f(r.get("net_margin")),
                    "debt_ratio": _f(r.get("debt_ratio")),
                    "current_ratio": _f(r.get("current_ratio")),
                    "source": source,
                    "fetched_at": now,
                }
            )
        count = self._repo.upsert_bulk(rows)
        latest = max((r["report_period"] for r in rows if r["report_period"]), default=None)
        return FinancialSyncResult(security_id, symbol, count, latest, source)


def _report_type(period) -> str:
    if period is None:
        return "ANNUAL"
    month = period.month if hasattr(period, "month") else None
    return {3: "Q1", 6: "H1", 9: "Q3", 12: "ANNUAL"}.get(month, "ANNUAL")


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None