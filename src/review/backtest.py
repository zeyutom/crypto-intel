"""真 IC/IR 回测引擎 (替换 ic_monitor 占位实现)。

数据流:
  factor_snapshots (每日存) → 未来 N 日收益 (从 raw_metrics 或 snapshot 算)
  → 滚动 IC (spearman corr of factor_value vs forward_return)
  → IR = mean(IC) / std(IC)

跑:
  python -m src.cli backtest
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from ..db import query_df, upsert_factor_performance
from ..utils import setup_logger

log = setup_logger("backtest", "INFO")

# 计算窗口
WINDOWS_DAYS = [30, 90]       # 回看期
FORWARDS_DAYS = [1, 7, 30]    # 前瞻收益期


def _get_asset_price_history(asset_id: str) -> pd.DataFrame:
    """拿 asset_id 的历史日级价格 (优先 binance > okx > coingecko)。"""
    df = query_df(
        """SELECT ts, value AS price, source FROM raw_metrics
           WHERE asset_id=? AND metric='price_usd'
             AND source IN ('binance','okx','coingecko')
           ORDER BY ts""", (asset_id,)
    )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
    # 每日取一个价 (偏好 binance)
    df["pref"] = df["source"].map({"binance": 1, "okx": 2, "coingecko": 3}).fillna(9)
    df = df.sort_values(["date", "pref"]).drop_duplicates("date", keep="first")
    return df[["date", "price"]].reset_index(drop=True)


def _forward_return(prices: pd.DataFrame, from_date: str, forward_days: int) -> float | None:
    """计算 from_date 往后 forward_days 的收益率。"""
    try:
        from_idx = prices.index[prices["date"] == from_date][0]
    except IndexError:
        return None
    target_date = (datetime.strptime(from_date, "%Y-%m-%d") +
                   timedelta(days=forward_days)).strftime("%Y-%m-%d")
    # 找 >= target_date 的第一个
    future = prices[prices["date"] >= target_date]
    if future.empty:
        return None
    p0 = prices.iloc[from_idx]["price"]
    p1 = future.iloc[0]["price"]
    if p0 == 0 or p0 is None or p1 is None:
        return None
    return (p1 - p0) / p0


def backtest_factor(factor: str, window_days: int, forward_days: int) -> list[dict]:
    """对某因子在 window_days 回看窗口内,计算 forward_days 前瞻 IC/IR。
       返回每个 asset_id 一行 (含 market 级)。"""
    # 1. 拿因子历史快照
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    df = query_df(
        """SELECT date, asset_id, raw_value, signal FROM factor_snapshots
           WHERE factor=? AND date >= ? ORDER BY date""",
        (factor, cutoff),
    )
    if df.empty or len(df) < 5:
        return []

    results = []
    for asset_id, grp in df.groupby(df["asset_id"].fillna("market")):
        if len(grp) < 5:
            continue
        aid = None if asset_id == "market" else asset_id
        # 拿价格历史 (market 级因子用 BTC 作为市场代理)
        price_asset = aid or "bitcoin"
        prices = _get_asset_price_history(price_asset)
        if prices.empty:
            continue
        rows = []
        for _, r in grp.iterrows():
            ret = _forward_return(prices, r["date"], forward_days)
            if ret is not None and r["raw_value"] is not None:
                rows.append({"factor_value": float(r["raw_value"]),
                             "signal": int(r["signal"] or 0),
                             "return": ret})
        if len(rows) < 5:
            continue
        bk = pd.DataFrame(rows)
        try:
            corr, _ = spearmanr(bk["factor_value"], bk["return"])
        except Exception:
            corr = np.nan
        if np.isnan(corr):
            continue
        # IR 用信号的 rolling IC 近似 (短窗口): mean(IC) / std(IC)
        # 简单实现: 把窗口切 5 段, 每段算一个 IC, 求 mean/std
        seg = max(3, len(bk) // 5)
        ics = []
        for i in range(0, len(bk) - seg, max(1, seg // 2)):
            sub = bk.iloc[i:i+seg]
            if len(sub) >= 3:
                try:
                    c, _ = spearmanr(sub["factor_value"], sub["return"])
                    if not np.isnan(c):
                        ics.append(c)
                except Exception:
                    pass
        ir = (np.mean(ics) / np.std(ics)) if len(ics) >= 2 and np.std(ics) > 0 else 0
        # 信号=1 时平均收益
        mean_ret_bull = bk[bk["signal"] == 1]["return"].mean()
        if pd.isna(mean_ret_bull):
            mean_ret_bull = 0

        results.append({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "factor": factor,
            "asset_id": aid,
            "window_days": window_days,
            "forward_days": forward_days,
            "ic": round(float(corr), 4),
            "ir": round(float(ir), 4),
            "n_obs": len(bk),
            "mean_return": round(float(mean_ret_bull), 4),
        })
    return results


def run_backtest_all() -> int:
    """对所有因子 × 所有 (window, forward) 组合回测, 存入 factor_performance。"""
    factors_df = query_df("SELECT DISTINCT factor FROM factor_snapshots")
    if factors_df.empty:
        log.warning("No snapshots yet; run snapshot first")
        return 0
    all_results = []
    for factor in factors_df["factor"]:
        for win in WINDOWS_DAYS:
            for fwd in FORWARDS_DAYS:
                try:
                    res = backtest_factor(factor, win, fwd)
                    all_results.extend(res)
                except Exception as e:
                    log.warning(f"Backtest {factor} w={win} f={fwd} failed: {e}")
    n = upsert_factor_performance(all_results)
    log.info(f"Backtest: {n} performance rows written")
    return n


# IR 权重的样本门槛 / 截断 (见 get_latest_ir_weights)
IR_MIN_N_EFF = 10
IR_CLIP = 2.0
# 最新计算日早于今天这么多天的 (factor, asset) 不再参与调权 (源头断了的因子会一直停在旧 IR)
IR_MAX_AGE_DAYS = 30


def get_latest_ir_weights() -> dict[tuple[str, str], float]:
    """取每个 (factor, asset_id) **最新计算日** 跨 window/forward 的平均 IR, 供信号合成调权。

    返回: {(factor, asset_id): mean_ir}, mean_ir ∈ [-2, 2], 负值也返回 (由
    signals.composite._effective_weights 决定如何降权)。
    规则 (修复 2026-10 回测发现的问题: 旧版对全历史平均、丢弃 IR<=0、不封顶,
    funding_composite 用 10-19 个重叠观测拿到 IR 13.9):
      1. 只用每个 (factor, asset_id) 的最大 date 那一天的记录;
      2. 要求 n_eff = n_obs / forward_days >= IR_MIN_N_EFF (重叠前瞻窗口下的有效独立样本);
      3. 单条 IR 先 clip 到 [-IR_CLIP, IR_CLIP] 再对 window/forward 求平均;
      4. window_days < 30 的短窗口不参与;
      5. ir == 0.0 是 backtest_factor "算不出" (IC 不足 2 个或标准差为 0) 的占位值, 丢弃;
      6. 最新计算日早于今天 IR_MAX_AGE_DAYS 天的 (factor, asset_id) 丢弃。
    注: 用 AVG 而非 MAX, 避免在多个 window/forward 组合里挑最佳 IR 造成系统性高估。"""
    df = query_df(
        """SELECT date, factor, asset_id, window_days, forward_days, ir, n_obs
           FROM factor_performance
           WHERE window_days >= 30"""
    )
    if df is None or df.empty:
        return {}
    df = df.copy()
    # 1. 每个 (factor, asset_id) 只保留最新 date (asset_id 可为 NULL → dropna=False)
    latest = df.groupby(["factor", "asset_id"], dropna=False)["date"].transform("max")
    df = df[df["date"] == latest]
    # 6. 时效: 最新计算日太旧 (数据源已断) 的不再参与
    cutoff = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None) - pd.Timedelta(days=IR_MAX_AGE_DAYS)
    df = df[pd.to_datetime(df["date"], errors="coerce") >= cutoff]
    # 2. n_eff 门槛 (forward_days 缺失 / 非正 → 无法判断, 丢弃)
    df["ir"] = pd.to_numeric(df["ir"], errors="coerce")
    df["n_obs"] = pd.to_numeric(df["n_obs"], errors="coerce")
    df["forward_days"] = pd.to_numeric(df["forward_days"], errors="coerce")
    df = df.dropna(subset=["ir", "n_obs", "forward_days"])
    df = df[df["ir"] != 0.0]   # 5. "算不出" 的占位值
    df = df[df["forward_days"] > 0]
    df = df[df["n_obs"] / df["forward_days"] >= IR_MIN_N_EFF]
    if df.empty:
        return {}
    # 3. clip 后再平均
    df["ir"] = df["ir"].clip(-IR_CLIP, IR_CLIP)
    weights = {}
    for (factor, aid), grp in df.groupby(["factor", "asset_id"], dropna=False):
        aid = None if pd.isna(aid) else aid
        weights[(factor, aid)] = float(grp["ir"].mean())
    return weights
