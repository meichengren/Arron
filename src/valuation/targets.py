"""Phase 3 industry valuation models - terminal multiple & growth per scenario.

Section 14 primary models:
  BANK          Future BVPS x Target PB            (Gordon P/B cross-check)
  INSURANCE     EV x Target P/EV                   (PE fallback w/ note)
  TECH/CONS/MFG Future EPS x Target PE
  RESOURCES     Normalized EPS x Mid-cycle PE

Each model returns three ScenarioParams (bear/base/bull) plus (optionally) the
conservative-case parameters used by the buy prices (Section 13). All inputs
come from prepared DataPoints; missing inputs yield None + explicit note -
never a disguised zero.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from src.config.settings import CONFIG_DIR
from src.research.datapoints import DataPoints
from src.valuation.scenario import percentile

_QUANTILES = {"bear": 0.30, "base": 0.50, "bull": 0.75}
_SCENARIO_GROWTH = {"bear_factor": 0.40, "bull_factor": 1.50, "bull_floor_pp": 0.03}
_DEFAULTS: dict[str, dict[str, Any]] = {
    "BANK": {"primary": "book_value", "cost_of_equity": 0.10,
             "retention_ratio_default": 0.65, "gordon_growth_floor": 0.0},
    "INSURANCE": {"primary": "ev", "target_pev": 1.0, "pe_fallback": True},
    "TECHNOLOGY": {"primary": "pe"},
    "CONSUMER": {"primary": "pe"},
    "MANUFACTURING": {"primary": "pe"},
    "RESOURCES": {"primary": "normalized_eps", "mid_cycle_pe": 12.0},
    "GENERIC": {"primary": "pe"},
}


@dataclass(frozen=True)
class ScenarioParams:
    key: str                 # "bear" | "base" | "bull"
    growth: float | None     # 3y fundamental growth assumption
    terminal_multiple: float | None
    multiple_type: str       # "PE" | "PB" | "P/EV"
    base_fundamental: float | None  # per-share EPS / BVPS / normalized EPS (CNY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "growth": self.growth,
            "terminal_multiple": self.terminal_multiple,
            "multiple_type": self.multiple_type,
            "base_fundamental": self.base_fundamental,
        }


@dataclass(frozen=True)
class ModelAssumptions:
    bear: ScenarioParams
    base: ScenarioParams
    bull: ScenarioParams
    conservative_growth_factor: float = 0.65  # base growth used for buy price
    conservative_multiple: float | None = None  # min(median, p30) per Section 13
    margin_of_safety_applies: bool = False      # high-volatility industries
    notes: tuple[str, ...] = field(default_factory=tuple)


def _load_valuation_cfg() -> dict[str, Any]:
    try:
        import yaml

        path = CONFIG_DIR / "valuation.yaml"
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _industry_cfg(model_name: str) -> dict[str, Any]:
    cfg = _load_valuation_cfg()
    industries = cfg.get("industries", {}) or {}
    base = dict(_DEFAULTS.get(model_name, _DEFAULTS["GENERIC"]))
    base.update(industries.get(model_name, {}) or {})
    return base


def _safe_div(n: float | None, d: float | None) -> float | None:
    if n is None or d is None or d == 0:
        return None
    return n / d


def _median_and_q(multiple_series: list[float]) -> dict[str, float | None]:
    if not multiple_series:
        return {"p30": None, "median": None, "p75": None}
    return {
        "p30": percentile(multiple_series, 0.30),
        "median": percentile(multiple_series, 0.50),
        "p75": percentile(multiple_series, 0.75),
    }


def build_assumptions(dp: DataPoints, industry_model: str) -> ModelAssumptions:
    """Per industry_model: scenario growth/multiple/base fundamental."""
    cfg = _industry_cfg(industry_model)
    primary = cfg.get("primary", "pe")
    notes: list[str] = []
    missing: list[str] = []

    if primary == "book_value":
        out = _bank_assumptions(dp, cfg, missing, notes)
    elif primary == "ev":
        out = _insurance_assumptions(dp, cfg, missing, notes)
    elif primary == "normalized_eps":
        out = _resources_assumptions(dp, cfg, missing, notes)
    else:
        out = _pe_assumptions(dp, cfg, missing, notes)

    if missing:
        notes.append(f"missing inputs: {', '.join(sorted(set(missing)))}")
    bear, base, bull = out
    return ModelAssumptions(
        bear=bear,
        base=base,
        bull=bull,
        conservative_growth_factor=_conservative_growth_factor(industry_model, cfg),
        conservative_multiple=_conservative_multiple(bear, dp),
        margin_of_safety_applies=_high_volatility(industry_model, dp),
        notes=tuple(notes),
    )


def _conservative_multiple(bear: ScenarioParams, dp: DataPoints) -> float | None:
    """Section 13: terminal multiple = min(history median, 30th percentile)."""
    series = dp.pb_history if bear.multiple_type == "PB" else dp.pe_history
    q = _median_and_q(series)
    if q["median"] is None or q["p30"] is None:
        return None
    return min(q["median"], q["p30"])


def _high_volatility(industry_model: str, dp: DataPoints) -> bool:
    if industry_model == "RESOURCES":
        return True
    if dp.ann_vol is not None and dp.ann_vol >= 40.0:
        return True
    return False


def _conservative_growth_factor(industry_model: str, cfg: dict[str, Any]) -> float:
    """Section 13: base growth haircut 60%-75%, negotiable per industry."""
    lo = 0.60
    hi = 0.75
    val = cfg.get("conservative_growth_factor")
    if val is not None:
        return max(lo, min(hi, float(val)))
    return (lo + hi) / 2.0


def _scenario_growth(base_growth: float | None) -> tuple[float | None, float | None, float | None]:
    """bear/base/bull 3y growth from base growth."""
    if base_growth is None:
        return None, None, None
    bear = base_growth * _SCENARIO_GROWTH["bear_factor"]
    bull = base_growth * _SCENARIO_GROWTH["bull_factor"]
    floor = base_growth + _SCENARIO_GROWTH["bull_floor_pp"]
    bull = max(bull, floor)
    return round(bear, 6), round(base_growth, 6), round(bull, 6)


# --------------------------------------------------------------------------- #
# PE 型行业 (TECHNOLOGY / CONSUMER / MANUFACTURING / GENERIC / INSURANCE PE)
# --------------------------------------------------------------------------- #
def _pe_assumptions(
    dp: DataPoints, cfg: dict[str, Any], missing: list[str], notes: list[str]
) -> tuple[ScenarioParams, ScenarioParams, ScenarioParams]:
    eps = dp.eps_ttm
    if eps is None or eps <= 0:
        missing.append("eps_ttm")
        empty = ScenarioParams("bear", None, None, "PE", None)
        return empty, ScenarioParams("base", None, None, "PE", None), ScenarioParams("bull", None, None, "PE", None)
    base_growth = dp.net_profit_cagr3
    if base_growth is None:
        base_growth = dp.revenue_cagr3
        if base_growth is not None:
            notes.append("net_profit_cagr3 missing -> revenue_cagr3 proxy used")
    if base_growth is None:
        missing.append("cagr3")
        notes.append("no 3y growth data -> flat earnings assumed")
        base_growth = 0.0

    q = _median_and_q(dp.pe_history)
    if q["median"] is None:
        missing.append("pe_history")
    g_bear, g_base, g_bull = _scenario_growth(base_growth)

    def mk(key: str, g: float | None, mult: float | None) -> ScenarioParams:
        return ScenarioParams(key, g, mult, "PE", eps)

    return (
        mk("bear", g_bear, q["p30"] if q["p30"] is not None else q["median"]),
        mk("base", g_base, q["median"]),
        mk("bull", g_bull, q["p75"] if q["p75"] is not None else q["median"]),
    )


# --------------------------------------------------------------------------- #
# BANK：Future BVPS x Target PB（Gordon P/B 交叉校验）
# --------------------------------------------------------------------------- #
def _bank_assumptions(
    dp: DataPoints, cfg: dict[str, Any], missing: list[str], notes: list[str]
) -> tuple[ScenarioParams, ScenarioParams, ScenarioParams]:
    # 当前每股净资产：close / pb（合成 PB 或快照 PB 二选一）
    pb_now = dp.pb if dp.pb is not None else dp.pb_raw
    bvps = _safe_div(dp.close, pb_now)
    if bvps is None or bvps <= 0:
        missing.append("bvps")
        empty = ScenarioParams("bear", None, None, "PB", None)
        return empty, ScenarioParams("base", None, None, "PB", None), ScenarioParams("bull", None, None, "PB", None)

    roe_s = dp.roe_avg3 if dp.roe_avg3 is not None else dp.roe   # sustainable ROE
    if roe_s is None:
        missing.append("roe")
        roe_s = 0.10  # 显式默认并提示
        notes.append("sustainable ROE missing -> 10% default used")
    elif abs(roe_s) > 1.0:
        roe_s = roe_s / 100.0  # datapoints ROE is in percentage points (11.2 = 11.2%)
    ke = float(cfg.get("cost_of_equity", 0.10))
    retention = float(cfg.get("retention_ratio_default", 0.65))

    # 简化 Gordon 合理性校验：P/B ≈ (ROE - g) / (Ke - g)
    g_bvps = max(roe_s * retention, float(cfg.get("gordon_growth_floor", 0.0)))
    q = _median_and_q(dp.pb_history)
    if q["median"] is None:
        missing.append("pb_history")

    def gordon_pb(g: float) -> float | None:
        if ke <= g or roe_s <= g:
            return None
        return (roe_s - g) / (ke - g)

    g_pb = gordon_pb(g_bvps)
    if g_pb is None:
        notes.append("Gordon P/B degenerates (ROE<=g or Ke<=g) -> historical multiple used")

    base_pb = q["median"]
    if g_pb is not None and q["p30"] is not None and q["median"] is not None:
        base_pb = max(q["p30"], min(g_pb, q["median"]))   # 夹在 P30 与中位数之间
    bear_pb = q["p30"] if q["p30"] is not None else q["median"]
    bull_pb = g_pb if g_pb is not None else q["median"]
    if bull_pb is not None and q["median"] is not None:
        bull_pb = max(bull_pb, q["median"])

    g_bear, g_base, g_bull = _scenario_growth(g_bvps)

    def mk(key: str, g: float | None, mult: float | None) -> ScenarioParams:
        return ScenarioParams(key, g, mult, "PB", bvps)

    return (
        mk("bear", g_bear, bear_pb),
        mk("base", g_base, base_pb),
        mk("bull", g_bull, bull_pb),
    )


# --------------------------------------------------------------------------- #
# INSURANCE：EV x Target P/EV，EV 缺失时 PE 降级（带说明）
# --------------------------------------------------------------------------- #
def _insurance_assumptions(
    dp: DataPoints, cfg: dict[str, Any], missing: list[str], notes: list[str]
) -> tuple[ScenarioParams, ScenarioParams, ScenarioParams]:
    ev_ps = dp.get("ev_per_share")  # 预留：数据源提供 EV/股时走 EV 主模型
    if ev_ps is not None and ev_ps > 0:
        base_growth = dp.net_profit_cagr3 or 0.0
        g_bear, g_base, g_bull = _scenario_growth(base_growth)
        target_pev = float(cfg.get("target_pev", 1.0))
        bear_pev = target_pev * 0.85
        bull_pev = target_pev * 1.15

        def mk(key: str, g: float | None, mult: float | None) -> ScenarioParams:
            return ScenarioParams(key, g, mult, "P/EV", ev_ps)

        return (
            mk("bear", g_bear, round(bear_pev, 6)),
            mk("base", g_base, target_pev),
            mk("bull", g_bull, round(bull_pev, 6)),
        )
    if cfg.get("pe_fallback", True):
        notes.append("EV data missing -> PE fallback model (insurance-specific flag EV_DATA_MISSING)")
        missing.append("ev_per_share")
        return _pe_assumptions(dp, cfg, missing, notes)
    missing.append("ev_per_share")
    empty = ScenarioParams("bear", None, None, "P/EV", None)
    return empty, ScenarioParams("base", None, None, "P/EV", None), ScenarioParams("bull", None, None, "P/EV", None)


# --------------------------------------------------------------------------- #
# RESOURCES：Normalized EPS x Mid-cycle PE
# --------------------------------------------------------------------------- #
def _resources_assumptions(
    dp: DataPoints, cfg: dict[str, Any], missing: list[str], notes: list[str]
) -> tuple[ScenarioParams, ScenarioParams, ScenarioParams]:
    # Normalized EPS：TTM 净利润序列中位数（平滑商品周期），退化到当期 EPS
    norm_eps: float | None = None
    if dp.np_ttm_history and dp.shares:
        norm_eps = percentile(dp.np_ttm_history, 0.50)
        if norm_eps is not None:
            norm_eps = norm_eps / dp.shares
    if norm_eps is None or norm_eps <= 0:
        if dp.eps_ttm is not None and dp.eps_ttm > 0:
            norm_eps = dp.eps_ttm
            notes.append("np_ttm_history unavailable -> current EPS used as normalized EPS")
        else:
            missing.append("eps_ttm")
            empty = ScenarioParams("bear", None, None, "PE", None)
            return empty, ScenarioParams("base", None, None, "PE", None), ScenarioParams("bull", None, None, "PE", None)

    mid_cyc = float(cfg.get("mid_cycle_pe", 12.0))
    q = _median_and_q(dp.pe_history)
    base_mult = q["median"] if q["median"] is not None else mid_cyc
    notes.append(f"mid-cycle PE={mid_cyc} blended with 5y PE median {q['median'] if q['median'] is not None else 'n/a'}")

    g_bear, g_base, g_bull = _scenario_growth(dp.net_profit_cagr3)
    if g_base is None:
        missing.append("cagr3")
        g_bear, g_base, g_bull = 0.0, 0.0, 0.0

    bear_mult = q["p30"] if q["p30"] is not None else min(base_mult, mid_cyc * 0.85)
    bull_mult = q["p75"] if q["p75"] is not None else max(base_mult, mid_cyc * 1.15)

    def mk(key: str, g: float | None, mult: float | None) -> ScenarioParams:
        return ScenarioParams(key, g, mult, "PE", norm_eps)

    return (
        mk("bear", g_bear, round(float(bear_mult), 6) if bear_mult is not None else None),
        mk("base", g_base, round(float(base_mult), 6) if base_mult is not None else None),
        mk("bull", g_bull, round(float(bull_mult), 6) if bull_mult is not None else None),
    )


def terminal_value(param: ScenarioParams, horizon_years: int) -> float | None:
    """Future fundamental x Terminal multiple (Section 12 definition)."""
    if param.base_fundamental is None or param.terminal_multiple is None:
        return None
    growth = param.growth or 0.0
    future_fundamental = param.base_fundamental * ((1.0 + growth) ** horizon_years)
    if future_fundamental <= 0:
        return None
    return round(future_fundamental * param.terminal_multiple, 6)