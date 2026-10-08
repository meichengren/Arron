from types import SimpleNamespace

from src.ingestion.security_sync import SecuritySyncService


def test_existing_security_skips_remote_basic_lookup():
    existing = SimpleNamespace(
        symbol="000333.SZ",
        industry_model="GENERIC",
    )

    class Repository:
        def get_by_symbol(self, symbol):
            assert symbol == "000333.SZ"
            return existing

    class Router:
        def get_stock_basic(self, symbol):
            raise AssertionError("existing security must not call remote basic lookup")

    service = object.__new__(SecuritySyncService)
    service._repo = Repository()
    service._router = Router()

    result = service.sync("000333")

    assert result.security is existing
    assert result.provider == "cache"
    assert result.industry_model == "GENERIC"
