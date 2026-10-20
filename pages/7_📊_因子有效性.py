"""因子有效性: IC/IR 滚动监控 + 自动加权展示。"""
import streamlit as st
import sys, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import require_password, page_header, sidebar_branding, COLOR
from src.db import query_df
from src.factors._metadata import factor_meta, asset_cn
from src.research.dashboard import (
    factor_mean_ir, ir_verdict, ir_weight_rule_text, ir_weight_source,
)
import plotly.graph_objects as go

st.set_page_config(page_title="因子有效性 · Crypto Intel", page_icon="📊", layout="wide")
require_password()
sidebar_branding()
page_header("📊 因子有效性", "滚动 IC/IR 监控 · IR 有界调权 (失效因子降权, 不归零)")

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

# 信号合成调权实际使用的 IR: 每个 (因子, 资产) 最新计算日、过 n_eff 门槛、clip 后的均值,
# 再按因子跨资产平均 — 与 composite._effective_weights 同口径。排行 / 评价 / 权重都用它。
try:
    from src.review.backtest import get_latest_ir_weights
    used_ir = factor_mean_ir(get_latest_ir_weights())
except Exception:
    used_ir = {}

# Summary cards (按因子计数, 不再数 factor×asset×window×forward 行)
st.subheader("① 最新回测汇总")
cols = st.columns(4)
cols[0].metric("已评估因子数", perf_df["factor"].nunique())
cols[1].metric("IR ≥ 0.5 (可用)", sum(1 for v in used_ir.values() if v >= 0.5))
cols[2].metric("IR ≥ 1.0 (优秀)", sum(1 for v in used_ir.values() if v >= 1.0))
cols[3].metric("IR < 0 (降权)", sum(1 for v in used_ir.values() if v < 0))
st.caption("按因子计数; IR = 信号合成调权实际使用的 IR 均值 (见 ②)")

st.divider()

# 2. 按因子聚合排行
st.subheader("② 因子有效性排行 (调权用 IR 降序)")

raw = (perf_df.groupby("factor")
              .agg({"ir": "max", "ic": "mean", "n_obs": "max"})
              .rename(columns={"ir": "max_ir"}))
agg = pd.DataFrame({"factor": sorted(set(raw.index) | set(used_ir))})
agg["used_ir"] = agg["factor"].map(used_ir)
agg = (agg.join(raw, on="factor")
          .sort_values("used_ir", ascending=False, na_position="last"))
agg["cn_name"] = agg["factor"].apply(
    lambda f: factor_meta(f).cn_name if factor_meta(f) else f)
agg["verdict"] = agg["factor"].apply(lambda f: ir_verdict(used_ir.get(f)))

# 渲染为表
display = agg[["cn_name", "verdict", "used_ir", "max_ir", "ic", "n_obs"]].copy()
display.columns = ["因子", "评价", "调权用 IR (clip 均值)", "单条最高 IR (未过滤)", "平均 IC", "观测数"]
display["调权用 IR (clip 均值)"] = display["调权用 IR (clip 均值)"].map(
    lambda v: "—" if pd.isna(v) else f"{v:+.3f}")
display["单条最高 IR (未过滤)"] = display["单条最高 IR (未过滤)"].map(
    lambda v: "—" if pd.isna(v) else f"{v:.3f}")
display["平均 IC"] = display["平均 IC"].map(lambda v: "—" if pd.isna(v) else f"{v:+.3f}")
display["观测数"] = display["观测数"].map(lambda v: "—" if pd.isna(v) else f"{int(v)}")
st.dataframe(display, use_container_width=True, hide_index=True)
st.caption("评价按「调权用 IR」; ⚪ 数据不足 = 没有通过样本门槛 (n_eff) 或只有占位 IR, 权重按默认。"
           "「单条最高 IR」只是诊断: 在所有资产/窗口组合里挑最大值, 会系统性高估。")

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
    # 按倍数标注: 之前只认 eff > default, 被降权的因子全被标成 "默认"
    mult, source = ir_weight_source(default_w, eff_w, f in used_ir)
    wrows.append({"因子": cn, "默认权重": default_w, "实际权重": round(eff_w, 3),
                  "倍数": round(mult, 3) if mult is not None else None,
                  "来源": source})
wdf = pd.DataFrame(wrows).sort_values("实际权重", ascending=False)
st.dataframe(wdf, use_container_width=True, hide_index=True)
st.caption(ir_weight_rule_text())
