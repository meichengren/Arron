"""ORM models per spec Section 5 (securities .. portfolio_snapshots).

Look-ahead safety is built into financial_reports via (report_period,
announcement_date) primary key and the requirement that backtesting only uses
rows whose announcement_date <= backtest date.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Security(Base):
    __tablename__ = "securities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    display_symbol: Mapped[str] = mapped_column(String(12), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    market: Mapped[str] = mapped_column(String(16), nullable=False, default="CN_A")
    industry_raw: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    industry_model: Mapped[str] = mapped_column(String(32), nullable=False, default="GENERIC")
    # V1.1 Phase 6: multi-tag exposure, e.g. {"COPPER": 0.45, "GOLD": 0.35, "LITHIUM": 0.10}
    secondary_exposure_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    list_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    market_data: Mapped[list["DailyMarket"]] = relationship(
        back_populates="security", cascade="all, delete-orphan"
    )
    financial_reports: Mapped[list["FinancialReport"]] = relationship(
        back_populates="security", cascade="all, delete-orphan"
    )


class DailyMarket(Base):
    __tablename__ = "daily_market"
    __table_args__ = (UniqueConstraint("security_id", "trade_date", name="uq_daily_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float | None] = mapped_column(Float)
    pre_close: Mapped[float | None] = mapped_column(Float)
    pct_change: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    amount: Mapped[float | None] = mapped_column(Float)
    pe: Mapped[float | None] = mapped_column(Float)
    pe_ttm: Mapped[float | None] = mapped_column(Float)
    pb: Mapped[float | None] = mapped_column(Float)
    ps_ttm: Mapped[float | None] = mapped_column(Float)
    dv_ratio: Mapped[float | None] = mapped_column(Float)
    total_mv: Mapped[float | None] = mapped_column(Float)
    circ_mv: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    security: Mapped[Security] = relationship(back_populates="market_data")


class FinancialReport(Base):
    __tablename__ = "financial_reports"
    __table_args__ = (
        UniqueConstraint(
            "security_id", "report_period", "announcement_date", name="uq_financial_report"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    report_period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    announcement_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    report_type: Mapped[str] = mapped_column(String(16), nullable=False, default="ANNUAL")
    revenue: Mapped[float | None] = mapped_column(Float)
    revenue_yoy: Mapped[float | None] = mapped_column(Float)
    net_profit: Mapped[float | None] = mapped_column(Float)
    net_profit_yoy: Mapped[float | None] = mapped_column(Float)
    deducted_net_profit: Mapped[float | None] = mapped_column(Float)
    operating_cashflow: Mapped[float | None] = mapped_column(Float)
    free_cashflow: Mapped[float | None] = mapped_column(Float)
    total_assets: Mapped[float | None] = mapped_column(Float)
    total_equity: Mapped[float | None] = mapped_column(Float)
    total_debt: Mapped[float | None] = mapped_column(Float)
    eps: Mapped[float | None] = mapped_column(Float)
    roe: Mapped[float | None] = mapped_column(Float)
    roa: Mapped[float | None] = mapped_column(Float)
    gross_margin: Mapped[float | None] = mapped_column(Float)
    net_margin: Mapped[float | None] = mapped_column(Float)
    debt_ratio: Mapped[float | None] = mapped_column(Float)
    current_ratio: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    security: Mapped[Security] = relationship(back_populates="financial_reports")


class IndustryMetric(Base):
    __tablename__ = "industry_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    metric_name: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "security_id", "metric_date", "metric_name", name="uq_industry_metric"
        ),
    )


class ResearchSnapshot(Base):
    __tablename__ = "research_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False, default="V1.0")
    fundamental_score: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    growth_score: Mapped[float | None] = mapped_column(Float)
    valuation_score: Mapped[float | None] = mapped_column(Float)
    cycle_score: Mapped[float | None] = mapped_column(Float)
    shareholder_score: Mapped[float | None] = mapped_column(Float)
    risk_score: Mapped[float | None] = mapped_column(Float)
    research_score: Mapped[float | None] = mapped_column(Float)
    expected_return_base: Mapped[float | None] = mapped_column(Float)
    expected_return_bear: Mapped[float | None] = mapped_column(Float)
    expected_return_bull: Mapped[float | None] = mapped_column(Float)
    fair_value_bear: Mapped[float | None] = mapped_column(Float)
    fair_value_base: Mapped[float | None] = mapped_column(Float)
    fair_value_bull: Mapped[float | None] = mapped_column(Float)
    standard_buy_price: Mapped[float | None] = mapped_column(Float)
    conservative_buy_price: Mapped[float | None] = mapped_column(Float)
    confidence_score: Mapped[float | None] = mapped_column(Float)
    data_freshness_score: Mapped[float | None] = mapped_column(Float)
    risk_flags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    explanation_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.15)
    max_portfolio_beta: Mapped[float] = mapped_column(Float, nullable=False, default=1.10)
    max_volatility: Mapped[float] = mapped_column(Float, nullable=False, default=0.22)
    max_single_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.30)
    max_industry_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.40)
    min_cash_weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)
    rebalance_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.03)
    hard_rebalance_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)
    risk_profile: Mapped[str] = mapped_column(String(16), nullable=False, default="MODERATE")
    # V1.1 Phase 6 risk-budget constraints (patch item 10 / 12)
    max_stress_drawdown: Mapped[float] = mapped_column(Float, nullable=False, default=-0.25)
    max_single_risk_contribution: Mapped[float] = mapped_column(Float, nullable=False, default=0.30)
    fee_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0003)     # 佣金双边
    stamp_tax: Mapped[float] = mapped_column(Float, nullable=False, default=0.0005)   # 卖出印花税
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.001)     # 滑点
    min_trade_amount: Mapped[float] = mapped_column(Float, nullable=False, default=20000.0)  # 最小调仓金额
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class PortfolioPosition(Base):
    __tablename__ = "portfolio_positions"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "security_id", name="uq_portfolio_position"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_price: Mapped[float | None] = mapped_column(Float)
    manual_current_weight: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    expected_return: Mapped[float | None] = mapped_column(Float)
    portfolio_beta: Mapped[float | None] = mapped_column(Float)
    annualized_volatility: Mapped[float | None] = mapped_column(Float)
    max_drawdown_estimate: Mapped[float | None] = mapped_column(Float)
    var_95: Mapped[float | None] = mapped_column(Float)
    cvar_95: Mapped[float | None] = mapped_column(Float)
    downside_deviation: Mapped[float | None] = mapped_column(Float)
    concentration_hhi: Mapped[float | None] = mapped_column(Float)
    cash_weight: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    target_weights_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    signals_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    # V1.1 patch additions (Phase 5) - all nullable so old rows stay valid.
    downside_beta: Mapped[float | None] = mapped_column(Float)
    beta_regime: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    normal_corr_avg: Mapped[float | None] = mapped_column(Float)
    stress_corr_avg: Mapped[float | None] = mapped_column(Float)
    stress_mdd: Mapped[float | None] = mapped_column(Float)
    forward_mdd: Mapped[float | None] = mapped_column(Float)
    risk_contrib_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    liquidity_flag: Mapped[str] = mapped_column(String(16), nullable=False, default="")

    # V1.1 Phase 6 additions (risk budget / regime / rebalance)
    market_regime: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    feasible: Mapped[bool | None] = mapped_column(nullable=True)          # TARGET NOT FEASIBLE marker
    max_achievable_return: Mapped[float | None] = mapped_column(Float)   # best achievable under budget
    turnover: Mapped[float | None] = mapped_column(Float)                # one-way turnover of the rebalance
    transaction_cost: Mapped[float | None] = mapped_column(Float)        # fees+stamp+slippage (decimal)
    regime_params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    opportunity_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class StressScenario(Base):
    """Phase 5 scenario stress-test definitions (built-in five + user ones)."""

    __tablename__ = "stress_scenarios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # JSON: {"benchmark_pct": -0.20, "sector_shocks": {"TECHNOLOGY": -0.30, ...}}
    factors_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    description: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class MarketRegime(Base):
    """Phase 6 daily market-regime snapshots (Risk-On / Neutral / Risk-Off / Stress)."""

    __tablename__ = "market_regimes"
    __table_args__ = (UniqueConstraint("as_of_date", name="uq_market_regime_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    regime: Mapped[str] = mapped_column(String(16), nullable=False)
    # JSON: {"trend_up": true, "ma20": x, "ma60": y, "ann_vol": z, "ret60d": w,
    #        "scenario_weights": {...}, "max_beta": 1.0, "stock_weight_cap": 0.85,
    #        "cash_weight_min": 0.05, "cash_weight_max": 0.20, "note": "..."}
    signals_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DecisionJournal(Base):
    """Phase 7: immutable daily decision journal.

    Append-only log (no updated_at column, no UPDATE code path) to prevent
    look-ahead bias: a decision for (portfolio_id, as_of_date) can only be
    written once, enforced by a unique constraint.
    """

    __tablename__ = "decision_journal"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "as_of_date", name="uq_journal_portfolio_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    market_regime: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    feasible: Mapped[bool | None] = mapped_column(nullable=True)
    # JSON: {symbol: research_score, ...}
    scores_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # JSON: {symbol: target_weight, ...}
    target_weights_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    # JSON: [StockSignal.to_dict(), ...]
    signals_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # JSON: {"cash_weight": x, "portfolio_beta": y, "turnover": z, ...}
    metrics_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    confidence: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class DataQualityLog(Base):
    """Phase 7: per-security data quality gate results.

    verdict is one of OK / DATA_WARNING / DATA_BLOCK; a DATA_BLOCK for any
    security suppresses STRONG_BUY / BUY signals for that symbol.
    security_id is NULL for the batch-wide summary row.
    """

    __tablename__ = "data_quality_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    security_id: Mapped[int | None] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, default="")
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)  # OK / DATA_WARNING / DATA_BLOCK
    # JSON: {"dup_rows": {...}, "yoy_spike": {...}, ...} one entry per check
    checks_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ForecastError(Base):
    """Phase 8 (Model Lab): realised vs predicted errors, one row per horizon."""

    __tablename__ = "forecast_errors"
    __table_args__ = (
        UniqueConstraint("security_id", "as_of_date", "horizon", name="uq_forecast_error_horizon"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(
        ForeignKey("securities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    horizon: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # trading days ahead
    predicted: Mapped[float | None] = mapped_column(Float)
    actual: Mapped[float | None] = mapped_column(Float)
    error: Mapped[float | None] = mapped_column(Float)
    regime: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class FactorExposure(Base):
    """Phase 7 schema (persist layer lands with the daily journal writer)."""

    __tablename__ = "factor_exposures"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "as_of_date", "factor", name="uq_factor_exposure_day"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    factor: Mapped[str] = mapped_column(String(32), nullable=False)
    exposure: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="primary+secondary")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)