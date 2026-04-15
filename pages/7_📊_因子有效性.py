"""因子有效性: IC/IR 滚动监控 + 自动加权展示。"""
import streamlit as st
import sys, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import require_password, page_header, sidebar_branding, COLOR
from src.db import query_df
from src.factors._metadata import factor_meta, asset_cn
import plotly.graph_objects as go

st.set_page_config(page_title="因子有效性 · Crypto Intel", page_icon="📊", layout="wide")
require_password()
sidebar_branding()
page_header("📊 因子有效性", "滚动 IC/IR 监控 · 自动淘汰失效因子 · 动态加权")

st.info("💡 IC = 因子值与未来收益的相关系数 · IR = IC 均值 / IC 标准差 · IR > 0.5 = 可用, > 1.0 = 优秀")

# 1. 最新一次 IC/IR 汇总
perf_df = query_df(
    """SELECT factor, asset_id, window_days, forward_days, ic, ir, n_obs, mean_return, date
       FROM factor_performance
       WHERE date = (SELECT MAX(date) FROM factor_performance)
       ORDER BY ir DESC"""
)

if perf_df.empty:
    st.warning("⏳ 还没有 IC/IR 回测结果。需要先累积 30+ 天的每日 factor_snapshots。"
               "每天双击 2_本地AI日报+推送.command 一次, 30 天后系统会自动算出来。")
    st.stop()

# Summary cards
st.subheader("① 最新回测汇总")
cols = st.columns(4)
cols[0].metric("已评估因子数", perf_df["factor"].nunique())
cols[1].metric("IR > 0.5 (可用)", int((perf_df["ir"] >= 0.5).sum()))
cols[2].metric("IR > 1.0 (优秀)", int((perf_df["ir"] >= 1.0).sum()))
cols[3].metric("IR < 0 (失效)", int((perf_df["ir"] < 0).sum()))

st.divider()

# 2. 按因子聚合排行
st.subheader("② 因子有效性排行 (IR 降序)")

agg = (perf_df.groupby("factor")
              .agg({"ir": "max", "ic": "mean", "n_obs": "max"})
              .reset_index()
              .sort_values("ir", ascending=False))
agg["cn_name"] = agg["factor"].apply(
    lambda f: factor_meta(f).cn_name if factor_meta(f) else f)
agg["verdict"] = agg["ir"].apply(
    lambda ir: "🟢 优秀" if ir >= 1 else ("🟢 可用" if ir >= 0.5 else
                                          ("🟡 边缘" if ir >= 0 else "🔴 失效")))

# 渲染为表
display = agg[["cn_name", "verdict", "ir", "ic", "n_obs"]].copy()
display.columns = ["因子", "评价", "最佳 IR", "平均 IC", "观测数"]
display["最佳 IR"] = display["最佳 IR"].map(lambda v: f"{v:.3f}")
display["平均 IC"] = display["平均 IC"].map(lambda v: f"{v:+.3f}")
st.dataframe(display, use_container_width=True, hide_index=True)

st.divider()

# 3. 选某因子看详细 IC 走势
st.subheader("③ 某因子 IC 历史走势")
options = {factor_meta(f).cn_name if factor_meta(f) else f: f for f in agg["factor"]}
sel_cn = st.selectbox("选择因子", list(options.keys()))
sel = options[sel_cn]

hist_df = query_df(
    """SELECT date, forward_days, ic, ir, n_obs
       FROM factor_performance
       WHERE factor=? AND window_days=30
       ORDER BY date""", (sel,)
)

if hist_df.empty:
    st.caption("暂无历史数据 (需积累多日 snapshot)")
else:
    hist_df["date"] = pd.to_datetime(hist_df["date"])
    fig = go.Figure()
    for fwd, grp in hist_df.groupby("forward_days"):
        fig.add_trace(go.Scatter(x=grp["date"], y=grp["ic"],
                                 name=f"IC (前瞻 {fwd} 日)",
                                 mode="lines+markers",
                                 line=dict(width=2)))
    fig.add_hline(y=0.05, line_dash="dash", line_color=COLOR["ok"],
                  annotation_text="有效门槛 IC=0.05")
    fig.add_hline(y=0, line_color=COLOR["muted"])
    fig.update_layout(height=350,
                      plot_bgcolor=COLOR["panel"], paper_bgcolor=COLOR["bg"],
                      font=dict(color=COLOR["muted"]),
                      margin=dict(l=10, r=10, t=30, b=10),
                      hovermode="x unified")
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor=COLOR["line"])
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# 4. 当前 composite 用的实际权重
st.subheader("④ 当前信号合成实际使用的权重")
from src.signals.composite import _effective_weights, DEFAULT_WEIGHTS
eff = _effective_weights()
wrows = []
for f in sorted(set(list(eff.keys()) + list(DEFAULT_WEIGHTS.keys()))):
    cn = factor_meta(f).cn_name if factor_meta(f) else f
    default_w = DEFAULT_WEIGHTS.get(f, 0.5)
    eff_w = eff.get(f, default_w)
    source = "🎯 IR 动态" if eff_w > default_w else "🔧 默认"
    wrows.append({"因子": cn, "默认权重": default_w, "实际权重": round(eff_w, 3),
                  "来源": source})
wdf = pd.DataFrame(wrows).sort_values("实际权重", ascending=False)
st.dataframe(wdf, use_container_width=True, hide_index=True)
st.caption("系统会在 IR 接管后自动提升有效因子的权重, 降低无效因子。IR < 0 的因子权重为 0 (淘汰)。")
