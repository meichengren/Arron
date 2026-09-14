"""Daily market + valuation indicators sync (spec Sections 6.1 steps 5 & 7)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd

from src.db.engine import Engine
from src.db.repositories import MarketRepository
from src.providers.provider_router import ProviderRouter


@dataclass
class MarketSyncResult:
    security_id: int
    symbol: str
    rows_upserted: int
    latest_trade_date: date | None
    provider: str


class MarketSyncService:
    def __init__(self, engine: Engine, router: ProviderRouter) -> None:
        self._engine = engine
        self._router = router
        self._repo = MarketRepository(engine)

    def sync(
        self,
        security_id: int,
        symbol: str,
        years: int = 5,
        end_date: date | None = None,
    ) -> MarketSyncResult:
        today = end_date or date.today()
        start = today - timedelta(days=int(years * 366) + 30)
        start_s = start.strftime("%Y-%m-%d")
        end_s = today.strftime("%Y-%m-%d")

        bars, bars_source = self._router.get_daily_bars(symbol, start_s, end_s)
        try:
            basic, basic_source = self._router.get_daily_basic(symbol, start_s, end_s)
        except Exception:
            basic, basic_source = pd.DataFrame(), ""
        if bars is None or bars.empty:
            return MarketSyncResult(security_id, symbol, 0, None, bars_source)

        df = bars.copy()
        if basic is not None and not basic.empty:
            df = df.merge(
                basic, on="trade_date", how="left", suffixes=("", "_basic")
            )
        else:
            for c in ("pe", "pe_ttm", "pb", "ps_ttm", "dv_ratio", "total_mv", "circ_mv"):
                df[c] = None

        rows: list[dict] = []
        now = datetime.now()
        source = bars_source if bars_source else basic_source
        for _, r in df.iterrows():
            trade_date = r.get("trade_date")
            if trade_date is None:
                continue
            rows.append(
                {
                    "security_id": security_id,
                    "trade_date": trade_date,
                    "open": _f(r.get("open")),
                    "high": _f(r.get("high")),
                    "low": _f(r.get("low")),
                    "close": _f(r.get("close")),
                    "pre_close": _f(r.get("pre_close")),
                    "pct_change": _f(r.get("pct_change")),
                    "volume": _f(r.get("volume")),
                    "amount": _f(r.get("amount")),
                    "pe": _f(r.get("pe")),
                    "pe_ttm": _f(r.get("pe_ttm")),
                    "pb": _f(r.get("pb")),
                    "ps_ttm": _f(r.get("ps_ttm")),
                    "dv_ratio": _f(r.get("dv_ratio")),
                    "total_mv": _f(r.get("total_mv")),
                    "circ_mv": _f(r.get("circ_mv")),
                    "source": source,
                    "fetched_at": now,
                }
            )
        count = self._repo.upsert_bulk(rows)
        latest = self._repo.latest_trade_date(security_id)
        return MarketSyncResult(security_id, symbol, count, latest, source)


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None