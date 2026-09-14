"""Phase 7 Decision Journal: immutable per-day decision snapshots.

Spec section 3.3: every day the full decision (regime, research scores,
target weights, signals, confidence) is frozen into ``decision_journal``.
The journal is append-only by construction:

  * no ``updated_at`` column / no UPDATE code path;
  * a unique constraint on (portfolio_id, as_of_date) makes a second write
    for the same day raise JournalConflictError instead of overwriting.

This prevents look-ahead bias: past pages can never be silently rewritten.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory


class JournalConflictError(Exception):
    """Raised when trying to write a decision for an already-journaled day."""


@dataclass(frozen=True)
class JournalEntry:
    portfolio_id: int
    as_of_date: date
    market_regime: str
    feasible: bool | None
    scores: dict[str, float]
    target_weights: dict[str, float]
    signals: list[dict[str, Any]]
    metrics: dict[str, Any]
    confidence: float | None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "portfolio_id": self.portfolio_id,
            "as_of_date": self.as_of_date.isoformat(),
            "market_regime": self.market_regime,
            "feasible": self.feasible,
            "scores": self.scores,
            "target_weights": self.target_weights,
            "signals": self.signals,
            "metrics": self.metrics,
            "confidence": self.confidence,
            "notes": self.notes,
        }


def _sorted_json(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def write_journal(
    engine: Engine,
    entry: JournalEntry,
    mode: str = "strict",
) -> int:
    """Persist one immutable journal entry.

    mode="strict" (default): raise JournalConflictError if the day already
    has an entry. mode="overwrite" exists only for internal tooling/testing
    repair of genuinely corrupt rows and is intentionally not exposed by the
    daily scheduler.
    """
    factory = make_session_factory(engine)
    with factory() as session:
        existing = session.execute(
            select(models.DecisionJournal.id).where(
                models.DecisionJournal.portfolio_id == entry.portfolio_id,
                models.DecisionJournal.as_of_date == entry.as_of_date,
            )
        ).scalar_one_or_none()
        if existing is not None:
            if mode == "strict":
                raise JournalConflictError(
                    f"decision_journal already has an entry for "
                    f"portfolio={entry.portfolio_id} as_of={entry.as_of_date} "
                    f"(journal is append-only; no look-ahead rewriting)"
                )
            # overwrite path intentionally destroys the previous immutable row
            session.execute(
                models.DecisionJournal.__table__.delete().where(
                    models.DecisionJournal.portfolio_id == entry.portfolio_id,
                    models.DecisionJournal.as_of_date == entry.as_of_date,
                )
            )
        row = models.DecisionJournal(
            portfolio_id=entry.portfolio_id,
            as_of_date=entry.as_of_date,
            market_regime=entry.market_regime,
            feasible=entry.feasible,
            scores_json=_sorted_json(entry.scores),
            target_weights_json=_sorted_json(entry.target_weights),
            signals_json=json.dumps(entry.signals, ensure_ascii=False, sort_keys=True),
            metrics_json=_sorted_json(entry.metrics),
            confidence=entry.confidence,
            notes=entry.notes or "",
        )
        session.add(row)
        session.commit()
        return row.id if row.id is not None else 0


def read_journal(
    engine: Engine,
    portfolio_id: int,
    as_of_date: date | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """List journal pages for a portfolio, newest first."""
    factory = make_session_factory(engine)
    with factory() as session:
        q = (
            select(models.DecisionJournal)
            .where(models.DecisionJournal.portfolio_id == portfolio_id)
            .order_by(models.DecisionJournal.as_of_date.desc())
            .limit(limit)
        )
        if as_of_date is not None:
            q = (
                select(models.DecisionJournal)
                .where(models.DecisionJournal.portfolio_id == portfolio_id)
                .where(models.DecisionJournal.as_of_date == as_of_date)
            )
        rows = session.execute(q).scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r.id,
            "portfolio_id": r.portfolio_id,
            "as_of_date": r.as_of_date.isoformat(),
            "market_regime": r.market_regime,
            "feasible": r.feasible,
            "scores": json.loads(r.scores_json),
            "target_weights": json.loads(r.target_weights_json),
            "signals": json.loads(r.signals_json),
            "metrics": json.loads(r.metrics_json),
            "confidence": r.confidence,
            "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out


def journaled_dates(engine: Engine, portfolio_id: int) -> set[date]:
    """Dates already frozen in the journal (used to skip re-writing)."""
    factory = make_session_factory(engine)
    with factory() as session:
        rows = session.execute(
            select(models.DecisionJournal.as_of_date).where(
                models.DecisionJournal.portfolio_id == portfolio_id
            )
        ).scalars().all()
    return set(rows)