"""每日因子快照 + 前瞻收益回算。

日常运行:
  1. 今日 snapshot: 把当前全部因子值 + asset 当前价 存入 factor_snapshots
  2. 历史回算: 对过去 30/60/90 天的 snapshot 回算其后 1d/7d/30d 收益
  3. 这些数据是 IC/IR 回测的唯一输入
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
import pandas as pd
from .db import query_df, upsert_factor_snapshot, latest_factors
from .utils import setup_logger

log = setup_logger("snapshot", "INFO")


def _get_current_prices() -> dict[str, float]:
    """取每个 asset 当前最新价 (优先 binance > okx > coingecko)。"""
    df = query_df(
        """SELECT asset_id, price FROM (
              SELECT r.asset_id, r.value AS price,
                     ROW_NUMBER() OVER (PARTITION BY r.asset_id
                       ORDER BY CASE r.source WHEN 'binance' THEN 1 WHEN 'okx' THEN 2
                                              WHEN 'coingecko' THEN 3 ELSE 9 END,
                                r.ts DESC) AS rn
              FROM raw_metrics r
              WHERE r.metric='price_usd' AND r.source IN ('binance','okx','coingecko')
            ) WHERE rn = 1"""
    )
    return dict(zip(df["asset_id"], df["price"])) if not df.empty else {}


def take_daily_snapshot() -> int:
    """snapshot 今天的所有因子值 + asset 价格。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fact_df = latest_factors()
    if fact_df.empty:
        log.warning("No factors to snapshot")
        return 0

    prices = _get_current_prices()
    rows = []
    for _, r in fact_df.iterrows():
        asset_id = r.get("asset_id")
        price = prices.get(asset_id) if asset_id else None
        rows.append({
            "date": today,
            "factor": r["factor"],
            "asset_id": asset_id,
            "raw_value": float(r["raw_value"]) if r["raw_value"] is not None else None,
            "signal": int(r["signal"]) if r["signal"] is not None else 0,
            "current_price": float(price) if price else None,
            "meta": r["meta"] or "{}",
        })
    n = upsert_factor_snapshot(rows)
    log.info(f"Snapshot today: {n} factor rows")
    return n
