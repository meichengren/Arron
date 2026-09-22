"""AKShare provider (fallback / supplement source). No token required.

Data paths verified against akshare 1.18.94 in this environment:
  - stock_individual_info_em / stock_zh_a_hist (eastmoney): unreachable -> not used
  - stock_zh_a_daily (sina): daily OHLCV history            -> daily bars
  - stock_zh_a_spot_tx (tencent snapshot): pe_ttm/pb/mv     -> latest-day valuation
  - stock_financial_report_sina: 3 statements, rows = report
    periods, columns = metrics, includes announcement date  -> financial reports
  - stock_info_a_code_name (sina): code -> name mapping      -> stock basic info
"""
from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

import pandas as pd

from src.providers.base import DataProvider, ProviderError


def _num(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_date(value) -> Any:
    try:
        return pd.to_datetime(str(value)).date()
    except Exception:
        return None


def sina_symbol(symbol: str) -> str:
    """'600036.SH' -> 'sh600036' (sina / tencent style)."""
    code = symbol.split(".")[0]
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return code


def ak_symbol(symbol: str) -> str:
    """'600036.SH' -> '600036'."""
    return symbol.split(".")[0]


def _clean_name(name: str) -> str:
    """Strip XD/XR/DR ex-right markers leaked into stock names."""
    cleaned = str(name).strip()
    while cleaned[:2] in ("XD", "XR", "DR"):
        cleaned = cleaned[2:]
    return cleaned or str(name)


class AkshareProvider(DataProvider):
    name = "akshare"

    def __init__(self, settings=None) -> None:
        self._settings = settings
        self._name_map: dict[str, str] | None = None
        self._tx_snapshot: tuple[float, pd.DataFrame] | None = None

    # ------------------------------------------------------------------ #
    def health_check(self) -> bool:
        try:
            return bool(self._get_name_map())
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    def _get_name_map(self) -> dict[str, str]:
        """code('600036') -> name('招商银行'), cached from sina."""
        if self._name_map is not None:
            return self._name_map
        import akshare as ak

        try:
            df = ak.stock_info_a_code_name()
        except Exception as exc:
            raise ProviderError(f"stock_info_a_code_name failed: {exc}") from exc
        mapping: dict[str, str] = {}
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                mapping[str(row["code"]).zfill(6)] = str(row["name"])
        self._name_map = mapping
        return mapping

    def _get_tx_snapshot(self) -> pd.DataFrame:
        """Tencent full-market snapshot (cached 10 min). Columns:
        code(sh600036), name, pe_ttm, pn(PB), zsz(亿), ltsz(亿), zxj(price), zdf, hsl, volume(手)
        """
        now = time.time()
        if self._tx_snapshot is not None and now - self._tx_snapshot[0] < 600:
            return self._tx_snapshot[1]
        import akshare as ak

        try:
            df = ak.stock_zh_a_spot_tx()
        except Exception as exc:
            raise ProviderError(f"stock_zh_a_spot_tx failed: {exc}") from exc
        if df is None or df.empty:
            raise ProviderError("tencent snapshot is empty")
        self._tx_snapshot = (now, df)
        return df

    # ------------------------------------------------------------------ #
    def get_stock_basic(self, symbol: str) -> dict[str, Any] | None:
        code = ak_symbol(symbol)
        try:
            name = self._get_name_map().get(code)
        except Exception:
            # The Sina code/name list is occasionally blocked in cloud regions.
            # Tencent's snapshot is already used by this provider and supplies
            # the same code-to-name data, so use it as an operation-level fallback.
            snapshot = self._get_tx_snapshot()
            row = snapshot[snapshot["code"] == sina_symbol(symbol)]
            name = None if row.empty else row.iloc[0].get("name")

        if not name:
            return None
        display = code
        exchange = (
            "SSE"
            if display.startswith(("6", "9"))
            else "SZSE"
            if display.startswith(("0", "2", "3"))
            else "BSE"
        )
        # list date approximated by earliest daily bar (sina code-name api has no list date)
        list_date = None
        try:
            bars = self.get_daily_bars(symbol, "19900101", "21000101")
            if bars is not None and not bars.empty:
                list_date = bars["trade_date"].min()
        except Exception:
            pass
        return {
            "symbol": symbol,
            "display_symbol": display,
            "name": _clean_name(name),
            "exchange": exchange,
            "industry_raw": "",  # sina/tencent endpoints expose no industry; Tushare provides it when configured
            "list_date": list_date,
            "status": "ACTIVE",
        }

    # ------------------------------------------------------------------ #
    def get_daily_bars(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        import akshare as ak

        stock = sina_symbol(symbol)
        try:
            df = ak.stock_zh_a_daily(
                symbol=stock,
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="",
            )
        except Exception as exc:
            raise ProviderError(f"stock_zh_a_daily failed for {stock}: {exc}") from exc
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(
            columns={
                "date": "trade_date",
                "volume": "volume",
                "amount": "amount",
            }
        )
        df = df.dropna(subset=["close"])
        for c in ("open", "high", "low", "close", "volume", "amount"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"])
        # unify units with Tushare convention: volume in 手(100 shares), amount in 千元
        if "volume" in df.columns:
            df["volume"] = df["volume"] / 100.0
        if "amount" in df.columns:
            df["amount"] = df["amount"] / 1000.0
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df.sort_values("trade_date").reset_index(drop=True)
        df["pct_change"] = df["close"].pct_change() * 100.0
        df["pre_close"] = df["close"].shift(1)
        keep = [
            "trade_date", "open", "high", "low", "close",
            "pre_close", "pct_change", "volume", "amount",
        ]
        return df[[c for c in keep if c in df.columns]].reset_index(drop=True)

    def get_daily_basic(
        self, symbol: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """Latest-day valuation snapshot from tencent merged with the price history.

        Historical PE/PB percentile series is not available from the free
        fallback endpoints; the latest valuation is attached to the most recent
        trading day row. Tushare provides the full history when configured.
        """
        bars = self.get_daily_bars(symbol, start_date, end_date)
        if bars is None or bars.empty:
            return pd.DataFrame()
        snapshot = self._get_tx_snapshot()
        target = sina_symbol(symbol)
        row = snapshot[snapshot["code"] == target]
        if row.empty:
            row = snapshot[snapshot["name"] == symbol]
        out = bars.copy()
        if not row.empty:
            r = row.iloc[0]
            last_idx = out.index[-1]
            out.loc[last_idx, "pe"] = None
            out.loc[last_idx, "pe_ttm"] = _num(r.get("pe_ttm"))
            out.loc[last_idx, "pb"] = _num(r.get("pn"))
            out.loc[last_idx, "ps_ttm"] = None
            out.loc[last_idx, "dv_ratio"] = None
            # 统一单位（数据库口径：人民币元）。腾讯快照 zsz/ltsz 单位为亿元，
            # 换算为元（×1e8），与 financial_reports 中 total_equity 等字段一致，
            # 保证 datapoints.shares = total_mv / close 得到正确的总股本（股）。
            out.loc[last_idx, "total_mv"] = _num(r.get("zsz")) * 1e8 if _num(r.get("zsz")) else None
            out.loc[last_idx, "circ_mv"] = _num(r.get("ltsz")) * 1e8 if _num(r.get("ltsz")) else None
        else:
            for c in ("pe", "pe_ttm", "pb", "ps_ttm", "dv_ratio", "total_mv", "circ_mv"):
                out[c] = None
        cols = [
            "trade_date", "pe", "pe_ttm", "pb", "ps_ttm",
            "dv_ratio", "total_mv", "circ_mv",
        ]
        return out[[c for c in cols if c in out.columns]].reset_index(drop=True)

    # ------------------------------------------------------------------ #
    def get_financial_reports(self, symbol: str) -> pd.DataFrame:
        """Financial reports from sina's three statements.

        sina layout (akshare >= 1.18): rows = report periods, columns = metrics,
        includes an '公告日期' column -> announcement_date is real, look-ahead safe.
        """
        import akshare as ak

        stock = sina_symbol(symbol)
        frames: dict[str, pd.DataFrame] = {}
        for cn_name, key in (("利润表", "income"), ("资产负债表", "balance"), ("现金流量表", "cashflow")):
            try:
                df = ak.stock_financial_report_sina(stock=stock, symbol=cn_name)
                if df is not None and not df.empty:
                    frames[key] = df
            except Exception:
                continue
        if not frames:
            return pd.DataFrame()

        def period_col(frame: pd.DataFrame) -> str:
            for col in frame.columns:
                if "报告" in str(col):
                    return col
            return ""

        def series_of(frame: pd.DataFrame, keywords: tuple[str, ...]) -> pd.Series | None:
            """Extract a numeric metric as Series indexed by report period (datetime).

            Exact column-name match is tried first: substring matching can collide
            (e.g. "资产合计" hits "固定资产合计"), so preferred names come first.
            """
            pcol = period_col(frame)
            if not pcol:
                return None
            periods = pd.to_datetime(frame[pcol], errors="coerce")
            idx = periods.dropna().index

            def make_series(col: str) -> pd.Series:
                vals = pd.to_numeric(frame[col], errors="coerce")
                return pd.Series(vals.loc[idx].values, index=periods.loc[idx])

            exact = [str(c).strip() for c in frame.columns if str(c).strip() in keywords]
            for kw in keywords:
                if kw in exact:
                    return make_series(kw)
            for col in frame.columns:
                if any(kw in str(col) for kw in keywords):
                    return make_series(col)
            return None

        def ann_series(frame: pd.DataFrame) -> pd.Series | None:
            pcol = period_col(frame)
            if not pcol:
                return None
            periods = pd.to_datetime(frame[pcol], errors="coerce")
            idx = periods.dropna().index
            for col in frame.columns:
                if "公告日期" in str(col):
                    vals = pd.to_datetime(frame[col], errors="coerce")
                    return pd.Series(vals.loc[idx].values, index=periods.loc[idx])
            return None

        income = frames.get("income")
        balance = frames.get("balance")
        cashflow = frames.get("cashflow")
        revenue = series_of(income, ("营业总收入", "营业收入")) if income is not None else None
        net_profit = series_of(income, ("归属于母公司的净利润", "归属于母公司所有者的净利润", "净利润")) if income is not None else None
        deducted = series_of(income, ("归属于母公司股东的净利润", "扣除非经常性损益", "净利润")) if income is not None else None
        eps = series_of(income, ("基本每股收益", "每股收益")) if income is not None else None
        total_assets = series_of(balance, ("资产总计", "资产合计")) if balance is not None else None
        total_equity = series_of(balance, ("所有者权益（或股东权益）合计", "所有者权益合计", "股东权益合计", "归属于母公司股东权益合计", "归属于母公司股东的权益")) if balance is not None else None
        total_debt = series_of(balance, ("负债合计",)) if balance is not None else None
        operating_cashflow = series_of(cashflow, ("经营活动产生的现金流量净额", "经营活动现金流量净额")) if cashflow is not None else None
        ann_income = ann_series(income) if income is not None else None
        ann_balance = ann_series(balance) if balance is not None else None
        ann_cashflow = ann_series(cashflow) if cashflow is not None else None

        all_periods: pd.Index = pd.Index([])
        for s in (revenue, net_profit, deducted, eps, total_assets, total_equity,
                  total_debt, operating_cashflow):
            if s is not None:
                all_periods = all_periods.union(s.index)
        all_periods = all_periods.sort_values()

        def pick(series: pd.Series | None, period) -> float | None:
            if series is None:
                return None
            vals = series.get(period)
            return _num(vals)

        def pick_ann(series: pd.Series | None, period):
            if series is None:
                return None
            v = series.get(period)
            result = _num(v)
            if result is None and v is not None and not pd.isna(v):
                try:
                    result = pd.to_datetime(v).date()
                except Exception:
                    result = None
            return result

        def clamp_ann(period: date, ann) -> date | None:
            """Sina's 公告日期 column is frequently shifted/misaligned (e.g. the
            2024 annual report gets the 2025 announcement date). Clamp to the
            legally plausible window [period_end, period_end + legal_days] so the
            series stays look-ahead safe: never let a report "become available"
            earlier than its true disclosure, nor far later than allowed.
            """
            if ann is None:
                return ann
            legal_days = 120 if period.month == 12 else 60
            horizon = period + timedelta(days=legal_days)
            if ann < period or ann > horizon:
                return horizon
            return ann

        rows: list[dict[str, Any]] = []
        for p in all_periods:
            if pd.isna(p):
                continue
            period = p.date()
            rev = pick(revenue, p)
            np_ = pick(net_profit, p)
            ded_ = pick(deducted, p)
            eps_ = pick(eps, p)
            ta = pick(total_assets, p)
            te = pick(total_equity, p)
            td = pick(total_debt, p)
            ann = clamp_ann(period, pick_ann(ann_income, p) or pick_ann(ann_balance, p) or pick_ann(ann_cashflow, p))
            rows.append(
                {
                    "report_period": period,
                    "announcement_date": ann,
                    "revenue": rev,
                    "revenue_yoy": None,
                    "net_profit": np_,
                    "net_profit_yoy": None,
                    "deducted_net_profit": ded_,
                    "operating_cashflow": pick(operating_cashflow, p),
                    "free_cashflow": None,
                    "total_assets": ta,
                    "total_equity": te,
                    "total_debt": td,
                    "eps": eps_,
                    "roe": (np_ / te * 100) if np_ and te else None,
                    "roa": (np_ / ta * 100) if np_ and ta else None,
                    "gross_margin": None,
                    "net_margin": (np_ / rev * 100) if np_ and rev else None,
                    "debt_ratio": (td / ta * 100) if td and ta else None,
                    "current_ratio": None,
                }
            )

        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df = df.sort_values("report_period").reset_index(drop=True)
        df["revenue_yoy"] = _compute_yoy(df["report_period"], df["revenue"])
        df["net_profit_yoy"] = _compute_yoy(df["report_period"], df["net_profit"])
        return df


def _compute_yoy(periods: pd.Series, values: pd.Series) -> pd.Series:
    lookup: dict[tuple[int, int], float | None] = {}
    for p, v in zip(periods, values):
        key = (p.year, p.month)
        lookup[key] = v if pd.notna(v) else None
    out: list[float | None] = []
    for p, v in zip(periods, values):
        prev = lookup.get((p.year - 1, p.month))
        if v is not None and prev not in (None, 0) and prev is not None:
            try:
                out.append(float(v) / float(prev) - 1.0)
            except (TypeError, ZeroDivisionError):
                out.append(None)
        else:
            try:
                out.append(None if v is None else (None if prev is None or prev == 0 else float(v) / float(prev) - 1.0))
            except (TypeError, ZeroDivisionError):
                out.append(None)
    return pd.Series(out, index=periods.index)

