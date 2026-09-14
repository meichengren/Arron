"""Research scorer - the Phase 2 assembly layer.

Pipeline per security & as-of date:
    1. build_data_points(engine, security, as_of) -> look-ahead-safe DataPoints
    2. route industry model (GENERIC fallback, never raises)
    3. model.score() -> six DimensionScores + RiskComponents + risk_flags
    4. aggregate risk (weighted, missing components excluded)
    5. Research Score = sum(w_d * dim_d)  -  (risk/100 * max_risk_penalty)
       (weights from config/scoring.yaml, section 8)
    6. Phase 3 valuation: scenarios / buy prices / expected return (section 11-15)
    7. freshness + confidence; persist snapshot (idempotent by security+as_of)
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from src.config.settings import CONFIG_DIR
from src.db import models
from src.db.engine import init_database, make_session_factory
from src.research.base import (
    DIMENSIONS,
    DimensionScore,
    IndustryScoringModel,
    ResearchResult,
    RiskComponent,
)
from src.research.confidence import (
    compute_confidence_score,
    compute_data_freshness_score,
)
from src.research.datapoints import build_data_points
from src.research.models import get_model
from src.research.risk import aggregate_risk

_DEFAULT_WEIGHTS: dict[str, float] = {
    "fundamental": 0.30,
    "quality": 0.15,
    "growth": 0.20,
    "valuation": 0.20,
    "cycle": 0.10,
    "shareholder": 0.05,
}
_DEFAULT_RISK_PENALTY_MAX = 15.0


def _load_weights() -> tuple[dict[str, float], float]:
    """Read six-dimension weights + risk penalty from scoring.yaml (cached)."""
    try:
        import yaml

        path = CONFIG_DIR / "scoring.yaml"
        if not path.exists():
            return dict(_DEFAULT_WEIGHTS), _DEFAULT_RISK_PENALTY_MAX
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        research = data.get("research", {}) or {}
        weights = {k: float(research.get(k, _DEFAULT_WEIGHTS[k])) for k in DIMENSIONS}
        penalty = float((data.get("risk_penalty", {}) or {}).get("max_points", _DEFAULT_RISK_PENALTY_MAX))
        return weights, penalty
    except Exception:
        return dict(_DEFAULT_WEIGHTS), _DEFAULT_RISK_PENALTY_MAX


_WEIGHTS, _RISK_PENALTY = None, None


def research_weights() -> dict[str, float]:
    global _WEIGHTS
    if _WEIGHTS is None:
        _WEIGHTS, _ = _load_weights()
    return dict(_WEIGHTS)


def risk_penalty_max() -> float:
    global _RISK_PENALTY
    if _RISK_PENALTY is None:
        _, _RISK_PENALTY = _load_weights()
    return _RISK_PENALTY


def compute_research_score(
    dimensions: tuple[DimensionScore, ...],
    risk_score: float,
    weights: dict[str, float] | None = None,
    risk_penalty_points: float | None = None,
) -> float:
    """Section 8 formula: weighted six dims minus risk penalty points."""
    w = weights or research_weights()
    dim_map = {d.key: d.score for d in dimensions}
    base = sum(w.get(k, 0.0) * dim_map.get(k, 50.0) for k in DIMENSIONS)
    max_points = risk_penalty_points if risk_penalty_points is not None else _load_weights()[1]
    penalty = max_points * (risk_score / 100.0)
    return round(min(100.0, max(0.0, base - penalty)), 2)


class ResearchScorer:
    """Facade used by CLI / dashboard / tests to score and persist one security."""

    def __init__(self, engine: Engine, weights_yaml: str | None = None) -> None:
        self._engine = engine
        init_database(engine)
        if weights_yaml:
            self._weights, self._penalty = self._read_custom_yaml(weights_yaml)
        else:
            self._weights, self._penalty = _load_weights()

    @staticmethod
    def _read_custom_yaml(path: str) -> tuple[dict[str, float], float]:
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        research = data.get("research", {}) or {}
        weights = {k: float(research.get(k, _DEFAULT_WEIGHTS[k])) for k in DIMENSIONS}
        penalty = float((data.get("risk_penalty", {}) or {}).get("max_points", _DEFAULT_RISK_PENALTY_MAX))
        return weights, penalty

    def score_security(
        self,
        security_id: int,
        symbol: str,
        name: str,
        industry_model: str,
        as_of: date,
        persist: bool = True,
        model: IndustryScoringModel | None = None,
    ) -> ResearchResult:
        dp = build_data_points(self._engine, security_id, symbol, name, industry_model, as_of)

        m = model or get_model(industry_model)
        dimensions, risk_comps, flags = m.score({"dp": dp}, as_of)

        risk_score = aggregate_risk(list(risk_comps))
        research_score = compute_research_score(
            dimensions, risk_score, self._weights, self._penalty
        )

        # ---- Phase 3 valuation (sections 11-15) --------------------------- #
        # local import: src.valuation.service -> src.research.datapoints and
        # src.research.__init__ -> scorer form an import cycle at module level
        from src.valuation.service import ValuationService

        valuation = ValuationService().value(dp, m.name)

        model_missing = []
        for d in dimensions:
            model_missing.extend(d.missing_keys)
        missing_union = list(dict.fromkeys(dp.missing_keys + model_missing + list(valuation.missing_keys)))
        confidence = compute_confidence_score(dp, missing_union)
        freshness = compute_data_freshness_score(dp)

        result = ResearchResult(
            security_id=security_id,
            symbol=symbol,
            name=name,
            industry_model=m.name,
            as_of_date=as_of,
            model_version=m.version,
            dimensions=tuple(dimensions),
            risk_components=tuple(risk_comps),
            risk_score=risk_score,
            research_score=research_score,
            confidence_score=confidence,
            data_freshness_score=freshness,
            risk_flags=tuple(flags),
            extra={
                "latest_period": dp.latest_period.isoformat() if dp.latest_period else None,
                "latest_ann": dp.latest_ann.isoformat() if dp.latest_ann else None,
                "report_age_days": dp.report_age_days,
                "close": dp.close,
                "pe_ttm": dp.pe_ttm,
                "pb": dp.pb,
                "dv_ratio": dp.dv_ratio,
                "roe": dp.roe,
                "missing_keys": dp.missing_keys,
                "shares_simplified": dp.shares is not None,
                "weights": {k: self._weights[k] for k in DIMENSIONS},
                "risk_penalty_max": self._penalty,
            },
            valuation=valuation,
        )

        if persist:
            self.persist(result)
        return result

    def persist(self, result: ResearchResult) -> None:
        """Idempotent upsert: replace the snapshot for (security_id, as_of_date)."""
        factory = make_session_factory(self._engine)
        with factory() as session:
            session.execute(
                delete(models.ResearchSnapshot).where(
                    models.ResearchSnapshot.security_id == result.security_id,
                    models.ResearchSnapshot.as_of_date == result.as_of_date,
                )
            )
            session.flush()
            dims = result.scores_dict()
            session.add(
                models.ResearchSnapshot(
                    security_id=result.security_id,
                    as_of_date=result.as_of_date,
                    model_version=result.model_version,
                    fundamental_score=dims.get("fundamental"),
                    quality_score=dims.get("quality"),
                    growth_score=dims.get("growth"),
                    valuation_score=dims.get("valuation"),
                    cycle_score=dims.get("cycle"),
                    shareholder_score=dims.get("shareholder"),
                    risk_score=result.risk_score,
                    research_score=result.research_score,
                    # Phase 3 valuation outputs (Sections 11-15)
                    expected_return_base=_v(result.valuation, "expected_return_base"),
                    expected_return_bear=_v(result.valuation, "expected_return_bear"),
                    expected_return_bull=_v(result.valuation, "expected_return_bull"),
                    fair_value_bear=_v(result.valuation, "fair_value_bear"),
                    fair_value_base=_v(result.valuation, "fair_value_base"),
                    fair_value_bull=_v(result.valuation, "fair_value_bull"),
                    standard_buy_price=_v(result.valuation, "standard_buy_price"),
                    conservative_buy_price=_v(result.valuation, "conservative_buy_price"),
                    confidence_score=result.confidence_score,
                    data_freshness_score=result.data_freshness_score,
                    risk_flags_json=_json(result.risk_flags),
                    explanation_json=_json(result.explanation()),
                )
            )
            session.commit()

    def latest_snapshot(self, security_id: int) -> models.ResearchSnapshot | None:
        factory = make_session_factory(self._engine)
        with factory() as session:
            row = session.execute(
                select(models.ResearchSnapshot)
                .where(models.ResearchSnapshot.security_id == security_id)
                .order_by(models.ResearchSnapshot.as_of_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            return row


def _json(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


def _v(valuation, key: str):
    """Extract a valuation output field (None-safe)."""
    if valuation is None:
        return None
    return getattr(valuation, key, None)