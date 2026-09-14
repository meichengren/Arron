"""Phase 3 scenario valuation - pure formulas (Sections 11-15).

Scenario model exposes the three cases (bear/base/bull) as immutable
dataclasses; every business formula here has a dedicated unit test and never
touches the database.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Price-state bands (Section 17)
PRICE_STATE_DEEP_VALUE = "DEEP_VALUE"
PRICE_STATE_ATTRACTIVE = "ATTRACTIVE"
PRICE_STATE_FAIR = "FAIR"
PRICE_STATE_EXPENSIVE = "EXPENSIVE"
PRICE_STATE_VERY_EXPENSIVE = "VERY_EXPENSIVE"

PRICE_STATE_LABELS: dict[str, str] = {
    PRICE_STATE_DEEP_VALUE: "深度价值 / 强研究机会",
    PRICE_STATE_ATTRACTIVE: "有吸引力",
    PRICE_STATE_FAIR: "合理",
    PRICE_STATE_EXPENSIVE: "偏贵",
    PRICE_STATE_VERY_EXPENSIVE: "非常贵",
}


@dataclass(frozen=True)
class ScenarioCase:
    key: str                 # "bear" | "base" | "bull"
    label: str               # 悲观 / 中性 / 乐观
    growth: float | None     # 3y earnings/book growth assumption (0.05 = +5%)
    terminal_multiple: float | None
    multiple_type: str       # "PE" | "PB" | "P/EV" | "EV/EBITDA"
    terminal_value: float | None   # 3y target value per share (CNY)
    dividends_per_share: float     # 3y accumulated dividends per share (CNY)
    cagr: float | None       # 3y CAGR from current price (0.10 = +10%/yr)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "growth": self.growth,
            "terminal_multiple": self.terminal_multiple,
            "multiple_type": self.multiple_type,
            "terminal_value": self.terminal_value,
            "dividends_per_share": self.dividends_per_share,
            "cagr": self.cagr,
        }


@dataclass(frozen=True)
class ValuationResult:
    """Complete valuation output for a security at an as-of date."""

    industry_model: str
    current_price: float | None
    horizon_years: int
    fair_value_bear: float | None
    fair_value_base: float | None
    fair_value_bull: float | None
    standard_buy_price: float | None
    conservative_buy_price: float | None
    expected_return_bear: float | None
    expected_return_base: float | None
    expected_return_bull: float | None
    expected_return: float | None     # scenario-weighted
    price_state: str | None
    expected_return_conservative: float | None = None  # V1.1: conservative-target IRR
    scenarios: tuple[ScenarioCase, ...] = ()
    margin_of_safety_pct: float | None = None
    notes: tuple[str, ...] = ()       # explicit degradation / data notes
    missing_keys: tuple[str, ...] = ()  # valuation inputs explicitly missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "industry_model": self.industry_model,
            "current_price": self.current_price,
            "horizon_years": self.horizon_years,
            "fair_value_bear": self.fair_value_bear,
            "fair_value_base": self.fair_value_base,
            "fair_value_bull": self.fair_value_bull,
            "standard_buy_price": self.standard_buy_price,
            "conservative_buy_price": self.conservative_buy_price,
            "expected_return_bear": self.expected_return_bear,
            "expected_return_base": self.expected_return_base,
            "expected_return_bull": self.expected_return_bull,
            "expected_return": self.expected_return,
            "expected_return_conservative": self.expected_return_conservative,
            "price_state": self.price_state,
            "price_state_label": PRICE_STATE_LABELS.get(self.price_state or ""),
            "margin_of_safety_pct": self.margin_of_safety_pct,
            "scenarios": [c.to_dict() for c in self.scenarios],
            "notes": list(self.notes),
            "missing_keys": list(self.missing_keys),
        }


def cagr(target_value: float, dividends_per_share: float, current_price: float, years: int) -> float | None:
    """Section 15: CAGR = ((TargetValue + Dividends) / CurrentPrice)^(1/T) - 1."""
    if current_price is None or current_price <= 0 or years <= 0:
        return None
    ratio = (target_value + dividends_per_share) / current_price
    if ratio <= 0:
        return None
    return round(ratio ** (1.0 / years) - 1.0, 6)


def pv_terminal(terminal_value: float, dividends_per_share: float, required_return: float, years: int) -> float | None:
    """Sections 12-13: PV(Terminal Value_T + Expected Dividends, required_return)."""
    if terminal_value is None or required_return is None or years <= 0:
        return None
    total = terminal_value + dividends_per_share
    return round(total / ((1.0 + required_return) ** years), 6)


def weighted_expected_return(
    bear: float | None,
    base: float | None,
    bull: float | None,
    weights: dict[str, float] | None = None,
) -> float | None:
    """Section 15: w_bear*Bear + w_base*Base + w_bull*Bull (default 25/50/25)."""
    w = weights or {"bear": 0.25, "base": 0.50, "bull": 0.25}
    if bear is None or base is None or bull is None:
        return None
    total = w["bear"] * bear + w["base"] * base + w["bull"] * bull
    return round(total, 6)


def price_state(
    current: float | None,
    conservative_buy: float | None,
    standard_buy: float | None,
    fair_base: float | None,
    fair_bull: float | None,
) -> str | None:
    """Section 17 five-band mapping (evaluated in order)."""
    if current is None:
        return None
    if conservative_buy is not None and current <= conservative_buy:
        return PRICE_STATE_DEEP_VALUE
    if standard_buy is not None and current <= standard_buy:
        return PRICE_STATE_ATTRACTIVE
    if fair_base is not None and current <= fair_base:
        return PRICE_STATE_FAIR
    if fair_bull is not None and current <= fair_bull:
        return PRICE_STATE_EXPENSIVE
    return PRICE_STATE_VERY_EXPENSIVE


def percentile(values: list[float], q: float) -> float | None:
    """Simple linear-interpolation percentile on a sorted copy (0 <= q <= 1)."""
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if not vals:
        return None
    vals.sort()
    if q <= 0.0:
        return vals[0]
    if q >= 1.0:
        return vals[-1]
    pos = q * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] + (vals[hi] - vals[lo]) * frac