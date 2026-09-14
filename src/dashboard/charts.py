"""Phase 4: plotly chart factories (System A Research Detail).

Pure functions: input dicts in, plotly figures out. No streamlit, no database
access here so every layout rule is unit testable.
"""
from __future__ import annotations

from typing import Any, Sequence

import plotly.graph_objects as go

_CONS = "#c0392b"   # conservative buy price
_STAND = "#d35400"  # standard buy price
_FAIR = "#2980b9"   # fair value base
_BULL = "#27ae60"
_BEAR = "#7f8c8d"
_PRICE = "#1a1a2e"
_REV = "#2e86c1"
_PROFIT = "#27ae60"
_FCF = "#8e44ad"


def price_chart(close_series: Sequence[dict[str, Any]], current_price: float | None = None) -> go.Figure:
    """5-year closing price line with current-price marker."""
    fig = go.Figure()
    dates = [p["date"] for p in close_series]
    closes = [p["close"] for p in close_series]
    if dates:
        fig.add_trace(go.Scatter(x=dates, y=closes, mode="lines", name="收盘价",
                                 line=dict(color=_PRICE, width=1.6)))
    if current_price is not None:
        fig.add_hline(y=current_price, line_dash="dot", line_color=_CONS,
                      annotation_text=f"当前 {current_price:g}", annotation_position="top right")
    fig.update_layout(
        title="近 5 年股价（元）", height=330, margin=dict(l=10, r=10, t=44, b=10),
        xaxis_title=None, yaxis_title="元", template="plotly_white",
        hovermode="x unified",
    )
    return fig


def valuation_chart(
    current_price: float | None,
    fair_value_bear: float | None,
    fair_value_base: float | None,
    fair_value_bull: float | None,
    standard_buy_price: float | None,
    conservative_buy_price: float | None,
) -> go.Figure:
    """Bear/Base/Bull fair value interval vs current price & two buy prices."""
    fig = go.Figure()
    labels: list[str] = []
    values: list[float] = []
    colors: list[str] = []
    add = lambda label, v, c: (labels.append(label), values.append(v), colors.append(c)) if v is not None else None
    add("保守买入价", conservative_buy_price, _CONS)
    add("标准买入价", standard_buy_price, _STAND)
    add("当前价", current_price, _PRICE)
    add("Bear 公平价值", fair_value_bear, _BEAR)
    add("Base 公平价值", fair_value_base, _FAIR)
    add("Bull 公平价值", fair_value_bull, _BULL)
    if not values:
        fig.add_annotation(text="无估值数据", showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper")
    else:
        fig.add_trace(go.Bar(x=labels, y=values, marker_color=colors, name="价格（元）"))
        fig.add_trace(go.Scatter(x=labels, y=values, mode="markers+text",
                                 text=[f"{v:g}" for v in values], textposition="top center",
                                 marker=dict(color=colors), name=None, showlegend=False))
    fig.update_layout(
        title="当前价 vs 两档买入价 vs 三情景公平价值（元）", height=360,
        margin=dict(l=10, r=10, t=44, b=10), template="plotly_white",
        yaxis_title="元",
    )
    return fig


def financial_chart(reports: Sequence[dict[str, Any]]) -> go.Figure:
    """Revenue / Net profit (bar) + FCF (line) across report periods."""
    fig = go.Figure()
    periods = [r["period"] for r in reports if r.get("revenue") is not None or r.get("net_profit") is not None]
    revenue = [r.get("revenue") for r in reports]
    profit = [r.get("net_profit") for r in reports]
    fcf = [r.get("free_cashflow") for r in reports]
    if periods:
        fig.add_trace(go.Bar(x=periods, y=revenue, name="营业收入", marker_color=_REV))
        fig.add_trace(go.Bar(x=periods, y=profit, name="净利润", marker_color=_PROFIT))
        fig.add_trace(go.Scatter(x=periods, y=fcf, name="自由现金流",
                                 line=dict(color=_FCF, width=2), mode="lines+markers"))
    fig.update_layout(
        title="营业收入 / 净利润 / 自由现金流", height=360, barmode="group",
        margin=dict(l=10, r=10, t=44, b=10), template="plotly_white",
        xaxis_title=None, hovermode="x unified",
    )
    return fig


def roe_chart(reports: Sequence[dict[str, Any]]) -> go.Figure:
    """ROE(%) across report periods."""
    fig = go.Figure()
    periods = [r["period"] for r in reports if r.get("roe") is not None]
    roe = [r["roe"] for r in reports if r.get("roe") is not None]
    if periods:
        fig.add_trace(go.Scatter(x=periods, y=roe, mode="lines+markers", name="ROE(%)",
                                 line=dict(color=_FAIR, width=2)))
        fig.add_hline(y=10.0, line_dash="dash", line_color="#95a5a6",
                      annotation_text="ROE 10% 参考线", annotation_position="top left")
    fig.update_layout(
        title="ROE（%）", height=300, margin=dict(l=10, r=10, t=44, b=10),
        template="plotly_white", yaxis_title="%",
    )
    return fig


def percentile_chart(
    history: Sequence[float],
    current: float | None,
    label: str,
    color: str,
) -> go.Figure:
    """PE/PB historical distribution with current-value marker.

    Renders the percentile bands (p10/p25/median/p75/p90) as a horizontal
    range so the reader can place the current value inside its 5y history
    without inventing dates for the synthetic series.
    """
    import numpy as np

    fig = go.Figure()
    vals = [float(v) for v in history if v is not None and np.isfinite(v)]
    if vals:
        qs = np.percentile(vals, [10, 25, 50, 75, 90])
        bands = [
            ("p10–p90", qs[0], qs[4], "#d5dbdb"),
            ("p25–p75", qs[1], qs[3], "#aeb6bf"),
        ]
        for name, lo, hi, c in bands:
            fig.add_trace(go.Bar(x=[hi - lo], y=[label], base=lo, marker_color=c, name=name,
                                 orientation="h", hovertemplate=f"{name}: {lo:g} – {hi:g}<extra></extra>"))
        fig.add_shape(type="line", x0=qs[2], x1=qs[2], y0=-0.5, y1=0.5, line=dict(color="#566573", width=2, dash="dot"))
        if current is not None:
            fig.add_shape(type="line", x0=current, x1=current, y0=-0.5, y1=0.5,
                          line=dict(color=color, width=3))
            fig.add_annotation(x=current, y=0, text=f"当前 {current:g}", showarrow=True, arrowhead=2,
                               ax=0, ay=-46, font=dict(color=color))
        fig.update_xaxes(title_text=f"{label} 历史分布")
        fig.update_yaxes(visible=False)
    else:
        fig.add_annotation(text="无历史数据", showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper")
    pctl = ""
    fig.update_layout(
        title=f"{label} 历史分位（5 年，当前值蓝线）{pctl}", height=240,
        margin=dict(l=10, r=10, t=44, b=10), template="plotly_white", showlegend=True,
    )
    return fig