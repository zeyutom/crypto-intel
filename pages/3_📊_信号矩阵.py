"""信号矩阵: 资产 × 因子 的方向矩阵 + 各 asset 详细信号。"""
import streamlit as st
import sys, json
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import (
    require_password, page_header, sidebar_branding,
    load_signals, COLOR,
)
from src.db import latest_factors
from src.factors._metadata import asset_cn, regime_cn, factor_meta

st.set_page_config(page_title="信号矩阵 · Crypto Intel", page_icon="📊", layout="wide")
require_password()
sidebar_branding()
page_header("📊 信号矩阵", "资产 × 因子 全景视图")

sig_df = load_signals()
fact_df = latest_factors()

if sig_df.empty:
    st.warning("尚无信号。请先到主页刷新数据。")
    st.stop()

# === 1. 信号汇总表 ===
st.subheader("① 资产合成信号汇总")
rows = []
for _, s in sig_df.iterrows():
    breakdown = {}
    if s["factor_breakdown"]:
        try: breakdown = json.loads(s["factor_breakdown"])
        except: pass
    drivers = [f for f, v in breakdown.items() if v.get("signal") != 0]
    rows.append({
        "资产": asset_cn(s["asset_id"]),
        "方向": s["direction"],
        "合成分": round(float(s["composite"]), 3),
        "置信度 %": round(float(s["confidence"]) * 100),
        "Regime": regime_cn(s["regime"])[0],
        "贡献因子": ", ".join(drivers[:3]) or "—",
    })
df = pd.DataFrame(rows)

def color_direction(val):
    if val == "BULL": return f"background-color: rgba(88,214,141,0.15); color: {COLOR['ok']}"
    if val == "BEAR": return f"background-color: rgba(255,107,139,0.15); color: {COLOR['alert']}"
    return f"color: {COLOR['muted']}"

styled = df.style.map(color_direction, subset=["方向"])
st.dataframe(styled, use_container_width=True, hide_index=True)

st.divider()

# === 2. 因子 × 资产 矩阵 ===
st.subheader("② 因子 × 资产 信号矩阵")
st.caption("行 = 因子, 列 = 资产, 单元格 = 当前信号 (1 看多 / -1 看空 / 0 中性)")

if not fact_df.empty:
    pivot_data = []
    for _, r in fact_df.iterrows():
        meta = factor_meta(r["factor"])
        pivot_data.append({
            "因子": meta.cn_name if meta else r["factor"],
            "资产": asset_cn(r["asset_id"]),
            "信号": int(r["signal"]) if r["signal"] is not None else 0,
        })
    pdf = pd.DataFrame(pivot_data)
    pivot = pdf.pivot_table(index="因子", columns="资产", values="信号",
                            aggfunc="first", fill_value=0)

    def color_signal(v):
        try: v = int(v)
        except: return ""
        if v == 1: return f"background-color: rgba(88,214,141,0.25); color: {COLOR['ok']}; font-weight: 600;"
        if v == -1: return f"background-color: rgba(255,107,139,0.25); color: {COLOR['alert']}; font-weight: 600;"
        return f"color: {COLOR['muted']}"

    st.dataframe(pivot.style.map(color_signal), use_container_width=True)
else:
    st.info("无因子数据。")

st.divider()

# === 3. 选某资产看详细 breakdown ===
st.subheader("③ 资产详细信号拆解")
selected = st.selectbox("选择资产查看详细因子贡献",
                        [asset_cn(a) for a in sig_df["asset_id"].tolist()])

for _, s in sig_df.iterrows():
    if asset_cn(s["asset_id"]) != selected:
        continue
    if s["factor_breakdown"]:
        try:
            bd = json.loads(s["factor_breakdown"])
        except:
            bd = {}
        rows = []
        for fname, v in bd.items():
            meta = factor_meta(fname)
            rows.append({
                "因子": meta.cn_name if meta else fname,
                "原值": v.get("raw_value"),
                "信号": v.get("signal"),
                "置信度": f"{v.get('confidence', 0):.0%}",
                "权重": v.get("weight"),
                "实际贡献": round(v.get("signal", 0) * v.get("confidence", 0) * v.get("weight", 0), 3),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.caption(f"合成 = Σ(信号 × 置信度 × 权重) / Σ(置信度 × 权重) = {float(s['composite']):+.3f}")
