from datetime import date

from src.dashboard import services


def test_pending_batch_reuses_one_provider_router(monkeypatch):
    """A batch must retain free-source caches between pending symbols."""
    rows = [
        {
            "security_id": 1,
            "symbol": "000333.SZ",
            "name": "美的集团",
            "industry_model": "GENERIC",
            "research_score": None,
        },
        {
            "security_id": 2,
            "symbol": "600036.SH",
            "name": "招商银行",
            "industry_model": "GENERIC",
            "research_score": None,
        },
    ]
    received_routers = []
    created_routers = []

    class FakeRouter:
        def __init__(self, settings):
            created_routers.append(self)

    class FakeScorer:
        def __init__(self, engine):
            self.engine = engine

    def fake_add_security_and_research(
        engine, symbol, as_of=None, force_sync=False, router=None
    ):
        received_routers.append(router)
        return {"research_score": 60.0}

    monkeypatch.setattr(services, "library_rows", lambda engine: rows)
    monkeypatch.setattr(services, "ResearchScorer", FakeScorer)
    monkeypatch.setattr(services, "add_security_and_research", fake_add_security_and_research)
    monkeypatch.setattr(services.time, "sleep", lambda seconds: None)

    from src.providers import provider_router

    monkeypatch.setattr(provider_router, "ProviderRouter", FakeRouter)

    result = services.refresh_daily_research_scores(
        object(), as_of=date(2026, 9, 29), sync_data=True, only_pending=True
    )

    assert len(result["succeeded"]) == 2
    assert len(created_routers) == 1
    assert received_routers == [created_routers[0], created_routers[0]]
