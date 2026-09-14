"""Confidence Score (data completeness + freshness).

Starts at 100 and deducts for: missing critical metrics (by declared weight),
stale financial reports (> 200 days), missing market snapshot, short valuation
history, heavy announcement-date clamping. <50 triggers DATA_BLOCK per the
signalling rules (section 27); >=70 is required for STRONG_BUY.
"""
from __future__ import annotations

from src.research.datapoints import DataPoints

_CRITICAL_KEYS: tuple[str, ...] = (
    "roe", "net_margin", "growth", "valuation", "dividend", "cashflow",
)

_BLOCK_THRESHOLD = 50.0
_STRONG_BUY_THRESHOLD = 70.0


def compute_data_freshness_score(dp: DataPoints) -> float:
    """Freshness of the underlying data, independent of model scoring."""
    score = 100.0
    if dp.latest_period is None:
        return 0.0
    if dp.report_age_days is not None:
        if dp.report_age_days > 365:
            score -= 40.0
        elif dp.report_age_days > 200:
            score -= 25.0
        elif dp.report_age_days > 120:
            score -= 10.0
    if dp.close is None:
        score -= 20.0
    return round(max(0.0, score), 1)


def compute_confidence_score(dp: DataPoints, missing_keys: list[str]) -> float:
    """0-100 confidence in the produced research scores."""
    missing = set(missing_keys)
    score = 100.0

    critical_missing = [k for k in _CRITICAL_KEYS if k in missing]
    score -= 12.0 * len(critical_missing)

    if "financial_report" in missing or "market" in missing:
        score -= 40.0
    if "valuation_history" in missing or "pe_ttm" in missing:
        score -= 15.0
    if "dividend" in missing:
        score -= 5.0
    if "cashflow" in missing:
        score -= 5.0
    if "stale_report" in missing:
        score -= 20.0

    if dp.report_age_days is not None:
        if dp.report_age_days > 365:
            score -= 25.0
        elif dp.report_age_days > 200:
            score -= 15.0

    if dp.pe_history:
        if len(dp.pe_history) < 250:
            score -= 10.0
        elif len(dp.pe_history) < 500:
            score -= 5.0

    # missing industry-specific metrics (e.g. NIM, EV, resource prices) flagged
    # by the model reduce confidence further (handled by models via missing_keys).
    return round(max(0.0, min(100.0, score)), 1)


def confidence_blocked(confidence: float) -> bool:
    return confidence < _BLOCK_THRESHOLD


def confidence_allows_strong_buy(confidence: float) -> bool:
    return confidence >= _STRONG_BUY_THRESHOLD