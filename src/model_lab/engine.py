"""Model Lab look-back engine (spec section 3.3).

Read-only analytics over persisted snapshots. Every function works with
"insufficient history" gracefully: real look-backs need ~1y of accumulated
snapshots, so until then the engine returns n=0 / status="insufficient"
payloads instead of raising.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from scipy import stats
from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory

# -- horizon definitions (trading days ahead) -------------------------------- #
HORIZONS = {"3M": 63, "6M": 126, "12M": 252, "24M": 504}
TRADING_DAYS_PER_YEAR = 252

# -- score bands (spec 3.3.1: >=80 / 70-80 / 60-70 / <60) -------------------- #
SCORE_BANDS: list[tuple[str, float | None, float | None]] = [
    (">=80", 80.0, None),
    ("70-80", 70.0, 80.0),
    ("60-70", 60.0, 70.0),
    ("<60", None, 60.0),
]


@dataclass
class FutureReturn:
    """One snapshot + its realised future return for one horizon."""

    security_id: int
    symbol: str
    industry_model: str
    as_of_date: date
    score: float
    expected_return_base: float | None      # model annualised forecast
    standard_buy_price: float | None
    conservative_buy_price: float | None
    horizon: str                            # "3M" / "6M" / "12M" / "24M"
    horizon_days: int
    base_close: float
    future_close: float
    future_return: float                    # simple return over the horizon
    annualized_return: float                # (1+r)^(252/horizon_days) - 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "industry_model": self.industry_model,
            "as_of_date": self.as_of_date.isoformat(),
            "score": round(self.score, 2),
            "expected_return_base": round(self.expected_return_base, 4)
            if self.expected_return_base is not None else None,
            "standard_buy_price": self.standard_buy_price,
            "conservative_buy_price": self.conservative_buy_price,
            "horizon": self.horizon,
            "base_close": round(self.base_close, 4),
            "future_close": round(self.future_close, 4),
            "future_return": round(self.future_return, 4),
            "annualized_return": round(self.annualized_return, 4),
        }


# --------------------------------------------------------------------------- #
# data loading
# --------------------------------------------------------------------------- #
def _load_closes(session, security_id: int) -> list[tuple[date, float]]:
    rows = session.execute(
        select(models.DailyMarket.trade_date, models.DailyMarket.close)
        .where(models.DailyMarket.security_id == security_id)
        .where(models.DailyMarket.close.isnot(None))
        .order_by(models.DailyMarket.trade_date)
    ).all()
    return [(d, float(c)) for d, c in rows]


def collect_future_returns(
    engine: Engine,
    horizons: tuple[str, ...] | None = None,
    as_of_min: date | None = None,
    as_of_max: date | None = None,
) -> list[FutureReturn]:
    """Join every ResearchSnapshot with realised close prices after its
    as_of_date. A snapshot only yields a FutureReturn once enough trading days
    have elapsed (base close on/after as_of, future close exactly horizon days
    later). Snapshots without future data are simply skipped."""
    horizons = horizons or tuple(HORIZONS.keys())
    horizon_days = {h: HORIZONS[h] for h in horizons}

    factory = make_session_factory(engine)
    out: list[FutureReturn] = []
    with factory() as session:
        secs = session.execute(
            select(models.Security)
            .where(models.Security.status == "ACTIVE")
            .order_by(models.Security.symbol)
        ).scalars().all()

        for sec in secs:
            closes = _load_closes(session, sec.id)
            if len(closes) < 2:
                continue
            dates = [d for d, _ in closes]
            closes_by_date = dict(closes)

            snaps = session.execute(
                select(models.ResearchSnapshot)
                .where(models.ResearchSnapshot.security_id == sec.id)
                .where(models.ResearchSnapshot.research_score.isnot(None))
                .order_by(models.ResearchSnapshot.as_of_date)
            ).scalars().all()
            for snap in snaps:
                as_of = snap.as_of_date
                if as_of_min and as_of < as_of_min:
                    continue
                if as_of_max and as_of > as_of_max:
                    continue
                # base close: on as_of or the next available trading day
                base_idx = next(
                    (i for i, d in enumerate(dates) if d >= as_of), None
                )
                if base_idx is None or dates[base_idx] != as_of:
                    # require a close exactly on as_of for reproducible look-backs
                    if base_idx is None:
                        continue
                base_close = closes_by_date.get(dates[base_idx])
                if base_close is None:
                    continue
                for h in horizons:
                    days = horizon_days[h]
                    fwd = base_idx + days
                    if fwd >= len(dates):
                        continue
                    future_close = closes[fwd][1]
                    if not future_close or base_close <= 0:
                        continue
                    r = future_close / base_close - 1.0
                    out.append(FutureReturn(
                        security_id=sec.id,
                        symbol=sec.symbol,
                        industry_model=sec.industry_model,
                        as_of_date=as_of,
                        score=float(snap.research_score),
                        expected_return_base=snap.expected_return_base,
                        standard_buy_price=snap.standard_buy_price,
                        conservative_buy_price=snap.conservative_buy_price,
                        horizon=h,
                        horizon_days=days,
                        base_close=base_close,
                        future_close=future_close,
                        future_return=r,
                        annualized_return=(
                            (1.0 + r) ** (TRADING_DAYS_PER_YEAR / days) - 1.0
                        ),
                    ))
    return out


# --------------------------------------------------------------------------- #
# 1. score validity: bucket stats + monotonicity (Spearman)
# --------------------------------------------------------------------------- #
@dataclass
class BucketStats:
    band: str
    n: int
    mean_return: float | None
    median_return: float | None
    hit_rate: float | None          # fraction of returns > 0
    sharpe: float | None            # annualised mean/std of realised returns

    def to_dict(self) -> dict[str, Any]:
        return {
            "band": self.band,
            "n": self.n,
            "mean_return": round(self.mean_return, 4) if self.mean_return is not None else None,
            "median_return": round(self.median_return, 4) if self.median_return is not None else None,
            "hit_rate": round(self.hit_rate, 4) if self.hit_rate is not None else None,
            "sharpe": round(self.sharpe, 4) if self.sharpe is not None else None,
        }


@dataclass
class ScoreValidityResult:
    horizon: str
    n: int
    buckets: list[BucketStats]
    spearman_rho: float | None
    spearman_p: float | None
    monotonic: bool | None          # True if mean returns strictly increase with band

    @property
    def status(self) -> str:
        return "ok" if self.n >= 5 else "insufficient"

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "n": self.n,
            "status": self.status,
            "buckets": [b.to_dict() for b in self.buckets],
            "spearman_rho": round(self.spearman_rho, 4)
            if self.spearman_rho is not None else None,
            "spearman_p": round(self.spearman_p, 4)
            if self.spearman_p is not None else None,
            "monotonic": self.monotonic,
        }


def _bucket_for(score: float) -> str | None:
    for band, lo, hi in SCORE_BANDS:
        if lo is not None and score < lo:
            continue
        if hi is not None and score >= hi:
            continue
        return band
    return None


def score_validity(
    rows: list[FutureReturn], horizon: str = "12M"
) -> ScoreValidityResult:
    """Bucket by score band and test monotonicity of realised returns."""
    pairs = [(r.score, r.future_return) for r in rows if r.horizon == horizon]
    if len(pairs) < 2:
        return ScoreValidityResult(horizon=horizon, n=0, buckets=[], spearman_rho=None, spearman_p=None, monotonic=None)

    buckets: list[BucketStats] = []
    for band, lo, hi in SCORE_BANDS:
        vals = [ret for s, ret in pairs
                if (lo is None or s >= lo) and (hi is None or s < hi)]
        n = len(vals)
        if n == 0:
            buckets.append(BucketStats(band=band, n=0, mean_return=None,
                                       median_return=None, hit_rate=None, sharpe=None))
            continue
        mean_r = sum(vals) / n
        median_r = sorted(vals)[n // 2]
        hit = sum(1 for v in vals if v > 0) / n
        if n >= 2:
            std = math.sqrt(sum((v - mean_r) ** 2 for v in vals) / (n - 1))
            sharpe = (mean_r / std * math.sqrt(
                TRADING_DAYS_PER_YEAR / HORIZONS[horizon]
            )) if std > 1e-12 else None
        else:
            sharpe = None
        buckets.append(BucketStats(band=band, n=n, mean_return=mean_r,
                                   median_return=median_r, hit_rate=hit, sharpe=sharpe))

    rho, p = None, None
    monotonic: bool | None = None
    if len(pairs) >= 3:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            rho, p = stats.spearmanr([s for s, _ in pairs], [r for _, r in pairs])
        rho = float(rho) if not math.isnan(rho) else None
        p = float(p) if p is not None and not math.isnan(p) else None
        # bands are ordered high->low (>=80 ... <60); score-monotonicity means
        # realised returns strictly increase as score rises, i.e. means fall
        means = [b.mean_return for b in buckets if b.mean_return is not None]
        if len(means) >= 2:
            monotonic = all(
                m1 > m2 for m1, m2 in zip(means, means[1:])
            )
    return ScoreValidityResult(
        horizon=horizon, n=len(pairs), buckets=buckets,
        spearman_rho=rho, spearman_p=p, monotonic=monotonic,
    )


# --------------------------------------------------------------------------- #
# 2. walk-forward: rolling train(5y) -> test(1y), check score->return stability
# --------------------------------------------------------------------------- #
@dataclass
class WalkForwardFold:
    train_start: date
    train_end: date
    test_start: date
    test_end: date
    train_n: int
    test_n: int
    train_rho: float | None
    test_rho: float | None
    train_note: str = ""
    test_note: str = ""

    @property
    def stable(self) -> bool | None:
        """Train and test both show a positive score->return relationship."""
        if self.train_rho is None or self.test_rho is None:
            return None
        return self.train_rho > 0 and self.test_rho > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "train_window": f"{self.train_start}..{self.train_end}",
            "test_window": f"{self.test_start}..{self.test_end}",
            "train_n": self.train_n,
            "test_n": self.test_n,
            "train_rho": round(self.train_rho, 4) if self.train_rho is not None else None,
            "test_rho": round(self.test_rho, 4) if self.test_rho is not None else None,
            "stable": self.stable,
            "train_note": self.train_note,
            "test_note": self.test_note,
        }


def walk_forward(
    rows: list[FutureReturn],
    horizon: str = "12M",
    train_years: int = 5,
    test_years: int = 1,
) -> dict[str, Any]:
    """Rolling train/test windows over snapshots. Only snapshots whose
    as_of_date lies inside the train (or test) window are used; the realised
    future return must still exist (i.e. close prices available horizon days
    ahead of the snapshot date - guaranteed by collect_future_returns)."""
    rows = [r for r in rows if r.horizon == horizon]
    if not rows:
        return {"horizon": horizon, "folds": [], "status": "insufficient"}

    as_ofs = sorted({r.as_of_date for r in rows})
    start, end = as_ofs[0], as_ofs[-1]
    train_days = train_years * 365
    test_days = test_years * 365

    folds: list[WalkForwardFold] = []
    cursor = start
    while cursor + timedelta(days=train_days + test_days) <= end + timedelta(days=1):
        train_start = cursor
        train_end = cursor + timedelta(days=train_days)
        test_start = train_end + timedelta(days=1)
        test_end = train_end + timedelta(days=test_days)

        train = [r for r in rows if train_start <= r.as_of_date <= train_end]
        test = [r for r in rows if test_start <= r.as_of_date <= test_end]

        def rho(rs: list[FutureReturn]) -> tuple[float | None, int]:
            if len(rs) < 3:
                return None, len(rs)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                r, p = stats.spearmanr([x.score for x in rs], [x.future_return for x in rs])
            if math.isnan(r):
                return None, len(rs)
            return float(r), len(rs)

        train_rho, _tn = rho(train)
        test_rho, _xn = rho(test)
        # note why a fold cannot conclude (usually: no future data for recent test snapshots yet)
        note = ""
        if test_rho is None and len(test) >= 3:
            note = "test score-return relationship not measurable (rho=nan)"

        folds.append(WalkForwardFold(
            train_start=train_start, train_end=train_end,
            test_start=test_start, test_end=test_end,
            train_n=len(train), test_n=len(test),
            train_rho=train_rho, test_rho=test_rho, test_note=note,
        ))
        cursor = cursor + timedelta(days=test_days)

    return {
        "horizon": horizon,
        "train_years": train_years,
        "test_years": test_years,
        "folds": [f.to_dict() for f in folds],
        "status": "ok" if folds else "insufficient",
    }


# --------------------------------------------------------------------------- #
# 3. forecast calibration: realised vs predicted, linear alpha/beta
# --------------------------------------------------------------------------- #
@dataclass
class CalibrationResult:
    horizon: str
    n: int
    alpha: float | None          # intercept (bias)
    beta: float | None           # slope (scaling)
    r2: float | None
    mae: float | None
    rmse: float | None
    mean_error: float | None     # mean(actual - predicted)
    by_industry: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def status(self) -> str:
        return "ok" if self.n >= 5 else "insufficient"

    def to_dict(self) -> dict[str, Any]:
        return {
            "horizon": self.horizon,
            "n": self.n,
            "status": self.status,
            "alpha": round(self.alpha, 4) if self.alpha is not None else None,
            "beta": round(self.beta, 4) if self.beta is not None else None,
            "r2": round(self.r2, 4) if self.r2 is not None else None,
            "mae": round(self.mae, 4) if self.mae is not None else None,
            "rmse": round(self.rmse, 4) if self.rmse is not None else None,
            "mean_error": round(self.mean_error, 4) if self.mean_error is not None else None,
            "by_industry": self.by_industry,
        }


def forecast_calibration(
    engine: Engine,
    horizon: str = "12M",
    as_of_max: date | None = None,
) -> CalibrationResult:
    """Fit actual = alpha + beta * predicted on the persisted forecast_errors
    table (predicted = model annualised expected return, actual = realised
    annualised return over the horizon)."""
    factory = make_session_factory(engine)
    recs: list[tuple[str, float, float]] = []  # (industry, predicted, actual)
    with factory() as session:
        q = (
            select(models.ForecastError, models.Security.symbol, models.Security.industry_model)
            .join(models.Security, models.Security.id == models.ForecastError.security_id)
            .where(models.ForecastError.horizon == HORIZONS[horizon])
            .where(models.ForecastError.predicted.isnot(None))
            .where(models.ForecastError.actual.isnot(None))
        )
        if as_of_max:
            q = q.where(models.ForecastError.as_of_date <= as_of_max)
        for fe, _sym, industry in session.execute(q):
            recs.append((industry, float(fe.predicted), float(fe.actual)))

    if len(recs) < 5:
        return CalibrationResult(horizon=horizon, n=len(recs), alpha=None,
                                 beta=None, r2=None, mae=None, rmse=None,
                                 mean_error=None)

    xs = [p for _, p, _ in recs]
    ys = [a for _, _, a in recs]
    n = len(recs)
    xm, ym = sum(xs) / n, sum(ys) / n
    sxy = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    sxx = sum((x - xm) ** 2 for x in xs)
    beta = sxy / sxx if sxx > 1e-12 else None
    alpha = (ym - beta * xm) if beta is not None else None
    if beta is None:
        r2 = None
    else:
        sst = sum((y - ym) ** 2 for y in ys)
        ssr = sum((y - (alpha + beta * x)) ** 2 for y, x in zip(ys, xs))
        r2 = 1.0 - ssr / sst if sst > 1e-12 else None
    errs = [a - p for p, a in zip(xs, ys)]
    mae = sum(abs(e) for e in errs) / n
    rmse = math.sqrt(sum(e * e for e in errs) / n)
    mean_error = sum(errs) / n

    # per-industry summaries
    by_ind: dict[str, dict[str, Any]] = {}
    for ind in {i for i, _, _ in recs}:
        sub = [(p, a) for i, p, a in recs if i == ind]
        if len(sub) < 3:
            by_ind[ind] = {"n": len(sub), "status": "insufficient",
                           "mean_error": None, "mean_abs_error": None}
            continue
        sn = len(sub)
        sxm = sum(p for p, _ in sub) / sn
        symm = sum(a for _, a in sub) / sn
        ssxx = sum((p - sxm) ** 2 for p, _ in sub)
        ssxy = sum((p - sxm) * (a - symm) for p, a in sub)
        b = ssxy / ssxx if ssxx > 1e-12 else None
        a_ = (symm - b * sxm) if b is not None else None
        errs_ = [a - p for p, a in sub]
        by_ind[ind] = {
            "n": sn,
            "status": "ok",
            "alpha": round(a_, 4) if a_ is not None else None,
            "beta": round(b, 4) if b is not None else None,
            "mean_error": round(sum(errs_) / sn, 4),
            "mean_abs_error": round(sum(abs(e) for e in errs_) / sn, 4),
        }

    return CalibrationResult(
        horizon=horizon, n=n, alpha=alpha, beta=beta, r2=r2,
        mae=mae, rmse=rmse, mean_error=mean_error, by_industry=by_ind,
    )


# --------------------------------------------------------------------------- #
# 4. buy-zone: standard vs conservative buy price realised IRR
# --------------------------------------------------------------------------- #
@dataclass
class BuyZoneStats:
    zone: str                     # "standard" / "conservative"
    horizon: str                  # "1Y" / "2Y" / "3Y"
    n: int
    mean_irr: float | None        # annualised IRR realised from buy price
    median_irr: float | None
    hit_rate: float | None        # fraction with irr > 0
    excess_vs_base: float | None  # mean(irr) - mean(hold-to-future return)

    @property
    def status(self) -> str:
        return "ok" if self.n >= 3 else "insufficient"

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone": self.zone,
            "horizon": self.horizon,
            "n": self.n,
            "status": self.status,
            "mean_irr": round(self.mean_irr, 4) if self.mean_irr is not None else None,
            "median_irr": round(self.median_irr, 4) if self.median_irr is not None else None,
            "hit_rate": round(self.hit_rate, 4) if self.hit_rate is not None else None,
            "excess_vs_base": round(self.excess_vs_base, 4)
            if self.excess_vs_base is not None else None,
        }


def buy_zone_irr(
    rows: list[FutureReturn],
    horizons: tuple[str, str, str] = ("1Y", "2Y", "3Y"),
) -> list[BuyZoneStats]:
    """IRR realised by buying at the standard/conservative target price and
    holding to the end of the horizon. Horizons map to trading days."""
    H = {"1Y": 252, "2Y": 504, "3Y": 756}
    out: list[BuyZoneStats] = []

    def stat(zone: str, horizon: str, days: int) -> BuyZoneStats:
        vals: list[float] = []
        hold = []
        for r in rows:
            if r.horizon_days != days:
                continue
            buy = r.standard_buy_price if zone == "standard" else r.conservative_buy_price
            if not buy or buy <= 0:
                continue
            years = days / TRADING_DAYS_PER_YEAR
            irr = (r.future_close / buy) ** (1.0 / years) - 1.0
            vals.append(irr)
            hold.append(r.future_return)
        n = len(vals)
        if n == 0:
            return BuyZoneStats(zone=zone, horizon=horizon, n=0, mean_irr=None,
                                median_irr=None, hit_rate=None, excess_vs_base=None)
        mean_irr = sum(vals) / n
        hold_mean = sum(h / years for h in hold) / n  # annualised hold return
        median_irr = sorted(vals)[n // 2]
        hit = sum(1 for v in vals if v > 0) / n
        return BuyZoneStats(
            zone=zone, horizon=horizon, n=n, mean_irr=mean_irr,
            median_irr=median_irr, hit_rate=hit,
            excess_vs_base=mean_irr - hold_mean,
        )

    for h in horizons:
        days = H[h]
        out.append(stat("standard", h, days))
        out.append(stat("conservative", h, days))
    return out