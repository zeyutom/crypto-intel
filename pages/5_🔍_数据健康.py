"""数据健康: 数据源覆盖度 + 复核状态 + 价格交叉验证。"""
import streamlit as st
import sys, json
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dashboard_utils import (
    require_password, page_header, sidebar_branding,
    load_source_status, load_reviews, COLOR,
)
from src.factors._metadata import asset_cn, factor_meta

st.set_page_config(page_title="数据健康 · Crypto Intel", page_icon="🔍", layout="wide")
require_password()
sidebar_branding()
page_header("🔍 数据健康", "数据源覆盖度 · 价格交叉验证 · 因子有效性监控")

# === 1. 数据源覆盖 ===
st.subheader("① 数据源覆盖度")
src = load_source_status()
ok_n = sum(1 for s in src if s["ok"])
fail_n = len(src) - ok_n
cols = st.columns(3)
cols[0].metric("已采集源", f"{ok_n}/{len(src)}")
cols[1].metric("失败源", f"{fail_n}", delta=None if fail_n == 0 else f"{fail_n}")
cols[2].metric("总数据行", sum(s["n"] for s in src))

src_grid_cols = st.columns(3)
for i, s in enumerate(src):
    with src_grid_cols[i % 3]:
        with st.container(border=True):
            status = "✅" if s["ok"] else "❌"
            st.markdown(f"**{status} `{s['source']}`**")
            st.caption(f"采集 {s['n']} 行 · 最近 {s['last_ts']}" if s["ok"] else "未采集成功")

st.divider()

# === 2. 复核状态 ===
st.subheader("② 复核状态")
rev = load_reviews()
if rev.empty:
    st.info("尚无复核记录。")
else:
    rows = []
    for _, r in rev.iterrows():
        try:
            d = json.loads(r["detail"])
        except:
            d = {}
        # humanize
        if r["check_name"] == "price_cross":
            check_cn = "价格交叉验证"
            sub_cn = asset_cn(r["subject"])
            dev = d.get("max_deviation_pct", 0)
            thr = d.get("threshold_pct", 1)
            if dev < thr * 0.5: explain = f"3 源价格高度一致 (最大偏差 {dev:.3f}%)"
            elif dev < thr: explain = f"价格存在 {dev:.3f}% 偏差但未超阈值 {thr}%"
            else: explain = f"⚠ 偏差 {dev:.3f}% 超过阈值, 建议人工核查"
        else:  # ic_monitor
            check_cn = "因子有效性"
            meta = factor_meta(r["subject"])
            sub_cn = meta.cn_name if meta else r["subject"]
            obs = d.get("observations", 0)
            if obs < 7: explain = f"已积累 {obs} 个观测, 7 天后才能开始评估"
            else:
                ir = d.get("ir_proxy", 0)
                explain = f"IR代理 = {ir:.2f} ({'达标' if ir >= 0.3 else '低于阈值, 因子近期失效'})"
        rows.append({
            "检查项": check_cn,
            "对象": sub_cn,
            "等级": r["severity"],
            "解读": explain,
        })

    df = pd.DataFrame(rows)
    def color_sev(v):
        if v == "OK": return f"background-color: rgba(88,214,141,0.15); color: {COLOR['ok']}"
        if v == "WARN": return f"background-color: rgba(255,180,84,0.15); color: {COLOR['warn']}"
        if v == "ALERT": return f"background-color: rgba(255,107,139,0.18); color: {COLOR['alert']}"
        return ""
    st.dataframe(df.style.map(color_sev, subset=["等级"]),
                 use_container_width=True, hide_index=True)

st.divider()

# === 3. 价格交叉验证详情 ===
st.subheader("③ 价格交叉验证详情")
st.caption("同一币种从多个数据源拉的价格,应该高度一致。任何源偏差大都意味着数据问题或单源故障。")

from src.db import query_df
price_compare = query_df(
    """SELECT r.asset_id AS asset_id, r.source AS source, r.value AS price
       FROM raw_metrics r
       JOIN (SELECT source AS s_, asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
             WHERE metric='price_usd' GROUP BY source, asset_id) m
       ON r.source=m.s_ AND r.asset_id=m.a_ AND r.ts=m.mts
       WHERE r.metric='price_usd'
       ORDER BY r.asset_id, r.source"""
)
if not price_compare.empty:
    pivot = price_compare.pivot_table(
        index="asset_id", columns="source", values="price", aggfunc="first"
    )
    pivot.index = [asset_cn(i) for i in pivot.index]
    pivot["最大偏差%"] = pivot.apply(
        lambda r: round((r.max() - r.min()) / r.mean() * 100, 4) if pd.notna(r).sum() >= 2 else None,
        axis=1,
    )
    st.dataframe(pivot.round(2), use_container_width=True)
