"""Phase 8 Model Lab - C engine (spec section 3.3).

Look-back analytics over persisted snapshots. Everything here is read-only
over ``research_snapshots`` / ``daily_market`` / ``decision_journal`` (plus
writes to the append-only ``forecast_errors`` table). The engine never takes
part in live decisions; it exists to validate whether "high score -> high
future risk-adjusted return" actually holds, and to calibrate the valuation
forecasts against realised outcomes.

Sub-modules:
- ``engine``      lookback math (future returns, score buckets, walk-forward,
                  buy-zone IRR, calibration regression)
- ``calibration`` forecast_errors persistence (append-only, idempotent)
- ``service``     run_model_lab() orchestration + exportable report
"""