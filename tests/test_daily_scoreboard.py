from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.dashboard.services import delete_security, ranked_research_rows
from src.db import models


def _engine():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    return engine


def _seed_security(session: Session, symbol: str, name: str) -> models.Security:
    security = models.Security(
        symbol=symbol,
        display_symbol=symbol.split(".")[0],
        name=name,
        exchange="SSE",
    )
    session.add(security)
    session.flush()
    return security


def test_ranked_research_rows_orders_highest_score_first():
    engine = _engine()
    with Session(engine) as session:
        low = _seed_security(session, "600036.SH", "招商银行")
        high = _seed_security(session, "000001.SZ", "平安银行")
        session.add_all(
            [
                models.ResearchSnapshot(
                    security_id=low.id, as_of_date=date.today(), research_score=65.0
                ),
                models.ResearchSnapshot(
                    security_id=high.id, as_of_date=date.today(), research_score=88.0
                ),
            ]
        )
        session.commit()

    rows = ranked_research_rows(engine)

    assert [row["symbol"] for row in rows] == ["000001.SZ", "600036.SH"]


def test_delete_security_removes_the_selected_security():
    engine = _engine()
    with Session(engine) as session:
        security = _seed_security(session, "600036.SH", "招商银行")
        security_id = security.id
        session.commit()

    assert delete_security(engine, security_id) is True

    with Session(engine) as session:
        assert session.get(models.Security, security_id) is None
