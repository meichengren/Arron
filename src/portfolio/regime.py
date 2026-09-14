"""Phase 6 Market Regime Engine (patch items 3/5/14).

Classifies the current market into Risk-On / Neutral / Risk-Off / Stress from
index trend + volatility, and emits the regime-dependent parameters:

  - scenario probabilities (Bear/Base/Bull)
  - portfolio beta cap
  - target stock weight cap / cash weight range
  - a short human note

The engine never touches the fundamental score - it only changes the risk
budget and the scenario weights used downstream.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory
from src.portfolio.metrics import load_benchmark_symbol

REGIMES = ("RISK_ON", "NEUTRAL", "RISK_OFF", "STRESS")

# Regime table from the V1.1 spec (section 3.1).
_REGIME_PARAMS: dict[str, dict[str, Any]] = {
    "RISK_ON": {
        "scenario_weights": {"bear": 0.15, "base": 0.50, "bull": 0.35},
        "max_beta": 1.15,
        "stock_weight_cap": 0.95,
        "cash_weight_min": 0.05,
        "cash_weight_max": 0.20,
        "style": "GROWTH_UP",
        "note": "Risk-On: 趋势向上、波动偏低, 股票仓位可到 95%, 成长权重提高",
    },
    "NEUTRAL": {
        "scenario_weights": {"bear": 0.25, "base": 0.50, "bull": 0.25},
        "max_beta": 1.00,
        "stock_weight_cap": 0.85,
        "cash_weight_min": 0.05,
        "cash_weight_max": 0.20,
        "style": "BALANCED",
        "note": "Neutral: 趋势与波动均处中性区间, 均衡配置",
    },
    "RISK_OFF": {
        "scenario_weights": {"bear": 0.35, "base": 0.45, "bull": 0.20},
        "max_beta": 0.85,
        "stock_weight_cap": 0.75,
        "cash_weight_min": 0.05,
        "cash_weight_max": 0.35,
        "style": "QUALITY_DIVIDEND_UP",
        "note": "Risk-Off: 趋势转弱或波动抬升, 股票仓位 65%-75%, 提高质量/股息因子",
    },
    "STRESS": {
        "scenario_weights": {"bear": 0.45, "base": 0.40, "bull": 0.15},
        "max_beta": 0.70,
        "stock_weight_cap": 0.65,
        "cash_weight_min": 0.10,
        "cash_weight_max": 0.50,
        "style": "CASH_DEFENSIVE",
        "note": "Stress: 趋势显著破位且波动高企, 股票仓位 50%-65%, 优先现金与防守资产",
    },
}


@dataclass(frozen=True)
class RegimeParams:
    as_of: date
    regime: str
    trend_up: bool | None
    ma20: float | None
    ma60: float | None
    ret_60d: float | None
    ann_vol: float | None
    scenario_weights: dict[str, float] = field(default_factory=dict)
    max_beta: float | None = None
    stock_weight_cap: float | None = None
    cash_weight_min: float | None = None
    cash_weight_max: float | None = None
    style: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "regime": self.regime,
            "trend_up": self.trend_up,
            "ma20": self.ma20,
            "ma60": self.ma60,
            "ret_60d": self.ret_60d,
            "ann_vol": self.ann_vol,
            "scenario_weights": self.scenario_weights,
            "max_beta": self.max_beta,
            "stock_weight_cap": self.stock_weight_cap,
            "cash_weight_min": self.cash_weight_min,
            "cash_weight_max": self.cash_weight_max,
            "style": self.style,
            "note": self.note,
        }


def classify_regime(
    trend_up: bool | None = None,
    ann_vol: float | None = None,
    ret_60d: float | None = None,
    vol_quantile: float | None = None,
) -> str:
    """Rule-based regime classifier.

    Primary signal is trend (MA20 vs MA60); secondary signals are 60d index
    return and the volatility regime. Returns one of REGIMES.
    """
    if trend_up is None or ann_vol is None or ret_60d is None:
        return "NEUTRAL"

    if not trend_up and ret_60d < -0.10:
        return "STRESS"
    if not trend_up and (ret_60d < -0.03 or ann_vol > 0.35):
        return "RISK_OFF"
    if trend_up and ret_60d > 0.05 and ann_vol < 0.25:
        return "RISK_ON"
    return "NEUTRAL"


class MarketRegimeEngine:
    """Compute + persist the daily market regime from benchmark index prices."""

    def __init__(self, engine: Engine, benchmark_symbol: str | None = None) -> None:
        self._engine = engine
        self._benchmark_symbol = benchmark_symbol or load_benchmark_symbol()

    # ------------------------------------------------------------------ #
    def evaluate(self, as_of: date) -> RegimeParams:
        """Classify today's regime from the benchmark's last 60+ sessions."""
        closes = self._benchmark_closes(as_of)
        if closes is None or len(closes) < 60:
            p = RegimeParams(
                as_of=as_of, regime="NEUTRAL", trend_up=None,
                ma20=None, ma60=None, ret_60d=None, ann_vol=None,
                scenario_weights=dict(_REGIME_PARAMS["NEUTRAL"]["scenario_weights"]),
                max_beta=_REGIME_PARAMS["NEUTRAL"]["max_beta"],
                stock_weight_cap=_REGIME_PARAMS["NEUTRAL"]["stock_weight_cap"],
                cash_weight_min=_REGIME_PARAMS["NEUTRAL"]["cash_weight_min"],
                cash_weight_max=_REGIME_PARAMS["NEUTRAL"]["cash_weight_max"],
                style=_REGIME_PARAMS["NEUTRAL"]["style"],
                note="Regime: 基准行情不足 60 日, 默认 Neutral",
            )
            self._persist(as_of, p)
            return p

        c = np.asarray(closes, dtype=float)
        ma20 = float(np.mean(c[-20:]))
        ma60 = float(np.mean(c[-60:]))
        trend_up = ma20 > ma60

        daily = c[1:] / c[:-1] - 1.0
        ret_60d = c[-1] / c[-61] - 1.0
        ann_vol = float(np.std(daily[-60:], ddof=1) * np.sqrt(252))

        regime = classify_regime(trend_up=trend_up, ann_vol=ann_vol, ret_60d=ret_60d)
        params = _REGIME_PARAMS[regime]
        p = RegimeParams(
            as_of=as_of, regime=regime, trend_up=trend_up, ma20=ma20, ma60=ma60,
            ret_60d=round(ret_60d, 6), ann_vol=round(ann_vol, 6),
            scenario_weights=dict(params["scenario_weights"]),
            max_beta=params["max_beta"],
            stock_weight_cap=params["stock_weight_cap"],
            cash_weight_min=params["cash_weight_min"],
            cash_weight_max=params["cash_weight_max"],
            style=params["style"],
            note=params["note"],
        )
        self._persist(as_of, p)
        return p

    # ------------------------------------------------------------------ #
    def latest(self, as_of: date | None = None) -> RegimeParams | None:
        """Last persisted regime (used when the dashboard only has snapshots)."""
        factory = make_session_factory(self._engine)
        with factory() as session:
            row = session.execute(
                select(models.MarketRegime)
                .order_by(models.MarketRegime.as_of_date.desc())
                .limit(1)
            ).scalar_one_or_none()
        if row is None:
            return None
        sig = json.loads(row.signals_json or "{}")
        return RegimeParams(
            as_of=row.as_of_date, regime=row.regime, trend_up=sig.get("trend_up"),
            ma20=sig.get("ma20"), ma60=sig.get("ma60"), ret_60d=sig.get("ret_60d"),
            ann_vol=sig.get("ann_vol"),
            scenario_weights=dict(sig.get("scenario_weights", {})),
            max_beta=sig.get("max_beta"), stock_weight_cap=sig.get("stock_weight_cap"),
            cash_weight_min=sig.get("cash_weight_min"),
            cash_weight_max=sig.get("cash_weight_max"),
            style=dict(_REGIME_PARAMS.get(row.regime, {})).get("style", sig.get("style", "")),
            note=sig.get("note", row.regime),
        )

    # ------------------------------------------------------------------ #
    def _benchmark_closes(self, as_of: date) -> list[float] | None:
        """Daily closes of the benchmark symbol, oldest -> newest, <= as_of."""
        sym = self._benchmark_symbol
        factory = make_session_factory(self._engine)
        with factory() as session:
            sec = session.execute(
                select(models.Security).where(models.Security.symbol == sym)
            ).scalar_one_or_none()
            if sec is None:
                return None
            rows = session.execute(
                select(models.DailyMarket.close)
                .where(
                    models.DailyMarket.security_id == sec.id,
                    models.DailyMarket.trade_date <= as_of,
                )
                .order_by(models.DailyMarket.trade_date)
            ).scalars().all()
        return [float(r) for r in rows if r is not None]

    def _persist(self, as_of: date, p: RegimeParams) -> None:
        factory = make_session_factory(self._engine)
        with factory() as session:
            existing = session.execute(
                select(models.MarketRegime).where(models.MarketRegime.as_of_date == as_of)
            ).scalar_one_or_none()
            sig = {
                "trend_up": p.trend_up, "ma20": p.ma20, "ma60": p.ma60,
                "ret_60d": p.ret_60d, "ann_vol": p.ann_vol,
                "scenario_weights": p.scenario_weights, "max_beta": p.max_beta,
                "stock_weight_cap": p.stock_weight_cap,
                "cash_weight_min": p.cash_weight_min,
                "cash_weight_max": p.cash_weight_max,
                "note": p.note,
            }
            if existing is not None:
                existing.regime = p.regime
                existing.signals_json = json.dumps(sig, ensure_ascii=False)
            else:
                session.add(models.MarketRegime(
                    as_of_date=as_of, regime=p.regime,
                    signals_json=json.dumps(sig, ensure_ascii=False),
                ))
            session.commit()


def _load_benchmark_closes_sql(engine: Engine, symbol: str, as_of: date) -> list[float] | None:
    """SQL helper reused by tests / analytics (no dataclass coupling)."""
    factory = make_session_factory(engine)
    with factory() as session:
        sec = session.execute(
            select(models.Security).where(models.Security.symbol == symbol)
        ).scalar_one_or_none()
        if sec is None:
            return None
        rows = session.execute(
            select(models.DailyMarket.close)
            .where(
                models.DailyMarket.security_id == sec.id,
                models.DailyMarket.trade_date <= as_of,
            )
            .order_by(models.DailyMarket.trade_date)
        ).scalars().all()
    return [float(r) for r in rows if r is not None]