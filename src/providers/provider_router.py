"""Provider router: primary -> fallback automatic failover (spec Section 2.1).

Every method returns (result, provider_name) so the ingestion layer can record
the actual data provenance per row.
"""
from __future__ import annotations

from typing import Any, Callable

from src.config.settings import AppSettings
from src.providers.akshare_provider import AkshareProvider
from src.providers.base import DataProvider, ProviderError
from src.providers.tushare_provider import TushareProvider


class ProviderRouter:
    def __init__(
        self,
        settings: AppSettings,
        providers: dict[str, DataProvider] | None = None,
    ) -> None:
        self._settings = settings
        self._providers: dict[str, DataProvider] = providers or {
            "tushare": TushareProvider(settings),
            "akshare": AkshareProvider(settings),
        }
        ds = settings.yaml_config.data_source
        self.auto_failover = ds.auto_failover
        self._primary_name = ds.primary
        self._fallback_name = ds.fallback

    # ------------------------------------------------------------------ #
    def available(self, name: str) -> bool:
        provider = self._providers.get(name)
        return provider is not None and provider.health_check()

    def active_provider(self) -> DataProvider:
        """First healthy provider in (primary, fallback) order."""
        for name in (self._primary_name, self._fallback_name):
            provider = self._providers.get(name)
            if provider is not None and provider.health_check():
                return provider
        raise ProviderError(
            f"No data provider available (primary={self._primary_name}, "
            f"fallback={self._fallback_name})"
        )

    def _call(self, method: str, *args: Any, **kwargs: Any) -> tuple[Any, str]:
        primary = self._providers.get(self._primary_name)
        if primary is not None and primary.health_check():
            try:
                return getattr(primary, method)(*args, **kwargs), primary.name
            except Exception:
                if not self.auto_failover:
                    raise
        fallback = self._providers.get(self._fallback_name)
        if fallback is None:
            raise ProviderError(f"fallback provider '{self._fallback_name}' not registered")
        if not fallback.health_check():
            raise ProviderError("fallback provider unhealthy")
        return getattr(fallback, method)(*args, **kwargs), fallback.name

    # ------------------------------------------------------------------ #
    def health_report(self) -> dict[str, bool]:
        return {name: p.health_check() for name, p in self._providers.items()}

    def get_stock_basic(self, symbol: str) -> tuple[dict[str, Any] | None, str]:
        return self._call("get_stock_basic", symbol)

    def get_daily_bars(
        self, symbol: str, start_date: str, end_date: str
    ) -> tuple[Any, str]:
        return self._call("get_daily_bars", symbol, start_date, end_date)

    def get_daily_basic(
        self, symbol: str, start_date: str, end_date: str
    ) -> tuple[Any, str]:
        return self._call("get_daily_basic", symbol, start_date, end_date)

    def get_financial_reports(self, symbol: str) -> tuple[Any, str]:
        return self._call("get_financial_reports", symbol)