"""Phase 4 dashboard data services (System A).

Pure data-access layer between SQLite and the Streamlit UI: search, library,
detail and chart inputs. No streamlit import here -> every function is unit
testable and the UI only renders what these services return.
"""
from __future__ import annotations

import json
import time
from datetime import date
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory
from src.research.datapoints import build_data_points
from src.research.scorer import ResearchScorer
from src.valuation.zones import valuation_zones


# --------------------------------------------------------------------------- #
# Search / Library
# --------------------------------------------------------------------------- #

DEFAULT_WATCHLIST: tuple[tuple[str, str, str], ...] = (
    ("600406.SH", "国电南瑞", "SSE"),
    ("002050.SZ", "三花智控", "SZSE"),
    ("003816.SZ", "中国广核", "SZSE"),
    ("000333.SZ", "美的集团", "SZSE"),
    ("600276.SH", "恒瑞医药", "SSE"),
    ("600660.SH", "福耀玻璃", "SSE"),
    ("600036.SH", "招商银行", "SSE"),
    ("600900.SH", "长江电力", "SSE"),
    ("300308.SZ", "中际旭创", "SZSE"),
    ("002821.SZ", "凯莱英", "SZSE"),
    ("600809.SH", "山西汾酒", "SSE"),
    ("600941.SH", "中国移动", "SSE"),
    ("601985.SH", "中国核电", "SSE"),
    ("601628.SH", "中国人寿", "SSE"),
    ("000792.SZ", "盐湖股份", "SZSE"),
    ("600845.SH", "宝信软件", "SSE"),
    ("601138.SH", "工业富联", "SSE"),
    ("300750.SZ", "宁德时代", "SZSE"),
    ("300124.SZ", "汇川技术", "SZSE"),
    ("600150.SH", "中国船舶", "SSE"),
    ("601318.SH", "中国平安", "SSE"),
    ("600519.SH", "贵州茅台", "SSE"),
    ("601088.SH", "中国神华", "SSE"),
    ("002594.SZ", "比亚迪", "SZSE"),
    ("601899.SH", "紫金矿业", "SSE"),
    ("002371.SZ", "北方华创", "SZSE"),
    ("601398.SH", "工商银行", "SSE"),
)


def seed_default_watchlist(engine: Engine) -> int:
    """Import the initial screenshot watchlist once, without restoring deletions."""
    factory = make_session_factory(engine)
    with factory() as session:
        if session.get(models.SystemState, "default_watchlist_v1") is not None:
            return 0
        existing = {
            symbol
            for symbol in session.execute(select(models.Security.symbol)).scalars()
        }
        imported = 0
        for symbol, name, exchange in DEFAULT_WATCHLIST:
            if symbol in existing:
                continue
            session.add(
                models.Security(
                    symbol=symbol,
                    display_symbol=symbol.split(".")[0],
                    name=name,
                    exchange=exchange,
                    market="CN_A",
                    industry_model="GENERIC",
                )
            )
            imported += 1
        session.add(models.SystemState(key="default_watchlist_v1", value="imported"))
        session.commit()
    return imported


def search_securities(engine: Engine, query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search by symbol / display symbol / name (case-insensitive substring)."""
    q = query.strip()
    if not q:
        return []
    factory = make_session_factory(engine)
    like = f"%{q}%"
    with factory() as session:
        rows = session.execute(
            select(models.Security)
            .where(
                models.Security.symbol.like(like)
                | models.Security.display_symbol.like(like)
                | models.Security.name.like(like)
            )
            .order_by(models.Security.symbol)
            .limit(limit)
        ).scalars().all()
    return [
        {
            "id": s.id,
            "symbol": s.symbol,
            "display": s.display_symbol,
            "name": s.name,
            "exchange": s.exchange,
            "industry_model": s.industry_model,
            "industry_raw": s.industry_raw,
        }
        for s in rows
    ]


def library_rows(engine: Engine) -> list[dict[str, Any]]:
    """One row per security, latest snapshot first (history is preserved in DB)."""
    factory = make_session_factory(engine)
    with factory() as session:
        securities = session.execute(
            select(models.Security).order_by(models.Security.symbol)
        ).scalars().all()
        snaps = session.execute(
            select(models.ResearchSnapshot).order_by(
                models.ResearchSnapshot.as_of_date, models.ResearchSnapshot.created_at
            )
        ).scalars().all()
    latest: dict[int, models.ResearchSnapshot] = {}
    for s in snaps:
        latest[s.security_id] = s  # later as_of_date overwrites -> keep newest
    out: list[dict[str, Any]] = []
    for sec in securities:
        row = {
            "security_id": sec.id,
            "symbol": sec.symbol,
            "display": sec.display_symbol,
            "name": sec.name,
            "industry_model": sec.industry_model,
            "asset_type": "ETF" if sec.market == "CN_ETF" else "A股",
            "research_score": None,
            "risk_score": None,
            "confidence_score": None,
            "price_state": None,
            "fair_value_base": None,
            "standard_buy_price": None,
            "as_of_date": None,
        }
        snap = latest.get(sec.id)
        if snap is not None:
            expl = _safe_json(snap.explanation_json)
            val = expl.get("valuation") if isinstance(expl, dict) else None
            row.update(
                research_score=snap.research_score,
                risk_score=snap.risk_score,
                confidence_score=snap.confidence_score,
                price_state=(val or {}).get("price_state") if isinstance(val, dict) else None,
                fair_value_base=snap.fair_value_base,
                standard_buy_price=snap.standard_buy_price,
                conservative_buy_price=snap.conservative_buy_price,
                as_of_date=snap.as_of_date,
            )
        out.append(row)
    return out



def ranked_research_rows(engine: Engine) -> list[dict[str, Any]]:
    """Return the current research library ordered by composite score descending."""
    rows = library_rows(engine)
    return sorted(
        rows,
        key=lambda row: (
            row["research_score"] is not None,
            row["research_score"] if row["research_score"] is not None else float("-inf"),
        ),
        reverse=True,
    )


def refresh_daily_research_scores(
    engine: Engine,
    as_of: date | None = None,
    sync_data: bool = False,
    only_pending: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Score stored data or synchronize it, optionally restricting work to pending rows."""
    target_date = as_of or date.today()
    scorer = ResearchScorer(engine)
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    rows = library_rows(engine)
    if only_pending:
        rows = [row for row in rows if row["research_score"] is None]

    for index, row in enumerate(rows):
        attempts = 3 if sync_data else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                if sync_data or row["research_score"] is None:
                    result = add_security_and_research(
                        engine, row["symbol"], target_date, force_sync=sync_data
                    )
                    score = result["research_score"]
                else:
                    result = scorer.score_security(
                        row["security_id"], row["symbol"], row["name"],
                        row["industry_model"], target_date, persist=True,
                    )
                    score = result.research_score
                succeeded.append(
                    {"security_id": row["security_id"], "symbol": row["symbol"], "research_score": score}
                )
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - preserve the final source error
                last_error = exc
                if attempt < attempts - 1:
                    time.sleep(1.0 * (attempt + 1))
        if last_error is not None:
            failed.append({"symbol": row["symbol"], "error": str(last_error)})
        if sync_data and index < len(rows) - 1:
            time.sleep(0.35)

    return {"succeeded": succeeded, "failed": failed}

def delete_security(engine: Engine, security_id: int) -> bool:
    """Delete one security and all rows linked through its database foreign keys."""
    factory = make_session_factory(engine)
    with factory() as session:
        security = session.get(models.Security, security_id)
        if security is None:
            return False
        session.delete(security)
        session.commit()
    return True


def get_security(engine: Engine, security_id: int) -> dict[str, Any] | None:
    factory = make_session_factory(engine)
    with factory() as session:
        sec = session.get(models.Security, security_id)
    if sec is None:
        return None
    return {
        "id": sec.id,
        "symbol": sec.symbol,
        "display": sec.display_symbol,
        "name": sec.name,
        "exchange": sec.exchange,
        "industry_raw": sec.industry_raw,
        "industry_model": sec.industry_model,
    }


def resolve_symbol(engine: Engine, symbol: str) -> int | None:
    """Map user-typed symbol (600036 / 600036.SH / 平安) to a security id."""
    factory = make_session_factory(engine)
    q = symbol.strip().upper()
    with factory() as session:
        row = session.execute(
            select(models.Security.id).where(
                or_(
                    models.Security.symbol == q,
                    models.Security.display_symbol == q,
                    models.Security.name.like(f"%{symbol.strip()}%"),
                )
            ).limit(1)
        ).first()
        return row[0] if row else None


# --------------------------------------------------------------------------- #
# Portfolio risk (Phase 5)
# --------------------------------------------------------------------------- #
def portfolio_list(engine: Engine) -> list[dict[str, Any]]:
    """Portfolios with their latest snapshot risk_level when available."""
    factory = make_session_factory(engine)
    with factory() as session:
        pfs = session.execute(
            select(models.Portfolio).order_by(models.Portfolio.id)
        ).scalars().all()
        snaps = session.execute(
            select(models.PortfolioSnapshot).order_by(models.PortfolioSnapshot.as_of_date.desc())
        ).scalars().all()
    latest: dict[int, models.PortfolioSnapshot] = {}
    for s in snaps:
        latest.setdefault(s.portfolio_id, s)  # first (newest) wins
    out: list[dict[str, Any]] = []
    for p in pfs:
        snap = latest.get(p.id)
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "target_return": p.target_return,
                "max_portfolio_beta": p.max_portfolio_beta,
                "max_volatility": p.max_volatility,
                "risk_profile": p.risk_profile,
                "has_positions": False,
                "risk_level": snap.risk_level if snap else None,
                "portfolio_beta": snap.portfolio_beta if snap else None,
                "as_of": snap.as_of_date.isoformat() if snap else None,
            }
        )
    with factory() as session:
        pos = session.execute(select(models.PortfolioPosition)).scalars().all()
    by_pf: dict[int, int] = {}
    for p in pos:
        by_pf[p.portfolio_id] = by_pf.get(p.portfolio_id, 0) + 1
    for row in out:
        row["has_positions"] = by_pf.get(row["id"], 0) > 0
    return out


def portfolio_risk_payload(engine: Engine, portfolio_id: int) -> dict[str, Any] | None:
    """Compute (not just read) a fresh risk snapshot for one portfolio.

    Returns None when the portfolio does not exist. Raises ValueError when it
    has no positions (the UI shows a hint instead).
    """
    from src.portfolio.service import PortfolioRiskService

    svc = PortfolioRiskService(engine)
    as_of = date.today()
    try:
        result = svc.compute_snapshot(portfolio_id, as_of)
        svc.persist_snapshot(result)
    except KeyError:
        return None
    return svc.to_dict(result)


def portfolio_snapshot_history(engine: Engine, portfolio_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """Latest N snapshot rows (newest first) for a portfolio."""
    factory = make_session_factory(engine)
    with factory() as session:
        rows = session.execute(
            select(models.PortfolioSnapshot)
            .where(models.PortfolioSnapshot.portfolio_id == portfolio_id)
            .order_by(models.PortfolioSnapshot.as_of_date.desc())
            .limit(limit)
        ).scalars().all()
    return [
        {
            "as_of": r.as_of_date.isoformat(),
            "risk_level": r.risk_level,
            "portfolio_beta": r.portfolio_beta,
            "annualized_volatility": r.annualized_volatility,
            "max_drawdown_estimate": r.max_drawdown_estimate,
            "stress_mdd": r.stress_mdd,
            "var_95": r.var_95,
            "cvar_95": r.cvar_95,
            "expected_return": r.expected_return,
            "cash_weight": r.cash_weight,
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #
def detail_payload(engine: Engine, security_id: int) -> dict[str, Any] | None:
    """Everything the Research Detail page renders (snapshot + explanation).

    Returns None when the security does not exist; ``snapshot`` is None when
    the security was synced but not yet scored.
    """
    sec = get_security(engine, security_id)
    if sec is None:
        return None
    factory = make_session_factory(engine)
    with factory() as session:
        snap = session.execute(
            select(models.ResearchSnapshot)
            .where(models.ResearchSnapshot.security_id == security_id)
            .order_by(models.ResearchSnapshot.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()

    snapshot = None
    if snap is not None:
        expl = _safe_json(snap.explanation_json)
        valuation = (
            _safe_json(snap.explanation_json).get("valuation")
            if isinstance(expl, dict)
            else None
        )
        zones = valuation_zones(
            fair_value_bear=snap.fair_value_bear,
            fair_value_base=snap.fair_value_base,
            fair_value_bull=snap.fair_value_bull,
            standard_buy_price=snap.standard_buy_price,
            conservative_buy_price=snap.conservative_buy_price,
            confidence=snap.confidence_score,
            expected_return_base=snap.expected_return_base,
            expected_return_conservative=valuation.get("expected_return_conservative")
            if isinstance(valuation, dict)
            else None,
        )
        snapshot = {
            "as_of_date": snap.as_of_date.isoformat(),
            "model_version": snap.model_version,
            "research_score": snap.research_score,
            "risk_score": snap.risk_score,
            "confidence_score": snap.confidence_score,
            "data_freshness_score": snap.data_freshness_score,
            "dimension_scores": {
                "fundamental": snap.fundamental_score,
                "quality": snap.quality_score,
                "growth": snap.growth_score,
                "valuation": snap.valuation_score,
                "cycle": snap.cycle_score,
                "shareholder": snap.shareholder_score,
            },
            "risk_flags": _safe_json(snap.risk_flags_json) or [],
            "valuation": valuation,
            "zones": zones.to_dict() if zones is not None else None,
            "explanation": expl if isinstance(expl, dict) else {},
        }
    return {"security": sec, "snapshot": snapshot}


def chart_inputs(engine: Engine, security_id: int) -> dict[str, Any]:
    """Raw series for the Detail charts (no UI, no plotly here)."""
    factory = make_session_factory(engine)
    with factory() as session:
        bars = session.execute(
            select(
                models.DailyMarket.trade_date,
                models.DailyMarket.close,
                models.DailyMarket.total_mv,
            )
            .where(models.DailyMarket.security_id == security_id)
            .order_by(models.DailyMarket.trade_date)
        ).all()
        reports = session.execute(
            select(models.FinancialReport)
            .where(models.FinancialReport.security_id == security_id)
            .order_by(models.FinancialReport.report_period)
        ).scalars().all()
        sec = session.get(models.Security, security_id)

    close_series = [
        {"date": d.isoformat(), "close": c}
        for d, c, _mv in bars
        if c is not None
    ]

    financial = []
    for r in reports:
        if r.report_period is None:
            continue
        financial.append(
            {
                "period": r.report_period.isoformat(),
                "revenue": r.revenue,
                "net_profit": r.net_profit,
                "free_cashflow": r.free_cashflow,
                "roe": r.roe,
                "operating_cashflow": r.operating_cashflow,
            }
        )

    # PE / PB historical percentiles from the look-ahead-safe datapoints
    pe_pb = {"pe_history": [], "pb_history": [], "pe_ttm": None, "pb": None}
    if sec is not None:
        try:
            as_of = date.today()
            dp = build_data_points(
                engine, sec.id, sec.symbol, sec.name, sec.industry_model, as_of
            )
            pe_pb = {
                "pe_history": dp.pe_history,
                "pb_history": dp.pb_history,
                "pe_ttm": dp.pe_ttm,
                "pb": dp.pb,
                "pe_pctl": dp.pe_pctl,
                "pb_pctl": dp.pb_pctl,
            }
        except Exception:  # noqa: BLE001 - charts degrade gracefully
            pass

    return {
        "close": close_series,
        "financial": financial,
        "pe_pb": pe_pb,
    }


def run_research_now(engine: Engine, security_id: int, as_of: date | None = None) -> dict[str, Any]:
    """Re-run research scoring for a security (persists a new snapshot)."""
    factory = make_session_factory(engine)
    with factory() as session:
        sec = session.get(models.Security, security_id)
    if sec is None:
        raise KeyError(f"security {security_id} not found")
    scorer = ResearchScorer(engine)
    r = scorer.score_security(
        sec.id, sec.symbol, sec.name, sec.industry_model,
        as_of or date.today(), persist=True,
    )
    return {
        "research_score": r.research_score,
        "confidence_score": r.confidence_score,
        "price_state": r.valuation.price_state if r.valuation else None,
        "as_of_date": (as_of or date.today()).isoformat(),
    }


def _safe_json(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def portfolio_decision_payload(
    engine: Engine, portfolio_id: int, as_of: date | None = None
) -> dict[str, Any] | None:
    """Phase 6 decision payload: regime + opportunity + risk budget + rebalance
    plan + factor exposure, and persist the Phase 6 columns to the snapshot.

    Returns None when the portfolio does not exist; raises ValueError without
    positions.
    """
    from src.dashboard.services import portfolio_risk_payload  # noqa: F401 (ensure ordering)
    from src.portfolio.decision import build_decision, persist_decision
    from src.portfolio.exposure import compute_exposure_report

    as_of = as_of or date.today()
    try:
        decision = build_decision(engine, portfolio_id, as_of=as_of)
    except KeyError:
        return None
    persist_decision(engine, decision)
    exposure = compute_exposure_report(engine, portfolio_id, as_of=as_of)
    payload = decision.to_dict()
    payload["exposure"] = exposure.to_dict()
    return payload


# --------------------------------------------------------------------------- #
# Add-security (deployment / web entry, mirrors CLI run_sync + run_research)
# --------------------------------------------------------------------------- #
def add_security_and_research(
    engine: Engine, raw_symbol: str, as_of: date | None = None, force_sync: bool = False
) -> dict[str, Any]:
    """同步一个新标的（security -> market -> financial）并立即生成研究评分与
    估值快照（persist=True）。整条链路与 CLI 的 run_sync + run_research 一致。

    返回用于页面摘要展示的概要 dict；失败时抛出带有来源信息的异常。
    """
    from src.config.settings import get_settings
    from src.ingestion.financial_sync import FinancialSyncService
    from src.ingestion.market_sync import MarketSyncService
    from src.ingestion.security_sync import SecuritySyncService, normalize_symbol
    from src.providers.provider_router import ProviderRouter
    from src.research.scorer import ResearchScorer

    as_of = as_of or date.today()
    settings = get_settings()
    router = ProviderRouter(settings)

    sec_svc = SecuritySyncService(engine, router)
    mkt_svc = MarketSyncService(engine, router)
    fin_svc = FinancialSyncService(engine, router)

    sec_res = sec_svc.sync(raw_symbol)
    sec = sec_res.security
    mkt_res = mkt_svc.sync(
        sec.id, sec.symbol, years=settings.yaml_config.sync.market_history_years, force=force_sync
    )
    fin_res = fin_svc.sync(
        sec.id, sec.symbol, years=settings.yaml_config.sync.financial_history_years, force=force_sync
    )

    scorer = ResearchScorer(engine)
    r = scorer.score_security(
        sec.id, sec.symbol, sec.name, sec.industry_model, as_of, persist=True
    )
    val = r.valuation
    return {
        "symbol": sec.symbol,
        "name": sec.name,
        "industry_model": sec.industry_model,
        "normalized": normalize_symbol(raw_symbol),
        "market_rows": int(mkt_res.rows_upserted),
        "financial_rows": int(fin_res.rows_upserted),
        "latest_trade_date": mkt_res.latest_trade_date,
        "latest_report_period": fin_res.latest_report_period,
        "market_source": mkt_res.provider,
        "financial_source": fin_res.provider,
        "research_score": float(r.research_score),
        "confidence_score": float(r.confidence_score),
        "price_state": val.price_state if val else None,
        "fair_value": val.fair_value_base if val else None,
        "standard_buy_price": val.standard_buy_price if val else None,
        "as_of": as_of.isoformat(),
    }