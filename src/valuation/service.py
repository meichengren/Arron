"""Phase 3 valuation service - assembles the full Section 11-15 output.

Given prepared DataPoints and an industry model name, produces fair values for
the three scenarios, the two buy prices, scenario-weighted expected return and
the Section 17 price state. Explicitly missing inputs yield None plus a note;
never a disguised zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.config.settings import CONFIG_DIR
from src.research.datapoints import DataPoints
from src.valuation.scenario import ScenarioCase, ValuationResult, cagr, price_state, pv_terminal, weighted_expected_return
from src.valuation.targets import ModelAssumptions, build_assumptions, terminal_value

_BEAR_LABEL = "悲观"
_BASE_LABEL = "中性"
_BULL_LABEL = "乐观"


def _load_cfg() -> dict[str, Any]:
    try:
        import yaml

        path = CONFIG_DIR / "valuation.yaml"
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


@dataclass(frozen=True)
class ConservativePriceInputs:
    """Section 13 buy-price inputs (kept separate for unit testing)."""
    growth: float | None          # hair-cut base growth for the conservative case
    multiple: float | None        # min(median, p30) per Section 13
    required_return: float
    margin_of_safety: float       # 5% for high-vol industries, else 0.0
    base_fundamental: float | None


class ValuationService:
    """Pure-function facade; UI / CLI decide how to display the result."""

    def __init__(self, cfg: dict[str, Any] | None = None, model_name: str | None = None) -> None:
        self._cfg = dict(cfg or _load_cfg())
        self._model_name = model_name  # pinned industry model (tests) if given

    # ------------------------------------------------------------------
    def value(self, dp: DataPoints, industry_model: str | None = None) -> ValuationResult:
        model = (industry_model or self._model_name or "GENERIC").upper()
        assumptions = build_assumptions(dp, model)

        horizon = int(self._cfg.get("horizon_years", 3))
        std_r = float(self._cfg.get("standard_required_return", 0.12))
        cons_r = float(self._cfg.get("conservative_required_return", 0.15))
        weights = dict(self._cfg.get("scenario_weights", {"bear": 0.25, "base": 0.50, "bull": 0.25}))

        notes: list[str] = []
        price = dp.close
        dps = self._dividends_per_share(dp, price, horizon, assumptions.base.growth, notes)

        # ---- three scenarios: terminal value + CAGR ----------------------- #
        fv: dict[str, float | None] = {}
        er: dict[str, float | None] = {}
        cases: list[ScenarioCase] = []
        for key, param, label in (
            ("bear", assumptions.bear, _BEAR_LABEL),
            ("base", assumptions.base, _BASE_LABEL),
            ("bull", assumptions.bull, _BULL_LABEL),
        ):
            tv = terminal_value(param, horizon)
            fv[key] = tv
            er[key] = cagr(tv, dps, price, horizon) if (tv is not None and price is not None) else None
            cases.append(
                ScenarioCase(
                    key=key, label=label,
                    growth=param.growth, terminal_multiple=param.terminal_multiple,
                    multiple_type=param.multiple_type, terminal_value=tv,
                    dividends_per_share=dps, cagr=er[key],
                )
            )

        # ---- Section 12: standard buy price ------------------------------- #
        standard_buy = pv_terminal(fv["base"], dps, std_r, horizon) if fv["base"] is not None else None

        # ---- Section 13: conservative buy price --------------------------- #
        cons_inputs = self._conservative_inputs(dp, assumptions, cons_r, horizon)
        cons_buy: float | None = None
        cons_er: float | None = None
        if (
            cons_inputs.base_fundamental is not None
            and cons_inputs.multiple is not None
            and cons_inputs.growth is not None
        ):
            future = cons_inputs.base_fundamental * ((1.0 + cons_inputs.growth) ** horizon)
            if future > 0:
                cons_buy = (future * cons_inputs.multiple + dps) / ((1.0 + cons_inputs.required_return) ** horizon)
                if cons_inputs.margin_of_safety > 0:
                    cons_buy = cons_buy * (1.0 - cons_inputs.margin_of_safety)
                cons_buy = round(cons_buy, 6)
                # V1.1: IRR if bought at today's price and the conservative
                # (hair-cut fundamental, lower multiple) target materialises.
                cons_er = cagr(future * cons_inputs.multiple, dps, price, horizon)
            else:
                notes.append("conservative case: future fundamental <= 0 -> no conservative buy price")

        # ---- Section 17: price state -------------------------------------- #
        state = price_state(price, cons_buy, standard_buy, fv["base"], fv["bull"])
        mo_s = cons_inputs.margin_of_safety if cons_inputs.margin_of_safety > 0 else None

        weighted = weighted_expected_return(er["bear"], er["base"], er["bull"], weights)

        missing = self._collect_missing(assumptions, price, dp)

        if dps == 0.0 and dp.dv_ratio is None:
            notes.append("dividend data missing -> dividends assumed 0")
            missing.append("dv_ratio")

        all_notes = list(dict.fromkeys(list(assumptions.notes) + notes))

        return ValuationResult(
            industry_model=model,
            current_price=price,
            horizon_years=horizon,
            fair_value_bear=fv["bear"],
            fair_value_base=fv["base"],
            fair_value_bull=fv["bull"],
            standard_buy_price=standard_buy,
            conservative_buy_price=cons_buy,
            expected_return_bear=er["bear"],
            expected_return_base=er["base"],
            expected_return_bull=er["bull"],
            expected_return=weighted,
            expected_return_conservative=cons_er,
            price_state=state,
            scenarios=tuple(cases),
            margin_of_safety_pct=mo_s,
            notes=tuple(all_notes),
            missing_keys=tuple(sorted(set(missing))),
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _collect_missing(assumptions: ModelAssumptions, price: float | None, dp: DataPoints) -> list[str]:
        out: list[str] = []
        for n in assumptions.notes:
            if n.startswith("missing inputs: "):
                out.extend(x.strip() for x in n[len("missing inputs: "):].split(","))
        if price is None:
            out.append("close")
        if (dp.eps_ttm is None or dp.eps_ttm <= 0) and assumptions.base.multiple_type == "PE":
            out.append("eps_ttm")
        return out

    # ------------------------------------------------------------------
    @staticmethod
    def _dividends_per_share(
        dp: DataPoints,
        price: float | None,
        horizon: int,
        growth: float | None,
        notes: list[str],
    ) -> float:
        """3y accumulated dividends per share (grows with the fundamental)."""
        if dp.dv_ratio is None or dp.dv_ratio <= 0 or price is None:
            return 0.0
        dps0 = dp.dv_ratio * price
        if growth is not None and growth != 0.0 and growth > -1.0:
            total = dps0 * (((1.0 + growth) ** horizon - 1.0) / growth)
        else:
            total = dps0 * horizon
        return round(max(0.0, total), 6)

    def _conservative_inputs(
        self,
        dp: DataPoints,
        assumptions: ModelAssumptions,
        required_return: float,
        horizon: int,
    ) -> ConservativePriceInputs:
        """Section 13: hair-cut growth + min(median, p30) multiple + MoS."""
        mo_s = 0.0
        if assumptions.margin_of_safety_applies:
            mo_s = float(self._cfg.get("high_volatility_margin_of_safety", 0.05))
        growth = assumptions.base.growth
        if growth is not None:
            growth = growth * assumptions.conservative_growth_factor
        return ConservativePriceInputs(
            growth=growth,
            multiple=assumptions.conservative_multiple,
            required_return=required_return,
            margin_of_safety=mo_s,
            base_fundamental=assumptions.base.base_fundamental,
        )


def value_security(dp: DataPoints, industry_model: str, cfg: dict[str, Any] | None = None) -> ValuationResult:
    """Convenience entry used by CLI / dashboard / tests."""
    return ValuationService(cfg).value(dp, industry_model)