# -*- coding: utf-8 -*-
"""展示已查询标的的当日综合积分排名。"""
from __future__ import annotations

from datetime import date

import streamlit as st

from src.dashboard import services as svc
from src.db.engine import get_engine, init_database
from src.ui.auth import admin_required


st.set_page_config(page_title="当日综合积分榜", page_icon="🏆", layout="wide")


@st.cache_resource
def _engine():
    engine = get_engine()
    init_database(engine)
    return engine


engine = _engine()
imported_default_count = svc.seed_default_watchlist(engine)

st.title("🏆 当日综合积分榜")
st.caption("默认自选池已包含 27 个截图标的。刷新后会生成当日研究评分，并按综合积分从高到低排列。")
if imported_default_count:
    st.info(f"已导入 {imported_default_count} 个默认标的；点击“刷新当日积分”即可拉取数据并生成排名。")

left, right = st.columns([1, 4])
with left:
    refresh = st.button("🔄 刷新当日积分", type="primary", use_container_width=True)
with right:
    st.caption(f"评分日期：{date.today().isoformat()}。刷新会为所有已添加标的生成最新评分；单个标的失败不会中断其余标的。")

if refresh:
    if admin_required("daily_scoreboard_refresh"):
        with st.spinner("正在刷新当日综合积分…"):
            result = svc.refresh_daily_research_scores(engine)
        if result["succeeded"]:
            st.success(f"已更新 {len(result['succeeded'])} 个标的的当日积分。")
        else:
            st.info("当前没有可更新的标的。")
        if result["failed"]:
            st.warning(f"{len(result['failed'])} 个标的未能更新：")
            st.dataframe(result["failed"], use_container_width=True, hide_index=True)
        st.rerun()

rows = svc.ranked_research_rows(engine)
if not rows:
    st.info("尚未添加标的。请先前往“添加标的”完成查询与同步。")
    st.stop()

st.caption(f"共 {len(rows)} 个标的；“查看”可打开研究详情，“删除”会移除该标的及其关联的研究和风险记录。")
headers = st.columns([0.55, 2.2, 1.45, 1.2, 1.2, 1.7, 1.8])
for column, title in zip(headers, ["排名", "标的", "综合积分", "风险评分", "置信度", "最近评分", "操作"]):
    column.markdown(f"**{title}**")

for rank, row in enumerate(rows, start=1):
    columns = st.columns([0.55, 2.2, 1.45, 1.2, 1.2, 1.7, 1.8])
    columns[0].write(f"#{rank}")
    columns[1].write(f"{row['name']} · {row['symbol']}")
    score = row["research_score"]
    columns[2].write(f"{score:.1f}" if score is not None else "待评分")
    risk_score = row["risk_score"]
    columns[3].write(f"{risk_score:.1f}" if risk_score is not None else "—")
    confidence = row["confidence_score"]
    columns[4].write(f"{confidence:.0%}" if confidence is not None else "—")
    columns[5].write(str(row["as_of_date"] or "—"))

    security_id = row["security_id"]
    confirm_key = f"confirm_delete_scoreboard_{security_id}"
    if st.session_state.get(confirm_key):
        columns[6].warning("确认删除？")
        confirm, cancel = columns[6].columns(2)
        if confirm.button("确认", key=f"delete_security_{security_id}", type="primary"):
            if admin_required(f"daily_scoreboard_delete_{security_id}"):
                if svc.delete_security(engine, security_id):
                    st.success(f"已删除 {row['symbol']}。")
                else:
                    st.info("该标的已被删除。")
                st.session_state.pop(confirm_key, None)
                st.rerun()
        if cancel.button("取消", key=f"cancel_delete_{security_id}"):
            st.session_state.pop(confirm_key, None)
            st.rerun()
    else:
        detail, remove = columns[6].columns(2)
        detail.page_link(
            "pages/1_Research_Detail.py",
            label="查看",
        )
        if remove.button("删除", key=f"request_delete_{security_id}"):
            st.session_state[confirm_key] = True
            st.rerun()
