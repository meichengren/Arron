# -*- coding: utf-8 -*-
"""Phase 5 portfolio risk engine.

Pure statistics on aligned daily returns: beta (long / rolling / downside),
correlations (normal / down-market), vol, drawdowns (historical / stress /
forward), VaR/CVaR, downside deviation, HHI concentration, and per-stock
risk contribution. Nothing here touches the database - the service layer feeds
in aligned return matrices.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

# Windows default is fine; trading windows are small so this is exact anyway.
from src.config.settings import CONFIG_DIR


@dataclass(frozen=True)
class BetaStats:
    long_beta: float = 0.0
    beta_60d: float | None = None
    beta_120d: float | None = None
    beta_252d: float | None = None
    downside_beta: float | None = None
    r2: float | None = None
    obs: int = 0
    flag: str = ""  # "", "LOW_HISTORY", "ELEVATED", "DEFENSIVE"


@dataclass(frozen=True)
class PortfolioRisk:
    """Complete Phase 5 risk snapshot (the ≥10 Section 25 items + patch adds)."""

    expected_return: float | None = None
    portfolio_beta: float = 0.0
    annualized_volatility: float = 0.0
    max_drawdown_estimate: float = 0.0       # historical MDD
    stress_mdd: float | None = None          # worst stress scenario DD
    forward_mdd: float | None = None         # estimated forward range (lower)
    var_95: float = 0.0
    cvar_95: float = 0.0
    downside_deviation: float = 0.0
    concentration_hhi: float = 0.0
    normal_corr_avg: float | None = None     # average pairwise correlation
    stress_corr_avg: float | None = None     # correlation in worst 20% days
    downside_beta: float | None = None       # portfolio downside beta
    cash_weight: float = 0.0
    risk_contrib: dict[str, float] = field(default_factory=dict)  # symbol -> pct
    liquidity_flag: str = ""                 # "", "LIQUID", "ILLIQUID", "LOW_HISTORY"
    risk_level: str = ""                     # LOW / MODERATE / ELEVATED / HIGH / CRITICAL


def _annualize(daily_std: float) -> float:
    return daily_std * float(np.sqrt(252.0))


def _cov_var(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Covariance and variance of two aligned return arrays (sample, ddof=0)."""
    n = len(x)
    if n < 2:
        return 0.0, 0.0
    mx, my = x.mean(), y.mean()
    cov = float(((x - mx) * (y - my)).sum() / n)
    var = float(((y - my) ** 2).sum() / n)
    return cov, var


def beta_stats(stock: np.ndarray, bench: np.ndarray) -> BetaStats:
    """Long-history beta + rolling 60/120/252 + downside beta + R².

    ``stock`` and ``bench`` are aligned daily return vectors (already masked to
    complete pairs, in the same order). Returns zero-ish stats when there are
    fewer than 2 observations; ``flag`` reports LOW_HISTORY when < 252 obs.
    """
    n = len(stock)
    if n < 2:
        return BetaStats(beta_60d=None, beta_120d=None, beta_252d=None,
                         downside_beta=None, r2=None, obs=n, flag="LOW_HISTORY")
    cov, var = _cov_var(stock, bench)
    if var <= 0.0:
        return BetaStats(beta_60d=None, beta_120d=None, beta_252d=None,
                         downside_beta=None, r2=None, obs=n, flag="LOW_HISTORY")
    long_beta = cov / var
    r2 = (cov / (np.std(stock) * np.std(bench))) ** 2 if np.std(stock) > 0 and np.std(bench) > 0 else None

    def _roll(window: int) -> float | None:
        if n < window:
            return None
        c, v = _cov_var(stock[-window:], bench[-window:])
        return c / v if v > 0 else None

    b60, b120, b252 = _roll(60), _roll(120), _roll(252)

    # downside beta: only days when benchmark return < 0
    down = bench < 0.0
    if int(down.sum()) >= 20:
        c, v = _cov_var(stock[down], bench[down])
        down_beta = c / v if v > 0 else None
    else:
        down_beta = None

    flag = ""
    if n < 252:
        flag = "LOW_HISTORY"
    elif b60 is not None and b252 is not None:
        if b60 > b252 * 1.35 and b60 - b252 > 0.3:
            flag = "ELEVATED"
        elif b60 < b252 * 0.65 and b252 - b60 > 0.3:
            flag = "DEFENSIVE"

    return BetaStats(
        long_beta=round(float(long_beta), 3),
        beta_60d=round(b60, 3) if b60 is not None else None,
        beta_120d=round(b120, 3) if b120 is not None else None,
        beta_252d=round(b252, 3) if b252 is not None else None,
        downside_beta=round(float(down_beta), 3) if down_beta is not None else None,
        r2=round(float(r2), 3) if r2 is not None else None,
        obs=n,
        flag=flag,
    )


def normal_correlations(matrix: np.ndarray) -> float | None:
    """Average pairwise Pearson correlation of the return matrix columns."""
    n = matrix.shape[1]
    if n < 2:
        return None
    corr = np.corrcoef(matrix, rowvar=False)
    vals = [corr[i, j] for i in range(n) for j in range(i + 1, n)]
    if not vals:
        return None
    return round(float(np.mean(vals)), 3)


def down_market_correlations(matrix: np.ndarray, bench: np.ndarray, worst_quantile: float = 0.20) -> float | None:
    """Average pairwise correlation restricted to the worst ``worst_quantile``
    benchmark days (stress correlation, per the patch)."""
    q = float(np.quantile(bench, worst_quantile))
    mask = bench <= q
    if int(mask.sum()) < 10:
        return None
    return normal_correlations(matrix[mask])


def historical_max_drawdown(prices: np.ndarray) -> float:
    """Max drawdown in % (negative)."""
    if len(prices) < 2:
        return 0.0
    running_max = np.maximum.accumulate(prices)
    dd = (prices / running_max - 1.0) * 100.0
    return round(float(dd.min()), 2)


def forward_drawdown_estimate(ann_vol: float, horizon_years: float = 1.0, conf: float = 0.95) -> float:
    """McNeil-style forward drawdown estimate: -z * sigma * sqrt(horizon)."""
    from scipy.stats import norm

    z = float(norm.ppf(conf))
    return round(-z * ann_vol * float(np.sqrt(horizon_years)), 2)


def var_cvar(returns: np.ndarray, conf: float = 0.95) -> tuple[float, float]:
    """Historical VaR95 and CVaR95 in % of the portfolio daily return."""
    if len(returns) < 20:
        return 0.0, 0.0
    q = float(np.quantile(returns, 1.0 - conf))
    var = -q * 100.0
    tail = returns[returns <= q]
    cvar = -float(tail.mean()) * 100.0 if len(tail) else var
    return round(var, 2), round(cvar, 2)


def downside_deviation(returns: np.ndarray, mar: float = 0.0) -> float:
    """Annualized downside deviation (%) vs a MAR (default 0)."""
    if len(returns) < 2:
        return 0.0
    dev = np.minimum(returns - mar, 0.0)
    return round(float(np.sqrt((dev ** 2).mean()) * np.sqrt(252.0) * 100.0), 2)


def concentration_hhi(weights: Sequence[float]) -> float:
    w = np.asarray(weights, dtype=float)
    return round(float((w ** 2).sum()), 4)


def risk_contribution(returns: np.ndarray, weights: np.ndarray) -> dict[int, float]:
    """Marginal risk contributions (%) by column index: RC_i = w_i * (Σw)/σ² * (Σw_j σ_ij).

    Uses the empirical covariance matrix of the aligned daily returns.
    """
    n = len(weights)
    if n < 2 or returns.shape[0] < 3:
        return {i: 0.0 for i in range(n)}
    cov = np.cov(returns, rowvar=False)
    sigma2 = float(weights.T @ cov @ weights)
    if sigma2 <= 0.0:
        return {i: 0.0 for i in range(n)}
    contrib = (weights * (cov @ weights)) / sigma2
    total = float(contrib.sum())
    out: dict[int, float] = {}
    for i in range(n):
        pct = 100.0 * (float(contrib[i]) / total if total != 0 else 0.0)
        out[i] = round(pct, 2)
    return out


def portfolio_annual_vol(returns: np.ndarray, weights: np.ndarray) -> float:
    """Portfolio annualized volatility via wᵀΣw (%)."""
    if returns.shape[0] < 3:
        return 0.0
    cov = np.cov(returns, rowvar=False)
    var_p = float(weights.T @ cov @ weights)
    if var_p < 0:
        var_p = 0.0
    return round(float(np.sqrt(var_p) * np.sqrt(252.0) * 100.0), 2)


def portfolio_beta_from_stats(weights: np.ndarray, betas: Sequence[float]) -> float:
    """Portfolio beta = Σ w_i β_i (fallback when we only have per-stock betas)."""
    if not betas:
        return 0.0
    return round(float(np.asarray(weights) @ np.asarray(betas)), 3)


def simple_expected_return(ann_returns: Sequence[float], weights: np.ndarray) -> float | None:
    """Weighted annual return estimate from per-stock annualized returns."""
    if not ann_returns or len(ann_returns) != len(weights):
        return None
    return round(float(np.asarray(ann_returns) @ np.asarray(weights)), 2)


# --------------------------------------------------------------------------- #
# Config helpers
# --------------------------------------------------------------------------- #
def load_benchmark_symbol() -> str:
    try:
        import yaml

        p = CONFIG_DIR / "risk.yaml"
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return str(cfg.get("benchmark", "000300.SH"))
    except Exception:  # noqa: BLE001
        pass
    return "000300.SH"


def load_risk_levels() -> dict[str, dict]:
    try:
        import yaml

        p = CONFIG_DIR / "risk.yaml"
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            return cfg.get("risk_levels", {}) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def classify_risk_level(
    beta: float,
    vol: float,  # annualized volatility in PERCENT (e.g. 22.3 = 22.3%)
    max_single_weight: float,
    weights: Sequence[float],
    cash_weight: float,
    max_industry_weight: float = 0.50,
) -> str:
    """Section 30 classification from risk.yaml thresholds.

    Each level sets the *maximum* allowed beta/vol/single weight; the level is
    the strictest tier whose limits all hold. Breaching even the elevated
    limits with both beta and vol marks the portfolio critical.
    """
    lv = load_risk_levels()
    lo = lv.get("low", {})
    md = lv.get("moderate", {})
    el = lv.get("elevated", {})
    hi = lv.get("high", {})

    vol_dec = vol / 100.0  # percent -> decimal to match yaml

    # highest (least strict) tier -> strictest tier
    tiers = []
    if beta <= float(el.get("max_beta", 1.20)) and vol_dec <= float(el.get("max_vol", 0.28)):
        tiers.append("elevated")
    if beta <= float(md.get("max_beta", 1.05)) and vol_dec <= float(md.get("max_vol", 0.22)):
        tiers.append("moderate")
    if (
        beta <= float(lo.get("max_beta", 0.85))
        and vol_dec <= float(lo.get("max_vol", 0.18))
        and max(weights or [0.0]) <= float(lo.get("max_single_weight", 0.25))
    ):
        tiers.append("low")

    if tiers:
        # pick the strictest qualifying tier; order above is loosely -> strict
        return tiers[-1]

    # Outside all tiers: high when industry concentration is the breaker,
    # critical when both beta and vol simultaneously blow through elevated.
    if beta > float(el.get("max_beta", 1.20)) and vol_dec > float(el.get("max_vol", 0.28)):
        return "critical"
    if cash_weight >= float(hi.get("max_industry_weight", 0.50)):
        return "high"
    return "elevated"  # single-dimension breach stays elevated-ish