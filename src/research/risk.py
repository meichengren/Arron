"""System A risk scoring (Section 16).

Risk Score 0-100, higher = more risk. Seven components:
Financial / Earnings Stability / Valuation / Industry-Cycle /
Price Volatility / Drawdown / Data Quality.

Industry models contribute the industry/cycle component; the rest is computed
from prepared data points here.
"""
from __future__ import annotations

import math

from src.research.base import RiskComponent
from src.research.datapoints import DataPoints
from src.research.normalization import piecewise_score

# weights sum to 1.0
RISK_WEIGHTS: dict[str, float] = {
    "financial": 0.15,
    "stability": 0.15,
    "valuation": 0.15,
    "cycle": 0.20,
    "volatility": 0.10,
    "drawdown": 0.10,
    "data_quality": 0.15,
}


def _piecewise(value, segs, higher_better=True) -> float | None:
    return piecewise_score(value, segs, higher_better=higher_better)


def build_generic_risk_components(dp: DataPoints, industry_cycle: RiskComponent | None = None) -> list[RiskComponent]:
    """Compute the six generic risk components and attach the industry one."""
    comps: list[RiskComponent] = []

    # 1. Financial risk: leverage + margin health.
    if dp.debt_ratio is not None:
        # Banks/insurers sit naturally above 80%; generic curve rewards low debt.
        score = _piecewise(
            dp.debt_ratio,
            [(0.0, 0.0), (0.30, 10.0), (0.50, 30.0), (0.65, 55.0), (0.80, 75.0), (1.0, 100.0)],
        )
        comps.append(
            RiskComponent(
                "financial", "财务杠杆风险", score if score is not None else 50.0,
                0.15, evidence=f"debt_ratio={dp.debt_ratio:.1%}",
            )
        )
    else:
        comps.append(
            RiskComponent("financial", "财务杠杆风险", 50.0, 0.15,
                          evidence="debt_ratio unavailable", missing=True)
        )

    # 2. Earnings stability: volatility of net-profit growth + ROE stability.
    if dp.np_yoy_vol is not None:
        score = _piecewise(
            dp.np_yoy_vol,
            [(0.0, 0.0), (0.10, 15.0), (0.25, 40.0), (0.50, 70.0), (0.80, 90.0), (1.5, 100.0)],
        )
        comps.append(
            RiskComponent(
                "stability", "盈利稳定性风险", score if score is not None else 50.0,
                0.15,
                evidence=f"net-profit YoY vol={dp.np_yoy_vol:.1%}",
            )
        )
    else:
        comps.append(
            RiskComponent("stability", "盈利稳定性风险", 50.0, 0.15,
                          evidence="insufficient earnings history", missing=True)
        )

    # 3. Valuation risk: cheap is safe, rich is risky (percentile basis).
    if dp.pe_pctl is not None or dp.pb_pctl is not None:
        p = max([x for x in (dp.pe_pctl, dp.pb_pctl) if x is not None] or [50.0])
        score = _piecewise(p, [(0.0, 0.0), (50.0, 30.0), (70.0, 50.0), (90.0, 80.0), (100.0, 100.0)])
        comps.append(
            RiskComponent(
                "valuation", "估值风险", score if score is not None else 50.0,
                0.15,
                evidence=f"pe_pctl={dp.pe_pctl}, pb_pctl={dp.pb_pctl}",
            )
        )
    else:
        comps.append(
            RiskComponent("valuation", "估值风险", 50.0, 0.15,
                          evidence="no valuation history", missing=True)
        )

    # 4. Industry / cycle risk (from industry model or conservative default).
    if industry_cycle is not None:
        comps.append(industry_cycle)
    else:
        comps.append(
            RiskComponent("cycle", "行业周期风险", 40.0, 0.20,
                          evidence="generic model, no industry signal", missing=True)
        )

    # 5. Price volatility risk.
    if dp.ann_vol is not None:
        vol = dp.ann_vol / 100.0
        score = _piecewise(vol, [(0.0, 0.0), (0.15, 20.0), (0.30, 50.0), (0.50, 80.0), (0.80, 100.0)])
        comps.append(
            RiskComponent(
                "volatility", "价格波动风险", score if score is not None else 50.0,
                0.10, evidence=f"ann_vol={dp.ann_vol:.1f}%",
            )
        )
    else:
        comps.append(
            RiskComponent("volatility", "价格波动风险", 50.0, 0.10,
                          evidence="no price history", missing=True)
        )

    # 6. Drawdown risk.
    if dp.max_drawdown is not None:
        dd = -dp.max_drawdown / 100.0
        score = _piecewise(dd, [(0.0, 0.0), (0.15, 20.0), (0.30, 50.0), (0.50, 80.0), (0.70, 100.0)])
        comps.append(
            RiskComponent(
                "drawdown", "回撤风险", score if score is not None else 50.0,
                0.10, evidence=f"max_dd={dp.max_drawdown:.1f}%",
            )
        )
    else:
        comps.append(
            RiskComponent("drawdown", "回撤风险", 50.0, 0.10,
                          evidence="no price history", missing=True)
        )

    # 7. Data quality risk: anything missing/stale raises risk.
    dq_score = _data_quality_risk(dp)
    comps.append(
        RiskComponent(
            "data_quality", "数据质量风险", dq_score, 0.15,
            evidence=f"missing={len(dp.missing_keys)}; "
                     f"report_age={dp.report_age_days}d" if dp.report_age_days is not None else "missing",
        )
    )
    return comps


def _data_quality_risk(dp: DataPoints) -> float:
    """Score 0-100; missing/stale data pushes risk up."""
    score = 0.0
    missing = set(dp.missing_keys)
    # critical coverage
    if "financial_report" in missing or "market" in missing:
        score += 50.0
    if "valuation_history" in missing or "shares" in missing:
        score += 15.0
    if {"roe", "net_margin"}.intersection(missing):
        score += 10.0
    if "dividend" in missing:
        score += 5.0
    if "stale_report" in missing:
        score += 15.0
    return min(100.0, score) if score else 10.0


def aggregate_risk(comps: list[RiskComponent]) -> float:
    """Weighted average over available components (missing ones excluded)."""
    total_w = 0.0
    acc = 0.0
    for c in comps:
        if c.score is None:
            continue
        total_w += c.weight
        acc += c.score * c.weight
    if total_w <= 0:
        return 50.0
    return round(min(100.0, max(0.0, acc / total_w)), 2)