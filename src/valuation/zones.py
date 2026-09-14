"""V1.1 patch items 1-2: valuation zones + confidence-adjusted returns.

Pure formulas on top of the Phase 3 point estimates. The point buy prices are
kept (they are the seats of the zones) but the system now also emits:

  - Fair Value Range        [base - band, base + band]
  - Standard Buy Zone       [std_buy  - band*0.4, std_buy  + band*0.4]
  - Conservative Buy Zone   [con_buy  - band*0.3, con_buy  + band*0.3]
  - Valuation Confidence    0-100 reuse of the research confidence score
  - Forecast Confidence     idem (alias, patch vocabulary)
  - Base IRR @ current      expected_return_base
  - Adjusted Expected Return (confidence-scaled, patch example: ER 18% *
    confidence 62 -> ~14.6%)

band = max(bear_gap, bull_gap) * (0.55 - 0.0035 * confidence)
The higher the confidence the tighter the band; all values in CNY per share.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ValuationZones:
    fair_value_range: tuple[float, float] | None
    standard_buy_zone: tuple[float, float] | None
    conservative_buy_zone: tuple[float, float] | None
    band: float | None
    # confidence in percent (0-100)
    valuation_confidence: float | None
    forecast_confidence: float | None
    # returns in decimal (0.10 == +10%)
    base_irr: float | None
    conservative_irr: float | None
    adjusted_expected_return: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fair_value_range": self.fair_value_range,
            "standard_buy_zone": self.standard_buy_zone,
            "conservative_buy_zone": self.conservative_buy_zone,
            "band": self.band,
            "valuation_confidence": self.valuation_confidence,
            "forecast_confidence": self.forecast_confidence,
            "base_irr": self.base_irr,
            "conservative_irr": self.conservative_irr,
            "adjusted_expected_return": self.adjusted_expected_return,
        }


def _clamp_conf(confidence: float | None) -> float | None:
    if confidence is None:
        return None
    return round(max(0.0, min(100.0, float(confidence))), 1)


def valuation_zones(
    fair_value_bear: float | None,
    fair_value_base: float | None,
    fair_value_bull: float | None,
    standard_buy_price: float | None,
    conservative_buy_price: float | None,
    confidence: float | None,
    expected_return_base: float | None = None,
    expected_return_conservative: float | None = None,
) -> ValuationZones:
    """Compute the zone outputs for one valuation result (all prices in CNY)."""
    conf = _clamp_conf(confidence)
    band: float | None = None
    if all(v is not None for v in (fair_value_bear, fair_value_base, fair_value_bull)):
        bear_gap = abs(float(fair_value_base) - float(fair_value_bear))
        bull_gap = abs(float(fair_value_bull) - float(fair_value_base))
        spread = max(bear_gap, bull_gap)
        c = conf if conf is not None else 60.0
        band = round(spread * (0.55 - 0.0035 * c), 2)

    def _range(center: float | None, mult: float) -> tuple[float, float] | None:
        if center is None or band is None:
            return None
        return (round(float(center) - band * mult, 2), round(float(center) + band * mult, 2))

    fair_range = _range(fair_value_base, 1.0)
    std_zone = _range(standard_buy_price, 0.4)
    con_zone = _range(conservative_buy_price, 0.3)

    return ValuationZones(
        fair_value_range=fair_range,
        standard_buy_zone=std_zone,
        conservative_buy_zone=con_zone,
        band=band,
        valuation_confidence=conf,
        forecast_confidence=conf,
        base_irr=round(float(expected_return_base), 4) if expected_return_base is not None else None,
        conservative_irr=(
            round(float(expected_return_conservative), 4)
            if expected_return_conservative is not None
            else None
        ),
        adjusted_expected_return=adjusted_expected_return(
            expected_return_base, conf
        ),
    )


def adjusted_expected_return(raw_expected_return: float | None, confidence: float | None) -> float | None:
    """Patch item 2: confidence-scaled expected return.

    adjusted = raw * (0.5 + confidence/200). With confidence 100 -> raw
    unchanged; confidence 62 -> raw * 0.81 (matches the patch's 18% -> 14.6%).
    """
    if raw_expected_return is None:
        return None
    conf = _clamp_conf(confidence)
    if conf is None:
        return raw_expected_return
    scale = 0.5 + conf / 200.0
    return round(float(raw_expected_return) * scale, 4)


def hold_or_buy_decision(
    current_price: float | None,
    standard_buy_zone: tuple[float, float] | None,
    fundamental_score: float | None,
    risk_ok: bool = True,
    strong_buy_min_score: float = 70.0,
) -> str:
    """Patch item 1 rule: BUY needs zone entry + strong fundamentals + risk OK.

    Returns one of STRONG_BUY / BUY / HOLD / WAIT / NO_BUY.
    """
    if current_price is None or standard_buy_zone is None:
        return "WAIT"
    if current_price > standard_buy_zone[1]:
        return "HOLD"
    if not risk_ok:
        return "WAIT"
    if fundamental_score is None:
        return "BUY"
    if fundamental_score >= strong_buy_min_score:
        return "STRONG_BUY"
    return "BUY"