"""DataProvider abstraction (spec Section 2.1: business layer must NOT call Tushare/AKShare directly).

Providers return normalized pandas DataFrames / dicts with a fixed column
contract so that the ingestion layer and downstream engines are source-agnostic.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class ProviderError(RuntimeError):
    """Raised when a provider fails to fetch data (network, api, parsing...)."""


class DataProvider(ABC):
    """Abstract data source over normalized market & financial endpoints."""

    name: str = "base"

    # ------------------------------------------------------------------ #
    # Capability / health
    # ------------------------------------------------------------------ #
    @abstractmethod
    def health_check(self) -> bool:
        """Return True when this provider is usable right now (credentials + connectivity)."""

    # ------------------------------------------------------------------ #
    # Stock basic info
    # ------------------------------------------------------------------ #
    @abstractmethod
    def get_stock_basic(self, symbol: str) -> dict[str, Any] | None:
        """Fetch basic security info for a normalized symbol like '600036.SH'.

        Returns dict with keys:
            symbol, display_symbol, name, exchange, industry_raw, list_date, status
        or None when the security is not found.
        """

    # ------------------------------------------------------------------ #
    # Daily market data
    # ------------------------------------------------------------------ #
    @abstractmethod
    def get_daily_bars(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Daily OHLCV bars. Columns (normalized):
            trade_date(date), open, high, low, close, pre_close,
            pct_change, volume, amount
        """

    @abstractmethod
    def get_daily_basic(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Daily valuation indicators. Columns (normalized):
            trade_date(date), pe, pe_ttm, pb, ps_ttm, dv_ratio, total_mv, circ_mv
        """

    # ------------------------------------------------------------------ #
    # Financial reports
    # ------------------------------------------------------------------ #
    @abstractmethod
    def get_financial_reports(self, symbol: str) -> pd.DataFrame:
        """Fundamental report rows, one per report period. Columns (normalized):
            report_period(date), announcement_date(date), revenue, revenue_yoy,
            net_profit, net_profit_yoy, deducted_net_profit, operating_cashflow,
            total_assets, total_equity, total_debt, eps, roe, roa, gross_margin,
            net_margin, debt_ratio, current_ratio
        """


def columns_to_dates(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    """Coerce string date columns to pandas datetime (NaT safe)."""
    out = df.copy()
    for col in cols:
        if col in out.columns:
            out[col] = pd.to_datetime(out[col], errors="coerce").dt.date
    return out