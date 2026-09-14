# -*- coding: utf-8 -*-
"""添加新标的（受管理密码保护）——同步行情/财报 -> 生成研究评分与估值。

部署在 HF Spaces 时：
- 未配置 ADMIN_PASSWORD Secret -> 任何访客均可添加（本地/LAN 行为）。
- 配置了 ADMIN_PASSWORD -> 添加标的必须先输入管理密码。
"""
from __future__ import annotations

import streamlit as st
from datetime import date

from src.db.engine import get_engine, init_database
from src.ui.auth import admin_required

st.set_page_config(page_title="添加标的 · 投资投研", page_icon="➕", layout="wide")

engine = get_engine()
init_database(engine)

st.title("➕ 添加新标的")
st.caption("输入 A 股代码：系统将从免费数据源同步历史行情与财报，立即生成研究评分、估值与买入价。此操作会写入数据库，需管理权限。")

# ---- 写操作守卫：未配置 ADMIN_PASSWORD 时直接放行 ------------------------ #
if not admin_required("add_security"):
    st.stop()

symbol = st.text_input("股票代码", placeholder="例如：600036 / 601318.SH / 601899 / 000001")
as_of = st.date_input("分析基准日", value=date.today())

if st.button("🚀 同步并分析", type="primary", disabled=not symbol.strip()):
    with st.spinner(f"正在同步并分析 {symbol.strip()}（首次调用需联网拉取数据，耗时较长）…"):
        try:
            from src.dashboard.services import add_security_and_research

            out = add_security_and_research(engine, symbol.strip(), as_of=as_of)
        except Exception as exc:  # noqa: BLE001 - 页面需要展示真实失败原因
            st.error(f"添加失败：{exc}。请确认代码正确且数据源可访问。")
            st.stop()

    st.success(f"✅ {out['symbol']} {out['name']} 已入库并完成分析（{out['as_of']}）")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("研究评分", f"{out['research_score']:.1f}")
    col2.metric("置信度", f"{out['confidence_score']:.1f}")
    col3.metric("价格状态", out["price_state"] or "—")
    col4.metric(
        "公允价值(基准)",
        f"{out['fair_value']:.2f}" if out["fair_value"] is not None else "—",
    )
    st.markdown("---")
    st.subheader("同步明细")
    st.markdown(
        f"- **行业模型**：{out['industry_model']}  "
        f"· 行情行数：{out['market_rows']}（至 {out['latest_trade_date']}，"
        f"{out['market_source']}）  "
        f"· 财报行数：{out['financial_rows']}（至 {out['latest_report_period']}，"
        f"{out['financial_source']}）"
    )
    st.markdown(
        f"- **标准买入价**：{out['standard_buy_price']:.2f}  元（保守档见研究报告页）"
    )
    st.markdown("去 **研究报告** 页可查看完整评分明细与估值区间。")