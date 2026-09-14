# -*- coding: utf-8 -*-
"""Phase 5 stress-test engine (patch item 8).

Simulates what the portfolio would lose under market / sector shocks:
  A. CSI300 -20%             (broad market)
  B. AI sector -30%          (hits TECHNOLOGY / AI_INFRASTRUCTURE names)
  C. Copper -25%             (hits RESOURCES with copper exposure)
  D. Bank/property risk      (hits BANK / INSURANCE)
  E. Risk-Off combo          (CSI300 -20% + copper -20% + AI -30%)

Each scenario is a JSON row in stress_scenarios (seeded idempotently with the
five presets) so users can add custom ones. Expected loss per stock derives
from its long beta against the benchmark shock plus a sector overlay.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory

# Sector overlay shocks (+ = extra underperformance beyond market beta effect)
_SECTOR_OVERLAY: dict[str, float] = {
    "TECHNOLOGY": 0.10,      # AI crash hits tech twice as hard as the market
    "RESOURCES": 0.05,
    "BANK": 0.03,
    "INSURANCE": 0.03,
}

_PRESET_SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "CSI300_M20",
        "description": "情景A：沪深300 -20%（市场普跌）",
        "benchmark_pct": -0.20,
        "sector_shocks": {},
    },
    {
        "name": "AI_M30",
        "description": "情景B：AI 板块 -30%（科技成长大幅下杀）",
        "benchmark_pct": -0.15,
        "sector_shocks": {"TECHNOLOGY": -0.30},
    },
    {
        "name": "COPPER_M25",
        "description": "情景C：铜价 -25%（资源股承压）",
        "benchmark_pct": -0.10,
        "sector_shocks": {"RESOURCES": -0.25},
    },
    {
        "name": "BANK_PROPERTY_RISK",
        "description": "情景D：银行地产风险重燃（金融板块受影响）",
        "benchmark_pct": -0.12,
        "sector_shocks": {"BANK": -0.18, "INSURANCE": -0.15},
    },
    {
        "name": "RISK_OFF_COMBO",
        "description": "情景E：Risk-Off 联合冲击（沪深300 -20% + 铜 -20% + AI -30%）",
        "benchmark_pct": -0.20,
        "sector_shocks": {"TECHNOLOGY": -0.30, "RESOURCES": -0.20},
    },
]


@dataclass
class StressScenarioDef:
    name: str
    description: str
    benchmark_pct: float
    sector_shocks: dict[str, float] = field(default_factory=dict)


def load_preset_scenarios(engine: Engine) -> list[StressScenarioDef]:
    """Idempotently seed the five presets and return all active scenarios."""
    factory = make_session_factory(engine)
    with factory() as session:
        existing = set(
            session.execute(select(models.StressScenario.name)).scalars().all()
        )
        for p in _PRESET_SCENARIOS:
            if p["name"] in existing:
                continue
            session.add(
                models.StressScenario(
                    name=p["name"],
                    factors_json=json.dumps(
                        {
                            "benchmark_pct": p["benchmark_pct"],
                            "sector_shocks": p["sector_shocks"],
                        },
                        ensure_ascii=False,
                    ),
                    description=p["description"],
                    active=True,
                )
            )
        session.commit()

        rows = session.execute(
            select(models.StressScenario).where(models.StressScenario.active.is_(True))
        ).scalars().all()
    out: list[StressScenarioDef] = []
    for r in rows:
        try:
            factors = json.loads(r.factors_json or "{}")
        except (ValueError, TypeError):
            factors = {}
        out.append(
            StressScenarioDef(
                name=r.name,
                description=r.description,
                benchmark_pct=float(factors.get("benchmark_pct", 0.0)),
                sector_shocks=dict(factors.get("sector_shocks", {}) or {}),
            )
        )
    return out


class StressEngine:
    """Pure portfolio shock simulation (no DB writes beyond seeded scenarios)."""

    def __init__(
        self,
        engine: Engine | None = None,
        sector_overlay: dict[str, float] | None = None,
    ) -> None:
        self._engine = engine
        self._overlay = dict(sector_overlay or _SECTOR_OVERLAY)

    def run(
        self,
        holdings: Sequence[Any],
        base_betas: dict[str, float],
        bench_return: object,  # accepted for future use (unused in simple model)
        bench_pct: float,
        scenarios: Sequence[StressScenarioDef],
    ) -> dict[str, dict[str, Any]]:
        """Return {scenario_name: {scenario_pct, per_stock, note}}.

        Stock shock = beta * bench_pct + sector overlay; portfolio shock is the
        weighted sum. No beta available -> stock shock = bench_pct (neutral).
        """
        out: dict[str, dict[str, Any]] = {}
        for sc in scenarios:
            per_stock: dict[str, float] = {}
            port = 0.0
            for h in holdings:
                beta = base_betas.get(h.symbol, 1.0)
                shock = beta * sc.benchmark_pct
                overlay = sc.sector_shocks.get(h.industry_model, 0.0)
                shock += overlay
                stock_pct = shock
                per_stock[h.symbol] = round(stock_pct * 100.0, 2)
                port += h.weight * stock_pct
            out[sc.name] = {
                "scenario_pct": round(port * 100.0, 2),
                "per_stock": per_stock,
                "note": sc.description,
            }
        return out


def run_stress_with_db(
    engine: Engine,
    holdings: Sequence[Any],
    base_betas: dict[str, float],
    scenarios: Sequence[StressScenarioDef] | None = None,
) -> dict[str, dict[str, Any]]:
    eng = StressEngine(engine)
    scens = scenarios if scenarios is not None else load_preset_scenarios(engine)
    return eng.run(
        holdings=holdings,
        base_betas=base_betas,
        bench_return=None,
        bench_pct=1.0,
        scenarios=scens,
    )