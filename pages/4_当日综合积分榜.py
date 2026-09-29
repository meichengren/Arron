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


@st.cache_data(ttl=30, show_spinner=False)
def _ranked_rows():
    return svc.ranked_research_rows(engine)


st.title("🏆 当日综合积分榜")
st.caption("默认自选池已包含 27 个截图标的。刷新后会生成当日研究评分，并按综合积分从高到低排列。")
if imported_default_count:
    st.info(f"已导入 {imported_default_count} 个默认标的；点击“刷新当日积分”即可拉取数据并生成排名。")

with st.expander("📘 评分与数据置信度规则", expanded=True):
    score_rule, confidence_rule = st.columns(2)
    with score_rule:
        st.markdown("""**综合积分（用于排序）**

| 积分 | 标签 |
| --- | --- |
| ≥75 | 优先研究 |
| 65–74 | 值得关注 |
| 55–64 | 中性观察 |
| 45–54 | 谨慎 |
| ＜45 | 暂不优先 |""")
        st.caption("综合积分由基本面、成长、估值、质量、周期、股东回报加权，并按风险扣分；60 分已高于中性水平。")
    with confidence_rule:
        st.markdown("""**数据置信度（用于判断分数是否可靠）**

| 置信度 | 标签 |
| --- | --- |
| ≥80/100 | 数据可靠 |
| 60–79/100 | 可用，建议结合详情判断 |
| 40–59/100 | 数据不完整，谨慎参考 |
| ＜40/100 | 数据不足，不作为重点决策依据 |""")
        st.caption("置信度不是上涨概率；它衡量行情新鲜度、财报与估值数据完整度。ETF 不以缺少公司财报作为低置信度理由。")

last_failures = st.session_state.get("last_score_refresh_failures", [])
if last_failures:
    with st.expander(f"⚠️ 上次刷新有 {len(last_failures)} 个标的未完成评分", expanded=True):
        st.caption("这些标的仍显示“待评分”。可稍后点击“强制同步最新数据”重试。")
        st.dataframe(last_failures, use_container_width=True, hide_index=True)
        if st.button("清除失败记录", key="clear_score_refresh_failures"):
            st.session_state.pop("last_score_refresh_failures", None)
            st.rerun()

pending_count = sum(row["research_score"] is None for row in _ranked_rows())
quick_col, retry_col, sync_col, note_col = st.columns([1, 1.35, 1.25, 2.4])
with quick_col:
    quick_refresh = st.button("⚡ 快速重算积分", type="primary", use_container_width=True)
with retry_col:
    retry_pending = st.button(
        f"🩹 补全待评分（{pending_count}）",
        disabled=pending_count == 0,
        use_container_width=True,
    )
with sync_col:
    force_sync = st.button("🔄 强制同步最新数据", use_container_width=True)
with note_col:
    st.caption(
        f"评分日期：{date.today().isoformat()}。补全待评分会逐只重试并节流；强制同步会绕过 7 天财报缓存并更新最新行情。"
    )

if quick_refresh or retry_pending or force_sync:
    if admin_required("daily_scoreboard_refresh"):
        if retry_pending:
            label = "正在补全待评分标的（免费数据源会逐只重试）…"
        elif force_sync:
            label = "正在同步最新数据并评分…"
        else:
            label = "正在使用本地数据重算积分…"
        with st.spinner(label):
            result = svc.refresh_daily_research_scores(
                engine, sync_data=force_sync or retry_pending, only_pending=retry_pending
            )
        if result["succeeded"]:
            st.success(f"已更新 {len(result['succeeded'])} 个标的的当日积分。")
        else:
            st.info("当前没有可更新的标的。")
        st.session_state["last_score_refresh_failures"] = result["failed"]
        _ranked_rows.clear()
        st.rerun()

rows = _ranked_rows()
if not rows:
    st.info("尚未添加标的。请先前往“添加标的”完成查询与同步。")
    st.stop()

st.caption(f"共 {len(rows)} 个标的；“查看”可打开研究详情，“删除”会移除该标的及其关联的研究和风险记录。")
headers = st.columns([0.55, 2.0, 0.75, 1.35, 1.2, 1.2, 1.55, 1.8])
for column, title in zip(headers, ["排名", "标的", "类型", "综合积分", "风险评分", "置信度", "最近评分", "操作"]):
    column.markdown(f"**{title}**")

for rank, row in enumerate(rows, start=1):
    columns = st.columns([0.55, 2.0, 0.75, 1.35, 1.2, 1.2, 1.55, 1.8])
    columns[0].write(f"#{rank}")
    columns[1].write(f"{row['name']} · {row['symbol']}")
    columns[2].write(row["asset_type"])
    score = row["research_score"]
    columns[3].write(f"{score:.1f}" if score is not None else "待评分")
    risk_score = row["risk_score"]
    columns[4].write(f"{risk_score:.1f}" if risk_score is not None else "—")
    confidence = row["confidence_score"]
    columns[5].write(f"{confidence:.0f}/100" if confidence is not None else "—")
    columns[6].write(str(row["as_of_date"] or "—"))

    security_id = row["security_id"]
    confirm_key = f"confirm_delete_scoreboard_{security_id}"
    if st.session_state.get(confirm_key):
        columns[7].warning("确认删除？")
        confirm, cancel = columns[7].columns(2)
        if confirm.button("确认", key=f"delete_security_{security_id}", type="primary"):
            if admin_required(f"daily_scoreboard_delete_{security_id}"):
                if svc.delete_security(engine, security_id):
                    st.success(f"已删除 {row['symbol']}。")
                else:
                    st.info("该标的已被删除。")
                st.session_state.pop(confirm_key, None)
                _ranked_rows.clear()
                st.rerun()
        if cancel.button("取消", key=f"cancel_delete_{security_id}"):
            st.session_state.pop(confirm_key, None)
            st.rerun()
    else:
        detail, remove = columns[7].columns(2)
        detail.page_link(
            "pages/1_Research_Detail.py",
            label="查看",
        )
        if remove.button("删除", key=f"request_delete_{security_id}"):
            st.session_state[confirm_key] = True
            st.rerun()
