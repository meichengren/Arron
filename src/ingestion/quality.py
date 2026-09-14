"""Phase 7 Data Quality Engine (spec section 3.4).

Runs after sync and before scoring. Every check yields a severity
(OK / DATA_WARNING / DATA_BLOCK); the per-security verdict is the worst
failed check. A DATA_BLOCK verdict forbids STRONG_BUY / BUY signals for that
symbol (see ``src/portfolio/decision.py`` gate).

Results are persisted to ``data_quality_logs`` (one row per security plus one
batch summary row with security_id NULL).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory

OK = "OK"
DATA_WARNING = "DATA_WARNING"
DATA_BLOCK = "DATA_BLOCK"

VERDICTS = (OK, DATA_WARNING, DATA_BLOCK)

# -- severity tags used by individual checks ---------------------------------- #
_WARN = "DATA_WARNING"
_BLOCK = "DATA_BLOCK"

# -- thresholds --------------------------------------------------------------- #
YOY_BLOCK = 5.00        # |yoy| > 500% → impossible, hard error
YOY_WARN = 3.00         # |yoy| > 300% → suspect (spec: 同比异常>300%)
PE_BLOCK = 1.50         # pe jump > 150%
PE_WARN = 0.80          # pe jump > 80% (spec: PE 突跳>80%)
ROE_BLOCK = 100.0       # |roe| > 100% (percent points; db stores 8.98 = 8.98%)
ROE_DEVIATION_WARN = 20.0  # deviation from cross-sectional median > 20pp
PRICE_BLOCK = 0.25      # single-day |return| > 25% impossible for A-share caps
DUAL_SOURCE_WARN = 0.05  # same-day cross-source close diff > 5%
MISSING_WARN = 0.20     # missing close in last 60 trading days > 20%
MISSING_BLOCK = 0.40    # missing close in last 60 trading days > 40%
FRESH_DAYS_WARN = 10    # natural days (long week-end incl.)
FRESH_DAYS_BLOCK = 30
REPORT_DELAY_WARN = 400  # announcement delay beyond report period end


@dataclass
class QualityCheck:
    code: str
    label: str
    severity: str            # _WARN or _BLOCK
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "label": self.label,
            "severity": self.severity,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class SecurityQuality:
    symbol: str
    security_id: int | None
    verdict: str
    checks: list[QualityCheck] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "verdict": self.verdict,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass
class QualityReport:
    batch_id: str
    as_of: date
    securities: list[SecurityQuality] = field(default_factory=list)

    @property
    def overall_verdict(self) -> str:
        if any(s.verdict == DATA_BLOCK for s in self.securities):
            return DATA_BLOCK
        if any(s.verdict == DATA_WARNING for s in self.securities):
            return DATA_WARNING
        return OK

    @property
    def blocked_symbols(self) -> list[str]:
        return [s.symbol for s in self.securities if s.verdict == DATA_BLOCK]

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "as_of": self.as_of.isoformat(),
            "overall_verdict": self.overall_verdict,
            "blocked_symbols": self.blocked_symbols,
            "securities": [s.to_dict() for s in self.securities],
        }


def _worst_verdict(checks: list[QualityCheck]) -> str:
    if any(not c.passed and c.severity == _BLOCK for c in checks):
        return DATA_BLOCK
    if any(not c.passed for c in checks):
        return DATA_WARNING
    return OK


def _latest_report_rows(session, security_id: int) -> list[models.FinancialReport]:
    return session.execute(
        select(models.FinancialReport)
        .where(models.FinancialReport.security_id == security_id)
        .order_by(models.FinancialReport.report_period.desc())
        .limit(2)
    ).scalars().all()


def _market_rows(
    session, security_id: int, as_of: date, start: date
) -> list[models.DailyMarket]:
    return session.execute(
        select(models.DailyMarket)
        .where(models.DailyMarket.security_id == security_id)
        .where(models.DailyMarket.trade_date >= start)
        .where(models.DailyMarket.trade_date <= as_of)
        .order_by(models.DailyMarket.trade_date)
    ).scalars().all()


def _check_security(
    session, security_id: int, symbol: str, as_of: date
) -> SecurityQuality:
    checks: list[QualityCheck] = []

    rows = _market_rows(
        session, security_id, as_of, as_of - timedelta(days=400)
    )

    # --- freshness ---------------------------------------------------------- #
    if not rows:
        checks.append(QualityCheck(
            "freshness", "无任何行情数据", _BLOCK, False,
            f"as_of={as_of} 前 400 天无 daily_market 记录",
        ))
    else:
        latest = rows[-1].trade_date
        gap = (as_of - latest).days
        passed = gap <= FRESH_DAYS_WARN
        checks.append(QualityCheck(
            "freshness", "数据新鲜度", _BLOCK if gap > FRESH_DAYS_BLOCK else _WARN,
            passed,
            f"最新行情 {latest}，距 as_of {gap} 天",
        ))

    # --- missing data (last 60 trading days) -------------------------------- #
    window = [r for r in rows if r.trade_date >= as_of - timedelta(days=120)]
    if not window:
        checks.append(QualityCheck(
            "missing_data", "近期行情缺失", _BLOCK, False,
            "近 120 自然日无行情记录",
        ))
    else:
        expected = max(1, (window[-1].trade_date - window[0].trade_date).days // 7 * 5 + 1)
        have = sum(1 for r in window if r.close is not None)
        missing_ratio = 1.0 - (have / expected if expected else 1.0)
        passed = missing_ratio <= MISSING_WARN
        checks.append(QualityCheck(
            "missing_data", "行情缺失率", _BLOCK if missing_ratio > MISSING_BLOCK else _WARN,
            passed,
            f"最近窗口 expected≈{expected} 日，缺失率 {missing_ratio:.1%}",
        ))

    # --- price anomaly (|single-day return| > limit) ------------------------- #
    anomalies: list[str] = []
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if prev.close and cur.close and prev.close > 0:
            ret = abs(cur.close / prev.close - 1.0)
            if ret > PRICE_BLOCK:
                anomalies.append(f"{cur.trade_date}:{ret:.0%}")
    if anomalies:
        checks.append(QualityCheck(
            "price_anomaly", "复权价异常", _BLOCK, False,
            "单日涨跌幅超 25%: " + ", ".join(anomalies[:3]),
        ))

    # --- field consistency (close/pre_close vs pct_change tell different
    # stories => providers disagree in the same row) -------------------------- #
    incons: list[str] = []
    for r in rows:
        if r.close and r.pre_close and r.pre_close > 0 and r.pct_change is not None:
            implied = r.close / r.pre_close - 1.0
            declared = r.pct_change / 100.0
            if abs(implied - declared) > 0.02:
                incons.append(f"{r.trade_date}: implied={implied:+.1%} vs pct={r.pct_change:+.1f}%")
    if incons:
        checks.append(QualityCheck(
            "field_consistency", "字段/来源不一致", _WARN, False,
            "收盘价隐含涨跌与 pct_change 偏差 > 2pp: " + ", ".join(incons[:3]),
        ))

    # --- PE jump (latest pe_ttm vs ~5 trading days earlier) ------------------- #
    pe_series = [(r.trade_date, r.pe_ttm or r.pe) for r in rows if (r.pe_ttm or r.pe) is not None]
    if len(pe_series) >= 6:
        d0, p0 = pe_series[-1]
        d5, p5 = pe_series[-6]
        if p0 and p5 and p5 > 0:
            jump = abs(p0 / p5 - 1.0)
            passed = jump <= PE_WARN
            checks.append(QualityCheck(
                "pe_jump", "PE 突跳", _BLOCK if jump > PE_BLOCK else _WARN,
                passed, f"最近 {d5}(PE={p5:.2f}) → {d0}(PE={p0:.2f})，变动 {jump:.0%}",
            ))

    # --- financial reports: yoy spike / roe / dates --------------------------- #
    reports = _latest_report_rows(session, security_id)
    if not reports:
        checks.append(QualityCheck(
            "financial_missing", "财务报告缺失", _WARN, False,
            "financial_reports 无记录，无法校验同比/ROE",
        ))
    else:
        latest_rep = reports[0]
        # announcement vs report period
        if latest_rep.announcement_date and latest_rep.report_period:
            if latest_rep.announcement_date < latest_rep.report_period:
                checks.append(QualityCheck(
                    "report_date", "财报日期不合理", _BLOCK, False,
                    f"announcement {latest_rep.announcement_date} < report_period "
                    f"{latest_rep.report_period}",
                ))
            else:
                delay = (latest_rep.announcement_date - latest_rep.report_period).days
                checks.append(QualityCheck(
                    "report_date", "财报披露延迟", _WARN, delay <= REPORT_DELAY_WARN,
                    f"披露距报告期末 {delay} 天",
                ))
        # yoy spikes
        yoy_values = {
            "yoy_spike_net_profit": latest_rep.net_profit_yoy,
            "yoy_spike_revenue": latest_rep.revenue_yoy,
        }
        for code, val in yoy_values.items():
            if val is None:
                continue
            av = abs(val)
            passed = av <= YOY_WARN
            checks.append(QualityCheck(
                code, f"{code} 同比异常", _BLOCK if av > YOY_BLOCK else _WARN,
                passed, f"{code}={val:.2f}",
            ))
        # roe outlier
        if latest_rep.roe is not None:
            if abs(latest_rep.roe) > ROE_BLOCK:
                checks.append(QualityCheck(
                    "roe_outlier", "ROE 离群", _BLOCK, False,
                    f"roe={latest_rep.roe:.2f}（超过 ±100%）",
                ))

    return SecurityQuality(
        symbol=symbol,
        security_id=security_id,
        checks=checks,
        verdict=_worst_verdict(checks),
    )


def _cross_sectional_roe(session, security_ids: list[int]) -> float | None:
    """Cross-sectional ROE median across all tracked securities (decimal)."""
    rows = session.execute(
        select(models.FinancialReport.security_id, models.FinancialReport.roe)
        .where(models.FinancialReport.security_id.in_(security_ids))
        .where(models.FinancialReport.roe.isnot(None))
        .order_by(models.FinancialReport.report_period.desc())
    ).all()
    seen: dict[int, float] = {}
    for sid, roe in rows:
        seen.setdefault(sid, float(roe))
    vals = sorted(seen.values())
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def run_quality_gate(
    engine: Engine,
    as_of: date | None = None,
    symbols: list[str] | None = None,
    batch_id: str | None = None,
) -> QualityReport:
    """Run every quality check for the given (or all) securities and persist.

    Returns the report; a batch summary row (security_id NULL) is stored so
    downstream gates can look up the overall verdict cheaply.
    """
    as_of = as_of or date.today()
    batch_id = batch_id or as_of.strftime("%Y%m%d")
    factory = make_session_factory(engine)

    with factory() as session:
        q = select(models.Security)
        if symbols:
            q = q.where(models.Security.symbol.in_(symbols))
        secs = session.execute(q).scalars().all()

        median_roe = _cross_sectional_roe(
            session, [s.id for s in secs]
        ) if secs else None

        per_sec: list[SecurityQuality] = []
        for sec in secs:
            sq = _check_security(session, sec.id, sec.symbol, as_of)
            # cross-sectional ROE deviation warning (needs median across cohort)
            if median_roe is not None:
                rep = _latest_report_rows(session, sec.id)
                if rep and rep[0].roe is not None:
                    dev = abs(rep[0].roe - median_roe)
                    if dev > ROE_DEVIATION_WARN and not any(
                        c.code == "roe_outlier" for c in sq.checks if not c.passed
                    ):
                        sq.checks.append(QualityCheck(
                            "roe_outlier", "ROE 离群", _WARN, False,
                            f"roe={rep[0].roe:.2f} 偏离行业中位数 {median_roe:.2f} "
                            f"达 {dev:.2f}",
                        ))
                        sq.verdict = _worst_verdict(sq.checks)
            per_sec.append(sq)

    report = QualityReport(batch_id=batch_id, as_of=as_of, securities=per_sec)

    # --- persist ------------------------------------------------------------- #
    with factory() as session:
        for sq in per_sec:
            session.add(models.DataQualityLog(
                batch_id=batch_id, as_of_date=as_of,
                security_id=sq.security_id, symbol=sq.symbol,
                verdict=sq.verdict,
                checks_json=json.dumps(
                    {c.code: c.to_dict() for c in sq.checks}, ensure_ascii=False
                ),
            ))
        session.add(models.DataQualityLog(
            batch_id=batch_id, as_of_date=as_of, security_id=None,
            symbol="__BATCH__", verdict=report.overall_verdict,
            checks_json=json.dumps(
                {"blocked": report.blocked_symbols,
                 "n_securities": len(per_sec)},
                ensure_ascii=False,
            ),
        ))
        session.commit()
    return report


def quality_verdicts(
    engine: Engine, symbols: list[str] | None = None, as_of: date | None = None
) -> dict[str, str]:
    """Latest persisted verdict per symbol (most recent run wins)."""
    factory = make_session_factory(engine)
    out: dict[str, str] = {}
    with factory() as session:
        q = (
            select(models.DataQualityLog.symbol, models.DataQualityLog.verdict)
            .where(models.DataQualityLog.symbol != "__BATCH__")
            .order_by(
                models.DataQualityLog.as_of_date.desc(),
                models.DataQualityLog.id.desc(),
            )
        )
        if symbols:
            q = q.where(models.DataQualityLog.symbol.in_(symbols))
        rows = session.execute(q).all()
    for sym, verdict in rows:
        if sym not in out:
            out[sym] = verdict
    return out


def blocked_symbols(
    engine: Engine, symbols: list[str] | None = None, as_of: date | None = None
) -> set[str]:
    """Symbols whose latest quality verdict is DATA_BLOCK."""
    return {
        sym for sym, v in quality_verdicts(engine, symbols, as_of).items()
        if v == DATA_BLOCK
    }