"""GENERIC fallback industry model (section 7.1: never exit with error)."""
from __future__ import annotations

from datetime import date
from typing import Any

from src.research.base import DimensionScore, FactorScore, IndustryScoringModel, RiskComponent
from src.research.datapoints import DataPoints
from src.research.models._helpers import dim, pct_factor, pw_factor, vf
from src.research.risk import build_generic_risk_components


class GenericModel(IndustryScoringModel):
    name = "GENERIC"
    version = "V1.0"

    def score(
        self, data: dict[str, Any], as_of: date
    ) -> tuple[tuple[DimensionScore, ...], tuple[RiskComponent, ...], tuple[str, ...]]:
        dp: DataPoints = data["dp"]
        missing = list(dp.missing_keys)

        # --- Fundamental: profitability & leverage ----------------------- #
        # NOTE: roe/net_margin/debt_ratio are stored as percent values (e.g.
        # 44.77 = 44.77%), yoy/cagr are fractions, dv_ratio is a fraction.
        factors = [
            pct_factor("roe", "ROE", dp.roe, dp, "roe_history", 0.40),
            pw_factor(
                "net_margin", "净利率", dp.net_margin,
                [(0.0, 0.0), (5.0, 40.0), (12.0, 70.0), (20.0, 90.0), (35.0, 100.0)],
                0.30,
            ),
            pw_factor(
                "debt_ratio", "负债率（反向）", dp.debt_ratio,
                [(0.0, 100.0), (30.0, 85.0), (50.0, 65.0), (70.0, 40.0), (90.0, 10.0), (100.0, 0.0)],
                0.30, higher_better=False,
            ),
        ]
        fundamental = dim("fundamental", "基本面", factors, missing)

        # --- Quality: cash conversion & ROE stability -------------------- #
        quality_factors = [
            pw_factor(
                "ocf_to_np", "经营现金流/净利润", dp.ocf_to_np,
                [(0.0, 0.0), (0.5, 40.0), (1.0, 85.0), (1.5, 100.0)],
                0.55,
            ),
            vf("roe_stability", "ROE 稳定性", dp.roe_stability,
               dp.roe_stability * 100 if dp.roe_stability is not None else None, 0.45),
        ]
        quality = dim("quality", "质量", quality_factors, missing)

        # --- Growth ------------------------------------------------------- #
        growth_factors = [
            pct_factor("revenue_yoy", "营收同比", dp.revenue_yoy, dp, "revenue_yoy_history", 0.35),
            pct_factor("net_profit_yoy", "净利同比", dp.net_profit_yoy, dp, "np_yoy_history", 0.35),
            vf("revenue_cagr3", "3年营收CAGR", dp.revenue_cagr3,
               _cagr_score(dp.revenue_cagr3), 0.15),
            vf("net_profit_cagr3", "3年净利CAGR", dp.net_profit_cagr3,
               _cagr_score(dp.net_profit_cagr3), 0.15),
        ]
        growth = dim("growth", "成长", growth_factors, missing)

        # --- Valuation ----------------------------------------------------- #
        val_factors = [
            pct_factor("pe_ttm", "PE-TTM 5年分位", dp.pe_ttm, dp, "pe_history", 0.55,
                       higher_better=False, direction="lower"),
            pct_factor("pb", "PB 5年分位", dp.pb, dp, "pb_history", 0.25,
                       higher_better=False, direction="lower"),
            pw_factor(
                "dv_ratio", "股息率", dp.dv_ratio,
                [(0.0, 0.0), (0.02, 45.0), (0.04, 75.0), (0.06, 90.0), (0.10, 100.0)],
                0.20,
            ),
        ]
        valuation = dim("valuation", "估值", val_factors, missing)

        # --- Cycle: relative momentum proxy -------------------------------- #
        cycle_factors = [
            pw_factor(
                "growth_accel", "增速动能(净利YoY)", dp.net_profit_yoy,
                [(-0.5, 0.0), (-0.1, 30.0), (0.0, 50.0), (0.15, 75.0), (0.5, 100.0)],
                1.0,
            ),
        ]
        cycle = dim("cycle", "周期", cycle_factors, missing)

        # --- Shareholder ----------------------------------------------------- #
        sh_factors = [
            pw_factor(
                "dv_ratio_sh", "股息率", dp.dv_ratio,
                [(0.0, 0.0), (0.015, 40.0), (0.03, 70.0), (0.05, 90.0), (0.08, 100.0)],
                0.70,
            ),
            vf("roe_stability_sh", "盈利质地", dp.roe_stability,
               dp.roe_stability * 100 if dp.roe_stability is not None else None, 0.30),
        ]
        shareholder = dim("shareholder", "股东回报", sh_factors, missing)

        risk = build_generic_risk_components(dp)
        flags: list[str] = []
        if dp.industry_model == "GENERIC":
            flags.append("GENERIC_MODEL")
        return (fundamental, quality, growth, valuation, cycle, shareholder), tuple(risk), tuple(flags)


def _cagr_score(cagr: float | None) -> float | None:
    if cagr is None:
        return None
    if cagr >= 0.20:
        return 100.0
    if cagr >= 0.10:
        return 75.0
    if cagr >= 0.05:
        return 55.0
    if cagr >= 0.0:
        return 35.0
    if cagr >= -0.10:
        return 15.0
    return 0.0