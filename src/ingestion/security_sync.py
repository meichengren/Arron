"""Security Resolver & security sync (spec Section 6.1 steps 1-4 + Section 7 routing)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from src.config.settings import CONFIG_DIR
from src.db.engine import Engine
from src.db.repositories import SecurityRepository
from src.providers.provider_router import ProviderRouter

# Internal industry model enum (spec Section 7.1)
INDUSTRY_MODELS = {
    "BANK",
    "INSURANCE",
    "TECHNOLOGY",
    "CONSUMER",
    "MANUFACTURING",
    "RESOURCES",
    "GENERIC",
}
GENERIC_MODEL = "GENERIC"


def normalize_symbol(raw: str) -> str:
    """Normalize user input to canonical '600036.SH' form.

    Accepts: '600036', '600036.SH', 'sh600036', 'SZ000001', '000001.SZ', '300750'.
    """
    token = raw.strip().upper()
    if not token:
        raise ValueError("empty symbol")
    # strip exchange prefix like SH600036 / SZ000001
    m = re.match(r"^(SH|SZ|BJ)(\d{6})$", token)
    if m:
        return f"{m.group(2)}.{m.group(1)}"
    m = re.match(r"^(\d{6})(\.(SH|SZ|BJ))?$", token)
    if not m:
        raise ValueError(f"invalid symbol format: {raw!r}")
    code = m.group(1)
    suffix = m.group(3)
    if suffix:
        return f"{code}.{suffix}"
    # infer exchange from code prefix
    if code.startswith(("6", "9")):
        return f"{code}.SH"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    if code.startswith(("4", "8")):
        return f"{code}.BJ"
    raise ValueError(f"cannot infer exchange for: {raw!r}")


class IndustryRouter:
    """Route raw industry / company name to an internal model using YAML mapping."""

    def __init__(self, mapping_path: Path | None = None) -> None:
        path = mapping_path or (CONFIG_DIR / "industry_mapping.yaml")
        self._data: dict[str, Any] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                self._data = yaml.safe_load(f) or {}

    def resolve(self, symbol: str, industry_raw: str, company_name: str) -> str:
        overrides = self._data.get("symbol_overrides", {}) or {}
        if symbol in overrides:
            return str(overrides[symbol]).upper()

        haystack = f"{industry_raw} {company_name}".lower()
        for rule in self._data.get("keyword_rules", []) or []:
            model = str(rule.get("model", "")).upper()
            keywords = rule.get("keywords", []) or []
            for kw in keywords:
                if str(kw).lower() in haystack:
                    return model
        return GENERIC_MODEL


class SecuritySyncService:
    """Resolve a user symbol, fetch basic info, store security row with industry model."""

    def __init__(
        self, engine: Engine, router: ProviderRouter, industry_router: IndustryRouter | None = None
    ) -> None:
        self._engine = engine
        self._router = router
        self._repo = SecurityRepository(engine)
        self._industry = industry_router or IndustryRouter()

    def sync(self, raw_symbol: str) -> SecurityResult:
        symbol = normalize_symbol(raw_symbol)
        basic, source = self._router.get_stock_basic(symbol)
        if basic is None:
            raise ValueError(f"security not found: {symbol}")
        model = self._industry.resolve(
            symbol, basic.get("industry_raw", ""), basic.get("name", "")
        )
        values: dict[str, Any] = {
            "symbol": symbol,
            "display_symbol": basic["display_symbol"],
            "name": basic.get("name", ""),
            "exchange": basic.get("exchange", ""),
            "market": "CN_A",
            "industry_raw": basic.get("industry_raw", ""),
            "industry_model": model,
            "list_date": basic.get("list_date"),
            "status": basic.get("status", "ACTIVE"),
        }
        security = self._repo.upsert(values)
        return SecurityResult(security=security, provider=source, industry_model=model)


class SecurityResult:
    def __init__(self, security, provider: str, industry_model: str) -> None:
        self.security = security
        self.provider = provider
        self.industry_model = industry_model