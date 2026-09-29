"""Security Resolver & security sync (spec Section 6.1 steps 1-4 + Section 7 routing)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
 @dataclass(frozen=True)
class AShareSymbol:
    """Canonical identifiers for one supported A-share security."""

    code: str
    exchange: str
    canonical: str
    yfinance: str


_EXCHANGE_ALIASES = {
    "SH": "SH",
    "SS": "SH",
    "SSE": "SH",
    "SHSE": "SH",
    "XSHG": "SH",
    "SZ": "SZ",
    "SZSE": "SZ",
    "XSHE": "SZ",
    "BJ": "BJ",
    "BSE": "BJ",
}
_CODE_PREFIXES = {
    "SH": ("600", "601", "603", "605", "688", "900"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
    "BJ": ("4", "8"),
}


def _contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def _inferred_exchange(code: str) -> str:
    for exchange, prefixes in _CODE_PREFIXES.items():
        if code.startswith(prefixes):
            return exchange
    raise ValueError(f"暂不支持该证券代码: {code}")


def normalize_a_share_symbol(
    raw: str,
    *,
    lookup_by_name: Callable[[str], str | None] | None = None,
) -> AShareSymbol:
    """Normalize code, exchange and vendor formats for a supported A-share."""
    original = str(raw).strip()
    if not original:
        raise ValueError("请输入股票代码或中文股票名称")

    if _contains_chinese(original):
        if lookup_by_name is None:
            raise ValueError(f"无法解析中文股票名称: {original}")
        resolved = lookup_by_name(original)
        if not resolved:
            raise ValueError(f"未找到股票名称: {original}")
        return normalize_a_share_symbol(resolved)

    token = (
        original.upper()
        .replace("。", ".")
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )
    prefix_match = re.fullmatch(r"(SH|SS|SSE|SHSE|SZ|SZSE|BJ|BSE)(\d{6})", token)
    suffix_match = re.fullmatch(r"(\d{6})\.(SH|SS|SSE|SHSE|XSHG|SZ|SZSE|XSHE|BJ|BSE)", token)
    bare_match = re.fullmatch(r"\d{6}", token)

    if prefix_match:
        exchange = _EXCHANGE_ALIASES[prefix_match.group(1)]
        code = prefix_match.group(2)
    elif suffix_match:
        code = suffix_match.group(1)
        exchange = _EXCHANGE_ALIASES[suffix_match.group(2)]
    elif bare_match:
        code = token
        exchange = _inferred_exchange(code)
    else:
        raise ValueError(f"无法识别股票代码: {original}")

    inferred = _inferred_exchange(code)
    if exchange != inferred:
        raise ValueError(f"代码 {code} 不属于 {exchange} 交易所")

    return AShareSymbol(
        code=code,
        exchange=exchange,
        canonical=f"{code}.{exchange}",
        yfinance=f"{code}.SS" if exchange == "SH" else f"{code}.{exchange}",
    )


def normalize_symbol(raw: str) -> str:
    """Backward-compatible canonical symbol helper for existing callers."""
    return normalize_a_share_symbol(raw).canonicalrror(f"cannot infer exchange for: {raw!r}")


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

    def _lookup_symbol_by_name(self, name: str) -> str | None:
        code, _source = self._router.lookup_symbol_by_name(name)
        return code

    def sync(self, raw_symbol: str) -> SecurityResult:
        normalized = normalize_a_share_symbol(
            raw_symbol,
            lookup_by_name=self._lookup_symbol_by_name,
        )
        symbol = normalized.canonical
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