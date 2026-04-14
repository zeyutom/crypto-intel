"""历史走势: 各因子时序图 + 价格 / 稳定币 / F&G / ETF 历史。"""
import streamlit as st
import sys, json
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import (
    require_password, page_header, sidebar_branding,
    load_etf_history, load_fg_history, load_stablecoin_history,
    load_factor_history, COLOR,
)
from src.db import query_df
from src.factors._metadata import factor_meta, asset_cn
import plotly.graph_objects as go
from plotly.subplots import make_subplots

st.set_page_config(page_title="历史走势 · Crypto Intel", page_icon="📈", layout="wide")
require_password()
sidebar_branding()
page_header("📈 历史走势", "时序视图 · 看趋势, 不只看快照")


def base_layout(title: str = "", height: int = 280):
    return dict(
        title=title,
        height=height,
        margin=dict(l=10, r=10, t=30, b=10),
        plot_bgcolor=COLOR["panel"],
        paper_bgcolor=COLOR["bg"],
        font=dict(color=COLOR["muted"], size=11),
        hovermode="x unified",
    )


# === 1. 价格历史 (按 asset 选) ===
st.subheader("① 价格历史 (24h 内的多次采集快照)")
price_df = query_df(
    """SELECT ts, asset_id, value FROM raw_metrics
       WHERE source='binance' AND metric='price_usd'
       ORDER BY ts"""
)
if not price_df.empty:
    price_df["ts"] = pd.to_datetime(price_df["ts"])
    fig = go.Figure()
    for asset, grp in price_df.groupby("asset_id"):
        if len(grp) < 2:
            continue
        # 归一化为 0 = 第一次采集的价格
        norm = (grp["value"] / grp["value"].iloc[0] - 1) * 100
        fig.add_trace(go.Scatter(x=grp["ts"], y=norm, name=asset_cn(asset),
                                 mode="lines", line=dict(width=2)))
    fig.update_layout(**base_layout("自首次采集以来的相对涨跌 (%)", 320))
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor=COLOR["line"], zeroline=True, zerolinecolor=COLOR["muted"])
    st.plotly_chart(fig, use_container_width=True)
    st.caption("⏳ 数据点会随采集次数增多而变密。每天 GitHub Actions 跑 1 次,本地 cron 可设更频繁。")
else:
    st.info("还没有价格历史数据。")

st.divider()

# === 2. ETF 历史 ===
etf = load_etf_history()
if not etf.empty:
    st.subheader("② BTC 现货 ETF 净流入 (近 30 日)")
    colors = [COLOR["ok"] if v >= 0 else COLOR["alert"] for v in etf["value"]]
    fig = go.Figure(go.Bar(
        x=etf["ts"], y=etf["value"], marker_color=colors,
        text=[f"{v:+.0f}" for v in etf["value"]],
        textposition="outside",
    ))
    fig.update_layout(**base_layout("单位: 百万美元 ($M)", 320))
    fig.update_yaxes(gridcolor=COLOR["line"], zeroline=True, zerolinecolor=COLOR["muted"])
    fig.update_xaxes(showgrid=False)
    st.plotly_chart(fig, use_container_width=True)
    cum_5d = float(etf.tail(5)["value"].sum())
    cum_30d = float(etf["value"].sum())
    cols = st.columns(2)
    cols[0].metric("近 5 日累计", f"${cum_5d:+.1f}M")
    cols[1].metric("近 30 日累计", f"${cum_30d:+.1f}M")

    st.divider()

# === 3. F&G 历史 ===
fg = load_fg_history()
if not fg.empty:
    st.subheader("③ 恐慌贪婪指数走势")
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=fg["ts"], y=fg["value"], mode="lines+markers",
                             line=dict(color=COLOR["accent"], width=2),
                             marker=dict(size=5),
                             hovertemplate="%{x|%Y-%m-%d}<br>F&G: %{y}<extra></extra>"))
    # 恐慌/贪婪阈值线
    fig.add_hline(y=25, line_dash="dash", line_color=COLOR["ok"],
                  annotation_text="抄底线 (25)", annotation_position="left")
    fig.add_hline(y=75, line_dash="dash", line_color=COLOR["alert"],
                  annotation_text="减仓线 (75)", annotation_position="left")
    fig.update_layout(**base_layout("0 = 极度恐慌, 100 = 极度贪婪", 320))
    fig.update_yaxes(range=[0, 100], gridcolor=COLOR["line"])
    fig.update_xaxes(showgrid=False)
    st.plotly_chart(fig, use_container_width=True)
    st.caption("低于 25 = 历史抄底窗口, 高于 75 = 历史减仓窗口。")

    st.divider()

# === 4. 稳定币流通 ===
stable = load_stablecoin_history()
if not stable.empty and len(stable) >= 2:
    st.subheader("④ 全球稳定币流通量")
    fig = go.Figure(go.Scatter(
        x=stable["ts"], y=stable["value"] / 1e9, mode="lines",
        line=dict(color=COLOR["blue"], width=2),
        fill="tozeroy", fillcolor="rgba(106,166,255,0.1)",
    ))
    fig.update_layout(**base_layout("单位: 十亿美元 (B$)", 280))
    fig.update_yaxes(gridcolor=COLOR["line"])
    fig.update_xaxes(showgrid=False)
    st.plotly_chart(fig, use_container_width=True)
    cur = float(stable["value"].iloc[-1])
    chg = float(stable["value"].iloc[-1] - stable["value"].iloc[0])
    cols = st.columns(2)
    cols[0].metric("当前流通", f"${cur/1e9:.1f}B")
    cols[1].metric(f"近 {len(stable)} 个数据点变化", f"${chg/1e9:+.2f}B")

    st.divider()

# === 5. 任选因子看历史 ===
st.subheader("⑤ 任选因子看历史趋势")
all_factors = query_df("SELECT DISTINCT factor FROM factors")["factor"].tolist()
if all_factors:
    factor_names = {factor_meta(f).cn_name if factor_meta(f) else f: f for f in all_factors}
    sel = st.selectbox("选择因子", list(factor_names.keys()))
    fname = factor_names[sel]
    hist = load_factor_history(fname)
    if not hist.empty and len(hist) >= 2:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=hist["ts"], y=hist["raw_value"], name="原值",
                                 mode="lines+markers",
                                 line=dict(color=COLOR["accent"], width=2),
                                 marker=dict(size=5)))
        fig.add_trace(go.Bar(x=hist["ts"], y=hist["signal"], name="信号",
                             marker_color=[COLOR["ok"] if s == 1 else (COLOR["alert"] if s == -1 else COLOR["muted"]) for s in hist["signal"].fillna(0).astype(int)],
                             opacity=0.5,
                             yaxis="y2"), secondary_y=True)
        fig.update_layout(**base_layout(sel, 360))
        fig.update_yaxes(title_text="原值", gridcolor=COLOR["line"], secondary_y=False)
        fig.update_yaxes(title_text="信号 (-1/0/1)", range=[-1.5, 1.5], secondary_y=True, showgrid=False)
        fig.update_xaxes(showgrid=False)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.caption("⏳ 数据点不够画历史曲线 (需 ≥ 2 次采集)。")
