"""Security resolver and security sync for mainland equities and exchange-traded funds."""
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

GENERIC_MODEL = "GENERIC"

_EXCHANGE_ALIASES = {
    "SH": "SH", "SS": "SH", "SSE": "SH", "SHSE": "SH", "XSHG": "SH",
    "SZ": "SZ", "SZSE": "SZ", "XSHE": "SZ", "BJ": "BJ", "BSE": "BJ",
}
_STOCK_PREFIXES = {
    "SH": ("600", "601", "603", "605", "688", "900"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
    "BJ": ("4", "8"),
}
_ETF_PREFIXES = {
    "SH": ("510", "511", "512", "513", "515", "518", "588"),
    "SZ": ("159",),
}


@dataclass(frozen=True)
class CNSymbol:
    code: str
    exchange: str
    canonical: str
    yfinance: str
    asset_type: str


AShareSymbol = CNSymbol


def _contains_chinese(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def _asset_for_code(code: str) -> tuple[str, str]:
    for exchange, prefixes in _ETF_PREFIXES.items():
        if code.startswith(prefixes):
            return exchange, "ETF"
    for exchange, prefixes in _STOCK_PREFIXES.items():
        if code.startswith(prefixes):
            return exchange, "STOCK"
    raise ValueError(f"暂不支持该证券代码: {code}")


def normalize_cn_symbol(
    raw: str,
    *,
    lookup_by_name: Callable[[str], str | None] | None = None,
) -> CNSymbol:
    """Normalize supported A-share or on-exchange ETF input."""
    original = str(raw).strip()
    if not original:
        raise ValueError("请输入证券代码或中文名称")
    if _contains_chinese(original):
        if lookup_by_name is None:
            raise ValueError(f"无法解析中文名称: {original}")
        resolved = lookup_by_name(original)
        if not resolved:
            raise ValueError(f"未找到证券名称: {original}")
        return normalize_cn_symbol(resolved)

    token = original.upper().replace("。", ".").replace("_", "").replace("-", "").replace(" ", "")
    prefix_match = re.fullmatch(r"(SH|SS|SSE|SHSE|SZ|SZSE|BJ|BSE)(\d{6})", token)
    suffix_match = re.fullmatch(r"(\d{6})\.(SH|SS|SSE|SHSE|XSHG|SZ|SZSE|XSHE|BJ|BSE|ETF)", token)
    bare_match = re.fullmatch(r"\d{6}", token)
    if prefix_match:
        exchange = _EXCHANGE_ALIASES[prefix_match.group(1)]
        code = prefix_match.group(2)
    elif suffix_match:
        code = suffix_match.group(1)
        suffix = suffix_match.group(2)
        exchange = None if suffix == "ETF" else _EXCHANGE_ALIASES[suffix]
    elif bare_match:
        code = token
        exchange = None
    else:
        raise ValueError(f"无法识别证券代码: {original}")

    inferred_exchange, asset_type = _asset_for_code(code)
    if exchange is not None and exchange != inferred_exchange:
        raise ValueError(f"代码 {code} 不属于 {exchange} 交易所")
    return CNSymbol(
        code=code,
        exchange=inferred_exchange,
        canonical=f"{code}.{inferred_exchange}",
        yfinance=f"{code}.SS" if inferred_exchange == "SH" else f"{code}.{inferred_exchange}",
        asset_type=asset_type,
    )


def normalize_a_share_symbol(
    raw: str,
    *,
    lookup_by_name: Callable[[str], str | None] | None = None,
) -> AShareSymbol:
    """Backward-compatible A-share-only normalizer."""
    result = normalize_cn_symbol(raw, lookup_by_name=lookup_by_name)
    if result.asset_type != "STOCK":
        raise ValueError(f"请使用 ETF 输入路径: {raw}")
    return result


def normalize_symbol(raw: str) -> str:
    return normalize_cn_symbol(raw).canonical


class IndustryRouter:
    def __init__(self, mapping_path: Path | None = None) -> None:
        path = mapping_path or (CONFIG_DIR / "industry_mapping.yaml")
        self._data: dict[str, Any] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as stream:
                self._data = yaml.safe_load(stream) or {}

    def resolve(self, symbol: str, industry_raw: str, company_name: str) -> str:
        overrides = self._data.get("symbol_overrides", {}) or {}
        if symbol in overrides:
            return str(overrides[symbol]).upper()
        haystack = f"{industry_raw} {company_name}".lower()
        for rule in self._data.get("keyword_rules", []) or []:
            for keyword in rule.get("keywords", []) or []:
                if str(keyword).lower() in haystack:
                    return str(rule.get("model", GENERIC_MODEL)).upper()
        return GENERIC_MODEL


class SecurityResult:
    def __init__(self, security, provider: str, industry_model: str) -> None:
        self.security = security
        self.provider = provider
        self.industry_model = industry_model


class SecuritySyncService:
    """Resolve a supported symbol and store the canonical security row."""

    def __init__(
        self, engine: Engine, router: ProviderRouter, industry_router: IndustryRouter | None = None
    ) -> None:
        self._router = router
        self._repo = SecurityRepository(engine)
        self._industry = industry_router or IndustryRouter()

    def _lookup_symbol_by_name(self, name: str) -> str | None:
        code, _source = self._router.lookup_symbol_by_name(name)
        return code

    def sync(self, raw_symbol: str) -> SecurityResult:
        normalized = normalize_cn_symbol(raw_symbol, lookup_by_name=self._lookup_symbol_by_name)
        basic, source = self._router.get_stock_basic(normalized.canonical)
        if basic is None:
            raise ValueError(f"security not found: {normalized.canonical}")
        model = self._industry.resolve(
            normalized.canonical, basic.get("industry_raw", ""), basic.get("name", "")
        )
        values: dict[str, Any] = {
            "symbol": normalized.canonical,
            "display_symbol": basic.get("display_symbol", normalized.code),
            "name": basic.get("name", ""),
            "exchange": basic.get("exchange", normalized.exchange),
            "market": "CN_ETF" if normalized.asset_type == "ETF" else "CN_A",
            "industry_raw": basic.get("industry_raw", ""),
            "industry_model": model,
            "list_date": basic.get("list_date"),
            "status": basic.get("status", "ACTIVE"),
        }
        security = self._repo.upsert(values)
        return SecurityResult(security=security, provider=source, industry_model=model)
