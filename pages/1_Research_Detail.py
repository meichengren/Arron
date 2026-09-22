# -*- coding: utf-8 -*-
"""System A: Research Detail 页.

顶部卡片（当前价 / Research / Fundamental / Valuation / Risk / Expected
Return / Standard / Conservative Buy / 数据更新时间）+ 五档价格状态 +
图表区（5 年股价、PE/PB 历史分位、Revenue-Profit-FCF、ROE、Bear-Base-Bull
估值区间 vs 两档买入价）+ 因子贡献表。

本页仅渲染 src.dashboard.services / charts 返回的数据，不包含核心逻辑。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from src.db.engine import get_engine, init_database
from src.dashboard import charts, services as svc
from src.ui.auth import admin_required

st.set_page_config(page_title="Research Detail", page_icon="📊", layout="wide")


@st.cache_resource
def _engine():
    engine = get_engine()
    init_database(engine)
    return engine


engine = _engine()

# ------------------------------------------------------------------- route #
params = st.query_params
sec_id = params.get("security_id", [None])
try:
    security_id = int(sec_id[0] if isinstance(sec_id, list) else sec_id)
except (TypeError, ValueError):
    st.title("研究报告")
    st.warning("请先选择要查看的标的。")
    rows = svc.library_rows(engine)
    if rows:
        choices = {int(row["security_id"]): row for row in rows}
        selected_id = st.selectbox(
            "研究标的",
            options=list(choices),
            format_func=lambda item: (
                f'{choices[item]["name"]} · {choices[item]["symbol"]}'
            ),
        )
        if st.button("打开研究报告", type="primary"):
            st.query_params["security_id"] = str(selected_id)
            st.rerun()
    else:
        st.info("研究标的库为空，请先在「添加标的」页完成同步与分析。")
    st.page_link("app.py", label="返回主页", icon="🏠")
    st.stop()

payload = svc.detail_payload(engine, security_id)
if payload is None:
    st.error("标的不存在。")
    st.stop()

sec = payload["security"]
snap = payload["snapshot"]

st.title(f"{sec['name']} · {sec['symbol']}")
st.caption(f"行业: {sec['industry_raw']}（模型 {sec['industry_model']}） · 交易所 {sec['exchange']}")

if snap is None:
    st.warning("该标的已同步行情/财报，但尚无研究快照。点击下方「立即生成研究报告」完成 Phase 3 估值评分。")
else:
    st.caption(f"数据更新时间: **{snap['as_of_date']}** · 模型版本 {snap['model_version']}")

# ------------------------------------------------------------ top actions #
c0a, c0b = st.columns([1, 3])
rerun = c0a.button("⚡ 立即生成/更新研究报告", type="primary")
if rerun:
    if not admin_required("rerun_research"):
        st.warning("该操作会写入研究快照，需要管理密码。解锁后请再次点击执行。")
    else:
        with st.spinner("重新评分中…"):
            try:
                out = svc.run_research_now(engine, security_id, date.today())
                st.success(
                    f"评分完成: Research {out['research_score']:.1f} · "
                    f"Confidence {out['confidence_score']:.0f}% · 价格状态 {out['price_state']}"
                )
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(f"评分失败: {exc}")

ci = svc.chart_inputs(engine, security_id)
current_price = ci["close"][-1]["close"] if ci["close"] else None

if snap is None:
    st.stop()

# ---------------------------------------------------------------- top kpis #
val: dict = snap.get("valuation") or {}
dims: dict[str, float] = snap.get("dimension_scores") or {}
risk = snap.get("risk_score")

k1, k2, k3, k4 = st.columns(4)
k1.metric("当前价（元）", f"{current_price:.2f}" if current_price else "—")
k2.metric("Research 评分", f"{snap['research_score']:.1f}")
k3.metric("基本面 / 质量 / 成长",
          f"{dims.get('fundamental', 0):.0f} / {dims.get('quality', 0):.0f} / {dims.get('growth', 0):.0f}")
k4.metric("估值 / 周期 / 股东回报",
          f"{dims.get('valuation', 0):.0f} / {dims.get('cycle', 0):.0f} / {dims.get('shareholder', 0):.0f}")

k5, k6, k7, k8 = st.columns(4)
state = val.get("price_state")
state_label = val.get("price_state_label") or state or "—"
k5.metric("价格状态（五档）", state_label if state else "—")
exp_ret = val.get("expected_return")
adj_exp = (snap.get("zones") or {}).get("adjusted_expected_return")
k6.metric("预期收益（情景加权）",
          f"{exp_ret*100:.1f}%" if exp_ret is not None else "—",
          delta=f"置信调整 {adj_exp*100:.1f}%" if adj_exp is not None else None)
k7.metric("标准买入价（元）",
          f"{val['standard_buy_price']:.2f}" if val.get("standard_buy_price") else "—",
          delta=f"{current_price/val['standard_buy_price']-1:+.1%}" if (current_price and val.get("standard_buy_price")) else None,
          delta_color="inverse")
k8.metric("保守买入价（元）",
          f"{val['conservative_buy_price']:.2f}" if val.get("conservative_buy_price") else "—",
          delta=f"{current_price/val['conservative_buy_price']-1:+.1%}" if (current_price and val.get("conservative_buy_price")) else None,
          delta_color="inverse")

st.caption(f"Research {snap['research_score']:.1f} · Risk {risk:.1f} · Confidence {snap['confidence_score']:.0f}% · "
           f"DataFreshness {snap.get('data_freshness_score') or 0:.0f}")

# ----------------------------------------- V1.1 valuation zones (patch 1-2) -- #
zones = snap.get("zones") or {}
adj_er = zones.get("adjusted_expected_return")
conf_pct = zones.get("valuation_confidence")


def _fmt_zone(zr):
    if not zr:
        return "—"
    return f"¥{zr[0]:.2f} – {zr[1]:.2f}"


if zones.get("fair_value_range") or zones.get("standard_buy_zone"):
    z1, z2, z3, z4 = st.columns(4)
    z1.metric("公允价值区间（元）", _fmt_zone(zones.get("fair_value_range")))
    z2.metric("标准买入区间（元）", _fmt_zone(zones.get("standard_buy_zone")))
    z3.metric("保守买入区间（元）", _fmt_zone(zones.get("conservative_buy_zone")))
    z4.metric("估值置信度",
              f"{conf_pct:.0f}/100" if conf_pct is not None else "—",
              delta=f"Adjusted ER {adj_er*100:.1f}%" if adj_er is not None else None)
    st.caption("V1.1 估值区间：带宽随置信度收窄；Adjusted ER = 情景加权收益 × (0.5 + 置信度/200)，"
               "系统 B 决策以置信度调整后的收益为准。")

# -------------------------------------------------- three-scenario table -- #
scens = val.get("scenarios") or []
if scens:
    st.subheader("三情景预期收益（3 年）")
    st.dataframe(
        pd.DataFrame([
            {
                "情景": s.get("label"),
                "增长假设": f"{s.get('growth'):.1%}" if s.get("growth") is not None else "—",
                "退出倍数": f"{s.get('terminal_multiple'):g}" if s.get("terminal_multiple") is not None else "—",
                "3 年目标价(元)": f"{s.get('terminal_value'):.2f}" if s.get("terminal_value") is not None else "—",
                "累计股息(元)": f"{s.get('dividends_per_share'):.2f}" if s.get("dividends_per_share") else "—",
                "预期 CAGR": f"{s.get('cagr')*100:.1f}%" if s.get("cagr") is not None else "—",
            }
            for s in scens
        ]),
        hide_index=True, use_container_width=True,
    )

# ---------------------------------------------------------------- charts --- #
st.subheader("图表")
t1, t2 = st.tabs(["股价与估值分位", "财务与估值区间"])

with t1:
    cc1, cc2 = st.columns(2)
    with cc1:
        st.plotly_chart(charts.price_chart(ci["close"], current_price), use_container_width=True)
        st.plotly_chart(
            charts.percentile_chart(ci["pe_pb"].get("pe_history") or [], ci["pe_pb"].get("pe_ttm"), "PE(TTM)", "#2980b9"),
            use_container_width=True,
        )
    with cc2:
        st.plotly_chart(charts.roe_chart(ci["financial"]), use_container_width=True)
        st.plotly_chart(
            charts.percentile_chart(ci["pe_pb"].get("pb_history") or [], ci["pe_pb"].get("pb"), "PB", "#27ae60"),
            use_container_width=True,
        )

with t2:
    st.plotly_chart(charts.financial_chart(ci["financial"]), use_container_width=True)
    st.plotly_chart(
        charts.valuation_chart(
            current_price,
            val.get("fair_value_bear"), val.get("fair_value_base"), val.get("fair_value_bull"),
            val.get("standard_buy_price"), val.get("conservative_buy_price"),
        ),
        use_container_width=True,
    )

# -------------------------------------------------------- factor table ----- #
st.subheader("因子贡献明细")
expl = snap.get("explanation") or {}
dims_def = expl.get("dimensions") or []
factor_rows: list[dict] = []
for d in dims_def:
    for f in d.get("factors", []):
        factor_rows.append({
            "维度": d.get("label"),
            "因子": f.get("label"),
            "Raw": f"{f.get('raw'):.2f}" if f.get("raw") is not None else "缺",
            "得分": f"{f.get('score'):.1f}" if f.get("score") is not None else "—",
            "权重": f"{f.get('weight'):.3f}",
            "方向": f.get("direction"),
            "方法": f.get("method"),
            "缺失": "是" if f.get("missing") else "",
        })
if factor_rows:
    st.dataframe(pd.DataFrame(factor_rows), hide_index=True, use_container_width=True, height=420)
else:
    st.info("无因子明细（快照可能来自旧版本）。")

risks = snap.get("risk_flags") or []
if risks:
    with st.expander(f"风险标记（{len(risks)} 项）"):
        for r in risks:
            st.markdown(f"- {r}")

notes = (val.get("notes") or []) + (expl.get("extra") or {}).get("notes", []) if isinstance(expl.get("extra"), dict) else (val.get("notes") or [])
if val.get("missing_keys"):
    st.caption("⚠️ 估值数据缺口: " + ", ".join(val["missing_keys"]))
if notes:
    with st.expander("估值说明"):
        for n in notes:
            st.markdown(f"- {n}")

st.divider()
st.caption("仅作研究参考，不构成投资建议 · Phase 3 估值: 三情景 DCF/相对估值 + 两档买入价（标准/保守）")

