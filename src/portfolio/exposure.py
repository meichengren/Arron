"""Phase 6 factor exposure concentration (patch item 10).

A security may carry more than one industry tag:

  - ``industry_model``  -> primary model (e.g. TECHNOLOGY)
  - ``secondary_exposure_json`` -> dict of {"COPPER": 0.45, "GOLD": 0.35, ...}

Aggregating across a portfolio surfaces the *real* concentration risk: e.g.
Zijin + Jiangxi Copper + CMOC look diversified by industry but share a huge
copper exposure. We compute the portfolio-level factor exposure (weighted by
position weights) and its HHI so the risk center can flag "looks diversified,
but actually concentrated".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine

from src.db import models
from src.db.engine import make_session_factory


@dataclass(frozen=True)
class FactorExposure:
    factor: str
    source: str                    # primary / secondary / combined
    exposure: float                # 0..1 share of portfolio capital
    contribution: float            # share of total factor exposure (HHI input)

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor": self.factor,
            "source": self.source,
            "exposure": round(self.exposure, 6),
            "contribution": round(self.contribution, 6),
        }


@dataclass(frozen=True)
class ExposureReport:
    as_of: Any
    portfolio_id: int
    primary: dict[str, float]              # industry_model -> weighted exposure
    secondary: dict[str, float]            # factor (secondary tags) -> weighted exposure
    combined: dict[str, float]             # primary + secondary merged
    hhi_combined: float                    # concentration HHI over combined factors
    top_factors: list[FactorExposure]      # sorted desc by combined exposure, top N

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat() if hasattr(self.as_of, "isoformat") else str(self.as_of),
            "portfolio_id": self.portfolio_id,
            "primary": {k: round(v, 6) for k, v in self.primary.items()},
            "secondary": {k: round(v, 6) for k, v in self.secondary.items()},
            "combined": {k: round(v, 6) for k, v in self.combined.items()},
            "hhi_combined": round(self.hhi_combined, 4),
            "top_factors": [f.to_dict() for f in self.top_factors],
        }


def _parse_secondary(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in data.items():
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        if val > 0:
            out[str(k)] = val
    return out


def compute_exposure_report(engine: Engine, portfolio_id: int, as_of=None, top_n: int = 8) -> ExposureReport:
    """Aggregate primary + secondary factor exposure across a portfolio.

    Position weight comes from ``manual_current_weight`` or market value
    (quantity * latest close). Exposures are normalized so each stock's
    secondary tags sum to 1 before weighting.
    """
    from src.portfolio.decision import _latest_close

    factory = make_session_factory(engine)
    positions: list[tuple[int, float | None]] = []   # (security_id, weight_signal)
    sec_rows: dict[int, models.Security] = {}
    total_mv = 0.0
    with factory() as session:
        pf = session.get(models.Portfolio, portfolio_id)
        if pf is None:
            raise KeyError(f"portfolio {portfolio_id} not found")
        pos = session.execute(
            select(models.PortfolioPosition).where(
                models.PortfolioPosition.portfolio_id == portfolio_id
            )
        ).scalars().all()
        secs = session.execute(select(models.Security)).scalars().all()
        ses = {}
        for s in secs:
            ses[s.id] = s
        for p in pos:
            sec = ses.get(p.security_id)
            if sec is None:
                continue
            close = _latest_close(engine, p.security_id, as_of)
            mv = p.quantity * close if close is not None else None
            if p.manual_current_weight is not None:
                positions.append((p.security_id, float(p.manual_current_weight)))
                total_mv += float(p.manual_current_weight)
            elif mv is not None:
                positions.append((p.security_id, mv))
                total_mv += mv
        sec_rows = ses

    if total_mv <= 0:
        return ExposureReport(
            as_of=as_of, portfolio_id=portfolio_id,
            primary={}, secondary={}, combined={}, hhi_combined=0.0, top_factors=[],
        )

    primary: dict[str, float] = {}
    secondary: dict[str, float] = {}
    combined: dict[str, float] = {}
    total_secondary = 0.0

    for sec_id, w in positions:
        sec = sec_rows.get(sec_id)
        if sec is None:
            continue
        weight = w / total_mv if total_mv > 0 else 0.0
        if sec.industry_model:
            primary[sec.industry_model] = primary.get(sec.industry_model, 0.0) + weight
            combined[sec.industry_model] = combined.get(sec.industry_model, 0.0) + weight
        for factor, share in _parse_secondary(sec.secondary_exposure_json).items():
            contrib = weight * share
            secondary[factor] = secondary.get(factor, 0.0) + contrib
            combined[factor] = combined.get(factor, 0.0) + contrib
            total_secondary += contrib

    # combined exposure normalized for HHI over every tagged factor
    combined_values = list({f: v for f, v in combined.items() if v > 1e-9}.values())
    hhi = sum(v * v for v in combined_values) / (sum(combined_values) ** 2) if combined_values else 0.0

    top = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    top_factors = [
        FactorExposure(
            factor=f, source="combined", exposure=v,
            contribution=(v / sum(combined.values())) if combined else 0.0,
        )
        for f, v in top
    ]
    return ExposureReport(
        as_of=as_of, portfolio_id=portfolio_id,
        primary={k: round(v, 6) for k, v in primary.items() if v > 1e-9},
        secondary={k: round(v, 6) for k, v in secondary.items() if v > 1e-9},
        combined={k: round(v, 6) for k, v in combined.items() if v > 1e-9},
        hhi_combined=round(hhi, 4),
        top_factors=top_factors,
    )


def persist_exposure_report(
    engine: Engine, report: ExposureReport, source: str = "primary+secondary"
) -> int:
    """Persist one day of factor exposures into ``factor_exposures``.

    Idempotent for the same (portfolio_id, as_of_date): previous rows for that
    day are removed first, then combined exposures are written.
    """
    factory = make_session_factory(engine)
    as_of = report.as_of
    if hasattr(as_of, "date"):
        as_of = as_of.date() if as_of.__class__.__name__ == "datetime" else as_of
    with factory() as session:
        session.execute(
            models.FactorExposure.__table__.delete().where(
                models.FactorExposure.portfolio_id == report.portfolio_id,
                models.FactorExposure.as_of_date == as_of,
            )
        )
        for factor, exposure in sorted(report.combined.items()):
            session.add(models.FactorExposure(
                portfolio_id=report.portfolio_id,
                as_of_date=as_of,
                factor=factor,
                exposure=float(exposure),
                source=source,
            ))
        session.commit()
        return len(report.combined)