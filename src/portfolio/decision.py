"""Phase 6 decision orchestration: regime -> opportunity -> risk budget -> signal.

A full portfolio decision bundles the V1.1 Phase 6 objects:

  1. Market Regime Engine -> regime params (scenario weights / beta / cash cap)
  2. per-stock Opportunity Score (research snapshot + market data)
  3. RiskBudgetOptimizer (target return vs risk budget, TARGET NOT FEASIBLE)
  4. Rebalance Band + trading friction (only trade outside the band)

The output is persisted with the Phase 5 snapshot (PortfolioSnapshot Phase 6
columns) so every decision keeps an immutable history.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory
from src.ingestion.quality import blocked_symbols as _quality_blocked_symbols
from src.portfolio.friction import RebalancePlan, build_rebalance_plan
from src.portfolio.metrics import (
    beta_stats,
    historical_max_drawdown,
    load_benchmark_symbol,
)
from src.portfolio.opportunity import (
    RiskBudgetOptimizer,
    opportunity_score,
    opportunity_tags,
    rebalance_band_action,
)
from src.portfolio.regime import MarketRegimeEngine, RegimeParams
from src.valuation.zones import adjusted_expected_return


def symbol_blocked(symbol: str) -> bool:
    """Phase 7 DATA_BLOCK quality gate (module-level hook, set by build_decision).

    ``build_decision`` populates _blocked from the latest data_quality_logs
    verdicts; when the quality engine has never run the set stays empty so
    pure-Phase-6 callers are unaffected.
    """
    return symbol in _blocked


_blocked: set[str] = set()


def set_blocked_symbols(symbols: set[str], reset: bool = True) -> None:
    """Inject (or replace) the DATA_BLOCK set. reset=True replaces it."""
    global _blocked  # noqa: PLW0603
    _blocked = set(symbols) if reset else (_blocked | set(symbols))


@dataclass(frozen=True)
class StockSignal:
    symbol: str
    opportunity_score: float
    opportunity_tag: str
    adjusted_er: float | None
    research_score: float | None
    margin_of_safety: float | None
    momentum_60d: float | None
    beta: float | None
    annual_vol: float | None
    max_drawdown: float | None
    current_weight: float
    target_weight: float
    band_action: str          # BUY / REDUCE / HOLD
    signal: str               # STRONG_BUY / BUY / HOLD / REDUCE / NO_BUY / WAIT
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "opportunity_score": self.opportunity_score,
            "opportunity_tag": self.opportunity_tag,
            "adjusted_er": self.adjusted_er,
            "research_score": self.research_score,
            "margin_of_safety": self.margin_of_safety,
            "momentum_60d": self.momentum_60d,
            "beta": self.beta,
            "annual_vol": self.annual_vol,
            "max_drawdown": self.max_drawdown,
            "current_weight": round(self.current_weight, 6),
            "target_weight": round(self.target_weight, 6),
            "band_action": self.band_action,
            "signal": self.signal,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PortfolioDecision:
    as_of: date
    portfolio_id: int
    feasible: bool
    target_return: float | None
    max_achievable_return: float | None
    regime: RegimeParams | None
    weights: dict[str, float]
    cash_weight: float
    portfolio_beta: float | None
    portfolio_vol: float | None
    expected_return: float | None
    turnover: float
    transaction_cost: float
    signals: list[StockSignal]
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "portfolio_id": self.portfolio_id,
            "feasible": self.feasible,
            "target_return": self.target_return,
            "max_achievable_return": self.max_achievable_return,
            "regime": self.regime.to_dict() if self.regime else None,
            "weights": {k: round(v, 6) for k, v in self.weights.items()},
            "cash_weight": round(self.cash_weight, 6),
            "portfolio_beta": self.portfolio_beta,
            "portfolio_vol": self.portfolio_vol,
            "expected_return": self.expected_return,
            "turnover": self.turnover,
            "transaction_cost": self.transaction_cost,
            "signals": [s.to_dict() for s in self.signals],
            "violations": list(self.violations),
        }


# --------------------------------------------------------------------------- #
def build_decision(
    engine: Engine,
    portfolio_id: int,
    as_of: date,
    benchmark_symbol: str | None = None,
) -> PortfolioDecision:
    """One complete Phase 6/7 decision for a portfolio at ``as_of``."""
    # Phase 7: refresh the DATA_BLOCK gate from the quality engine's latest run.
    set_blocked_symbols(_quality_blocked_symbols(engine))
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
        if not pos:
            raise ValueError(f"portfolio {portfolio_id} has no positions")
        sec_rows = session.execute(
            select(models.Security)
        ).scalars().all()
    sec_map = {s.id: s for s in sec_rows}

    weights_now: dict[str, float] = {}
    closes: dict[str, float | None] = {}
    mkt_value: dict[str, float] = {}
    for p in pos:
        sec = sec_map.get(p.security_id)
        if sec is None:
            continue
        close = _latest_close(engine, p.security_id, as_of)
        closes[sec.symbol] = close
        if p.manual_current_weight is not None:
            weights_now[sec.symbol] = float(p.manual_current_weight)
        elif close is not None:
            weights_now[sec.symbol] = float(p.quantity * close)
        if close is not None:
            mkt_value[sec.symbol] = float(p.quantity * close)

    total_mv = sum(v for v in weights_now.values() if v is not None)
    if total_mv > 0:
        weights_now = {s: (w / total_mv if w is not None else 0.0) for s, w in weights_now.items()}
    # market value (money) used for friction math; manual weights fall back to 1.0
    portfolio_value = sum(mkt_value.values()) or 1.0

    symbols = list(weights_now.keys())
    if not symbols:
        raise ValueError("no resolvable holdings")

    regime_engine = MarketRegimeEngine(engine, benchmark_symbol)
    regime = regime_engine.evaluate(as_of)

    # --- per-stock market stats: momentum / vol / beta / mdd ---------------- #
    stats = _per_stock_stats(engine, symbols, as_of, benchmark_symbol, closes)

    # --- research snapshot inputs ------------------------------------------- #
    research = _research_inputs(engine, symbols)

    er_adj: dict[str, float | None] = {}
    for s in symbols:
        r = research.get(s)
        er_adj[s] = adjusted_expected_return(r.expected_return_base, r.confidence) if r else None

    opportunities: dict[str, float] = {}
    for s in symbols:
        st = stats.get(s, {})
        margin = None
        r = research.get(s)
        if r and r.standard_buy_price and closes.get(s):
            margin = max(0.0, r.standard_buy_price / closes[s] - 1.0)
        opportunities[s] = opportunity_score(
            expected_return=er_adj.get(s),
            research_score=r.research_score if r else None,
            margin_of_safety=margin,
            momentum_60d=st.get("momentum_60d"),
            beta=st.get("beta_60d"),
            annual_vol=st.get("ann_vol"),
            max_drawdown=st.get("mdd"),
            liquidity_ratio=st.get("liquidity_ratio"),
        )

    # --- risk budget -------------------------------------------------------- #
    cash_min = max(float(pf.min_cash_weight), float(regime.cash_weight_min or 0.05))
    cash_max = float(regime.cash_weight_max or 0.20)
    max_beta = min(float(pf.max_portfolio_beta), float(regime.max_beta or 1.0))

    corr = _correlation_matrix(engine, symbols, as_of)
    optimizer = RiskBudgetOptimizer(
        symbols=symbols,
        expected_returns=[er_adj[s] for s in symbols],
        betas=[stats.get(s, {}).get("long_beta", 1.0) for s in symbols],
        annual_vols=[stats.get(s, {}).get("ann_vol", 0.25) for s in symbols],
        correlations=corr,
        max_beta=max_beta,
        max_volatility=float(pf.max_volatility),
        max_single_weight=float(pf.max_single_weight),
        cash_min=cash_min,
        cash_max=cash_max,
        target_return=float(pf.target_return) if pf.target_return else None,
    )
    opt = optimizer.solve()

    # --- rebalance band + friction ------------------------------------------ #
    targets = dict(opt.weights or {})
    target_total = sum(targets.values())
    if target_total <= 0:
        # evenly distribute across stocks up to the single cap
        per = min(1.0 / len(symbols), float(pf.max_single_weight))
        targets = {s: round(per, 6) for s in symbols}

    plan = build_rebalance_plan(
        symbols=symbols,
        current_weights=[weights_now.get(s, 0.0) for s in symbols],
        target_weights=[targets.get(s, 0.0) for s in symbols],
        portfolio_value=portfolio_value,
        band_half=float(getattr(pf, "rebalance_threshold", 0.03) or 0.03),
        fee_rate=float(getattr(pf, "fee_rate", 0.0003) or 0.0003),
        stamp_tax=float(getattr(pf, "stamp_tax", 0.0005) or 0.0005),
        slippage=float(getattr(pf, "slippage", 0.001) or 0.001),
        min_trade_amount=float(getattr(pf, "min_trade_amount", 20000.0) or 20000.0),
    )
    plan_by_sym = {leg.symbol: leg for leg in plan.legs}

    # --- signals ------------------------------------------------------------- #
    signals: list[StockSignal] = []
    for s in symbols:
        r = research.get(s)
        margin = (
            max(0.0, r.standard_buy_price / closes[s] - 1.0)
            if r and r.standard_buy_price and closes.get(s)
            else None
        )
        opp = opportunities[s]
        tag = opportunity_tags(opp)
        band_act = plan_by_sym[s].action
        tgt_w = targets.get(s, 0.0)
        cur_w = weights_now.get(s, 0.0)

        if not opt.feasible and s not in (opt.weights or {}):
            signal, reason = "WAIT", "风险预算下该标的不进入组合"
        elif band_act == "HOLD":
            if opp >= 55:
                signal, reason = "HOLD", "目标区间内持有"
            elif opp < 40:
                signal, reason = "REDUCE", "机会分过低，建议降低仓位"
            else:
                signal, reason = "HOLD", "中性区间持有观察"
        elif band_act == "BUY":
            if opp >= 75:
                signal, reason = "STRONG_BUY", "进入买入区且机会分高"
            elif opp >= 55:
                signal, reason = "BUY", "进入买入区"
            elif opp >= 40:
                signal, reason = "WAIT", "机会分中性，暂缓加仓"
            else:
                signal, reason = "NO_BUY", "估值/风险不具吸引力，Cash 是合法答案"
        else:  # REDUCE
            if opp < 40:
                signal, reason = "NO_BUY", "机会分过低"
            else:
                signal, reason = "REDUCE", "超出目标区间上限"

        # Phase 7: DATA_BLOCK quality gate - a data-quality hard failure
        # forbids STRONG_BUY / BUY (no Strong Buy on bad data, spec 3.4).
        if symbol_blocked(s) and signal in ("STRONG_BUY", "BUY"):
            signal, reason = (
                ("HOLD", f"{reason} [DATA_BLOCK 门禁：数据异常，禁止强买]")
                if signal == "STRONG_BUY"
                else ("WAIT", f"{reason} [DATA_BLOCK 门禁：数据异常，禁止加仓]")
            )

        signals.append(StockSignal(
            symbol=s, opportunity_score=opp, opportunity_tag=tag,
            adjusted_er=er_adj.get(s),
            research_score=r.research_score if r else None,
            margin_of_safety=margin,
            momentum_60d=stats.get(s, {}).get("momentum_60d"),
            beta=stats.get(s, {}).get("beta_60d"),
            annual_vol=stats.get(s, {}).get("ann_vol"),
            max_drawdown=stats.get(s, {}).get("mdd"),
            current_weight=cur_w, target_weight=tgt_w,
            band_action=band_act, signal=signal, reason=reason,
        ))

    return PortfolioDecision(
        as_of=as_of,
        portfolio_id=portfolio_id,
        feasible=opt.feasible,
        target_return=opt.target_return,
        max_achievable_return=opt.max_achievable_return,
        regime=regime,
        weights=targets,
        cash_weight=float(opt.cash_weight),
        portfolio_beta=opt.portfolio_beta,
        portfolio_vol=opt.portfolio_vol,
        expected_return=opt.expected_return,
        turnover=plan.turnover,
        transaction_cost=plan.cost_pct,
        signals=signals,
        violations=list(opt.violations),
    )


# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _ResearchInput:
    expected_return_base: float | None
    confidence: float | None
    research_score: float | None
    standard_buy_price: float | None


def _research_inputs(engine: Engine, symbols: list[str]) -> dict[str, _ResearchInput]:
    factory = make_session_factory(engine)
    out: dict[str, _ResearchInput] = {}
    with factory() as session:
        sec_rows = session.execute(
            select(models.Security, models.ResearchSnapshot)
            .join(models.ResearchSnapshot, models.ResearchSnapshot.security_id == models.Security.id)
            .where(models.Security.symbol.in_(symbols))
            .order_by(models.ResearchSnapshot.as_of_date.desc())
        ).all()
    seen: set[str] = set()
    for sec, snap in sec_rows:
        if sec.symbol in seen:
            continue
        seen.add(sec.symbol)
        out[sec.symbol] = _ResearchInput(
            expected_return_base=snap.expected_return_base,
            confidence=snap.confidence_score,
            research_score=snap.research_score,
            standard_buy_price=snap.standard_buy_price,
        )
    for s in symbols:
        out.setdefault(s, _ResearchInput(None, None, None, None))
    return out


def _per_stock_stats(
    engine: Engine,
    symbols: list[str],
    as_of: date,
    benchmark_symbol: str | None,
    closes: dict[str, float | None],
) -> dict[str, dict[str, Any]]:
    """Per-stock 60d momentum / annual vol / 60d beta / MDD / liquidity."""
    from datetime import timedelta

    start = as_of - timedelta(days=700)
    factory = make_session_factory(engine)
    sec_rows: dict[str, models.Security] = {}
    rows: list[tuple[int, date, float | None, float | None, float | None]] = []
    with factory() as session:
        secs = session.execute(
            select(models.Security).where(models.Security.symbol.in_(symbols))
        ).scalars().all()
        for s in secs:
            sec_rows[s.symbol] = s
            mrows = session.execute(
                select(
                    models.DailyMarket.security_id, models.DailyMarket.trade_date,
                    models.DailyMarket.close, models.DailyMarket.pct_change,
                )
                .where(models.DailyMarket.security_id == s.id)
                .where(models.DailyMarket.trade_date > start)
                .where(models.DailyMarket.trade_date <= as_of)
                .order_by(models.DailyMarket.trade_date)
            ).all()
            for r in mrows:
                rows.append((s.id, r[1], r[2], r[3], None))

    bench = None
    bench_symbol = benchmark_symbol or load_benchmark_symbol()
    if bench_symbol:
        with factory() as session:
            b = session.execute(
                select(models.Security).where(models.Security.symbol == bench_symbol)
            ).scalar_one_or_none()
            if b is not None:
                bench_rows = session.execute(
                    select(models.DailyMarket.trade_date, models.DailyMarket.pct_change)
                    .where(models.DailyMarket.security_id == b.id)
                    .where(models.DailyMarket.trade_date > start)
                    .where(models.DailyMarket.trade_date <= as_of)
                    .order_by(models.DailyMarket.trade_date)
                ).all()
                # DB pct_change is in percent (x100); convert to decimal
                bench = pd.Series(
                    [float(r[1]) / 100.0 if r[1] is not None else np.nan for r in bench_rows],
                    index=[r[0] for r in bench_rows],
                )

    out: dict[str, dict[str, Any]] = {}
    for sym, sec in sec_rows.items():
        sym_rows = sorted(
            [r for r in rows if r[0] == sec.id], key=lambda r: r[1]
        )
        closes_series = [r[2] for r in sym_rows]
        # DB pct_change is stored in percent (x100); convert to decimal
        rets_series = [r[3] / 100.0 if r[3] is not None else None for r in sym_rows]
        dates = [r[1] for r in sym_rows]

        close_arr = np.array([c for c in closes_series if c is not None], dtype=float)
        ret_arr = np.array([x for x in rets_series if x is not None], dtype=float)

        momentum = None
        if len(close_arr) >= 61:
            momentum = float(close_arr[-1] / close_arr[-61] - 1.0)
        ann_vol = None
        if len(ret_arr) >= 30:
            ann_vol = float(np.std(ret_arr[-60:], ddof=1) * np.sqrt(252))

        mdd = None
        if len(close_arr) >= 2:
            mdd = float(historical_max_drawdown(close_arr))

        beta60 = None
        r2 = None
        if bench is not None and len(ret_arr) >= 30:
            idx = pd.to_datetime([pd.Timestamp(d) for d in dates])
            s_series = pd.Series(rets_series, index=idx)
            b_series = bench.copy()
            b_series.index = pd.to_datetime(b_series.index)
            df = pd.concat([s_series, b_series], axis=1)
            df.columns = ["s", "b"]
            df = df.dropna()
            if len(df) >= 30:
                bs = beta_stats(
                    np.asarray(df["s"].to_numpy(dtype=float)),
                    np.asarray(df["b"].to_numpy(dtype=float)),
                )
                beta60 = bs.beta_60d if bs.beta_60d is not None else bs.long_beta
                r2 = bs.r2

        long_beta = beta60
        obs = int(len(ret_arr))
        liq = min(1.0, obs / 252.0) if obs else None

        out[sym] = {
            "momentum_60d": round(momentum, 6) if momentum is not None else None,
            "ann_vol": round(ann_vol, 6) if ann_vol is not None else None,
            "beta_60d": round(beta60, 4) if beta60 is not None else None,
            "long_beta": round(long_beta, 4) if long_beta is not None else None,
            "mdd": round(mdd, 6) if mdd is not None else None,
            "obs": obs,
            "r2": r2,
            "liquidity_ratio": liq,
        }
        if closes.get(sym) is None and close_arr.size:
            closes[sym] = float(close_arr[-1])
    return out


def _correlation_matrix(engine: Engine, symbols: list[str], as_of: date) -> np.ndarray | None:
    """Aligned daily-return correlation (n x n); None when insufficient data."""
    from datetime import timedelta

    start = as_of - timedelta(days=400)
    factory = make_session_factory(engine)
    with factory() as session:
        secs = session.execute(
            select(models.Security).where(models.Security.symbol.in_(symbols))
        ).scalars().all()
        sec_map = {s.id: s.symbol for s in secs}
        ids = list(sec_map.keys())
        if len(ids) < 2:
            return None
        rows = session.execute(
            select(
                models.DailyMarket.security_id, models.DailyMarket.trade_date,
                models.DailyMarket.pct_change,
            )
            .where(models.DailyMarket.security_id.in_(ids))
            .where(models.DailyMarket.trade_date > start)
            .where(models.DailyMarket.trade_date <= as_of)
            .order_by(models.DailyMarket.trade_date)
        ).all()
    df = pd.DataFrame(
        [(r[1], sec_map[r[0]], r[2]) for r in rows],
        columns=["date", "symbol", "pct"],
    )
    pivot = df.pivot(index="date", columns="symbol", values="pct")
    pivot = pivot.dropna(how="all")
    if pivot.shape[0] < 30 or pivot.shape[1] < 2:
        return None
    corr = pivot.corr().to_numpy(dtype=float)
    return np.nan_to_num(corr, nan=0.0)


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


def persist_decision(engine: Engine, decision: PortfolioDecision) -> models.PortfolioSnapshot:
    """Attach the Phase 6 decision fields to the latest snapshot row.

    The phase-5 snapshot already holds the risk picture; we update its Phase 6
    columns in place (same row, same as_of) and return it.
    """
    factory = make_session_factory(engine)
    with factory() as session:
        latest = session.execute(
            select(models.PortfolioSnapshot)
            .where(models.PortfolioSnapshot.portfolio_id == decision.portfolio_id)
            .order_by(models.PortfolioSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            latest = models.PortfolioSnapshot(
                portfolio_id=decision.portfolio_id,
                as_of_date=decision.as_of,
            )
            session.add(latest)
        latest.market_regime = decision.regime.regime if decision.regime else ""
        latest.feasible = decision.feasible
        latest.max_achievable_return = decision.max_achievable_return
        latest.turnover = decision.turnover
        latest.transaction_cost = decision.transaction_cost
        latest.regime_params_json = json.dumps(
            decision.regime.to_dict() if decision.regime else {}, ensure_ascii=False
        )
        latest.opportunity_json = json.dumps(
            decision.to_dict().get("signals", []), ensure_ascii=False
        )
        latest.signals_json = json.dumps(
            [s.to_dict() for s in decision.signals], ensure_ascii=False
        )
        latest.target_weights_json = json.dumps(
            {k: round(v, 6) for k, v in decision.weights.items()}, ensure_ascii=False
        )
        session.commit()
        session.refresh(latest)
        return latest