# -*- coding: utf-8 -*-
"""System A: 投资投研与组合量化决策系统 - Research Dashboard 主页.

Search + Library, thin UI only: every data access goes through
src.dashboard.services so the whole data layer stays unit-testable.
"""
from __future__ import annotations

import streamlit as st

from src.db.engine import get_engine, init_database
from src.dashboard import services as svc

st.set_page_config(page_title="投资投研 · Research Dashboard", page_icon="📈", layout="wide")


@st.cache_resource
def _engine():
    engine = get_engine()
    init_database(engine)
    return engine


engine = _engine()

st.title("📈 投资投研与组合量化决策系统")
st.caption("System A · Research Dashboard — 搜索 / 标的库 / 个股研究详情（估值、买价、情景收益）")

tab_search, tab_library = st.tabs(["🔍 搜索", "🗂️ 研究标的库"])

# ---------------------------------------------------------------- search --- #
with tab_search:
    st.subheader("搜索股票")
    c1, c2 = st.columns([3, 1])
    query = c1.text_input("代码 / 简称", placeholder="如 600036、600036.SH、招商银行")
    do_search = c2.button("搜索", type="primary")
    if (do_search or query) and query.strip():
        hits = svc.search_securities(engine, query)
        if not hits:
            st.info("未找到匹配标的。可先在「🗂️ 研究标的库」点击「重新评分」，或通过 CLI 同步新股票。")
        else:
            for h in hits:
                with st.container(border=True):
                    hc1, hc2, hc3 = st.columns([3, 1, 1])
                    hc1.markdown(f"**{h['name']}** · `{h['symbol']}` · {h['exchange']}")
                    hc2.caption(f"行业模型: {h['industry_model']}")
                    hc3.page_link("pages/1_Research_Detail.py", label="查看详情", icon="🔎",
                                  query_params={"security_id": str(h["id"])})

# --------------------------------------------------------------- library --- #
with tab_library:
    rows = svc.library_rows(engine)
    if not rows:
        st.warning("研究标的库为空。请先运行 `python -m src.cli --research 600036` 同步并评分。")
    else:
        st.caption(f"共 {len(rows)} 只标的 · 每行显示最新快照")
        for r in rows:
            with st.container(border=True):
                head, meta, mid, action = st.columns([2.2, 1.2, 2.2, 1.0])
                head.markdown(f"**{r['name']}** · `{r['symbol']}`")
                if r["as_of_date"] is not None:
                    meta.caption(f"最新研究 {r['as_of_date']}")
                else:
                    meta.caption("尚未评分")
                bits: list[str] = []
                if r["research_score"] is not None:
                    bits.append(f"Research **{r['research_score']:.1f}**")
                if r["confidence_score"] is not None:
                    bits.append(f"Confidence **{r['confidence_score']:.0f}%**")
                if r["price_state"]:
                    bits.append(f"价格状态 **{r['price_state']}**")
                if r["fair_value_base"] is not None:
                    bits.append(f"Fair(Base) **{r['fair_value_base']:.2f}**")
                if r["standard_buy_price"] is not None:
                    bits.append(f"标准买价 **{r['standard_buy_price']:.2f}**")
                mid.markdown(" · ".join(bits) if bits else "（无快照，点击重新评分生成）")
                action.page_link(
                    "pages/1_Research_Detail.py",
                    label="详情 / 图表", icon="📊",
                    query_params={"security_id": str(r["security_id"])},
                )

st.divider()
database_label = "PostgreSQL（Neon）" if engine.dialect.name == "postgresql" else "SQLite"
st.caption(
    f"数据层: {database_label} · 行情/财报同步 + 5 档价格状态估值 · 仅作研究参考，不构成投资建议"
)