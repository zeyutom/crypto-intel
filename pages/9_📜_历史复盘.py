"""历史复盘 dashboard (W4-B).

选一个历史日期, 看那天系统推荐 (snapshot 里 Top-N) vs 实际 N 天后涨跌,
形成可信度证据链 — PM 视角最有价值的反向验证.

包含:
  1. 推荐命中率: Top-N 里 N 天后涨幅 > 中位的比例
  2. 等权 portfolio 收益 vs BTC 同期
  3. 单币种 forward return 表格 (with 颜色)
  4. 累计命中率曲线 (跨所有快照)
"""
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

# 纯函数放在 src.research.dashboard (可单测): 剔除合成快照 / coin_id 优先定价 / 同名币不互相覆盖
from src.research.dashboard import (
    exit_snapshot_dates, forward_returns, is_full_universe, load_real_snapshots,
    topn_hit_rate, truncated_baseline_counts,
)


@st.cache_data(ttl=300)
def load_snapshots() -> list[dict]:
    # 只要真实筛选快照: backfill 合成 (*_0000, 3 因子, 18-29 币) 不进复盘
    return load_real_snapshots(META_DIR)


@st.cache_data(ttl=300)
def build_forward_returns(snapshots: list[dict], lookahead_days: int = 7) -> dict:
    return forward_returns(snapshots, lookahead_days=lookahead_days)


# ────────────────────────────────────────────────────────────────────
#  UI
# ────────────────────────────────────────────────────────────────────

snaps = load_snapshots()
if not snaps:
    st.error("还没有真实快照数据 (backfill 合成快照不参与复盘)。"
             "先在 [🚀 控制中心] 跑一次筛选 / nightly, 积累真实快照后再来。")
    st.stop()

st.info(f"📊 已加载 **{len(snaps)}** 个真实快照 (backfill 合成快照已剔除), "
        f"覆盖 **{snaps[0]['date']} ~ {snaps[-1]['date']}**")

# 参数
col1, col2, col3 = st.columns([2, 2, 3])
with col1:
    lookahead = st.slider("Lookahead 天数", 1, 14, 7,
                          help="N 天后看实际涨跌")
with col2:
    top_n = st.slider("Top N", 5, 30, 10, help="评估前 N 个推荐")
with col3:
    # 按日期去重 (同一天可能有多个 snapshot 文件), 再用真实日期算 cutoff,
    # 与 build_forward_returns 的 dt + timedelta(days=lookahead) 口径一致,
    # 避免 positional slice 把不连续/重复日期切错
    _by_date = {s["date"]: s for s in snaps if s.get("date")}
    _all_dates = sorted(_by_date)
    selectable_dates = []
    if _all_dates:
        try:
            _cutoff = datetime.strptime(_all_dates[-1], "%Y-%m-%d") - timedelta(days=lookahead)
            selectable_dates = [d for d in _all_dates
                                if datetime.strptime(d, "%Y-%m-%d") <= _cutoff]
        except ValueError:
            selectable_dates = []
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
    "快照内中位",
    f"{stats['all_median']*100:+.2f}%" if stats['all_median'] is not None else "—",
)
c4.metric(
    "快照内平均",
    f"{stats['all_mean']*100:+.2f}%" if stats.get('all_mean') is not None else "—",
)
_sel_snap = _by_date.get(sel_date) or {}
_n_coins = len(_sel_snap.get("coins") or [])
_exit_date = exit_snapshot_dates(snaps, lookahead).get(sel_date)
_exit_snap = _by_date.get(_exit_date) or {}
st.caption(
    f"基准 = 该快照里 {lookahead} 天后仍能匹配到价格的 {stats['n_compared']} 个币 · 入场快照宇宙: "
    + (f"全量评分宇宙 ({_n_coins} 币)" if is_full_universe(_sel_snap)
       else f"⚠️ 只有 composite 前 {_n_coins} 名 (2026-10-12 之前的快照被截断, 不是全市场)")
    + f" · 出场快照 {_exit_date}: "
    + ("全量评分宇宙" if is_full_universe(_exit_snap)
       else f"⚠️ 也被截断, 只能匹配到 {lookahead} 天后仍在前 200 的币")
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
# 补 symbol 列 (per_coin 按 coin_id 建键, 显示用行里的 symbol)
df_view.insert(0, "symbol", [r["symbol"] for r in rows[:top_n]])

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
        # 快照币数不足 top_n 时实际只评了 n_topn 个, 按实际个数加权
        cumulative_hits += s["hit_rate"] * s["n_topn"]
        cumulative_total += s["n_topn"]
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
        # 出场快照是 schema 2 全量宇宙时 (如 10-06 ~ 10-11 + 7 天) 没有 "仍在前 200" 的偏差,
        # 只对出场也截断的日期提幸存者偏差
        _n_trunc, _n_surv = truncated_baseline_counts(
            snaps, [r["date"] for r in hist_rows], lookahead_days=lookahead)
        if _n_trunc:
            st.caption(
                f"其中 {_n_trunc} 天的入场快照只存了 composite 前 200 名 (2026-10-12 之前): "
                "这些日期的「快照内中位」只覆盖入场时的前 200 名, 不是全市场"
                + (f"; 其中 {_n_surv} 天的出场快照也被截断, "
                   f"基准还只含 {lookahead} 天后仍在前 200 的币" if _n_surv else "")
                + "。")
else:
    st.info("可比快照不足 3 个, 累计曲线暂不显示")


# ──────────────────────────────────────────────────
#  解读说明
# ──────────────────────────────────────────────────
with st.expander("📖 怎么读这页"):
    st.markdown("""
    - **命中率** = Top-N 里实际 ret > 快照内中位的比例.
      0.5 = 随机水平, >0.5 = 系统有信息含量, <0.5 = 系统反向了
    - **Top-N 平均收益 vs 快照内中位**: 这俩做差是相对快照宇宙的"超额"
    - **快照宇宙**: 2026-10-12 之前的快照只存了 composite 前 200 名, 基准偏向高分币;
      之后的快照保存完整评分宇宙
    - **累计命中率曲线**: 应该相对稳定在 0.5 以上, 才说明系统持续有效
    - 合成 backfill 快照 (只用 3 个动量因子) 已剔除, 不参与复盘
    - 同名币 (如两个 VELO) 按 coin_id 区分; 旧快照里没有 coin_id 的同名币:
      价格相差 2% 以内视为同一个币列了两次, 只算名次高的那行; 否则都不计入
    """)
