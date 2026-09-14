"""Phase 6 trading friction model + rebalance band (patch item 12).

Instead of chasing the exact target weight every day, we only trade when the
current weight leaves the band around the target. When a trade is triggered we
estimate the transaction costs:

  cost = |delta_value| * (fee_rate + stamp_tax_on_sell + slippage)

If a position change is smaller than ``min_trade_amount`` it is skipped
(no churn on tiny differences).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

from src.portfolio.opportunity import rebalance_band_action


@dataclass(frozen=True)
class TradeLeg:
    symbol: str
    action: str                    # BUY / REDUCE / HOLD
    current_weight: float
    target_weight: float
    delta_value: float             # absolute money to transact (>0), 0 when HOLD
    cost: float                    # transaction cost in money
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "action": self.action,
            "current_weight": round(self.current_weight, 6),
            "target_weight": round(self.target_weight, 6),
            "delta_value": round(self.delta_value, 2),
            "cost": round(self.cost, 2), "reason": self.reason,
        }


@dataclass(frozen=True)
class RebalancePlan:
    as_of: Any
    legs: tuple[TradeLeg, ...]
    turnover: float                # one-way turnover (0..1)
    total_cost: float
    cost_pct: float                # total cost / portfolio value

    @property
    def has_trades(self) -> bool:
        return any(leg.action != "HOLD" for leg in self.legs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if hasattr(self.as_of, "isoformat") else str(self.as_of),
            "legs": [leg.to_dict() for leg in self.legs],
            "turnover": round(self.turnover, 6),
            "total_cost": round(self.total_cost, 2),
            "cost_pct": round(self.cost_pct, 6),
            "has_trades": self.has_trades,
        }


def build_rebalance_plan(
    symbols: Sequence[str],
    current_weights: Sequence[float],
    target_weights: Sequence[float],
    portfolio_value: float,
    band_half: float = 0.03,
    fee_rate: float = 0.0003,
    stamp_tax: float = 0.0005,
    slippage: float = 0.001,
    min_trade_amount: float = 20000.0,
) -> RebalancePlan:
    """Compare current vs target weights per security and plan trades.

    Only weights whose deviation leaves the band produce a leg; legs below
    ``min_trade_amount`` are skipped. Turnover = sum(|delta|)/2 (one-way),
    cost = sum over executed legs of delta_value * rate(s).
    """
    legs: list[TradeLeg] = []
    turnover_sum = 0.0
    cost_sum = 0.0

    for sym, cur, tgt in zip(symbols, current_weights, target_weights):
        action = rebalance_band_action(cur, tgt, band_half=band_half)
        if action == "HOLD":
            legs.append(TradeLeg(sym, "HOLD", cur, tgt, 0.0, 0.0, "band 内不交易"))
            continue
        delta_value = abs(tgt - cur) * portfolio_value
        if delta_value < min_trade_amount:
            legs.append(TradeLeg(
                sym, "HOLD", cur, tgt, 0.0, 0.0,
                f"低于最小调仓金额 {min_trade_amount:,.0f} 元",
            ))
            continue
        rate = fee_rate + (stamp_tax if action == "REDUCE" else 0.0) + slippage
        cost = delta_value * rate
        turnover_sum += delta_value
        cost_sum += cost
        reason = "进入买入区（低于目标区间下限）" if action == "BUY" else "超出目标区间上限"
        legs.append(TradeLeg(sym, action, cur, tgt, delta_value, cost, reason))

    turnover = min(1.0, turnover_sum / (2.0 * portfolio_value)) if portfolio_value > 0 else 0.0
    cost_pct = cost_sum / portfolio_value if portfolio_value > 0 else 0.0
    return RebalancePlan(
        as_of=None, legs=tuple(legs),
        turnover=round(turnover, 6),
        total_cost=round(cost_sum, 2),
        cost_pct=round(cost_pct, 6),
    )


def rebalance_signal_verdict(plan: RebalancePlan, threshold: float = 0.005) -> str:
    """Combined portfolio-level action from a plan.

    threshold: minimum total cost-to-value ratio that justifies trading.
    """
    if not plan.has_trades:
        return "HOLD"
    return "REBALANCE" if plan.cost_pct <= 0.05 else "REBALANCE_HIGH_COST"