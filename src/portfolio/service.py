# -*- coding: utf-8 -*-
"""Phase 5 portfolio service: build/load portfolios, persist risk snapshots.

The service reads holdings + daily returns from SQLite, computes the complete
Section 25 risk picture (extended with the V1.1 patch items) and persists an
immutable PortfolioSnapshot row (history preserved, never overwritten).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory
from src.portfolio.metrics import (
    PortfolioRisk,
    beta_stats,
    classify_risk_level,
    concentration_hhi,
    downside_deviation,
    down_market_correlations,
    forward_drawdown_estimate,
    historical_max_drawdown,
    load_benchmark_symbol,
    normal_correlations,
    portfolio_annual_vol,
    risk_contribution,
    simple_expected_return,
    var_cvar,
)
from src.portfolio.stress import StressEngine, load_preset_scenarios


@dataclass
class Holding:
    security_id: int
    symbol: str
    display: str
    name: str
    industry_model: str
    quantity: float
    cost_price: float | None
    weight: float  # market-value weight (computed)
    close: float | None


@dataclass
class PortfolioRiskResult:
    as_of: date
    portfolio_id: int
    holdings: list[Holding]
    risk: PortfolioRisk
    per_stock: dict[str, dict[str, Any]]  # symbol -> beta stats etc.
    stress: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def _load_weighted_holdings(engine: Engine, portfolio_id: int, as_of: date) -> tuple[list[Holding], float]:
    """Holdings with market-value weights at as_of; returns (holdings, cash_weight).

    Weight = quantity * latest close / total value (or manual override when
    manual_current_weight is provided). Cash weight defaults to
    portfolio.min_cash_weight (target buffer); the position weights are
    renormalized to 1 - cash_weight.
    """
    factory = make_session_factory(engine)
    with factory() as session:
        pf = session.get(models.Portfolio, portfolio_id)
        if pf is None:
            raise KeyError(f"portfolio {portfolio_id} not found")
        pos = session.execute(
            select(models.PortfolioPosition).where(
                models.PortfolioPosition.portfolio_id == portfolio_id
            )
        ).scalars().all()
        sec_rows = session.execute(
            select(models.Security)
        ).scalars().all()
    sec_map = {s.id: s for s in sec_rows}
    if not pos:
        raise ValueError(f"portfolio {portfolio_id} has no positions")

    rows: list[Holding] = []
    for p in pos:
        sec = sec_map.get(p.security_id)
        if sec is None:
            continue
        close = _latest_close(engine, p.security_id, as_of)
        rows.append(
            Holding(
                security_id=p.security_id,
                symbol=sec.symbol,
                display=sec.display_symbol,
                name=sec.name,
                industry_model=sec.industry_model,
                quantity=p.quantity,
                cost_price=p.cost_price,
                weight=0.0,
                close=close,
            )
        )
    if not rows:
        raise ValueError("no securities resolved from positions")

    # weights: manual override wins; otherwise market value
    total = 0.0
    manual_weights: dict[int, float | None] = {r.security_id: None for r in rows}
    for p in pos:
        if p.manual_current_weight is not None:
            manual_weights[p.security_id] = float(p.manual_current_weight)
    for r in rows:
        mv = (r.quantity * r.close) if r.close else None
        if manual_weights[r.security_id] is not None:
            r.weight = float(manual_weights[r.security_id])
        elif mv is not None:
            total += mv
        else:
            r.weight = 0.0
    # normalize market-value weights
    for r in rows:
        if manual_weights[r.security_id] is None and r.close:
            r.weight = (r.quantity * r.close) / total if total > 0 else 0.0

    cash_weight = float(
        getattr(pf, "min_cash_weight", 0.05) or 0.05
    )
    # If manual full weights are provided and sum to ~1, treat cash as 0.
    mw_sum = sum(w for w in manual_weights.values() if w is not None)
    if mw_sum and abs(mw_sum - 1.0) < 1e-6:
        cash_weight = 0.0
    scale = 1.0 - cash_weight
    for r in rows:
        r.weight = round(r.weight * scale, 6)
    return rows, round(cash_weight, 4)


def _latest_close(engine: Engine, security_id: int, as_of: date) -> float | None:
    factory = make_session_factory(engine)
    with factory() as session:
        row = session.execute(
            select(models.DailyMarket.close)
            .where(models.DailyMarket.security_id == security_id)
            .where(models.DailyMarket.trade_date <= as_of)
            .order_by(models.DailyMarket.trade_date.desc())
            .limit(1)
        ).first()
        return row[0] if row else None


def _return_matrix(
    engine: Engine,
    security_ids: list[int],
    as_of: date,
    days: int = 400,
) -> tuple[np.ndarray, list[date], dict[int, list[float]]]:
    """Aligned daily pct_change matrix (n_days x n_stocks) ending at as_of.

    Returns the matrix (with NaNs for missing days), the date index, and per
    security close prices (used for historical MDD when no returns exist yet).
    """
    start = as_of - timedelta(days=int(days * 1.5) + 30)
    factory = make_session_factory(engine)
    with factory() as session:
        q = (
            select(
                models.DailyMarket.security_id,
                models.DailyMarket.trade_date,
                models.DailyMarket.pct_change,
                models.DailyMarket.close,
            )
            .where(models.DailyMarket.security_id.in_(security_ids))
            .where(models.DailyMarket.trade_date > start)
            .where(models.DailyMarket.trade_date <= as_of)
            .order_by(models.DailyMarket.trade_date)
        )
        rows = session.execute(q).all()
    df = pd.DataFrame(
        rows, columns=["security_id", "trade_date", "pct_change", "close"]
    )
    if df.empty:
        return np.empty((0, len(security_ids))), [], {}

    pivot = df.pivot_table(
        index="trade_date", columns="security_id", values="pct_change", aggfunc="last"
    )
    dates = list(pivot.index)
    mat = pivot.to_numpy(dtype=float) / 100.0  # pct -> decimal
    closes: dict[int, list[float]] = {}
    for sid in security_ids:
        c = df[df["security_id"] == sid].dropna(subset=["close"])["close"].tolist()
        if c:
            closes[sid] = c
    return mat, dates, closes


def _benchmark_returns(engine: Engine, as_of: date, sym: str | None = None) -> tuple[np.ndarray, list[date]]:
    """Benchmark pct_change series (fallback to an empty array when missing)."""
    target = sym or load_benchmark_symbol()
    factory = make_session_factory(engine)
    with factory() as session:
        bm = session.execute(
            select(models.Security.id).where(models.Security.symbol == target)
        ).first()
        # tolerate "000300" without suffix as well
        if bm is None:
            bm = session.execute(
                select(models.Security.id).where(models.Security.symbol.like(f"{target.split('.')[0]}%"))
            ).first()
        if bm is None:
            return np.empty(0), []
        rows = session.execute(
            select(models.DailyMarket.trade_date, models.DailyMarket.pct_change)
            .where(models.DailyMarket.security_id == bm[0])
            .order_by(models.DailyMarket.trade_date)
        ).all()
    if not rows:
        return np.empty(0), []
    dates = [r[0] for r in rows if r[1] is not None]
    vals = [float(r[1]) / 100.0 for r in rows if r[1] is not None]
    return np.asarray(vals), dates


class PortfolioRiskService:
    def __init__(self, engine: Engine, benchmark_symbol: str | None = None) -> None:
        self._engine = engine
        self._benchmark = benchmark_symbol or load_benchmark_symbol()

    # ------------------------------------------------------------------
    def compute_snapshot(self, portfolio_id: int, as_of: date) -> PortfolioRiskResult:
        holdings, cash_weight = _load_weighted_holdings(self._engine, portfolio_id, as_of)
        ids = [h.security_id for h in holdings]
        mat, dates, closes = _return_matrix(self._engine, ids, as_of)
        bm_ret, bm_dates = _benchmark_returns(self._engine, as_of, self._benchmark)

        # Align stocks and benchmark on calendar dates for beta calculations.
        stocks_df = pd.DataFrame(mat, index=dates)
        if len(bm_ret):
            bm_series = pd.Series(bm_ret, index=bm_dates)
            stocks_df = stocks_df.loc[stocks_df.index.isin(bm_series.index)]
            bm_series = bm_series.loc[stocks_df.index]
            stocks_df = stocks_df.dropna(how="all")
            bm_series = bm_series.reindex(stocks_df.index)
            bm_arr = bm_series.to_numpy(dtype=float)
        else:
            bm_arr = np.empty(0)

        weights = np.asarray([h.weight for h in holdings], dtype=float)
        if mat.shape[0] >= 30:
            ret_matrix = stocks_df.to_numpy(dtype=float)
        else:
            ret_matrix = mat

        # --- per-stock beta against benchmark ------------------------------ #
        per_stock: dict[str, dict[str, Any]] = {}
        stock_betas: list[float] = []
        beta_contrib = {}
        for i, h in enumerate(holdings):
            sym = h.symbol
            if len(bm_arr) and stocks_df.shape[0] >= 2:
                col = pd.to_numeric(stocks_df.iloc[:, i], errors="coerce").to_numpy(dtype=float)
                valid = ~np.isnan(col)
                if int(valid.sum()) >= 2:
                    bs = beta_stats(col[valid], bm_arr[valid])
                else:
                    bs = beta_stats(np.array([], dtype=float), np.empty(0))
            else:
                bs = beta_stats(np.array([], dtype=float), np.empty(0))
            per_stock[sym] = {
                "long_beta": bs.long_beta,
                "beta_60d": bs.beta_60d,
                "beta_120d": bs.beta_120d,
                "beta_252d": bs.beta_252d,
                "downside_beta": bs.downside_beta,
                "r2": bs.r2,
                "obs": bs.obs,
                "flag": bs.flag,
            }
            if bs.long_beta != 0.0 or bs.obs >= 2:
                stock_betas.append(bs.long_beta)
                beta_contrib[sym] = bs
            else:
                beta_contrib[sym] = bs

        # Portfolio beta: from aligned returns vs benchmark when possible,
        # otherwise Σwβ.
        if len(bm_arr) and stocks_df.shape[0] >= 30:
            port_ret = (
                stocks_df.to_numpy(dtype=float) @ weights
            )
            pf_beta = beta_stats(port_ret, bm_arr).long_beta
        elif stock_betas:
            pf_beta = float(np.asarray(weights) @ np.asarray(stock_betas))
        else:
            pf_beta = 0.0

        # --- portfolio-level risk ------------------------------------------ #
        weights_np = np.asarray(weights, dtype=float)
        notes: list[str] = []
        vol = 0.0
        var95 = cvar95 = dd_dev = 0.0
        hist_mdd = 0.0
        if stocks_df.shape[0] >= 30:
            mat_f = stocks_df.to_numpy(dtype=float)
            # drop rows where all stocks are NaN
            mat_f = mat_f[~np.isnan(mat_f).all(axis=1)]
            if mat_f.shape[0] >= 30:
                port_ret = np.nansum(mat_f * weights_np, axis=1)
                port_ret = port_ret[~np.isnan(port_ret)]
                vol = portfolio_annual_vol(np.nan_to_num(mat_f, nan=0.0), weights_np)
                var95, cvar95 = var_cvar(port_ret)
                dd_dev = downside_deviation(port_ret)
                # historical MDD from cumulative portfolio returns
                cum = np.cumprod(1.0 + port_ret)
                hist_mdd = historical_max_drawdown(cum)
            else:
                notes.append("insufficient portfolio returns (<30) for vol/VaR")
        else:
            notes.append("insufficient portfolio returns (<30) for vol/VaR")

        # per-stock prices fallback for MDD when returns matrix is short
        if hist_mdd == 0.0 and closes:
            mdd = 0.0
            for sid, c in closes.items():
                if len(c) >= 2:
                    mdd = min(mdd, historical_max_drawdown(np.asarray(c)))
            hist_mdd = round(mdd, 2) if mdd else 0.0

        hhi = concentration_hhi(weights)
        avg_corr = normal_correlations(stocks_df.to_numpy(dtype=float)) if stocks_df.shape[1] >= 2 and stocks_df.shape[0] >= 30 else None
        stress_corr = (
            down_market_correlations(stocks_df.to_numpy(dtype=float), bm_arr)
            if stocks_df.shape[1] >= 2 and stocks_df.shape[0] >= 30 and len(bm_arr) >= 30
            else None
        )
        rc = risk_contribution(np.nan_to_num(stocks_df.to_numpy(dtype=float), nan=0.0), weights_np) if stocks_df.shape[0] >= 30 else {}
        rc_by_sym = {holdings[i].symbol: rc.get(i, 0.0) for i in range(len(holdings))}

        expected_ret = _expected_return_from_research(self._engine, holdings)
        fwd_mdd = (
            round(forward_drawdown_estimate(vol / 100.0) * 100.0, 2)
            if vol > 0
            else None
        )

        risk_level = classify_risk_level(
            pf_beta, vol, float(max(weights_np.tolist() or [0.0])), list(weights_np), cash_weight
        )

        # liquidity flag: all holdings with close and >=252 obs -> LIQUID
        min_obs = min((per_stock[s]["obs"] for s in per_stock), default=0)
        if min_obs >= 252:
            liquidity = "LIQUID"
        elif min_obs >= 60:
            liquidity = "LOW_HISTORY"
        else:
            liquidity = "ILLIQUID"

        # --- stress scenarios ---------------------------------------------- #
        stress_engine = StressEngine(self._engine)
        scenarios = load_preset_scenarios(self._engine)
        stress_out: dict[str, dict[str, Any]] = {}
        if len(bm_arr) and stocks_df.shape[0] >= 30:
            try:
                stress_out = stress_engine.run(
                    holdings=holdings,
                    base_betas={s: per_stock[s]["long_beta"] for s in per_stock},
                    bench_return=bm_arr,
                    bench_pct=1.0,
                    scenarios=scenarios,
                )
            except Exception as exc:  # noqa: BLE001
                notes.append(f"stress test skipped: {exc}")
        else:
            notes.append("stress test skipped: benchmark/model history insufficient")

        stress_mdd = None
        if stress_out:
            mdd_vals = [v.get("scenario_pct", 0.0) for v in stress_out.values() if v.get("scenario_pct") is not None]
            if mdd_vals:
                stress_mdd = round(float(min(mdd_vals)), 2)

        risk = PortfolioRisk(
            expected_return=expected_ret,
            portfolio_beta=round(float(pf_beta), 3),
            annualized_volatility=vol,
            max_drawdown_estimate=hist_mdd,
            stress_mdd=stress_mdd,
            forward_mdd=fwd_mdd,
            var_95=var95,
            cvar_95=cvar95,
            downside_deviation=dd_dev,
            concentration_hhi=hhi,
            normal_corr_avg=avg_corr,
            stress_corr_avg=stress_corr,
            downside_beta=per_stock and min((v["downside_beta"] for v in per_stock.values() if v["downside_beta"] is not None), default=None),
            cash_weight=cash_weight,
            risk_contrib=rc_by_sym,
            liquidity_flag=liquidity,
            risk_level=risk_level,
        )
        return PortfolioRiskResult(
            as_of=as_of,
            portfolio_id=portfolio_id,
            holdings=holdings,
            risk=risk,
            per_stock=per_stock,
            stress=stress_out,
            notes=notes,
        )

    # ------------------------------------------------------------------
    def persist_snapshot(self, result: PortfolioRiskResult) -> models.PortfolioSnapshot:
        """Write one immutable snapshot row; history is preserved."""
        factory = make_session_factory(self._engine)
        with factory() as session:
            row = models.PortfolioSnapshot(
                portfolio_id=result.portfolio_id,
                as_of_date=result.as_of,
                expected_return=result.risk.expected_return,
                portfolio_beta=result.risk.portfolio_beta,
                annualized_volatility=result.risk.annualized_volatility,
                max_drawdown_estimate=result.risk.max_drawdown_estimate,
                var_95=result.risk.var_95,
                cvar_95=result.risk.cvar_95,
                downside_deviation=result.risk.downside_deviation,
                concentration_hhi=result.risk.concentration_hhi,
                cash_weight=result.risk.cash_weight,
                risk_level=result.risk.risk_level,
                target_weights_json=json.dumps(
                    {h.symbol: h.weight for h in result.holdings}, ensure_ascii=False
                ),
                signals_json=json.dumps([], ensure_ascii=False),
                # V1.1 patch columns
                downside_beta=result.risk.downside_beta,
                beta_regime=_portfolio_beta_regime(result),
                normal_corr_avg=result.risk.normal_corr_avg,
                stress_corr_avg=result.risk.stress_corr_avg,
                stress_mdd=result.risk.stress_mdd,
                forward_mdd=result.risk.forward_mdd,
                risk_contrib_json=json.dumps(
                    result.risk.risk_contrib, ensure_ascii=False
                ),
                liquidity_flag=result.risk.liquidity_flag,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    def latest_snapshot(self, portfolio_id: int) -> models.PortfolioSnapshot | None:
        factory = make_session_factory(self._engine)
        with factory() as session:
            return session.execute(
                select(models.PortfolioSnapshot)
                .where(models.PortfolioSnapshot.portfolio_id == portfolio_id)
                .order_by(models.PortfolioSnapshot.as_of_date.desc())
                .limit(1)
            ).scalar_one_or_none()

    def to_dict(self, result: PortfolioRiskResult) -> dict[str, Any]:
        return {
            "as_of": result.as_of.isoformat(),
            "portfolio_id": result.portfolio_id,
            "risk": {
                "expected_return": result.risk.expected_return,
                "portfolio_beta": result.risk.portfolio_beta,
                "annualized_volatility": result.risk.annualized_volatility,
                "max_drawdown_estimate": result.risk.max_drawdown_estimate,
                "stress_mdd": result.risk.stress_mdd,
                "forward_mdd": result.risk.forward_mdd,
                "var_95": result.risk.var_95,
                "cvar_95": result.risk.cvar_95,
                "downside_deviation": result.risk.downside_deviation,
                "concentration_hhi": result.risk.concentration_hhi,
                "normal_corr_avg": result.risk.normal_corr_avg,
                "stress_corr_avg": result.risk.stress_corr_avg,
                "downside_beta": result.risk.downside_beta,
                "cash_weight": result.risk.cash_weight,
                "risk_contrib": result.risk.risk_contrib,
                "liquidity_flag": result.risk.liquidity_flag,
                "risk_level": result.risk.risk_level,
            },
            "holdings": [
                {
                    "symbol": h.symbol,
                    "display": h.display,
                    "name": h.name,
                    "weight": h.weight,
                    "close": h.close,
                    "beta": result.per_stock.get(h.symbol, {}).get("long_beta"),
                    "downside_beta": result.per_stock.get(h.symbol, {}).get("downside_beta"),
                    "beta_flag": result.per_stock.get(h.symbol, {}).get("flag", ""),
                }
                for h in result.holdings
            ],
            "stress": result.stress,
            "notes": result.notes,
        }


def _portfolio_beta_regime(result: PortfolioRiskResult) -> str:
    flags = [v.get("flag", "") for v in result.per_stock.values()]
    if any(f == "ELEVATED" for f in flags):
        return "ELEVATED"
    if any(f == "DEFENSIVE" for f in flags):
        return "DEFENSIVE"
    if any(f == "LOW_HISTORY" for f in flags):
        return "LOW_HISTORY"
    return "NORMAL"


def _expected_return_from_research(engine: Engine, holdings: list[Holding]) -> float | None:
    """Weighted expected return from the latest research snapshots (base case)."""
    factory = make_session_factory(engine)
    vals: list[tuple[float, float]] = []  # (weight, expected_return_base)
    with factory() as session:
        for h in holdings:
            row = session.execute(
                select(models.ResearchSnapshot)
                .where(models.ResearchSnapshot.security_id == h.security_id)
                .order_by(models.ResearchSnapshot.as_of_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if row is not None and row.expected_return_base is not None:
                vals.append((h.weight, float(row.expected_return_base)))
    if not vals:
        return None
    total_w = sum(w for w, _ in vals)
    if total_w <= 0:
        return None
    return round(sum(w * er for w, er in vals) / total_w, 2)