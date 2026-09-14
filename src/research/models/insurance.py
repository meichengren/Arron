"""INSURANCE industry model.

Spec section 14 names EV x P/EV as the primary valuation model, but the free
snapshot API does not expose embedded value (EV). We therefore fall back to
PE/PB percentile ranks plus dividend yield for valuation (noted in the model
note and flag EV_DATA_MISSING) and keep the sector traits that matter for
insurers: returns on equity (long-duration business), cash conversion,
earnings stability and dividends.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from src.research.base import DimensionScore, FactorScore, IndustryScoringModel, RiskComponent
from src.research.datapoints import DataPoints
from src.research.models._helpers import dim, pct_factor, pw_factor, vf
from src.research.risk import RISK_WEIGHTS, build_generic_risk_components


class InsuranceModel(IndustryScoringModel):
    name = "INSURANCE"
    version = "V1.0"

    def score(
        self, data: dict[str, Any], as_of: date
    ) -> tuple[tuple[DimensionScore, ...], tuple[RiskComponent, ...], tuple[str, ...]]:
        dp: DataPoints = data["dp"]
        missing = list(dp.missing_keys)

        # ---- Fundamental: profitability & solvency proxy ----------------- #
        fund_factors: list[FactorScore] = [
            pct_factor("roe", "ROE", dp.roe, dp, "roe_history", 0.40),
            _roe_level(dp.roe, 0.20),
            pw_factor(
                "equity_buffer", "缓冲垫(权益/资产)", _equity_ratio(dp),
                [(0.0, 0.0), (0.05, 35.0), (0.10, 70.0), (0.15, 90.0), (0.25, 100.0)],
                0.20,
            ),
            vf("ocf_to_np", "经营现金流/净利", dp.ocf_to_np,
               None if dp.ocf_to_np is None else max(0.0, min(100.0, dp.ocf_to_np * 100)), 0.20,
               method="designated"),
        ]
        fundamental = dim("fundamental", "基本面", fund_factors, missing)

        # ---- Quality: earnings stability & cash quality ------------------ #
        quality_factors = [
            vf("np_yoy_vol", "盈利成长波动(反向)", dp.np_yoy_vol,
               None if dp.np_yoy_vol is None else _stab_inv(dp.np_yoy_vol), 0.45),
            vf("roe_stability", "ROE 稳定性", dp.roe_stability,
               dp.roe_stability * 100 if dp.roe_stability is not None else None, 0.30),
            vf("ocf_to_np_q", "现金流质量", dp.ocf_to_np,
               None if dp.ocf_to_np is None else max(0.0, min(100.0, dp.ocf_to_np * 100)), 0.25),
        ]
        quality = dim("quality", "质量", quality_factors, missing)

        # ---- Growth -------------------------------------------------------- #
        growth_factors = [
            pct_factor("revenue_yoy", "营收同比", dp.revenue_yoy, dp, "revenue_yoy_history", 0.45),
            pct_factor("net_profit_yoy", "净利同比", dp.net_profit_yoy, dp, "np_yoy_history", 0.45),
            vf("np_cagr3", "3年净利CAGR", dp.net_profit_cagr3, _cagr_score(dp.net_profit_cagr3), 0.10),
        ]
        growth = dim("growth", "成长", growth_factors, missing)

        # ---- Valuation: PE/PB percentile proxies for missing P/EV ---------- #
        val_factors = [
            pct_factor("pe_ttm", "PE-TTM 5年分位", dp.pe_ttm, dp, "pe_history", 0.50,
                       higher_better=False, direction="lower",
                       note="P/EV 主模型因无EV数据未生效，以估值分位近似"),
            pct_factor("pb", "PB 5年分位", dp.pb, dp, "pb_history", 0.30,
                       higher_better=False, direction="lower"),
            pw_factor(
                "dv_ratio", "股息率", dp.dv_ratio,
                [(0.0, 0.0), (0.02, 40.0), (0.04, 70.0), (0.06, 90.0), (0.09, 100.0)],
                0.20,
            ),
        ]
        valuation = dim("valuation", "估值", val_factors, missing)

        # ---- Cycle: earnings acceleration + sector overlay ---------------- #
        cycle_factors = [
            pw_factor(
                "growth_accel", "净利YoY动能", dp.net_profit_yoy,
                [(-0.5, 0.0), (-0.1, 30.0), (0.0, 50.0), (0.15, 75.0), (0.5, 100.0)],
                0.60,
            ),
            vf("roe_momentum", "ROE 动量", dp.roe, _mom(dp.roe_history), 0.40),
        ]
        cycle = dim("cycle", "周期", cycle_factors, missing)

        # ---- Shareholder ---------------------------------------------------- #
        sh_factors = [
            pw_factor(
                "dv_ratio_sh", "股息率", dp.dv_ratio,
                [(0.0, 0.0), (0.015, 40.0), (0.03, 70.0), (0.05, 90.0), (0.08, 100.0)],
                0.70,
            ),
            vf("roe_level_sh", "ROE 水平", dp.roe, _roe_level(dp.roe, 1.0).score, 0.30),
        ]
        shareholder = dim("shareholder", "股东回报", sh_factors, missing)

        generic = build_generic_risk_components(
            dp,
            industry_cycle=RiskComponent(
                "cycle", "行业周期风险",
                _insurance_cycle_risk(dp), RISK_WEIGHTS["cycle"],
                evidence=_cycle_evidence(dp),
            ),
        )

        flags: list[str] = []
        flags.append("EV_DATA_MISSING")  # free snapshot has no embedded value
        if dp.net_profit_yoy is not None and dp.net_profit_yoy < 0:
            flags.append("PROFIT_DOWN")
        if dp.pb is not None and dp.pb <= 0.8:
            flags.append("PB_BELOW_08")

        return (fundamental, quality, growth, valuation, cycle, shareholder), tuple(generic), tuple(flags)


def _roe_level(roe: float | None, weight: float) -> FactorScore:
    """Insurance ROE 10-18% p.a. is the typical healthy band."""
    if roe is None:
        return vf("roe_level", "ROE 水平", None, None, weight, method="piecewise")
    if roe >= 18.0:
        s = 100.0
    elif roe >= 15.0:
        s = 90.0
    elif roe >= 12.0:
        s = 75.0
    elif roe >= 9.0:
        s = 55.0
    elif roe >= 6.0:
        s = 35.0
    else:
        s = 10.0
    return vf("roe_level", "ROE 水平", roe, s, weight, method="piecewise")


def _stab_inv(vol: float) -> float:
    """Lower earnings-growth volatility = higher quality score."""
    if vol <= 0.10:
        return 90.0
    if vol <= 0.25:
        return 65.0
    if vol <= 0.50:
        return 40.0
    return 15.0


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


def _mom(hist: list[float]) -> float | None:
    if len(hist) < 2:
        return None
    latest = hist[-1]
    prev = sum(hist[:-1]) / (len(hist) - 1)
    if prev == 0:
        return None
    return max(0.0, min(100.0, 50.0 + (latest / prev - 1.0) * 500.0))


def _equity_ratio(dp: DataPoints) -> float | None:
    if dp.debt_ratio is not None:
        return max(0.0, 1.0 - dp.debt_ratio)
    return None


def _insurance_cycle_risk(dp: DataPoints) -> float:
    score = 30.0
    if dp.net_profit_yoy is not None and dp.net_profit_yoy < 0:
        score += 20.0
    if dp.roe is not None and dp.roe_avg3 is not None and dp.roe < dp.roe_avg3 * 0.85:
        score += 15.0
    if dp.ann_vol is not None and dp.ann_vol > 45.0:
        score += 15.0
    return min(100.0, score)


def _cycle_evidence(dp: DataPoints) -> str:
    parts = []
    if dp.net_profit_yoy is not None:
        parts.append(f"net_profit_yoy={dp.net_profit_yoy:.1%}")
    if dp.roe is not None:
        parts.append(f"roe={dp.roe:.2f}")
    if dp.ann_vol is not None:
        parts.append(f"ann_vol={dp.ann_vol:.0f}%")
    return "; ".join(parts) or "no cycle data"