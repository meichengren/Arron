"""MANUFACTURING industry model (section 14: EPS x PE primary valuation).

Industrial cyclicals: valuation rank matters most (buy low PE percentile of
the cycle), balance-sheet discipline and cash conversion keep a heavy weight;
growth is pro-cyclical and treated with care (CAGR only supportive).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from src.research.base import DimensionScore, FactorScore, IndustryScoringModel, RiskComponent
from src.research.datapoints import DataPoints
from src.research.models._helpers import dim, pct_factor, pw_factor, vf
from src.research.risk import RISK_WEIGHTS, build_generic_risk_components


class ManufacturingModel(IndustryScoringModel):
    name = "MANUFACTURING"
    version = "V1.0"

    def score(
        self, data: dict[str, Any], as_of: date
    ) -> tuple[tuple[DimensionScore, ...], tuple[RiskComponent, ...], tuple[str, ...]]:
        dp: DataPoints = data["dp"]
        missing = list(dp.missing_keys)

        # ---- Fundamental: operating efficiency & leverage ------------------- #
        fund_factors: list[FactorScore] = [
            pct_factor("roe", "ROE", dp.roe, dp, "roe_history", 0.30),
            pw_factor(
                "net_margin", "净利率", dp.net_margin,
                [(0.0, 0.0), (4.0, 35.0), (8.0, 60.0), (15.0, 85.0), (25.0, 100.0)],
                0.25,
            ),
            pw_factor(
                "gross_margin", "毛利率", dp.gross_margin,
                [(0.0, 0.0), (15.0, 40.0), (25.0, 65.0), (40.0, 85.0), (55.0, 100.0)],
                0.20,
            ),
            pw_factor(
                "debt_ratio", "负债率（反向）", dp.debt_ratio,
                [(0.0, 100.0), (30.0, 85.0), (50.0, 65.0), (70.0, 40.0), (90.0, 10.0), (100.0, 0.0)],
                0.25, higher_better=False,
            ),
        ]
        fundamental = dim("fundamental", "基本面", fund_factors, missing)

        # ---- Quality ----------------------------------------------------------- #
        quality_factors = [
            pw_factor(
                "ocf_to_np", "经营现金流/净利润", dp.ocf_to_np,
                [(0.0, 0.0), (0.5, 40.0), (1.0, 80.0), (1.5, 100.0)],
                0.45,
            ),
            vf("roe_stability", "ROE 稳定性", dp.roe_stability,
               dp.roe_stability * 100 if dp.roe_stability is not None else None, 0.30),
            vf("np_yoy_vol", "成长稳定性(反向)", dp.np_yoy_vol,
               None if dp.np_yoy_vol is None else _stab_inv(dp.np_yoy_vol), 0.25),
        ]
        quality = dim("quality", "质量", quality_factors, missing)

        # ---- Growth: supportive, not dominant for cyclicals --------------------- #
        growth_factors = [
            pct_factor("revenue_yoy", "营收同比", dp.revenue_yoy, dp, "revenue_yoy_history", 0.35),
            pct_factor("net_profit_yoy", "净利同比", dp.net_profit_yoy, dp, "np_yoy_history", 0.35),
            vf("revenue_cagr3", "3年营收CAGR", dp.revenue_cagr3, _cagr_score(dp.revenue_cagr3), 0.15),
            vf("net_profit_cagr3", "3年净利CAGR", dp.net_profit_cagr3, _cagr_score(dp.net_profit_cagr3), 0.15),
        ]
        growth = dim("growth", "成长", growth_factors, missing)

        # ---- Valuation: dominant (cycle timing) ------------------------------------ #
        val_factors = [
            pct_factor("pe_ttm", "PE-TTM 5年分位", dp.pe_ttm, dp, "pe_history", 0.50,
                       higher_better=False, direction="lower",
                       note="周期股在盈利高位 PE 分位低时反而警惕"),
            pct_factor("pb", "PB 5年分位", dp.pb, dp, "pb_history", 0.30,
                       higher_better=False, direction="lower"),
            pw_factor(
                "dv_ratio", "股息率", dp.dv_ratio,
                [(0.0, 0.0), (0.02, 45.0), (0.04, 75.0), (0.06, 90.0), (0.09, 100.0)],
                0.20,
            ),
        ]
        valuation = dim("valuation", "估值", val_factors, missing)

        # ---- Cycle: earnings momentum + ROE position -------------------------------- #
        cycle_factors = [
            pw_factor(
                "growth_accel", "净利YoY动能", dp.net_profit_yoy,
                [(-0.6, 0.0), (-0.15, 30.0), (0.0, 50.0), (0.20, 75.0), (0.8, 100.0)],
                0.60,
            ),
            vf("roe_momentum", "ROE 动量", dp.roe, _mom(dp.roe_history), 0.40),
        ]
        cycle = dim("cycle", "周期", cycle_factors, missing)

        # ---- Shareholder -------------------------------------------------------------- #
        sh_factors = [
            pw_factor(
                "dv_ratio_sh", "股息率", dp.dv_ratio,
                [(0.0, 0.0), (0.015, 40.0), (0.03, 70.0), (0.05, 90.0), (0.08, 100.0)],
                0.65,
            ),
            vf("roe_level", "ROE 水平", dp.roe, _roe_score(dp.roe), 0.35, method="piecewise"),
        ]
        shareholder = dim("shareholder", "股东回报", sh_factors, missing)

        generic = build_generic_risk_components(
            dp,
            industry_cycle=RiskComponent(
                "cycle", "行业周期风险",
                _mfg_cycle_risk(dp), RISK_WEIGHTS["cycle"],
                evidence=_cycle_evidence(dp),
            ),
        )

        flags: list[str] = []
        if dp.net_profit_yoy is not None and dp.net_profit_yoy < 0:
            flags.append("PROFIT_DOWN")
        if dp.revenue_yoy is not None and dp.revenue_yoy < 0:
            flags.append("REVENUE_DOWN")
        return (fundamental, quality, growth, valuation, cycle, shareholder), tuple(generic), tuple(flags)


def _roe_score(roe: float | None) -> float | None:
    if roe is None:
        return None
    if roe >= 20.0:
        return 100.0
    if roe >= 15.0:
        return 85.0
    if roe >= 10.0:
        return 65.0
    if roe >= 5.0:
        return 45.0
    if roe >= 2.0:
        return 25.0
    return 5.0


def _stab_inv(vol: float) -> float:
    if vol <= 0.20:
        return 90.0
    if vol <= 0.40:
        return 65.0
    if vol <= 0.70:
        return 40.0
    return 15.0


def _cagr_score(cagr: float | None) -> float | None:
    if cagr is None:
        return None
    if cagr >= 0.20:
        return 100.0
    if cagr >= 0.12:
        return 80.0
    if cagr >= 0.05:
        return 60.0
    if cagr >= 0.0:
        return 35.0
    if cagr >= -0.10:
        return 15.0
    return 0.0


def _mom(hist: list[float]) -> float | None:
    if len(hist) < 2:
        return None
    latest = hist[-1]
    prev = sum(hist[:-1]) / (len(hist) - 1)
    if prev == 0:
        return None
    return max(0.0, min(100.0, 50.0 + (latest / prev - 1.0) * 500.0))


def _mfg_cycle_risk(dp: DataPoints) -> float:
    score = 30.0
    if dp.net_profit_yoy is not None and dp.net_profit_yoy < 0:
        score += 25.0
    if dp.revenue_yoy is not None and dp.revenue_yoy < 0:
        score += 15.0
    if dp.debt_ratio is not None and dp.debt_ratio > 70.0:
        score += 15.0  # high leverage amplifies cyclicals
    return min(100.0, score)


def _cycle_evidence(dp: DataPoints) -> str:
    parts = []
    if dp.net_profit_yoy is not None:
        parts.append(f"net_profit_yoy={dp.net_profit_yoy:.1%}")
    if dp.revenue_yoy is not None:
        parts.append(f"revenue_yoy={dp.revenue_yoy:.1%}")
    if dp.debt_ratio is not None:
        parts.append(f"debt_ratio={dp.debt_ratio:.0f}%")
    return "; ".join(parts) or "no cycle data"