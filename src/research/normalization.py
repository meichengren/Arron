"""Metric normalization helpers (Section 10).

All functions map a raw indicator value onto a 0-100 scale. The two supported
approaches per spec:

* Method A - percentile: rank current value inside the company's own 5-year
  history (or a provided reference series). ``higher_better=False`` flips the
  rank so that cheap valuation = high score.
* Method B - piecewise: business-meaningful breakpoints (e.g. NPL ratio).

``zscore_to_0_100`` is a monotone transform of a z-score clipped to [-3, 3].

Every metric declares its direction (higher/lower/optimal) upstream in the
industry models, this module only provides the math.
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence


def _clean(series: Iterable[float | int | None]) -> list[float]:
    out: list[float] = []
    for v in series:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out.append(f)
    return out


def percentile_score(
    value: float | None,
    series: Iterable[float | int | None],
    *,
    higher_better: bool = True,
    min_obs: int = 2,
) -> float | None:
    """Method A: percentile rank of ``value`` inside ``series`` as a 0-100 score.

    Uses *weak* percentile (value itself counts as equal), which keeps the
    score bounded while still ranking ties fairly. Returns None when there are
    fewer than ``min_obs`` usable observations or the value is missing.
    """
    if value is None:
        return None
    try:
        cur = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(cur) or math.isinf(cur):
        return None

    hist = _clean(series)
    if len(hist) < min_obs:
        return None
    below = sum(1 for x in hist if x <= cur)
    pct = below / len(hist) * 100.0
    return pct if higher_better else 100.0 - pct


def piecewise_score(
    value: float | None,
    segments: Sequence[tuple[float, float]],
    *,
    higher_better: bool = True,
) -> float | None:
    """Method B: linear interpolation over ``segments``.

    ``segments`` is a sorted list of (value, score) breakpoints, e.g.
    ``[(0.0, 0.0), (0.15, 80.0), (0.25, 100.0)]``. Points outside the range are
    clamped to the nearest end. With ``higher_better=False`` the same table is
    mirrored so that small ``value`` gets a high score. Returns None when the
    value is missing/NaN.
    """
    if value is None:
        return None
    try:
        cur = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(cur) or math.isinf(cur):
        return None

    pts: list[tuple[float, float]] = []
    for v, s in segments:
        try:
            pts.append((float(v), float(s)))
        except (TypeError, ValueError):
            continue
    if len(pts) < 2:
        return None
    pts = sorted(pts, key=lambda p: p[0])
    if not higher_better:
        pts = [(-v, s) for v, s in pts][::-1]
        cur = -cur

    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if cur <= xs[0]:
        return ys[0]
    if cur >= xs[-1]:
        return ys[-1]
    for i in range(len(xs) - 1):
        if xs[i] <= cur <= xs[i + 1]:
            span = xs[i + 1] - xs[i]
            if span == 0:
                continue
            t = (cur - xs[i]) / span
            return ys[i] + (ys[i + 1] - ys[i]) * t
    return ys[-1]


def zscore_to_0_100(z: float | None) -> float | None:
    """Map a z-score onto 0-100 with a soft logistic-style tail.

    ``z=0 -> 50``, ``z=1 -> ~76``, ``z=-1 -> ~24``, clipped at [-3, 3].
    """
    if z is None:
        return None
    try:
        zf = float(z)
    except (TypeError, ValueError):
        return None
    if math.isnan(zf) or math.isinf(zf):
        return None
    zc = max(-3.0, min(3.0, zf))
    return round(50.0 * (1.0 + math.tanh(zc / 2.2)), 2)