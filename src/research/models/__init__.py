"""Industry model registry (Phase 2).

Maps the IndustryRouter output name to a concrete scoring model; unknown/gap
names always fall back to GENERIC so scoring never raises (spec section 7.1).
"""
from __future__ import annotations

from src.research.base import IndustryScoringModel
from src.research.models.bank import BankModel
from src.research.models.consumer import ConsumerModel
from src.research.models.generic import GenericModel
from src.research.models.insurance import InsuranceModel
from src.research.models.manufacturing import ManufacturingModel
from src.research.models.resources import ResourcesModel
from src.research.models.technology import TechnologyModel

_REGISTRY: dict[str, type[IndustryScoringModel]] = {
    model.name: model
    for model in (
        BankModel,
        InsuranceModel,
        TechnologyModel,
        ConsumerModel,
        ManufacturingModel,
        ResourcesModel,
        GenericModel,
    )
}


def get_model(name: str) -> IndustryScoringModel:
    """Return a model instance by industry name; GENERIC as fallback."""
    cls = _REGISTRY.get(str(name).upper(), GenericModel)
    return cls()


def registered_models() -> tuple[str, ...]:
    return tuple(k for k in _REGISTRY if k != "GENERIC")


__all__ = ["get_model", "registered_models"]