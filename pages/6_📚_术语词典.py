"""术语词典: 11 个核心名词的中文解释。"""
import streamlit as st
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import require_password, page_header, sidebar_branding, COLOR
from src.report.glossary import GLOSSARY
from src.factors._metadata import META

st.set_page_config(page_title="术语词典 · Crypto Intel", page_icon="📚", layout="wide")
require_password()
sidebar_branding()
page_header("📚 术语词典", "不熟悉某个名词? 在这里查")

# === 通用术语 ===
st.subheader("① 通用术语")
search = st.text_input("🔎 搜索术语 (按中文/英文/缩写过滤)", "")

for term, defn in GLOSSARY:
    if search and search.lower() not in term.lower() and search not in defn:
        continue
    with st.container(border=True):
        st.markdown(f"### {term}")
        st.write(defn)

st.divider()

# === 因子定义查询 ===
st.subheader("② 当前所有因子的定义")
for fname, meta in META.items():
    with st.expander(f"📊 {meta.cn_name} · `{fname}`"):
        st.markdown(f"**类别**: {meta.category}")
        st.markdown(f"**一句话**: {meta.one_line}")
        st.markdown(f"**为什么重要**: {meta.why_matters}")
        st.markdown(f"**单位**: {meta.fmt_unit}")
        st.markdown("**信号方向解读**:")
        for sig, expl in meta.how_to_read.items():
            label = "看多 +1" if sig == 1 else ("看空 -1" if sig == -1 else "中性 0")
            st.markdown(f"- *{label}*: {expl}")
        st.markdown("**对应 PM 行动**:")
        for sig, action in meta.pm_action.items():
            label = "看多 +1" if sig == 1 else ("看空 -1" if sig == -1 else "中性 0")
            st.markdown(f"- *{label}*: {action}")
