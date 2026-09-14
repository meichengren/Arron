"""Lightweight schema migrations for SQLite.

V1.1 forked extra nullable columns into portfolio_snapshots (and a new
stress_scenarios table). ``create_all`` won't alter an existing table, so we
run idempotent ALTER TABLE ... ADD COLUMN for the new columns only.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

# (table, column, ddl_type) - must be nullable or have a default.
_V11_ADD_COLUMNS: list[tuple[str, str, str]] = [
    ("portfolio_snapshots", "downside_beta", "FLOAT"),
    ("portfolio_snapshots", "beta_regime", "VARCHAR(16) DEFAULT ''"),
    ("portfolio_snapshots", "normal_corr_avg", "FLOAT"),
    ("portfolio_snapshots", "stress_corr_avg", "FLOAT"),
    ("portfolio_snapshots", "stress_mdd", "FLOAT"),
    ("portfolio_snapshots", "forward_mdd", "FLOAT"),
    ("portfolio_snapshots", "risk_contrib_json", "TEXT DEFAULT '{}'"),
    ("portfolio_snapshots", "liquidity_flag", "VARCHAR(16) DEFAULT ''"),
]

# V1.2 / Phase 6: risk budget + regime + rebalance snapshots, securities exposure.
_V12_ADD_COLUMNS: list[tuple[str, str, str]] = [
    ("portfolio_snapshots", "market_regime", "VARCHAR(16) DEFAULT ''"),
    ("portfolio_snapshots", "feasible", "BOOLEAN"),
    ("portfolio_snapshots", "max_achievable_return", "FLOAT"),
    ("portfolio_snapshots", "turnover", "FLOAT"),
    ("portfolio_snapshots", "transaction_cost", "FLOAT"),
    ("portfolio_snapshots", "regime_params_json", "TEXT DEFAULT '{}'"),
    ("portfolio_snapshots", "opportunity_json", "TEXT DEFAULT '{}'"),
    ("portfolios", "max_stress_drawdown", "FLOAT DEFAULT -0.25"),
    ("portfolios", "max_single_risk_contribution", "FLOAT DEFAULT 0.30"),
    ("portfolios", "fee_rate", "FLOAT DEFAULT 0.0003"),
    ("portfolios", "stamp_tax", "FLOAT DEFAULT 0.0005"),
    ("portfolios", "slippage", "FLOAT DEFAULT 0.001"),
    ("portfolios", "min_trade_amount", "FLOAT DEFAULT 20000.0"),
    ("securities", "secondary_exposure_json", "TEXT DEFAULT '{}'"),
]


def _existing_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return {r[1] for r in rows}


def migrate(engine: Engine) -> list[str]:
    """Apply pending column additions. Returns the list of added columns."""
    applied: list[str] = []
    all_columns = [*_V11_ADD_COLUMNS, *_V12_ADD_COLUMNS]
    for table, column, ddl in all_columns:
        try:
            existing = _existing_columns(engine, table)
        except Exception:  # noqa: BLE001 - table may not exist yet
            existing = set()
        if column in existing:
            continue
        stmt = text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        with engine.begin() as conn:
            conn.execute(stmt)
        applied.append(f"{table}.{column}")
    return applied