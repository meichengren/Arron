"""Shared helpers for industry models."""
from __future__ import annotations

from typing import Any

from src.research.base import DimensionScore, FactorScore
from src.research.datapoints import DataPoints
from src.research.normalization import percentile_score, piecewise_score


def dim(
    key: str,
    label: str,
    factors: list[FactorScore],
    missing_keys: list[str] | None = None,
) -> DimensionScore:
    """Aggregate factors into a weighted 0-100 dimension score.

    Missing factors are dropped and the weights of the remaining factors are
    re-normalized to 1.0 so a partially-filled model still yields a fair score
    while missing_keys are reported for confidence deduction.
    """
    avail = [f for f in factors if f.score is not None and not f.missing]
    mk = list(
        dict.fromkeys(
            [f.key for f in factors if f.missing]
            + (missing_keys or [])
        )
    )
    if not avail:
        return DimensionScore(key=key, label=label, score=50.0, factors=tuple(factors), missing_keys=tuple(mk))
    total_w = sum(f.weight for f in avail)
    score = sum(f.score * (f.weight / total_w) for f in avail)
    return DimensionScore(
        key=key,
        label=label,
        score=round(min(100.0, max(0.0, score)), 2),
        factors=tuple(factors),
        missing_keys=tuple(mk),
    )


def pct_factor(
    key: str,
    label: str,
    value: float | None,
    dp: DataPoints,
    hist_attr: str,
    weight: float,
    higher_better: bool = True,
    direction: str = "higher",
    note: str = "",
) -> FactorScore:
    """Percentile-based factor (Method A) over the company's own history."""
    hist = getattr(dp, hist_attr)
    score = percentile_score(value, hist, higher_better=higher_better)
    missing = score is None or value is None
    return FactorScore(
        key=key, label=label, raw=value, score=score, weight=weight,
        method="percentile", direction=direction, missing=missing, note=note,
    )


def pw_factor(
    key: str,
    label: str,
    value: float | None,
    segments: list[tuple[float, float]],
    weight: float,
    direction: str = "higher",
    higher_better: bool = True,
    note: str = "",
) -> FactorScore:
    """Piecewise factor (Method B) with business breakpoints."""
    score = piecewise_score(value, segments, higher_better=higher_better)
    missing = score is None or value is None
    return FactorScore(
        key=key, label=label, raw=value, score=score, weight=weight,
        method="piecewise", direction=direction, missing=missing, note=note,
    )


def vf(
    key: str, label: str, raw: Any, score: float | None, weight: float,
    method: str = "designated", direction: str = "higher", note: str = "",
) -> FactorScore:
    """Directly assigned factor value (e.g. dividend yield)."""
    missing = score is None or raw is None
    return FactorScore(
        key=key, label=label, raw=_num(raw), score=score, weight=weight,
        method=method, direction=direction, missing=missing, note=note,
    )


def _num(x: Any) -> float | None:
    try:
        v = float(x) if x is not None else None
    except (TypeError, ValueError):
        return None
    return v