"""Phase 3 valuation engine (Sections 11-15)."""
from src.valuation.scenario import (
    PRICE_STATE_ATTRACTIVE,
    PRICE_STATE_DEEP_VALUE,
    PRICE_STATE_EXPENSIVE,
    PRICE_STATE_FAIR,
    PRICE_STATE_VERY_EXPENSIVE,
    ScenarioCase,
    ValuationResult,
)
from src.valuation.service import ValuationService, value_security
from src.valuation.targets import build_assumptions, terminal_value

__all__ = [
    "PRICE_STATE_ATTRACTIVE",
    "PRICE_STATE_DEEP_VALUE",
    "PRICE_STATE_EXPENSIVE",
    "PRICE_STATE_FAIR",
    "PRICE_STATE_VERY_EXPENSIVE",
    "ScenarioCase",
    "ValuationResult",
    "ValuationService",
    "value_security",
    "build_assumptions",
    "terminal_value",
]