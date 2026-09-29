"""Free domestic ETF provider backed by Eastmoney endpoints exposed through AKShare."""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from src.providers.base import DataProvider, ProviderError


def _code(symbol: str) -> str:
    return str(symbol).split(".")[0]


class EastmoneyEtfProvider(DataProvider):
    name = "eastmoney_etf"

    def _spot(self) -> pd.DataFrame:
        import akshare as ak
        try:
            frame = ak.fund_etf_spot_em()
        except Exception as exc:
            raise ProviderError(f"eastmoney ETF spot failed: {exc}") from exc
        if frame is None or frame.empty:
            raise ProviderError("eastmoney ETF spot is empty")
        return frame

    def health_check(self) -> bool:
        try:
            return not self._spot().empty
        except Exception:
            return False

    def get_stock_basic(self, symbol: str) -> dict[str, Any] | None:
        code = _code(symbol)
        frame = self._spot()
        code_col = next((c for c in frame.columns if str(c) in ("代码", "基金代码")), None)
        name_col = next((c for c in frame.columns if str(c) in ("名称", "基金名称")), None)
        if not code_col or not name_col:
            raise ProviderError("eastmoney ETF spot columns changed")
        match = frame[frame[code_col].astype(str).str.zfill(6) == code]
        if match.empty:
            return None
        row = match.iloc[0]
        exchange = "SSE" if code.startswith(("5", "588")) else "SZSE"
        return {
            "symbol": symbol,
            "display_symbol": code,
            "name": str(row[name_col]).strip(),
            "exchange": exchange,
            "industry_raw": "ETF",
            "list_date": None,
            "status": "ACTIVE",
        }

    def get_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        import akshare as ak
        try:
            frame = ak.fund_etf_hist_em(
                symbol=_code(symbol),
                period="daily",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="",
            )
        except Exception as exc:
            raise ProviderError(f"eastmoney ETF history failed for {symbol}: {exc}") from exc
        if frame is None or frame.empty:
            return pd.DataFrame()
        mapping = {
            "日期": "trade_date", "开盘": "open", "最高": "high", "最低": "low",
            "收盘": "close", "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_change",
        }
        out = frame.rename(columns=mapping)
        if "trade_date" not in out or "close" not in out:
            raise ProviderError("eastmoney ETF history columns changed")
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
        for column in ("open", "high", "low", "close", "volume", "amount", "pct_change"):
            if column in out:
                out[column] = pd.to_numeric(out[column], errors="coerce")
        out = out.dropna(subset=["trade_date", "close"]).sort_values("trade_date")
        out["pre_close"] = out["close"].shift(1)
        return out[[c for c in ("trade_date", "open", "high", "low", "close", "pre_close", "pct_change", "volume", "amount") if c in out]].reset_index(drop=True)

    def get_daily_basic(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        bars = self.get_daily_bars(symbol, start_date, end_date)
        if bars.empty:
            return pd.DataFrame()
        return pd.DataFrame({"trade_date": bars["trade_date"]})

    def get_financial_reports(self, symbol: str) -> pd.DataFrame:
        return pd.DataFrame()
