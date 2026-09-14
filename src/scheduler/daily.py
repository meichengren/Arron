"""Phase 7 Daily Scheduler pipeline (spec sections 3.3 / 3.4 / 15).

One daily run chains, in order:

    sync (auto data source) -> data quality gate -> research scoring
    -> portfolio decision -> factor exposure -> decision journal

The pipeline is deliberately resilient: a failed sync/score for one symbol is
reported and skipped (the user's default is "skip the error and continue"),
while the quality gate decides whether the *data* is trustworthy enough for
buy signals to be emitted at all.

Scheduling is left to the OS / hosting platform:

  * local Windows  : `scripts/register_daily_task.ps1` (schtasks, daily 16:10)
  * online deploy  : platform cron running `python -m src.cli --daily`
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import get_engine, init_database, make_session_factory
from src.ingestion.quality import run_quality_gate
from src.portfolio.decision import build_decision, persist_decision
from src.portfolio.exposure import (
    compute_exposure_report,
    persist_exposure_report,
)
from src.portfolio.journal import (
    JournalEntry,
    journaled_dates,
    write_journal,
)


@dataclass
class DailyRunReport:
    as_of: date
    batch_id: str
    overall_verdict: str = ""
    blocked_symbols: list[str] = field(default_factory=list)
    symbols_scored: list[str] = field(default_factory=list)
    sync_failures: list[str] = field(default_factory=list)
    portfolio_id: int | None = None
    feasible: bool | None = None
    journal_status: str = ""       # written / skipped-already-journaled / failed
    journal_id: int | None = None
    notes: str = ""
    forecast_refresh: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "batch_id": self.batch_id,
            "overall_verdict": self.overall_verdict,
            "blocked_symbols": self.blocked_symbols,
            "symbols_scored": self.symbols_scored,
            "sync_failures": self.sync_failures,
            "portfolio_id": self.portfolio_id,
            "feasible": self.feasible,
            "journal_status": self.journal_status,
            "journal_id": self.journal_id,
            "forecast_refresh": self.forecast_refresh,
            "notes": self.notes,
        }


def _active_symbols(engine: Engine) -> list[str]:
    factory = make_session_factory(engine)
    with factory() as session:
        rows = session.execute(
            select(models.Security.symbol).where(models.Security.status == "ACTIVE")
        ).scalars().all()
    return list(rows)


def _sync_symbols(engine: Engine, symbols: list[str]) -> list[str]:
    """Auto data-source sync; per-symbol failures are skipped and reported."""
    from src.config.settings import get_settings
    from src.ingestion.financial_sync import FinancialSyncService
    from src.ingestion.market_sync import MarketSyncService
    from src.ingestion.security_sync import SecuritySyncService
    from src.providers.provider_router import ProviderRouter

    settings = get_settings()
    router = ProviderRouter(settings)
    security_svc = SecuritySyncService(engine, router)
    market_svc = MarketSyncService(engine, router)
    financial_svc = FinancialSyncService(engine, router)

    failures: list[str] = []
    for raw in symbols:
        try:
            sec_res = security_svc.sync(raw)
            sec = sec_res.security
            market_svc.sync(
                sec.id, sec.symbol,
                years=settings.yaml_config.sync.market_history_years,
            )
            financial_svc.sync(
                sec.id, sec.symbol,
                years=settings.yaml_config.sync.financial_history_years,
            )
        except Exception as exc:  # noqa: BLE001 - per-symbol best effort
            failures.append(f"{raw}: {exc}")
    return failures


def _research_symbols(engine: Engine, symbols: list[str], as_of: date) -> list[str]:
    from src.research.scorer import ResearchScorer

    scorer = ResearchScorer(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        sec_rows = session.execute(
            select(models.Security).where(models.Security.symbol.in_(symbols))
        ).scalars().all()
    scored: list[str] = []
    for sec in sec_rows:
        try:
            scorer.score_security(
                sec.id, sec.symbol, sec.name, sec.industry_model, as_of, persist=True
            )
            scored.append(sec.symbol)
        except Exception:  # noqa: BLE001 - skip and continue
            continue
    return scored


def _journable_confidence(engine: Engine, symbols: list[str]) -> float | None:
    factory = make_session_factory(engine)
    confs: list[float] = []
    with factory() as session:
        for sym in symbols:
            sec = session.execute(
                select(models.Security).where(models.Security.symbol == sym)
            ).scalar_one_or_none()
            if sec is None:
                continue
            snap = session.execute(
                select(models.ResearchSnapshot.confidence_score)
                .where(models.ResearchSnapshot.security_id == sec.id)
                .order_by(models.ResearchSnapshot.as_of_date.desc())
                .limit(1)
            ).scalar_one_or_none()
            if snap is not None:
                confs.append(float(snap))
    if not confs:
        return None
    return round(sum(confs) / len(confs), 2)


def run_daily_pipeline(
    engine: Engine | None = None,
    portfolio_id: int | None = None,
    symbols: list[str] | None = None,
    as_of: date | None = None,
    benchmark_symbol: str | None = None,
    skip_sync: bool = False,
    notes: str = "",
    batch_id: str | None = None,
) -> DailyRunReport:
    """One full daily automation run: sync -> quality -> research -> decision
    -> exposure -> journal. Never raises on per-symbol problems; the journal
    write is the only step that may surface JournalConflictError (by design).
    """
    engine = engine or get_engine()
    init_database(engine)
    as_of = as_of or date.today()
    batch_id = batch_id or as_of.strftime("%Y%m%d")
    report = DailyRunReport(as_of=as_of, batch_id=batch_id, notes=notes)

    symbols = symbols or _active_symbols(engine)
    if not symbols:
        report.notes += "no active securities; nothing to do. "
        return report

    # 1) sync (auto data source, best effort)
    if not skip_sync:
        report.sync_failures = _sync_symbols(engine, symbols)

    # 2) data quality gate (persisted; verdict gates buy signals downstream)
    quality = run_quality_gate(engine, as_of=as_of, symbols=symbols, batch_id=batch_id)
    report.overall_verdict = quality.overall_verdict
    report.blocked_symbols = quality.blocked_symbols

    # 3) research scoring (Phase 2 scorer, persisted snapshots)
    report.symbols_scored = _research_symbols(engine, symbols, as_of)

    # 4) portfolio decision + snapshot persistence
    if portfolio_id is None:
        factory = make_session_factory(engine)
        with factory() as session:
            pf = session.execute(
                select(models.Portfolio).order_by(models.Portfolio.id).limit(1)
            ).scalar_one_or_none()
        portfolio_id = pf.id if pf is not None else None
    if portfolio_id is not None:
        decision = build_decision(engine, portfolio_id, as_of, benchmark_symbol)
        persist_decision(engine, decision)
        report.portfolio_id = portfolio_id
        report.feasible = decision.feasible

        # 5) factor exposure persistence (Phase 7 factor_exposures rows)
        exposure = compute_exposure_report(engine, portfolio_id, as_of=as_of)
        persist_exposure_report(engine, exposure)

        # 6) immutable decision journal
        regime = decision.regime.regime if decision.regime else ""
        if as_of in journaled_dates(engine, portfolio_id):
            report.journal_status = "skipped-already-journaled"
        else:
            journal_id = write_journal(
                engine,
                JournalEntry(
                    portfolio_id=portfolio_id,
                    as_of_date=as_of,
                    market_regime=regime,
                    feasible=decision.feasible,
                    scores={
                        s.symbol: s.research_score
                        for s in decision.signals if s.research_score is not None
                    },
                    target_weights=decision.weights,
                    signals=[s.to_dict() for s in decision.signals],
                    metrics={
                        "cash_weight": decision.cash_weight,
                        "portfolio_beta": decision.portfolio_beta,
                        "portfolio_vol": decision.portfolio_vol,
                        "expected_return": decision.expected_return,
                        "turnover": decision.turnover,
                        "transaction_cost": decision.transaction_cost,
                        "max_achievable_return": decision.max_achievable_return,
                        "violations": decision.violations,
                        "batch_id": batch_id,
                    },
                    confidence=_journable_confidence(engine, symbols),
                    notes=notes or batch_id,
                ),
            )
            report.journal_id = journal_id
            report.journal_status = "written"

    # 7) Model Lab: refresh resolvable forecast-error pairs (predicted vs
    #    realised) - accumulates daily, drives calibration once enough time
    #    has elapsed. Never blocks the pipeline on failure.
    try:
        from src.model_lab.calibration import refresh_forecast_errors
        report.forecast_refresh = refresh_forecast_errors(engine)
    except Exception:  # noqa: BLE001 - background analytics, best-effort
        report.forecast_refresh = {"error": "refresh skipped"}

    return report