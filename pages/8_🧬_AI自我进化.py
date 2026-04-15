"""AI 自我进化日志: 周复盘 / 因子提议 / 源发现 / prompt 迭代 + narrative 追踪。"""
import streamlit as st
import sys, json
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import require_password, page_header, sidebar_branding, COLOR
from src.db import query_df
from src.feedback import stats as fb_stats
import plotly.graph_objects as go

st.set_page_config(page_title="AI 自我进化 · Crypto Intel", page_icon="🧬", layout="wide")
require_password()
sidebar_branding()
page_header("🧬 AI 自我进化日志", "Claude 写的周复盘 · 因子提议 · 源发现 · 叙事追踪 · PM 反馈汇总")

# === 顶部 stats ===
fb = fb_stats()
cols = st.columns(4)
evo_count = int(query_df("SELECT COUNT(*) AS c FROM evolution_log").iloc[0]["c"])
cols[0].metric("进化事件累计", evo_count)
cols[1].metric("PM 反馈", f"{fb['total']} 条", f"👍{fb['thumbs_up']} / 👎{fb['thumbs_down']}")
perf_n = int(query_df("SELECT COUNT(DISTINCT factor) AS c FROM factor_performance").iloc[0]["c"])
cols[2].metric("已回测因子", perf_n)
snap_days = int(query_df("SELECT COUNT(DISTINCT date) AS c FROM factor_snapshots").iloc[0]["c"])
cols[3].metric("积累快照天数", snap_days)

st.divider()

# === Narrative 热度排行 ===
st.subheader("🔥 当前 Narrative 热度")
nar_df = query_df(
    """SELECT * FROM narratives
       WHERE date = (SELECT MAX(date) FROM narratives)
       ORDER BY heat_score DESC"""
)
if nar_df.empty:
    st.info("⏳ Narrative 追踪未跑。运行 `python -m src.cli track-narratives` 或等每日 launchd。")
else:
    # Bar chart
    fig = go.Figure()
    colors = [COLOR["ok"] if d > 0 else (COLOR["alert"] if d < 0 else COLOR["muted"])
              for d in nar_df["delta_7d"]]
    fig.add_trace(go.Bar(
        y=nar_df["narrative"], x=nar_df["heat_score"], orientation="h",
        marker_color=colors,
        text=[f"{s:.0f} ({'+' if d>=0 else ''}{d:.0f})" for s, d in
              zip(nar_df["heat_score"], nar_df["delta_7d"])],
        textposition="outside",
    ))
    fig.update_layout(height=400, plot_bgcolor=COLOR["panel"], paper_bgcolor=COLOR["bg"],
                      font=dict(color=COLOR["muted"]),
                      margin=dict(l=10, r=10, t=30, b=10),
                      xaxis=dict(title="热度分 (0-100)", range=[0, 100]))
    st.plotly_chart(fig, use_container_width=True)

    # 每个 narrative 的详情
    with st.expander("📋 Narrative 详情 (top tokens + 事件)"):
        for _, r in nar_df.iterrows():
            tokens = json.loads(r["top_tokens"] or "[]")
            events = json.loads(r["trigger_events"] or "[]")
            st.markdown(f"**{r['narrative']}** · 热度 {r['heat_score']:.0f} · 7d {'+' if r['delta_7d']>=0 else ''}{r['delta_7d']:.0f}")
            if tokens:
                st.caption(f"Top tokens: {', '.join(tokens[:5])}")
            if events:
                for e in events[:3]:
                    st.caption(f"- {e}")

st.divider()

# === 进化日志按类型 Tab ===
st.subheader("📚 进化日志")

tabs = st.tabs(["📝 周度复盘", "💡 因子提议", "🔍 源发现", "🎨 Prompt 迭代", "📮 PM 反馈"])

def _render_log(kind: str, limit: int = 10):
    df = query_df(
        """SELECT ts, title, content, action FROM evolution_log
           WHERE kind=? ORDER BY ts DESC LIMIT ?""", (kind, limit)
    )
    if df.empty:
        st.info("⏳ 还没有记录。每周/每月 launchd 会自动跑产生内容。")
        return
    for _, r in df.iterrows():
        act_color = {"accepted": "🟢", "rejected": "🔴", "pending": "🟡"}
        icon = act_color.get(r["action"], "⚪")
        with st.expander(f"{icon} {r['title']} · {r['ts'][:16].replace('T', ' ')} UTC"):
            st.markdown(r["content"])

with tabs[0]:
    _render_log("weekly_review")
with tabs[1]:
    _render_log("factor_proposal")
with tabs[2]:
    _render_log("source_discovery")
with tabs[3]:
    _render_log("prompt_evolve")
with tabs[4]:
    # PM 反馈列表
    fb_df = query_df(
        """SELECT ts, brief_ts, rating, comment FROM brief_feedback
           ORDER BY ts DESC LIMIT 30"""
    )
    if fb_df.empty:
        st.info("还没有任何 PM 反馈。在主页简报下方点 👍/👎 + 写评论。")
    else:
        for _, r in fb_df.iterrows():
            tag = "👍" if r["rating"] == 1 else ("👎" if r["rating"] == -1 else "中性")
            with st.container(border=True):
                st.markdown(f"{tag} · {r['ts'][:16].replace('T', ' ')} UTC · 对应简报 {r['brief_ts'][:10]}")
                if (r["comment"] or "").strip():
                    st.caption(r["comment"])
