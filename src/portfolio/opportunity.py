"""Phase 6 Opportunity Score (per security) + risk-budget portfolio optimizer.

Opportunity Score (V1.0 spec Section 19, patched):
  30% ExpectedReturn + 20% ResearchScore + 15% MarginOfSafety
  + 10% Momentum - 10% BetaPenalty - 7% VolPenalty - 5% DrawdownPenalty
  - 3% LiquidityPenalty

All sub-scores are normalized to [0, 100] before weighting so the result is
comparable across stocks and regimes. The optimizer solves the risk-budget
problem: maximize risk-adjusted expected return subject to the risk budget
(beta / volatility / single-weight / cash) set on the Portfolio + regime;
if the target return is not reachable it reports TARGET NOT FEASIBLE with the
best achievable return.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from scipy.optimize import minimize

# --------------------------------------------------------------------------- #
# Opportunity Score
# --------------------------------------------------------------------------- #
_WEIGHTS = {
    "expected_return": 0.30,
    "research": 0.20,
    "margin": 0.15,
    "momentum": 0.10,
    "beta": -0.10,
    "volatility": -0.07,
    "drawdown": -0.05,
    "liquidity": -0.03,
}

_SUM_POSITIVE = sum(v for v in _WEIGHTS.values() if v > 0)  # 0.75


def _clip01(x: float | None, lo: float = 0.0, hi: float = 1.0) -> float:
    if x is None:
        return 0.0
    return max(lo, min(hi, float(x)))


def opportunity_score(
    expected_return: float | None = None,      # adjusted ER (decimal)
    research_score: float | None = None,       # 0-100
    margin_of_safety: float | None = None,     # 0-1 (1 - current/std_buy)
    momentum_60d: float | None = None,         # decimal return over 60d
    beta: float | None = None,                 # current (rolling) beta
    annual_vol: float | None = None,           # per-stock annual vol (decimal)
    max_drawdown: float | None = None,         # historical MDD (negative decimal)
    liquidity_ratio: float | None = None,      # 0-1 (turnover/amount normalized)
    er_cap: float = 0.30,
    mom_cap: float = 0.30,
    vol_cap: float = 0.60,
) -> float:
    """Weighted opportunity score in [0, 100].

    - expected_return: linear up to ``er_cap`` (30% -> 100)
    - research_score: /100
    - margin_of_safety: 0.25+ -> 100 (a 25% margin gives full marks)
    - momentum: linear up to ``mom_cap``
    - beta penalty: beta >= 1.5 -> 100 penalty
    - volatility penalty: annual vol >= ``vol_cap`` -> 100 penalty
    - drawdown penalty: MDD <= -40% -> 100 penalty
    - liquidity penalty: (1 - liquidity_ratio)*100, liquidity_ratio below 1
    """
    er_s = _clip01(expected_return / er_cap) * 100.0 if expected_return is not None else 0.0
    research_s = _clip01((research_score or 0.0) / 100.0) * 100.0
    margin_s = _clip01((margin_of_safety or 0.0) / 0.25) * 100.0
    mom_s = _clip01((momentum_60d or 0.0) / mom_cap) * 100.0
    beta_s = _clip01((beta or 0.0) / 1.5) * 100.0
    vol_s = _clip01((annual_vol or 0.0) / vol_cap) * 100.0
    dd_s = _clip01((-(max_drawdown or 0.0)) / 0.40) * 100.0
    liq_s = (1.0 - _clip01(liquidity_ratio)) * 100.0 if liquidity_ratio is not None else 0.0

    raw = (
        _WEIGHTS["expected_return"] * er_s
        + _WEIGHTS["research"] * research_s
        + _WEIGHTS["margin"] * margin_s
        + _WEIGHTS["momentum"] * mom_s
        + _WEIGHTS["beta"] * beta_s
        + _WEIGHTS["volatility"] * vol_s
        + _WEIGHTS["drawdown"] * dd_s
        + _WEIGHTS["liquidity"] * liq_s
    )
    return round(max(0.0, min(100.0, raw / _SUM_POSITIVE)), 2)


def opportunity_tags(score: float) -> str:
    """Map an opportunity score to a BUY-side tag."""
    if score >= 75:
        return "STRONG_OPPORTUNITY"
    if score >= 55:
        return "OPPORTUNITY"
    if score >= 40:
        return "NEUTRAL"
    return "POOR"


# --------------------------------------------------------------------------- #
# Risk-budget optimizer
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptimizationResult:
    feasible: bool
    target_return: float | None = None
    max_achievable_return: float | None = None
    weights: dict[str, float] | None = None      # symbol -> weight (stocks only)
    cash_weight: float = 0.0
    portfolio_beta: float | None = None
    portfolio_vol: float | None = None
    expected_return: float | None = None
    violations: list[str] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "feasible": self.feasible,
            "target_return": self.target_return,
            "max_achievable_return": self.max_achievable_return,
            "weights": self.weights,
            "cash_weight": self.cash_weight,
            "portfolio_beta": self.portfolio_beta,
            "portfolio_vol": self.portfolio_vol,
            "expected_return": self.expected_return,
            "violations": list(self.violations),
        }


class RiskBudgetOptimizer:
    """Maximize expected return under the risk budget.

    Decision variables: w = [w_1..w_n, cash] with sum(w) = 1 and
      - cash_min <= w_cash <= cash_max        (regime-dependent)
      - w_i <= max_single_weight
      - sum_i w_i * beta_i <= max_beta
      - w^T Sigma w <= max_vol^2
    """

    def __init__(
        self,
        symbols: Sequence[str],
        expected_returns: Sequence[float | None],
        betas: Sequence[float | None],
        annual_vols: Sequence[float | None],
        correlations: np.ndarray | None = None,   # n x n correlation matrix
        max_beta: float = 1.00,
        max_volatility: float = 0.22,
        max_single_weight: float = 0.30,
        cash_min: float = 0.05,
        cash_max: float = 0.20,
        target_return: float | None = None,
    ) -> None:
        self.symbols = list(symbols)
        n = len(self.symbols)
        self.er = np.array([(v if v is not None else 0.0) for v in expected_returns])
        self.beta = np.array([(v if v is not None else 1.0) for v in betas])
        self.vol = np.array([(v if v is not None else 0.25) for v in annual_vols])
        if correlations is None:
            correlations = np.eye(n) + 0.25 * (np.ones((n, n)) - np.eye(n))
        corr = np.asarray(correlations, dtype=float)
        # covariance = diag(vol) @ corr @ diag(vol)
        v = np.diag(self.vol)
        self.cov = v @ corr @ v
        self.max_beta = max_beta
        self.max_volatility = max_volatility
        self.max_single = max_single_weight
        self.cash_min = cash_min
        self.cash_max = cash_max
        self.target_return = target_return

    # ------------------------------------------------------------------ #
    def solve(self, x0: np.ndarray | None = None) -> OptimizationResult:
        n = len(self.symbols)
        if n == 0:
            return OptimizationResult(feasible=False, violations=["no holdings"])

        def objective(x: np.ndarray) -> float:
            w = x[:n]
            # tiny ridge keeps SLSQP from stalling at the boundary of a
            # linear objective ("Positive directional derivative").
            return -(float(w @ self.er) - 1e-8 * float(w @ w))

        def constraint_sum(x: np.ndarray) -> float:
            return float(np.sum(x) - 1.0)

        def constraint_beta(x: np.ndarray) -> float:
            w = x[:n]
            return float(self.max_beta - w @ self.beta)

        def constraint_vol(x: np.ndarray) -> float:
            w = x[:n]
            return float(self.max_volatility ** 2 - w @ self.cov @ w)

        def constraint_single(x: np.ndarray, i: int) -> float:
            return float(self.max_single - x[i])

        constraints = [
            {"type": "eq", "fun": constraint_sum},
            {"type": "ineq", "fun": constraint_beta},
            {"type": "ineq", "fun": constraint_vol},
            # stock total must remain inside [1-cash_max, 1-cash_min]
            {"type": "ineq", "fun": lambda x: float(np.sum(x[:n]) - (1.0 - self.cash_max))},
            {"type": "ineq", "fun": lambda x: float((1.0 - self.cash_min) - np.sum(x[:n]))},
        ]
        for i in range(n):
            constraints.append({"type": "ineq", "fun": lambda x, i=i: constraint_single(x, i)})

        bounds = [(0.0, self.max_single)] * n + [(self.cash_min, self.cash_max)]

        starts: list[np.ndarray] = []
        if x0 is not None:
            starts.append(np.asarray(x0, dtype=float))
        # default: equal split at mid cash
        stock = (1.0 - (self.cash_min + self.cash_max) / 2.0) / max(n, 1)
        base = np.array(
            [min(stock, self.max_single)] * n + [(self.cash_min + self.cash_max) / 2.0]
        )
        if sum(base[:n]) > 1.0 - self.cash_min:
            base[:n] = np.minimum(base[:n] * (1.0 - self.cash_min) / sum(base[:n]), self.max_single)
        starts.append(base)
        # cash-at-min variant (aggressive) and cash-at-max variant (defensive)
        for cw in (self.cash_min, self.cash_max):
            w = np.array([(1.0 - cw) / max(n, 1)] * n + [cw])
            if sum(w[:n]) > 1.0 - self.cash_min:
                w[:n] = np.minimum(w[:n] * (1.0 - self.cash_min) / sum(w[:n]), self.max_single)
            starts.append(w)

        best: OptimizationResult | None = None
        for s0 in starts:
            res = minimize(
                objective, s0, method="SLSQP",
                bounds=bounds, constraints=constraints,
                options={"maxiter": 600, "ftol": 1e-12},
            )
            cand = self._evaluate(res.x, res.message if not res.success else "")
            if best is None or (cand.expected_return or 0.0) > (best.expected_return or -1e9):
                if not cand.violations:
                    best = cand
        if best is None:
            best = self._evaluate(starts[0], "no feasible start found")
        b = best
        return OptimizationResult(
            feasible=b.feasible,
            target_return=self.target_return,
            max_achievable_return=b.max_achievable_return,
            weights=b.weights,
            cash_weight=b.cash_weight,
            portfolio_beta=b.portfolio_beta,
            portfolio_vol=b.portfolio_vol,
            expected_return=b.expected_return,
            violations=b.violations,
        )

    # ------------------------------------------------------------------ #
    def _evaluate(self, x: np.ndarray, warn: str = "") -> OptimizationResult:
        """Check a candidate solution against all risk-budget constraints."""
        n = len(self.symbols)
        w = np.clip(x[:n], 0.0, self.max_single)
        cash = float(np.clip(x[n], self.cash_min, self.cash_max))
        stock_sum = float(np.sum(w))
        needed = 1.0 - cash
        if stock_sum > 0:
            scale = needed / stock_sum
            if scale > 1.0 + 1e-9:
                # cannot fill stock budget without breaking single-weight caps
                w = w * min(scale, 1.0)
                # allow the shortfall to be absorbed by cash
                cash = max(self.cash_min, 1.0 - float(np.sum(w)))
            else:
                w = w * scale

        port_beta = float(w @ self.beta)
        port_vol = float(np.sqrt(w @ self.cov @ w))
        port_er = float(w @ self.er)

        violations: list[str] = []
        if port_beta > self.max_beta + 1e-6:
            violations.append(f"beta {port_beta:.3f} > max {self.max_beta}")
        if port_vol > np.sqrt(self.max_volatility ** 2) + 1e-6:
            violations.append(f"vol {port_vol:.3f} > max {self.max_volatility}")
        if cash < self.cash_min - 1e-9 or cash > self.cash_max + 1e-9:
            violations.append(f"cash {cash:.3f} outside [{self.cash_min}, {self.cash_max}]")
        if violations and warn:
            violations.append(warn)

        feasible = (
            not violations
            and (self.target_return is None or port_er >= self.target_return - 1e-9)
        )
        return OptimizationResult(
            feasible=feasible,
            target_return=self.target_return,
            max_achievable_return=round(port_er, 4),
            weights={s: round(float(wi), 6) for s, wi in zip(self.symbols, w) if wi > 1e-6},
            cash_weight=round(cash, 6),
            portfolio_beta=round(port_beta, 4),
            portfolio_vol=round(port_vol, 4),
            expected_return=round(port_er, 4),
            violations=violations,
        )


def rebalance_band_action(
    current_weight: float,
    target_weight: float,
    band_half: float = 0.03,
) -> str:
    """Patch item 12: only trade when the weight leaves the band.

    band = [target - band_half, target + band_half].
    """
    if current_weight < target_weight - band_half:
        return "BUY"
    if current_weight > target_weight + band_half:
        return "REDUCE"
    return "HOLD"