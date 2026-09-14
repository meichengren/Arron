"""BANK industry model (section 9.1).

Weights follow the spec table: ROE/ROA 18, asset quality 15, buffer 10,
NIM 12, growth 10, capital 8, PB percentile 15, PE percentile 5, dividend 7.
Free-snapshot data cannot provide NPL/buffer/NIM/capital-adequacy, so those
factors are marked missing (conf. reduced) instead of being fabricated.
Valuation MUST prefer PB/ROE over PE.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from src.research.base import DimensionScore, FactorScore, IndustryScoringModel, RiskComponent
from src.research.datapoints import DataPoints
from src.research.models._helpers import dim, pct_factor, pw_factor, vf
from src.research.risk import RISK_WEIGHTS, build_generic_risk_components


class BankModel(IndustryScoringModel):
    name = "BANK"
    version = "V1.0"

    def score(
        self, data: dict[str, Any], as_of: date
    ) -> tuple[tuple[DimensionScore, ...], tuple[RiskComponent, ...], tuple[str, ...]]:
        dp: DataPoints = data["dp"]
        missing = list(dp.missing_keys)
        ndp = dp
        roe_ann = _annualize_roe(dp.roe, dp.latest_period)
        roa_ann = _annualize_roa(dp.roa, dp.latest_period)

        # ---- Fundamental (ROE/ROA 18 + NIM 12 + capital 8 = 38) ---------- #
        fund_factors: list[FactorScore] = [
            pct_factor("roe", "ROE", dp.roe, dp, "roe_history", 0.47),
            vf("roa", "ROA", roa_ann, _roa_score(roa_ann), 0.13,
               method="piecewise", note="ROA % annualized"),
            # NIM unavailable in free snapshot -> missing, not fabricated
            pw_factor("nim", "净息差(NIM)", None, [(0.0, 0.0), (0.02, 60.0), (0.03, 90.0), (0.04, 100.0)], 0.27),
            # capital adequacy -> proxy: equity/assets (equity buffer)
            pw_factor(
                "equity_buffer", "资本缓冲(权益/资产)", _equity_ratio(dp),
                [(0.0, 0.0), (0.03, 30.0), (0.06, 65.0), (0.10, 90.0), (0.15, 100.0)],
                0.13,
            ),
        ]
        fundamental = dim("fundamental", "基本面", fund_factors, missing)

        # ---- Quality (asset quality 15 + buffer 10 = 25) ------------------ #
        quality_factors: list[FactorScore] = [
            # NPL ratio unavailable -> missing (reduces confidence)
            pw_factor("npl", "不良率", None, [(0.0, 100.0), (0.01, 80.0), (0.02, 55.0), (0.04, 20.0), (0.06, 0.0)], 0.55),
            # provision coverage unavailable -> missing
            pw_factor("provision", "拨备覆盖率", None,
                      [(0.0, 0.0), (1.5, 40.0), (2.5, 75.0), (4.0, 95.0), (6.0, 100.0)],
                      0.30),
            vf("ocf_to_np", "经营现金流/净利", dp.ocf_to_np,
               None if dp.ocf_to_np is None else max(0.0, min(100.0, dp.ocf_to_np * 100)), 0.15,
               method="designated"),
        ]
        quality = dim("quality", "质量", quality_factors, missing)

        # ---- Growth (10) --------------------------------------------------- #
        growth_factors = [
            pct_factor("revenue_yoy", "营收同比", dp.revenue_yoy, dp, "revenue_yoy_history", 0.50),
            pct_factor("net_profit_yoy", "净利同比", dp.net_profit_yoy, dp, "np_yoy_history", 0.50),
        ]
        growth = dim("growth", "成长", growth_factors, missing)

        # ---- Valuation (PB 15 + PE 5 = 20, PB dominates) ------------------- #
        val_factors = [
            pct_factor("pb", "PB 5年分位", dp.pb, dp, "pb_history", 0.75,
                       higher_better=False, direction="lower",
                       note="银行估值主模型为 PB/ROE"),
            pct_factor("pe_ttm", "PE-TTM 5年分位", dp.pe_ttm, dp, "pe_history", 0.25,
                       higher_better=False, direction="lower"),
        ]
        valuation = dim("valuation", "估值", val_factors, missing)

        # ---- Cycle: NIM trend proxy -------- (uses margin health) ---------- #
        cycle_factors = [
            pw_factor(
                "margin_trend", "盈利能力趋势(ROA)", roa_ann,
                [(0.0, 0.0), (0.4, 40.0), (0.8, 75.0), (1.2, 90.0), (2.0, 100.0)],
                0.60,
            ),
            vf("roe_momentum", "ROE 动量", roe_ann,
               _mom(dp.roe_history), 0.40, method="designated"),
        ]
        cycle = dim("cycle", "周期", cycle_factors, missing)

        # ---- Shareholder (7) ------------------------------------------------ #
        sh_factors = [
            pw_factor(
                "dv_ratio", "股息率", dp.dv_ratio,
                [(0.0, 0.0), (0.02, 40.0), (0.04, 70.0), (0.06, 90.0), (0.09, 100.0)],
                0.70,
            ),
            vf("roe_quality", "ROE 水平", roe_ann, _roe_score(roe_ann), 0.30,
               method="piecewise"),
        ]
        shareholder = dim("shareholder", "股东回报", sh_factors, missing)

        # ---- Industry cycle risk: bank-specific overlay --------------------- #
        generic = build_generic_risk_components(
            dp,
            industry_cycle=RiskComponent(
                "cycle", "行业周期风险",
                _bank_cycle_risk(dp), RISK_WEIGHTS["cycle"],
                evidence=_cycle_evidence(dp),
            ),
        )

        flags: list[str] = []
        if dp.latest_period is not None and dp.latest_period.month not in (12, 6):
            flags.append("INTERIM_REPORT")
        if dp.net_profit_yoy is not None and dp.net_profit_yoy < 0:
            flags.append("PROFIT_DOWN")
        if dp.pb is not None and dp.pb <= 0.8:
            flags.append("PB_BELOW_08")
        if "nim" in [f.key for f in fund_factors if f.missing]:
            flags.append("NIM_MISSING")

        return (fundamental, quality, growth, valuation, cycle, shareholder), tuple(generic), tuple(flags)


def _annualize_roe(roe: float | None, period: date | None) -> float | None:
    """Annualize cumulative ROE: H1x2, Q1x4, Q3x4/3, annual x1."""
    if roe is None:
        return None
    if period is None:
        return roe
    m = period.month
    mult = {12: 1.0, 9: 4.0 / 3.0, 6: 2.0, 3: 4.0}.get(m, 1.0)
    return roe * mult


def _annualize_roa(roa: float | None, period: date | None) -> float | None:
    return _annualize_roe(roa, period)


def _equity_ratio(dp: DataPoints) -> float | None:
    if dp.debt_ratio is not None:
        return max(0.0, 1.0 - dp.debt_ratio)
    return None


def _roa_score(roa: float | None) -> float | None:
    if roa is None:
        return None
    # banks: ROA 0.6-1.2% (annualized percent) is strong
    if roa >= 1.2:
        return 100.0
    if roa >= 0.9:
        return 90.0
    if roa >= 0.7:
        return 75.0
    if roa >= 0.5:
        return 55.0
    if roa >= 0.3:
        return 30.0
    return 10.0


def _roe_score(roe: float | None) -> float | None:
    if roe is None:
        return None
    # roe is annualized percent (e.g. 11.5 = 11.5%)
    if roe >= 16.0:
        return 100.0
    if roe >= 13.0:
        return 85.0
    if roe >= 10.0:
        return 65.0
    if roe >= 7.0:
        return 45.0
    if roe >= 4.0:
        return 25.0
    return 5.0


def _mom(hist: list[float]) -> float | None:
    if len(hist) < 2:
        return None
    latest = hist[-1]
    prev = sum(hist[:-1]) / (len(hist) - 1)
    if prev == 0:
        return None
    return max(0.0, min(100.0, 50.0 + (latest / prev - 1.0) * 500.0))


def _bank_cycle_risk(dp: DataPoints) -> float:
    """Asset-deterioration proxy: rising leverage + falling margins = risk up."""
    score = 30.0
    if dp.net_profit_yoy is not None and dp.net_profit_yoy < 0:
        score += 20.0
    if dp.roe is not None and dp.roe_avg3 is not None and dp.roe < dp.roe_avg3 * 0.85:
        score += 20.0
    if dp.pb is not None and dp.pb < 0.7:
        score += 10.0  # low PB often signals asset-quality stress in banks
    return min(100.0, score)


def _cycle_evidence(dp: DataPoints) -> str:
    parts = []
    if dp.net_profit_yoy is not None:
        parts.append(f"net_profit_yoy={dp.net_profit_yoy:.1%}")
    if dp.roe is not None:
        parts.append(f"roe={dp.roe:.2f}")
    if dp.pb is not None:
        parts.append(f"pb={dp.pb:.2f}")
    return "; ".join(parts) or "no cycle data"