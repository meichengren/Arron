"""Tushare Pro provider (primary data source). Requires TUSHARE_TOKEN in .env."""
from __future__ import annotations

from typing import Any

import pandas as pd

from src.config.settings import AppSettings
from src.providers.base import DataProvider, ProviderError


def _to_date(s) -> Any:
    try:
        return pd.to_datetime(str(s)).date()
    except Exception:
        return None


class TushareProvider(DataProvider):
    name = "tushare"

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._pro: Any = None
        self._initialized = False

    # ------------------------------------------------------------------ #
    def _ensure_client(self):
        if self._initialized:
            return self._pro
        token = self._settings.tushare_token
        if not token:
            raise ProviderError("TUSHARE_TOKEN is empty - configure .env to use Tushare")
        try:
            import tushare as ts

            ts.set_token(token)
            self._pro = ts.pro_api()
            self._initialized = True
        except Exception as exc:  # pragma: no cover
            raise ProviderError(f"Tushare initialization failed: {exc}") from exc
        return self._pro

    def health_check(self) -> bool:
        if not self._settings.tushare_token:
            return False
        try:
            pro = self._ensure_client()
            df = pro.trade_cal(exchange="SSE", start_date="20260101", end_date="20260110")
            return df is not None and not df.empty
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    def get_stock_basic(self, symbol: str) -> dict[str, Any] | None:
        pro = self._ensure_client()
        try:
            df = pro.stock_basic(ts_code=symbol)
        except Exception as exc:
            raise ProviderError(f"stock_basic failed for {symbol}: {exc}") from exc
        if df is None or df.empty:
            return None
        row = df.iloc[0]
        return {
            "symbol": row.get("ts_code", symbol),
            "display_symbol": str(row.get("ts_code", symbol)).split(".")[0],
            "name": row.get("name", ""),
            "exchange": row.get("exchange", ""),
            "industry_raw": row.get("industry", "") or "",
            "list_date": _to_date(row.get("list_date")),
            "status": row.get("list_status", "ACTIVE").upper() or "ACTIVE",
        }

    def get_daily_bars(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        pro = self._ensure_client()
        try:
            df = pro.daily(
                ts_code=symbol,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except Exception as exc:
            raise ProviderError(f"daily failed for {symbol}: {exc}") from exc
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date")
        rename = {
            "vol": "volume",
            "change": "change_amt",
            "pct_chg": "pct_change",
        }
        df = df.rename(columns=rename)
        keep = [
            "trade_date", "open", "high", "low", "close",
            "pre_close", "pct_change", "volume", "amount",
        ]
        df = df[[c for c in keep if c in df.columns]].copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        for c in ("open", "high", "low", "close", "pre_close", "pct_change", "volume", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df.reset_index(drop=True)

    def get_daily_basic(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        pro = self._ensure_client()
        try:
            df = pro.daily_basic(
                ts_code=symbol,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
            )
        except Exception as exc:
            raise ProviderError(f"daily_basic failed for {symbol}: {exc}") from exc
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.sort_values("trade_date")
        keep = [
            "trade_date", "pe", "pe_ttm", "pb", "ps_ttm",
            "dv_ratio", "total_mv", "circ_mv",
        ]
        df = df[[c for c in keep if c in df.columns]].copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        for c in ("pe", "pe_ttm", "pb", "ps_ttm", "dv_ratio", "total_mv", "circ_mv"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        # 统一数据库单位：Tushare daily_basic 的 total_mv/circ_mv 为万元 -> 元，
        # 与 financial_reports 的 total_equity（元）一致，保证 shares 推断正确。
        for c in ("total_mv", "circ_mv"):
            if c in df.columns:
                df[c] = df[c] * 10000.0
        return df.reset_index(drop=True)

    def get_financial_reports(self, symbol: str) -> pd.DataFrame:
        pro = self._ensure_client()
        try:
            indicator = pro.fina_indicator(ts_code=symbol)
            income = pro.income(ts_code=symbol)
            cashflow = pro.cashflow(ts_code=symbol)
            balance = pro.balancesheet(ts_code=symbol)
        except Exception as exc:
            raise ProviderError(f"financial reports failed for {symbol}: {exc}") from exc

        rows: list[dict[str, Any]] = []
        if indicator is None or indicator.empty:
            return pd.DataFrame()

        income_map: dict[str, dict[str, Any]] = {}
        if income is not None and not income.empty:
            for _, r in income.iterrows():
                period = str(r.get("end_date", ""))
                income_map[period] = {
                    "revenue": _num(r.get("total_revenue") if pd.notna(r.get("total_revenue")) else r.get("revenue")),
                    "net_profit": _num(r.get("n_income_attr_p")),
                    "deducted_net_profit": _num(r.get("n_income_attr_p")),
                }
        cash_map: dict[str, float | None] = {}
        if cashflow is not None and not cashflow.empty:
            for _, r in cashflow.iterrows():
                cash_map[str(r.get("end_date", ""))] = _num(r.get("n_cashflow_act"))
        balance_map: dict[str, dict[str, Any]] = {}
        if balance is not None and not balance.empty:
            for _, r in balance.iterrows():
                period = str(r.get("end_date", ""))
                balance_map[period] = {
                    "total_assets": _num(r.get("total_assets")),
                    "total_equity": _num(r.get("total_hldr_eqy_exc_min_int")),
                    "total_debt": _num(r.get("total_liab")),
                }

        for _, r in indicator.iterrows():
            period = r.get("end_date")
            if pd.isna(period):
                continue
            period_str = str(period)
            inc = income_map.get(period_str, {})
            cash = cash_map.get(period_str)
            bal = balance_map.get(period_str, {})
            rows.append(
                {
                    "report_period": _to_date(period_str),
                    "announcement_date": _to_date(r.get("ann_date")),
                    "revenue": inc.get("revenue"),
                    "revenue_yoy": _num(r.get("or_yoy")),
                    "net_profit": inc.get("net_profit"),
                    "net_profit_yoy": _num(r.get("netprofit_yoy")),
                    "deducted_net_profit": inc.get("deducted_net_profit"),
                    "operating_cashflow": cash,
                    "free_cashflow": None,
                    "total_assets": bal.get("total_assets"),
                    "total_equity": bal.get("total_equity"),
                    "total_debt": bal.get("total_debt"),
                    "eps": _num(r.get("eps")),
                    "roe": _num(r.get("roe")),
                    "roa": _num(r.get("roa")),
                    "gross_margin": _num(r.get("grossprofit_margin")),
                    "net_margin": _num(r.get("netprofit_margin")),
                    "debt_ratio": _num(r.get("debt_to_assets")),
                    "current_ratio": _num(r.get("current_ratio")),
                }
            )
        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("report_period").drop_duplicates(
                subset=["report_period", "announcement_date"], keep="last"
            )
        return df


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None