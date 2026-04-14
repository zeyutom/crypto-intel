"""Crypto Intel · Streamlit 仪表盘主入口 (PM 早会简报页)。

启动方式 (本地):
    streamlit run streamlit_app.py
"""
import streamlit as st
import pandas as pd
import sys
from pathlib import Path

# 确保可以 import src/
sys.path.insert(0, str(Path(__file__).parent))

from src.db import init_db, query_df, latest_factors
from src.dashboard_utils import (
    require_password, page_header, sidebar_branding,
    load_factor_cards, load_signals, load_snapshot, load_etf_history,
    load_source_status, signal_pill, COLOR,
)
from src.report.insights import build_briefing
from src.factors._metadata import regime_cn

st.set_page_config(
    page_title="Crypto Intel · 早会简报",
    page_icon="📋",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 确保 DB 表存在 (首次部署)
init_db()

require_password()
sidebar_branding()

# ============ 页面 ============
page_header("📋 PM 早会简报",
            "今日多空论据 · Regime · 行动建议")

# === 简报 ===
fact_df = latest_factors()
sig_df = load_signals()
src_status = load_source_status()
missing = [s["source"] for s in src_status if not s["ok"]]
regime = sig_df.iloc[0]["regime"] if not sig_df.empty else "UNKNOWN"
briefing = build_briefing(sig_df, fact_df, regime, missing)
rgm_cn, rgm_exp = regime_cn(regime)

# Headline + Regime
st.markdown(f"""
<div style='background: linear-gradient(135deg, rgba(94,224,193,0.08), rgba(168,132,255,0.08));
            border: 1px solid {COLOR["line"]}; border-radius: 14px;
            padding: 20px 24px; margin-bottom: 18px;'>
  <div style='font-size: 18px; font-weight: 600; margin-bottom: 6px;'>📋 {briefing.headline}</div>
  <div style='color: {COLOR["muted"]}; font-size: 13px;'>
    当前市场状态: <b style='color:{COLOR["accent"]}'>{briefing.regime_cn}</b> — {briefing.regime_explain}
  </div>
</div>
""", unsafe_allow_html=True)

if briefing.coverage_warning:
    st.warning(briefing.coverage_warning)

# Bull / Bear / Risk / Action 四栏
c1, c2 = st.columns(2)
with c1:
    if briefing.bull_points:
        st.markdown(f"#### 📈 看多论据 ({len(briefing.bull_points)})")
        for p in briefing.bull_points:
            st.markdown(f"- {p}")
    if briefing.risk_warnings:
        st.markdown(f"#### ⚠️ 风险提示")
        for p in briefing.risk_warnings:
            st.warning(p)
with c2:
    if briefing.bear_points:
        st.markdown(f"#### 📉 看空论据 ({len(briefing.bear_points)})")
        for p in briefing.bear_points:
            st.markdown(f"- {p}")
    if briefing.pm_actions:
        st.markdown(f"#### 🎯 给 PM 的行动建议")
        for p in briefing.pm_actions:
            st.success(p)

st.divider()

# === 行情快照 ===
st.subheader("① 行情快照")
snap = load_snapshot()
if not snap.empty:
    cols = st.columns(min(len(snap), 4))
    for i, (_, r) in enumerate(snap.iterrows()):
        with cols[i % len(cols)]:
            ch = r.get("change_24h")
            delta = f"{ch:+.2f}%" if pd.notna(ch) else None
            st.metric(
                label=f"{r['symbol']} · {r['cn_name']}",
                value=f"${r['price']:,.2f}",
                delta=delta,
            )
else:
    st.info("行情快照暂无数据,请先运行采集。")

st.divider()

# === 多因子合成信号 ===
st.subheader("② 多因子合成信号")
if not sig_df.empty:
    rows = []
    for _, s in sig_df.iterrows():
        from src.factors._metadata import asset_cn
        rows.append({
            "资产": asset_cn(s["asset_id"]),
            "方向": s["direction"],
            "合成分": round(float(s["composite"]), 3),
            "置信度": f"{float(s['confidence']):.0%}",
            "Regime": regime_cn(s["regime"])[0],
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("合成分: > +0.2 看多 / < -0.2 看空 · 置信度 = 实际贡献的因子权重 / 全部因子权重")
else:
    st.info("信号尚未合成。")

st.divider()

# === ETF 净流入 (近 10 日) ===
etf = load_etf_history()
if not etf.empty:
    st.subheader("③ BTC 现货 ETF 近 10 日净流入")
    import plotly.graph_objects as go
    e10 = etf.tail(10).copy()
    colors = [COLOR["ok"] if v >= 0 else COLOR["alert"] for v in e10["value"]]
    fig = go.Figure(go.Bar(
        x=e10["ts"].dt.strftime("%m/%d"),
        y=e10["value"],
        marker_color=colors,
        text=[f"{v:+.0f}M" for v in e10["value"]],
        textposition="outside",
    ))
    fig.update_layout(
        height=280,
        margin=dict(l=10, r=10, t=10, b=10),
        plot_bgcolor=COLOR["panel"],
        paper_bgcolor=COLOR["bg"],
        font=dict(color=COLOR["muted"], size=11),
        yaxis=dict(title="净流入 ($M)", gridcolor=COLOR["line"]),
        xaxis=dict(showgrid=False),
    )
    st.plotly_chart(fig, use_container_width=True)
    cum_5d = float(e10.tail(5)["value"].sum())
    direction = "📈 流入" if cum_5d > 0 else "📉 流出"
    st.caption(f"近 5 日累计 {direction} ${cum_5d:+.1f}M · 绿色=机构买入, 红色=机构赎回")

st.divider()
st.info("👉 左侧导航查看更多: 因子详解 / 信号矩阵 / 历史走势 / 数据健康 / 术语词典")
