"""System A research package (Phase 2)."""
from src.research.base import (
    DIMENSIONS,
    DimensionScore,
    FactorScore,
    IndustryScoringModel,
    ResearchResult,
    RiskComponent,
)
from src.research.models import get_model, registered_models
from src.research.scorer import (
    ResearchScorer,
    compute_research_score,
    research_weights,
    risk_penalty_max,
)

__all__ = [
    "DIMENSIONS",
    "DimensionScore",
    "FactorScore",
    "IndustryScoringModel",
    "ResearchResult",
    "RiskComponent",
    "get_model",
    "registered_models",
    "ResearchScorer",
    "compute_research_score",
    "research_weights",
    "risk_penalty_max",
]