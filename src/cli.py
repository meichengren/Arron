"""Phase 1/2 acceptance CLI:
    python -m src.cli 600036 [601318 ...]          # sync data pipeline
    python -m src.cli --research 600036 [601318]    # Phase 2 research scoring

Phase 1 acceptance: entering 600036 stores the security, market bars,
valuation indicators and financial reports into SQLite.
Phase 2 acceptance: the four seeded stocks get explainable scores.
"""
from __future__ import annotations

import argparse
from datetime import date
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.settings import get_settings  # noqa: E402
from src.db.engine import get_engine, init_database, make_engine  # noqa: E402
from src.ingestion.financial_sync import FinancialSyncService  # noqa: E402
from src.ingestion.market_sync import MarketSyncService  # noqa: E402
from src.ingestion.security_sync import SecuritySyncService  # noqa: E402
from src.providers.provider_router import ProviderRouter  # noqa: E402


def run_sync(symbols: list[str]) -> None:
    settings = get_settings()
    engine = get_engine()
    init_database(engine)
    router = ProviderRouter(settings)
    print(f"[providers] {router.health_report()}", flush=True)

    security_svc = SecuritySyncService(engine, router)
    market_svc = MarketSyncService(engine, router)
    financial_svc = FinancialSyncService(engine, router)

    for raw in symbols:
        try:
            sec_res = security_svc.sync(raw)
            sec = sec_res.security
            market_res = market_svc.sync(
                sec.id, sec.symbol, years=settings.yaml_config.sync.market_history_years
            )
            fin_res = financial_svc.sync(
                sec.id, sec.symbol, years=settings.yaml_config.sync.financial_history_years
            )
            print(
                f"OK {sec.symbol} {sec.name} model={sec_res.industry_model} "
                f"rows(market)={market_res.rows_upserted} "
                f"rows(financial)={fin_res.rows_upserted} "
                f"latest_market={market_res.latest_trade_date} "
                f"latest_report={fin_res.latest_report_period} "
                f"source={market_res.provider}/{fin_res.provider}"
            , flush=True)
        except Exception as exc:  # noqa: BLE001 - CLI reports per-symbol failures
            print(f"FAIL {raw}: {exc}", file=sys.stderr)


def run_research(symbols: list[str], as_of: date | None = None) -> None:
    """Phase 2: score each existing security and print explainable results."""
    engine = get_engine()
    init_database(engine)

    from src.db import models
    from sqlalchemy import select

    from src.research.scorer import ResearchScorer

    scorer = ResearchScorer(engine)
    as_of = as_of or date.today()
    with engine.connect() as conn:
        rows = conn.execute(
            select(models.Security).order_by(models.Security.id)
        ).fetchall()
    by_sym = {s.symbol: s for s in rows}
    scored = 0
    for raw in symbols:
        sec = by_sym.get(raw)
        if sec is None:
            try:
                from src.ingestion.security_sync import normalize_symbol

                sec = by_sym.get(normalize_symbol(raw))
            except Exception:  # noqa: BLE001
                sec = None
        if sec is None:
            print(f"SKIP {raw}: not in database (run sync first)", file=sys.stderr)
            continue
        try:
            r = scorer.score_security(
                sec.id, sec.symbol, sec.name, sec.industry_model, as_of, persist=True
            )
            dims = " ".join(f"{d.key}={d.score:g}" for d in r.dimensions)
            val = r.valuation
            if val is not None:
                val_line = (
                    f" | fair(base)={_fmt(val.fair_value_base)} "
                    f"std_buy={_fmt(val.standard_buy_price)} "
                    f"cons_buy={_fmt(val.conservative_buy_price)} "
                    f"exp_ret={_fmt(val.expected_return)} "
                    f"state={val.price_state}"
                )
            else:
                val_line = " | valuation=n/a"
            print(
                f"OK {sec.symbol} {sec.name} [{r.industry_model}] "
                f"research={r.research_score} risk={r.risk_score} "
                f"conf={r.confidence_score} | {dims} | flags={list(r.risk_flags)}"
                f"{val_line}",
                flush=True,
            )
            scored += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {raw}: {exc}", file=sys.stderr)
    print(f"scored={scored}/{len(symbols)}", flush=True)


def run_sqlite_to_postgres_migration(source_db_url: str) -> None:
    """Copy one SQLite database to the configured PostgreSQL database."""
    from src.db.transfer import copy_sqlite_database

    target = get_engine()
    source = make_engine(source_db_url)
    try:
        counts = copy_sqlite_database(source, target)
    finally:
        source.dispose()
    total = sum(counts.values())
    print(f"Migration complete: {total} rows copied across {len(counts)} tables.", flush=True)


def _fmt(v) -> str:
    return f"{v:.2f}" if v is not None else "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description="Investment system CLI")
    parser.add_argument("symbols", nargs="*", help="A-share codes, e.g. 600036")
    parser.add_argument("--research", action="store_true", help="Phase 2 research scoring instead of sync")
    parser.add_argument("--as-of", default=None, help="as-of date YYYY-MM-DD (research / daily mode)")
    parser.add_argument("--daily", action="store_true", help="Phase 7: run the full daily automation pipeline")
    parser.add_argument("--portfolio-id", type=int, default=None, help="portfolio id for --daily")
    parser.add_argument("--skip-sync", action="store_true", help="--daily: reuse existing data instead of fetching")
    parser.add_argument("--batch-id", default=None, help="--daily: quality batch id (default YYYYMMDD)")
    parser.add_argument("--notes", default="", help="--daily: journal notes")
    parser.add_argument("--model-lab", action="store_true",
                        help="Phase 8: run Model Lab look-back analytics and print a JSON report")
    parser.add_argument("--export-csv", default=None,
                        help="--model-lab: also export forecast errors to this CSV path")
    parser.add_argument("--horizon", default="12M",
                        help="--model-lab/-export-csv: horizon (3M/6M/12M/24M, default 12M)")
    parser.add_argument("--migrate-sqlite-to-postgres", action="store_true",
                        help="copy a SQLite file to the configured PostgreSQL database")
    parser.add_argument("--source-db-url", default=None,
                        help="SQLite URL used with --migrate-sqlite-to-postgres")
    args = parser.parse_args()
    as_of = date.fromisoformat(args.as_of) if args.as_of else None
    try:
        if args.migrate_sqlite_to_postgres:
            if not args.source_db_url:
                parser.error("--migrate-sqlite-to-postgres requires --source-db-url")
            run_sqlite_to_postgres_migration(args.source_db_url)
        elif args.daily:
            from src.scheduler.daily import run_daily_pipeline

            report = run_daily_pipeline(
                portfolio_id=args.portfolio_id,
                symbols=args.symbols or None,
                as_of=as_of,
                skip_sync=args.skip_sync,
                notes=args.notes,
                batch_id=args.batch_id,
            )
            import json as _json

            print(
                _json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                flush=True,
            )
        elif args.model_lab:
            from src.model_lab.service import (
                export_forecast_errors_csv,
                run_model_lab,
            )

            report = run_model_lab(get_engine())
            if args.export_csv:
                n = export_forecast_errors_csv(get_engine(), args.export_csv, args.horizon)
                report["_csv_export"] = {"path": args.export_csv, "rows": n}
            import json as _json

            print(_json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        elif args.research:
            if not args.symbols:
                parser.error("--research requires at least one symbol")
            run_research(args.symbols, as_of=as_of)
        else:
            if not args.symbols:
                parser.error("at least one symbol is required (or use --daily)")
            run_sync(args.symbols)
    finally:
        get_engine().dispose()


if __name__ == "__main__":
    main()