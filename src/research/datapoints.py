"""Prepared fundamental data points for one security at one as-of date.

Everything here is look-ahead safe: a financial report only becomes usable on
its announcement date, which is clamped into a legally plausible window by the
provider. Valuation percentiles are computed from a *synthetic* historical
series (daily close against the latest announced earnings/equity), because the
free snapshot API only exposes one valuation point.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import Engine

from src.research.normalization import percentile_score

_FIN_COLS = [
    "report_period", "announcement_date", "report_type",
    "revenue", "revenue_yoy", "net_profit", "net_profit_yoy",
    "deducted_net_profit", "operating_cashflow", "total_assets",
    "total_equity", "total_debt", "eps", "roe", "roa",
    "gross_margin", "net_margin", "debt_ratio",
]
_MKT_COLS = [
    "trade_date", "close", "pct_change", "pe_ttm", "pb", "dv_ratio",
    "total_mv", "circ_mv", "source",
]


@dataclass
class DataPoints:
    """All raw/prepared inputs a score model can consume.

    ``raw`` holds numbers (or None when not computable); ``history`` holds
    pandas Series used for percentile ranks. Suffix semantics:
      *_ttm    - trailing-twelve-month net earnings, annualized where needed
      *_pctl   - 0-100 percentile of the current value inside 5y history
    """

    as_of: date
    symbol: str
    name: str
    industry_model: str

    # --- latest report snapshot ---------------------------------------- #
    latest_period: date | None = None
    latest_ann: date | None = None
    report_age_days: int | None = None

    # --- single point metrics (None = not computable) ------------------- #
    roe: float | None = None
    roa: float | None = None
    net_margin: float | None = None
    gross_margin: float | None = None
    debt_ratio: float | None = None
    revenue_yoy: float | None = None
    net_profit_yoy: float | None = None
    revenue_cagr3: float | None = None
    net_profit_cagr3: float | None = None
    eps_ttm: float | None = None
    ocf_to_np: float | None = None           # operating cashflow / net profit
    cash_ratio: float | None = None          # cash & equivalents asset ratio (unused for banks)
    roe_avg3: float | None = None
    roe_stability: float | None = None       # 1 - cv, higher is more stable
    np_yoy_vol: float | None = None          # stdev of trailing net profit growth

    # --- market / valuation --------------------------------------------- #
    close: float | None = None
    total_mv: float | None = None
    shares: float | None = None              # inferred total shares
    dv_ratio: float | None = None
    pe_ttm_raw: float | None = None          # snapshot PE-TTM if provider gave one
    pb_raw: float | None = None
    pe_ttm: float | None = None              # synthetic PE-TTM at latest close
    pb: float | None = None                  # synthetic PB at latest close
    pe_pctl: float | None = None             # 5y percentile of synthetic PE
    pb_pctl: float | None = None             # 5y percentile of synthetic PB
    ann_vol: float | None = None             # annualized price volatility
    max_drawdown: float | None = None        # 5y maximum drawdown (%)

    # --- history for percentile bases ------------------------------------ #
    roe_history: list[float] = field(default_factory=list)
    net_margin_history: list[float] = field(default_factory=list)
    revenue_yoy_history: list[float] = field(default_factory=list)
    np_yoy_history: list[float] = field(default_factory=list)
    pe_history: list[float] = field(default_factory=list)
    pb_history: list[float] = field(default_factory=list)
    np_ttm_history: list[float] = field(default_factory=list)
    growth_history_ok: bool = False

    # --- data quality ----------------------------------------------------- #
    missing_keys: list[str] = field(default_factory=list)
    clamp_ann_count: int = 0

    def add_missing(self, key: str) -> None:
        if key not in self.missing_keys:
            self.missing_keys.append(key)

    def get(self, key: str) -> float | None:
        return getattr(self, key, None)


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _cagr(start: float | None, end: float | None, years: float) -> float | None:
    if not start or not end or start <= 0:
        return None
    ratio = end / start
    if ratio <= 0:
        return None
    return (ratio ** (1.0 / years)) - 1.0


def build_data_points(
    engine: Engine,
    security_id: int,
    symbol: str,
    name: str,
    industry_model: str,
    as_of: date,
) -> DataPoints:
    """Load financial + market history and prepare DataPoints (look-ahead safe)."""
    with engine.connect() as conn:
        fin = pd.read_sql(
            f"SELECT {', '.join(_FIN_COLS)} FROM financial_reports "
            f"WHERE security_id = {int(security_id)} ORDER BY report_period",
            conn,
        )
        mkt = pd.read_sql(
            f"SELECT {', '.join(_MKT_COLS)} FROM daily_market "
            f"WHERE security_id = {int(security_id)} ORDER BY trade_date",
            conn,
        )

    fin["report_period"] = pd.to_datetime(fin["report_period"], errors="coerce").dt.date
    ann = pd.to_datetime(fin["announcement_date"], errors="coerce").dt.date
    fin["ann_eff"] = ann.where(ann.notna(), pd.NaT)

    dp = DataPoints(
        as_of=as_of, symbol=symbol, name=name, industry_model=industry_model
    )

    if mkt.empty:
        dp.add_missing("market")
    else:
        mkt["trade_date"] = pd.to_datetime(mkt["trade_date"], errors="coerce").dt.date
        mkt = mkt[(mkt["trade_date"].notna()) & (mkt["trade_date"] <= as_of)]
        _prepare_market(dp, mkt, as_of)

    # ---- financial side ------------------------------------------------ #
    usable = fin[fin["ann_eff"].notna() & (fin["ann_eff"] <= as_of)].copy()
    if usable.empty:
        dp.add_missing("financial_report")
        dp.missing_keys += ["roe", "roa", "net_margin", "growth", "valuation"]
        return dp

    usable = usable.sort_values("report_period").reset_index(drop=True)
    latest = usable.iloc[-1]
    dp.latest_period = latest["report_period"]
    latest_ann = latest["ann_eff"]
    dp.latest_ann = latest_ann
    if latest_ann is not None:
        dp.report_age_days = max(0, (as_of - latest_ann).days)

    # 3y growth from annual reports (full-year periods)
    annual = usable[usable["report_type"].astype(str).isin(["ANNUAL", "年报"])]
    annual = annual.drop_duplicates("report_period").sort_values("report_period")
    if len(annual) >= 4:
        y3_rev0 = _f(annual.iloc[-4]["revenue"])
        y3_rev1 = _f(annual.iloc[-1]["revenue"])
        y3_np0 = _f(annual.iloc[-4]["net_profit"])
        y3_np1 = _f(annual.iloc[-1]["net_profit"])
        dp.revenue_cagr3 = _cagr(y3_rev0, y3_rev1, 3.0)
        dp.net_profit_cagr3 = _cagr(y3_np0, y3_np1, 3.0)
        dp.growth_history_ok = True

    # latest single-period metrics (cumulative values for interim reports)
    dp.roe = _f(latest.get("roe"))
    dp.roa = _f(latest.get("roa"))
    dp.net_margin = _f(latest.get("net_margin"))
    dp.gross_margin = _f(latest.get("gross_margin"))
    dp.debt_ratio = _f(latest.get("debt_ratio"))
    dp.revenue_yoy = _f(latest.get("revenue_yoy"))
    dp.net_profit_yoy = _f(latest.get("net_profit_yoy"))
    eps_raw = _f(latest.get("eps"))
    if eps_raw is None and _f(latest.get("net_profit")) is not None and dp.shares:
        eps_raw = _f(latest.get("net_profit")) / dp.shares

    # trailing-twelve-month net profit: annual + YTD - prior-year YTD
    periods = usable["report_period"].tolist()
    reports = usable.to_dict("records")
    period_to_row = {pd.Timestamp(r["report_period"]).date(): r for r in reports}
    np_ttm: dict[date, float | None] = {}
    for r in reports:
        p: date = pd.Timestamp(r["report_period"]).date()
        if p.month == 12:
            np_ttm[p] = _f(r["net_profit"])
            continue
        prev_annual = None
        prev_ytd = None
        # same month last year (YTD), and latest annual before p
        for q in periods:
            qd: date = pd.Timestamp(q).date()
            if qd < p and qd.month == p.month:
                prev_ytd = period_to_row.get(qd, {}).get("net_profit")
            if qd < p and qd.month == 12:
                prev_annual = period_to_row.get(qd, {}).get("net_profit")
        cur = _f(r["net_profit"])
        if cur is not None and prev_ytd is not None and prev_annual is not None:
            np_ttm[p] = cur + prev_annual - prev_ytd
        elif prev_annual is not None:
            # no prior-year YTD (e.g. newly disclosed interim) -> annual-only proxy
            np_ttm[p] = prev_annual

    dp.np_ttm_history = [v for v in np_ttm.values() if v is not None]
    latest_np_ttm = np_ttm.get(dp.latest_period)
    if dp.shares and latest_np_ttm:
        dp.eps_ttm = latest_np_ttm / dp.shares

    # operating cash flow conversion (latest period, cumulative basis note)
    ocf = _f(latest.get("operating_cashflow"))
    np_cur = _f(latest.get("net_profit"))
    if ocf is not None and np_cur:
        dp.ocf_to_np = ocf / np_cur

    # ROE history & stability (annualized-normalized where possible)
    roe_s: list[float] = []
    np_margin_s: list[float] = []
    rev_yoy_s: list[float] = []
    np_yoy_s: list[float] = []
    for r in usable.itertuples(index=False):
        v_roe = _f(getattr(r, "roe", None))
        v_margin = _f(getattr(r, "net_margin", None))
        v_ryoy = _f(getattr(r, "revenue_yoy", None))
        v_nyoy = _f(getattr(r, "net_profit_yoy", None))
        if v_roe is not None:
            roe_s.append(v_roe)
        if v_margin is not None:
            np_margin_s.append(v_margin)
        if v_ryoy is not None:
            rev_yoy_s.append(v_ryoy)
        if v_nyoy is not None:
            np_yoy_s.append(v_nyoy)
    dp.roe_history = roe_s
    dp.net_margin_history = np_margin_s
    dp.revenue_yoy_history = rev_yoy_s
    dp.np_yoy_history = np_yoy_s

    if roe_s:
        dp.roe_avg3 = sum(roe_s[-4:]) / len(roe_s[-4:]) if roe_s else None
        if len(roe_s) >= 3:
            mean = sum(roe_s) / len(roe_s)
            var = sum((x - mean) ** 2 for x in roe_s) / len(roe_s)
            sd = math.sqrt(var)
            dp.roe_stability = max(0.0, 1.0 - (sd / mean if mean else 0.0))
    if len(np_yoy_s) >= 3:
        mean = sum(np_yoy_s) / len(np_yoy_s)
        var = sum((x - mean) ** 2 for x in np_yoy_s) / len(np_yoy_s)
        dp.np_yoy_vol = math.sqrt(var)

    # ---- synthetic valuation history (daily close vs latest announced TTM) - #
    if not mkt.empty:
        _synthetic_valuation(dp, mkt, usable, np_ttm)

    _quality_flags(dp, latest)
    return dp


def _prepare_market(dp: DataPoints, mkt: pd.DataFrame, as_of: date) -> None:
    """Latest price/mv + volatility + max drawdown from daily bars."""
    if mkt.empty:
        return
    mkt = mkt.sort_values("trade_date")
    last = mkt.iloc[-1]
    dp.close = _f(last.get("close"))
    dp.total_mv = _f(last.get("total_mv"))
    dp.pe_ttm_raw = _f(last.get("pe_ttm"))
    dp.pb_raw = _f(last.get("pb"))
    dp.dv_ratio = _f(last.get("dv_ratio"))
    if dp.close and dp.total_mv:
        dp.shares = dp.total_mv / dp.close

    # volatility from daily pct_change (annualized)
    pct = pd.to_numeric(mkt["pct_change"], errors="coerce").dropna()
    if len(pct) >= 30:
        sd = pct.std(ddof=0)
        dp.ann_vol = (sd * math.sqrt(252) * 100.0) if sd == sd else None

    closes = pd.to_numeric(mkt["close"], errors="coerce").dropna()
    if len(closes) >= 30:
        running_max = closes.cummax()
        dd = (closes / running_max - 1.0) * 100.0
        dp.max_drawdown = float(dd.min())


def _synthetic_valuation(
    dp: DataPoints,
    mkt: pd.DataFrame,
    usable: pd.DataFrame,
    np_ttm: dict[date, float | None],
) -> None:
    """Compute daily-synthetic PE/PB series and their 5y percentiles.

    PE_t = close_t * shares / TTM_net_profit(r_t)      (r_t: latest report with
    announcement <= t). PB_t = close_t * shares / total_equity(r_t).
    Shares are inferred from the snapshot total market value, a documented
    approximation (discussed in the explanation note).
    """
    if not dp.shares:
        dp.add_missing("shares")
        return

    rows = usable.to_dict("records")
    # build timeline of "report becomes available" events
    events: list[tuple[date, float | None, float | None]] = []  # (eff_date, np_ttm, equity)
    for r in rows:
        p: date = pd.Timestamp(r["report_period"]).date()
        eff = pd.Timestamp(r["ann_eff"]).date() if r.get("ann_eff") is not None else None
        if eff is None:
            continue
        events.append((eff, np_ttm.get(p), _f(r.get("total_equity"))))
    events.sort(key=lambda e: e[0])
    if not events:
        dp.add_missing("valuation")
        return

    # forward-fill the latest usable fundamental by date
    ev_idx = 0
    cur_ttm = None
    cur_eq = None
    pe_ser: list[float] = []
    pb_ser: list[float] = []

    mkt2 = mkt.sort_values("trade_date")
    for _, row in mkt2.iterrows():
        t: date = row["trade_date"]
        while ev_idx < len(events) and events[ev_idx][0] <= t:
            cur_ttm = events[ev_idx][1]
            cur_eq = events[ev_idx][2]
            ev_idx += 1
        c = _f(row.get("close"))
        if c is None:
            continue
        if cur_ttm and cur_ttm > 0:
            pe_ser.append(c * dp.shares / cur_ttm)
        if cur_eq and cur_eq > 0:
            pb_ser.append(c * dp.shares / cur_eq)

    dp.pe_history = pe_ser
    dp.pb_history = pb_ser

    if pe_ser:
        dp.pe_ttm = pe_ser[-1]
        dp.pe_pctl = percentile_score(dp.pe_ttm, pe_ser, higher_better=False)
    else:
        dp.add_missing("pe_ttm")
    if pb_ser:
        dp.pb = pb_ser[-1]
        dp.pb_pctl = percentile_score(dp.pb, pb_ser, higher_better=False)
    else:
        dp.add_missing("pb")


def _quality_flags(dp: DataPoints, latest: Any) -> None:
    """Flags for warning-level data/quality issues consumed by confidence."""
    if dp.latest_ann is None:
        dp.add_missing("announcement_date")
    if dp.report_age_days is not None and dp.report_age_days > 200:
        dp.add_missing("stale_report")
    if dp.dv_ratio is None:
        dp.add_missing("dividend")
    if dp.ocf_to_np is None:
        dp.add_missing("cashflow")
    if dp.revenue_cagr3 is None:
        dp.add_missing("cagr3")
    if not dp.pe_history or not dp.pb_history:
        dp.add_missing("valuation_history")
    if dp.ann_vol is None:
        dp.add_missing("volatility")