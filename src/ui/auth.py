# -*- coding: utf-8 -*-
"""部署层权限门禁（HF Spaces 场景）。

规则：
- 未配置 ADMIN_PASSWORD 环境变量 / HF Space Secret → 直接放行（保持本地/LAN 单机行为不变）。
- 配置了 ADMIN_PASSWORD → “查看”无需鉴权，任何“写操作/添加标的”必须先输入管理密码解锁。
密码比较使用 hmac.compare_digest，避免时序攻击。
"""
from __future__ import annotations

import hmac
import os

import streamlit as st


def admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", "").strip()


def admin_enabled() -> bool:
    return bool(admin_password())


def _verify(pw: str) -> bool:
    expected = admin_password()
    if not expected:
        return True
    return hmac.compare_digest(pw or "", expected)


def _unlock_key(panel_key: str) -> str:
    return f"_admin_ok_{panel_key}"


def admin_required(panel_key: str = "admin") -> bool:
    """写操作守卫。返回 True 表示当前会话已授权；页面在收到 False 时应 st.stop()。"""
    if not admin_enabled():
        return True
    if st.session_state.get(_unlock_key(panel_key)):
        return True
    with st.container(border=True):
        st.markdown("🔒 **管理操作**：该操作会写入数据，请输入管理密码。")
        pw = st.text_input("管理密码", type="password", key=f"_admin_pw_{panel_key}")
        if st.button("解锁", key=f"_admin_btn_{panel_key}"):
            if _verify(pw):
                st.session_state[_unlock_key(panel_key)] = True
                st.success("已解锁，可执行管理操作。")
                st.rerun()
            else:
                st.error("密码错误。")
    return False