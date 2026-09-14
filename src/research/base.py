"""System A research domain: immutable result model + industry scoring contract.

Section 8 unified output: every industry model produces dimension scores on
0-100 plus research/risk/confidence aggregates with a machine-readable
explanation trail.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - circular-import guard for type hints
    from src.valuation.scenario import ValuationResult

# Six dimensions under the unified framework (Section 8)
DIMENSIONS: tuple[str, ...] = (
    "fundamental",
    "quality",
    "growth",
    "valuation",
    "cycle",
    "shareholder",
)

DIMENSION_LABELS: dict[str, str] = {
    "fundamental": "基本面",
    "quality": "质量",
    "growth": "成长",
    "valuation": "估值",
    "cycle": "周期",
    "shareholder": "股东回报",
}


@dataclass(frozen=True)
class FactorScore:
    """One industry-specific factor inside a dimension.

    ``weight`` is the factor's weight bump inside its dimension before
    re-normalization; ``contribution`` = score * weight (normalized later).
    """
    key: str
    label: str
    raw: float | None
    score: float | None
    weight: float
    method: str  # "percentile" | "piecewise" | "zscore" | "designated"
    direction: str  # "higher" | "lower" | "optimal"
    missing: bool = False
    note: str = ""


@dataclass(frozen=True)
class DimensionScore:
    key: str
    label: str
    score: float  # 0-100 after weight re-normalization over available factors
    factors: tuple[FactorScore, ...] = field(default_factory=tuple)
    missing_keys: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "score": self.score,
            "factors": [asdict(f) for f in self.factors],
            "missing_keys": list(self.missing_keys),
        }


@dataclass(frozen=True)
class RiskComponent:
    key: str
    label: str
    score: float  # 0-100, higher = more risk
    weight: float
    evidence: str = ""
    missing: bool = False


@dataclass(frozen=True)
class ResearchResult:
    security_id: int
    symbol: str
    name: str
    industry_model: str
    as_of_date: date
    model_version: str
    dimensions: tuple[DimensionScore, ...]
    risk_components: tuple[RiskComponent, ...]
    risk_score: float
    research_score: float
    confidence_score: float
    data_freshness_score: float
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    extra: dict[str, Any] = field(default_factory=dict)
    valuation: Any | None = None  # Phase 3 ValuationResult (see src/valuation)

    def score(self, key: str) -> float:
        for d in self.dimensions:
            if d.key == key:
                return d.score
        raise KeyError(f"unknown dimension: {key}")

    def scores_dict(self) -> dict[str, float]:
        out = {d.key: d.score for d in self.dimensions}
        out["risk_score"] = self.risk_score
        out["research_score"] = self.research_score
        out["confidence_score"] = self.confidence_score
        return out

    def explanation(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "industry_model": self.industry_model,
            "model_version": self.model_version,
            "as_of_date": self.as_of_date.isoformat(),
            "dimensions": [d.to_dict() for d in self.dimensions],
            "risks": [
                {
                    "key": r.key,
                    "label": r.label,
                    "score": r.score,
                    "evidence": r.evidence,
                }
                for r in self.risk_components
            ],
            "risk_flags": list(self.risk_flags),
            "valuation": self.valuation.to_dict() if self.valuation is not None else None,
            "extra": dict(self.extra),
        }


class IndustryScoringModel(ABC):
    """Contract every industry model implements.

    Implementations read prepared data points (see datapoints.py) produced by
    the caller; they never touch the database directly, which keeps them
    testable with plain dicts.
    """

    name: str = "GENERIC"
    version: str = "V1.0"

    @abstractmethod
    def score(
        self, data: dict[str, Any], as_of: date
    ) -> tuple[tuple[DimensionScore, ...], tuple[RiskComponent, ...], tuple[str, ...]]:
        """Return (dimensions, risk_components, risk_flags) for prepared data."""
        raise NotImplementedError