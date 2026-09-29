from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.dashboard.services import DEFAULT_WATCHLIST, seed_default_watchlist
from src.db import models


def test_seed_default_watchlist_imports_the_screenshot_symbols_once():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)

    assert seed_default_watchlist(engine) == len(DEFAULT_WATCHLIST)
    assert seed_default_watchlist(engine) == 0

    with Session(engine) as session:
        symbols = set(session.query(models.Security.symbol).all())
        assert ("600406.SH",) in symbols
        assert ("601398.SH",) in symbols
        assert session.get(models.SystemState, "default_watchlist_v1") is not None
