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


def get_latest_ir_weights() -> dict[tuple[str, str], float]:
    """取每个因子当前最佳 IR (忽略负 IR 和低观测), 作为信号合成的动态权重。
       返回: {(factor, asset_id): weight} — weight = max(0, IR)。"""
    df = query_df(
        """SELECT factor, asset_id, MAX(ir) AS ir, MAX(n_obs) AS n_obs
           FROM factor_performance
           WHERE window_days >= 30 AND n_obs >= 10
           GROUP BY factor, asset_id"""
    )
    weights = {}
    for _, r in df.iterrows():
        ir = float(r["ir"]) if r["ir"] else 0
        n = int(r["n_obs"]) if r["n_obs"] else 0
        if ir > 0 and n >= 10:
            weights[(r["factor"], r["asset_id"])] = ir
    return weights
