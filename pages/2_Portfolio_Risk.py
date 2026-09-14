# -*- coding: utf-8 -*-
"""System B: 组合风险中心 (Phase 5).

风险画像（Beta / Vol / 三回撤 / VaR / CVaR / 相关 / 集中度 / 风险贡献）、
压力测试 5 情景、历史快照。UI 薄层，所有数据经 src.dashboard.services。
"""
from __future__ import annotations

import streamlit as st

from src.db.engine import get_engine, init_database
from src.dashboard import services as svc
from src.ui.auth import admin_required

st.set_page_config(page_title="组合风险 · Risk Center", page_icon="🛡️", layout="wide")


@st.cache_resource
def _engine():
    engine = get_engine()
    init_database(engine)
    return engine


engine = _engine()

st.title("🛡️ 组合风险中心")
st.caption("System B · Portfolio Risk — 完整风险画像 / 压力测试 / 历史快照（Phase 5 · V1.1）")

pfs = svc.portfolio_list(engine)
if not pfs:
    st.info("尚未创建组合。请先通过 CLI / 数据层建立 portfolios 与 positions。")
    st.stop()

names = {f"{p['id']} · {p['name']}": p for p in pfs}
sel = st.selectbox("选择组合", list(names), format_func=lambda k: k)
pf = names[sel]

if not pf["has_positions"]:
    st.warning("该组合还没有持仓。请先添加 portfolio_positions 后再生成风险快照。")
    st.stop()

if st.button("🔄 生成 / 刷新风险快照", type="primary"):
    if not admin_required("risk_snapshot"):
        st.warning("该操作会写入风险快照，需要管理密码。解锁后请再次点击执行。")
    else:
        with st.spinner("计算完整风险画像与压力测试…"):
            payload = svc.portfolio_risk_payload(engine, pf["id"])
        if payload is None:
            st.error("组合不存在或计算失败。")
            st.stop()
        st.session_state["risk_payload"] = payload

payload = st.session_state.get("risk_payload")
if payload is None:
    try:
        with st.spinner("计算完整风险画像与压力测试…"):
            payload = svc.portfolio_risk_payload(engine, pf["id"])
        st.session_state["risk_payload"] = payload
    except ValueError as exc:
        st.warning(f"生成风险快照失败：{exc}")
        st.stop()

risk = payload["risk"]
st.caption(f"快照日期 {payload['as_of']} · 风险等级 **{risk['risk_level'] or '—'}**")

# ---- None 容错格式化（快照字段可能因数据不足而缺失） --------------------- #
def _pct(v, digits: int = 1) -> str:
    return f"{v * 100:.{digits}f}%" if v is not None else "—"


def _num(v, digits: int = 2) -> str:
    return f"{v:.{digits}f}" if v is not None else "—"


# ------------------------------------------------------------------ KPI row - #
c = st.columns(6)
kpis = [
    ("预期年化收益", _pct(risk["expected_return"])),
    ("组合 Beta", _num(risk["portfolio_beta"])),
    ("年化波动", _pct(risk["annualized_volatility"])),
    ("历史 MDD", _pct(risk["max_drawdown_estimate"])),
    ("压力 MDD", _pct(risk["stress_mdd"])),
    ("前瞻回撤区间", _pct(risk["forward_mdd"])),
]
for col, (label, val) in zip(c, kpis):
    col.metric(label, val)

# ------------------------------------------------------------------ panels - #
left, right = st.columns([3, 2])

with left:
    st.subheader("持仓与风险贡献")
    rows = []
    for h in payload["holdings"]:
        rc = risk["risk_contrib"].get(h["symbol"], 0.0)
        rows.append(
            {
                "股票": f"{h['name']} · {h['symbol']}",
                "权重": f"{h['weight']*100:.1f}%",
                "Beta": f"{h['beta']:.2f}" if h["beta"] is not None else "—",
                "Downside β": f"{h['downside_beta']:.2f}" if h["downside_beta"] is not None else "—",
                "风险贡献": f"{rc:.1f}%",
                "Beta 状态": h.get("beta_flag") or "NORMAL",
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)
    # capital vs risk contribution discrepancy callout
    warn = [
        r for r in rows
        if float(r["风险贡献"].rstrip("%")) > float(r["权重"].rstrip("%")) * 1.5
    ]
    if warn:
        st.warning(
            "⚠️ 以下持仓的资金权重明显低于其风险贡献（补丁第 11 条的风险集中提示）：\n\n"
            + "、".join(r["股票"] for r in warn)
        )

with right:
    st.subheader("风险画像")
    items = [
        ("VaR 95%（日）", _pct(risk["var_95"])),
        ("CVaR 95%（日）", _pct(risk["cvar_95"])),
        ("下行偏差（年化）", _pct(risk["downside_deviation"])),
        ("HHI 集中度", _num(risk["concentration_hhi"], 3)),
        ("常态平均相关", _num(risk["normal_corr_avg"])),
        ("压力相关（最差20%日）", _num(risk["stress_corr_avg"])),
        ("现金权重", _pct(risk["cash_weight"])),
        ("流动性标记", risk.get("liquidity_flag") or "—"),
    ]
    for label, val in items:
        t1, t2 = st.columns([4, 2])
        t1.write(label)
        t2.metric("", val)

# ------------------------------------------------------------------ stress ---- #
st.subheader("压力测试（5 情景）")
if payload["stress"]:
    srows = []
    for name, s in payload["stress"].items():
        srows.append(
            {
                "情景": name,
                "预计组合跌幅": f"{s['scenario_pct']:.1f}%",
                "说明": s.get("note", ""),
                "个股冲击": "、".join(f"{k} {v:.1f}%" for k, v in s["per_stock"].items()),
            }
        )
    st.dataframe(srows, use_container_width=True, hide_index=True, column_config={
        "说明": st.column_config.TextColumn(width="large"),
        "个股冲击": st.column_config.TextColumn(width="large"),
    })
else:
    st.caption("压力测试未生成（基准行情或样本不足）。")

if payload.get("notes"):
    with st.expander("计算说明"):
        for n in payload["notes"]:
            st.write(f"- {n}")

# ------------------------------------------------------------------ history - #
st.subheader("历史快照（不可变，逐次追加）")
hist = svc.portfolio_snapshot_history(engine, pf["id"])
if hist:
    st.dataframe(hist, use_container_width=True, hide_index=True)
else:
    st.caption("暂无历史快照。")

# ================================================================== Phase 6 == #
st.divider()
st.subheader("🎯 组合决策中心 · Decision Center（Phase 6 · V1.1）")
st.caption("Market Regime 动态参数 / Opportunity Score / 风险预算与 TARGET NOT FEASIBLE / Rebalance Band / 因子暴露")

if st.button("🔄 生成 / 刷新决策（含 Regime + 机会分 + 风险预算 + 调仓计划）", type="primary"):
    if not admin_required("decision"):
        st.warning("该操作会写入决策快照，需要管理密码。解锁后请再次点击执行。")
    else:
        with st.spinner("运行 Phase 6 决策引擎…"):
            decision = svc.portfolio_decision_payload(engine, pf["id"])
        st.session_state["decision_payload"] = decision

decision = st.session_state.get("decision_payload")
if decision is None:
    try:
        with st.spinner("运行 Phase 6 决策引擎…"):
            decision = svc.portfolio_decision_payload(engine, pf["id"])
        st.session_state["decision_payload"] = decision
    except ValueError as exc:
        st.warning(f"生成决策失败：{exc}")
        st.stop()

regime = decision.get("regime") or {}
c1, c2, c3, c4 = st.columns(4)
c1.metric("市场状态", regime.get("regime", "—"))
c2.metric("Beta 上限", f"{regime.get('max_beta') or 0:.2f}")
c3.metric("现金区间", f"{regime.get('cash_weight_min') or 0:.0%}–{regime.get('cash_weight_max') or 0:.0%}")
c4.metric("配置风格", regime.get("style") or "—")

if not decision.get("feasible"):
    st.error(
        f"🚫 TARGET NOT FEASIBLE — 在风险预算下，组合合理预期收益最高约 "
        f"{decision.get('max_achievable_return') or 0:.1%}（目标 {decision.get('target_return') or 0:.0%}），"
        "优化器不通过加风险来迎合目标收益。"
    )
if decision.get("violations"):
    with st.expander("预算违例明细"):
        for v in decision["violations"]:
            st.write(f"- {v}")

st.markdown("**目标权重 vs 当前权重 · 信号**")
sig_rows = [
    {
        "股票": s["symbol"],
        "当前权重": f"{s['current_weight']*100:.1f}%",
        "目标权重": f"{s['target_weight']*100:.1f}%",
        "Opportunity": f"{s['opportunity_score']:.0f}",
        "机会标签": s["opportunity_tag"],
        "信号": f"**{s['signal']}**",
        "理由": s["reason"],
    }
    for s in decision.get("signals", [])
]
st.dataframe(sig_rows, use_container_width=True, hide_index=True, column_config={
    "理由": st.column_config.TextColumn(width="large"),
})

cp1, cp2 = st.columns(2)
with cp1:
    st.markdown(
        f"**调仓计划** — Turnover **{decision.get('turnover', 0):.1%}**，"
        f"交易摩擦成本 **{decision.get('transaction_cost', 0):.3%}**，" +
        ("有小幅调仓（超出 Rebalance Band）" if decision.get("turnover", 0) > 1e-6 else "仓位在 Band 内，无需调仓（避免无意义交易）")
    )
with cp2:
    st.markdown(
        f"**风险预算结果** — 组合 Beta **{decision.get('portfolio_beta') or 0:.2f}**，"
        f"波动 **{decision.get('portfolio_vol') or 0:.1%}**，"
        f"预期收益 **{decision.get('expected_return') or 0:.1%}**，现金 **{decision.get('cash_weight') or 0:.0%}**"
    )

# factor exposure
expo = decision.get("exposure") or {}
st.markdown("**因子暴露（行业双标签聚合）**")
if expo.get("top_factors"):
    ef_rows = [
        {"因子": f["factor"], "暴露": f"{f['exposure']:.1%}", "贡献占比": f"{f['contribution']:.1%}"}
        for f in expo["top_factors"]
    ]
    st.dataframe(ef_rows, use_container_width=True, hide_index=True)
    st.caption(f"主标签 {list((expo.get('primary') or {}).keys())} · 二级因子 {list((expo.get('secondary') or {}).keys())} · 因子暴露 HHI {expo.get('hhi_combined', 0):.3f}")
else:
    st.caption("暂无因子暴露数据（未配置 secondary exposure）。")
st.caption(f"决策日期 {decision.get('as_of')} · 完整决策 JSON 已随快照持久化（regime_params / opportunity / signals / target_weights）。")