"""因子详解页: 每个因子一张大卡片, 带"是什么/现在多少/怎么读/PM 怎么做"。"""
import streamlit as st
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import (
    require_password, page_header, sidebar_branding,
    load_factor_cards, load_factor_history, signal_pill, COLOR,
)
import plotly.graph_objects as go

st.set_page_config(page_title="因子详解 · Crypto Intel", page_icon="🎯", layout="wide")
require_password()
sidebar_branding()
page_header("🎯 因子详解", "每个因子: 是什么 → 现在多少 → 该怎么读 → PM 怎么做")

cards = load_factor_cards()

if not cards:
    st.warning("尚无因子数据。请先在主页点击「立即刷新数据」。")
    st.stop()

# 按类别分组
categories: dict[str, list[dict]] = {}
for c in cards:
    categories.setdefault(c["category"], []).append(c)

for cat, items in categories.items():
    st.subheader(f"📂 {cat}")
    for c in items:
        with st.container(border=True):
            cols = st.columns([3, 1])
            with cols[0]:
                st.markdown(f"### {c['cn_name']}")
                st.caption(c["one_line"])
            with cols[1]:
                muted = COLOR["muted"]
                rv = c['raw_value_fmt']
                fu = c['fmt_unit']
                pill = signal_pill(c['signal'])
                st.markdown(
                    f"<div style='text-align:right; padding-top:10px;'>"
                    f"<div style='font-size:24px; font-weight:600;'>{rv}</div>"
                    f"<div style='color:{muted}; font-size:11px;'>{fu}</div>"
                    f"<div style='margin-top:6px;'>{pill}</div>"
                    f"</div>", unsafe_allow_html=True)

            # 解读 + 行动
            tab1, tab2, tab3 = st.tabs(["📖 怎么读", "🎯 PM 建议", "📊 历史 + 详情"])
            with tab1:
                st.markdown(f"**{c['how_to_read'] or '当前值未触发明显信号。'}**")
                if c["why_matters"]:
                    st.caption(f"为什么重要: {c['why_matters']}")
            with tab2:
                if c["pm_action"]:
                    st.success(c["pm_action"])
                else:
                    st.info("当前因子无明确动作建议。")
                st.caption(f"置信度: {c['confidence']:.0%}")
            with tab3:
                hist = load_factor_history(c["factor"])
                if not hist.empty and len(hist) >= 2:
                    fig = go.Figure(go.Scatter(
                        x=hist["ts"], y=hist["raw_value"],
                        mode="lines+markers",
                        line=dict(color=COLOR["accent"], width=2),
                        marker=dict(size=6),
                    ))
                    fig.update_layout(
                        height=240,
                        margin=dict(l=10, r=10, t=10, b=10),
                        plot_bgcolor=COLOR["panel"],
                        paper_bgcolor=COLOR["bg"],
                        font=dict(color=COLOR["muted"], size=10),
                        yaxis=dict(title=c["fmt_unit"], gridcolor=COLOR["line"]),
                        xaxis=dict(showgrid=False),
                    )
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.caption("⏳ 数据点不够画历史曲线 (需至少 2 次采集)。继续运行几次即可。")
                if c["raw_meta"]:
                    with st.expander("原始 meta 数据 (调试用)"):
                        st.json(c["raw_meta"])
