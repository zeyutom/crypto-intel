"""历史复盘 dashboard (W4-B).

选一个历史日期, 看那天系统推荐 (snapshot 里 Top-N) vs 实际 N 天后涨跌,
形成可信度证据链 — PM 视角最有价值的反向验证.

包含:
  1. 推荐命中率: Top-N 里 N 天后涨幅 > 中位的比例
  2. 等权 portfolio 收益 vs BTC 同期
  3. 单币种 forward return 表格 (with 颜色)
  4. 累计命中率曲线 (跨所有快照)
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

st.set_page_config(page_title="📜 历史复盘 · Crypto Intel", layout="wide")

# v0.9 Bug fix: 之前漏调 require_password, 线上部署会绕过密码门
from src.dashboard_utils import require_password
require_password()

st.title("📜 历史复盘 · 推荐 vs 实际")
st.caption('选一个日期, 看那天系统"看好"的币 N 天后实际跑赢了没')


# ────────────────────────────────────────────────────────────────────
#  Data loading
# ────────────────────────────────────────────────────────────────────

META_DIR = ROOT / "data" / "meta"


@st.cache_data(ttl=300)
def load_snapshots() -> list[dict]:
    snaps = []
    for f in sorted(META_DIR.glob("snapshot_*.json")):
        try:
            d = json.loads(f.read_text())
            if d.get("date") and d.get("coins"):
                d["_file"] = f.name
                snaps.append(d)
        except Exception:
            continue
    return snaps


@st.cache_data(ttl=300)
def build_forward_returns(snapshots: list[dict], lookahead_days: int = 7) -> dict:
    """对每个快照, 计算其 Top-N 在 lookahead 天后的 forward return.

    Returns: {snapshot_date: {symbol: {entry_price, exit_price, ret_pct, ranked}}}
    """
    by_date = {s["date"]: s for s in snapshots}
    dates = sorted(by_date.keys())

    out: dict[str, dict] = {}
    for i, d in enumerate(dates):
        snap = by_date[d]
        # 找 lookahead 天后最近的快照
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        target_dt = dt + timedelta(days=lookahead_days)
        next_snap = None
        for j in range(i + 1, len(dates)):
            try:
                cand_dt = datetime.strptime(dates[j], "%Y-%m-%d")
            except ValueError:
                continue
            if cand_dt >= target_dt:
                next_snap = by_date[dates[j]]
                break

        if not next_snap:
            continue   # 还没到 lookahead 日

        # 拼 forward return
        exit_prices = {c["symbol"]: c.get("price")
                       for c in next_snap.get("coins", [])
                       if c.get("price")}

        per_coin = {}
        ranked = sorted(snap.get("coins", []),
                        key=lambda c: c.get("composite_score", 0),
                        reverse=True)
        for rank_idx, c in enumerate(ranked):
            sym = c["symbol"]
            entry = c.get("price")
            exit_ = exit_prices.get(sym)
            if not entry or not exit_ or entry <= 0:
                continue
            per_coin[sym] = {
                "rank": rank_idx + 1,
                "entry_price": entry,
                "exit_price": exit_,
                "ret_pct": (exit_ - entry) / entry,
                "composite_score": c.get("composite_score", 0),
            }
        out[d] = per_coin
    return out


def topn_hit_rate(per_coin: dict, n: int = 10) -> dict:
    """Top-N 推荐里, ret > 整体中位数的比例."""
    if not per_coin:
        return {"hit_rate": None, "topn_avg_ret": None, "all_median": None}
    rows = sorted(per_coin.values(), key=lambda r: r["rank"])
    all_rets = [r["ret_pct"] for r in rows]
    median = sorted(all_rets)[len(all_rets) // 2]
    topn = rows[:n]
    if not topn:
        return {"hit_rate": None, "topn_avg_ret": None, "all_median": median}
    hits = sum(1 for r in topn if r["ret_pct"] > median)
    return {
        "hit_rate": hits / len(topn),
        "topn_avg_ret": sum(r["ret_pct"] for r in topn) / len(topn),
        "all_median": median,
        "all_mean": sum(all_rets) / len(all_rets),
        "n_compared": len(rows),
    }


# ────────────────────────────────────────────────────────────────────
#  UI
# ────────────────────────────────────────────────────────────────────

snaps = load_snapshots()
if not snaps:
    st.error("还没有快照数据。先在 [🚀 控制中心 · ⚡ 第一次跑] 跑一次 backfill。")
    st.stop()

st.info(f"📊 已加载 **{len(snaps)}** 个快照, "
        f"覆盖 **{snaps[0]['date']} ~ {snaps[-1]['date']}**")

# 参数
col1, col2, col3 = st.columns([2, 2, 3])
with col1:
    lookahead = st.slider("Lookahead 天数", 1, 14, 7,
                          help="N 天后看实际涨跌")
with col2:
    top_n = st.slider("Top N", 5, 30, 10, help="评估前 N 个推荐")
with col3:
    selectable_dates = [s["date"] for s in snaps[:-lookahead] if s["date"]]
    if not selectable_dates:
        st.warning("没有足够历史可复盘, 减小 lookahead")
        st.stop()
    sel_date = st.selectbox("选一个日期复盘", selectable_dates,
                            index=len(selectable_dates) - 1)

forward = build_forward_returns(snaps, lookahead_days=lookahead)

# ──────────────────────────────────────────────────
#  单日详情
# ──────────────────────────────────────────────────
st.markdown("---")
st.markdown(f"### 📅 {sel_date} 推荐 vs {lookahead} 天后实际")

per_coin = forward.get(sel_date, {})
if not per_coin:
    st.warning(f"{sel_date} 还没有 {lookahead} 天后的快照可对照")
    st.stop()

stats = topn_hit_rate(per_coin, n=top_n)

c1, c2, c3, c4 = st.columns(4)
hit = stats["hit_rate"]
c1.metric(
    f"Top-{top_n} 命中率",
    f"{hit*100:.1f}%" if hit is not None else "—",
    f"{(hit - 0.5)*100:+.1f}pp vs 50%" if hit is not None else None,
    delta_color="normal" if (hit is not None and hit >= 0.5) else "inverse",
)
c2.metric(
    f"Top-{top_n} 平均收益",
    f"{stats['topn_avg_ret']*100:+.2f}%" if stats['topn_avg_ret'] is not None else "—",
)
c3.metric(
    "全市场中位",
    f"{stats['all_median']*100:+.2f}%" if stats['all_median'] is not None else "—",
)
c4.metric(
    "全市场平均",
    f"{stats['all_mean']*100:+.2f}%" if stats.get('all_mean') is not None else "—",
)

# 单币种表
st.markdown(f"**Top-{top_n} 推荐详情**")
rows = sorted(per_coin.values(), key=lambda r: r["rank"])
df = pd.DataFrame(rows[:top_n])
df["entry"] = df["entry_price"].round(4)
df["exit"] = df["exit_price"].round(4)
df["ret %"] = (df["ret_pct"] * 100).round(2)
df["score"] = df["composite_score"].round(4)


def color_ret(val):
    if val > 0:
        return "color: #16a34a; font-weight: bold"
    if val < 0:
        return "color: #dc2626; font-weight: bold"
    return ""

# 还原 symbol 列 (DataFrame 的 index 是 0..N, 但每行有 rank 字段, 让 symbol 显示)
df_view = df[["rank", "entry", "exit", "ret %", "score"]].copy()
df_view.index = [r["rank"] for r in rows[:top_n]]  # rank 当 index
df_view.index.name = "rank"
# 补 symbol 列
syms = [s for s, _ in sorted(per_coin.items(), key=lambda kv: kv[1]["rank"])][:top_n]
df_view.insert(0, "symbol", syms)

styled = df_view.style.applymap(color_ret, subset=["ret %"])
st.dataframe(styled, use_container_width=True)


# ──────────────────────────────────────────────────
#  累计命中率曲线 (跨所有可比快照)
# ──────────────────────────────────────────────────
st.markdown("---")
st.markdown(f"### 📈 累计 Top-{top_n} 命中率 (跨全部 {len(forward)} 个可比快照)")

if len(forward) >= 3:
    hist_rows = []
    cumulative_hits = 0
    cumulative_total = 0
    for d in sorted(forward.keys()):
        s = topn_hit_rate(forward[d], n=top_n)
        if s["hit_rate"] is None:
            continue
        cumulative_hits += s["hit_rate"] * top_n
        cumulative_total += top_n
        hist_rows.append({
            "date": d,
            "daily_hit_rate": s["hit_rate"],
            "cumulative_hit_rate": cumulative_hits / cumulative_total,
            "topn_avg_ret": s["topn_avg_ret"],
            "all_median": s["all_median"],
        })
    if hist_rows:
        hdf = pd.DataFrame(hist_rows).set_index("date")
        st.line_chart(hdf[["cumulative_hit_rate", "daily_hit_rate"]],
                      height=280)

        total = hdf["cumulative_hit_rate"].iloc[-1]
        col_a, col_b, col_c = st.columns(3)
        col_a.metric("累计命中率",
                     f"{total*100:.1f}%",
                     f"{(total-0.5)*100:+.1f}pp vs 50% 随机基线",
                     delta_color="normal" if total >= 0.5 else "inverse")
        beat_btc = sum(1 for r in hist_rows
                       if r["topn_avg_ret"] > r["all_median"])
        col_b.metric("超额收益天数比",
                     f"{beat_btc}/{len(hist_rows)}",
                     f"{beat_btc/len(hist_rows)*100:.0f}%")
        col_c.metric("可比快照数", str(len(hist_rows)))
else:
    st.info("可比快照不足 3 个, 累计曲线暂不显示")


# ──────────────────────────────────────────────────
#  解读说明
# ──────────────────────────────────────────────────
with st.expander("📖 怎么读这页"):
    st.markdown("""
    - **命中率** = Top-N 里实际 ret > 全市场中位的比例.
      0.5 = 随机水平, >0.5 = 系统有信息含量, <0.5 = 系统反向了
    - **Top-N 平均收益 vs 全市场中位**: 这俩做差是真实"超额"
    - **累计命中率曲线**: 应该相对稳定在 0.5 以上, 才说明系统持续有效
    - 注意: 合成 backfill 快照只用了 3 个因子 (momentum/ATH drawdown), 信号不全
    """)
