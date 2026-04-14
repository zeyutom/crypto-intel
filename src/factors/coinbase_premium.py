"""因子: Coinbase Premium = (CB价 - Binance价) / Binance价。

美区资金主导识别,与 ETF 净流入共振时信号更强。
"""
import json
from ..config import CFG
from ..db import query_df
from ..utils import now_iso

FACTOR = "coinbase_premium"


def compute() -> list[dict]:
    extreme = CFG["factors"]["coinbase_premium"]["extreme_pct"]
    rows = []
    ts = now_iso()
    for asset in CFG["universe"]:
        if not asset.get("coinbase"):
            continue
        # 取两侧最近价格
        df = query_df(
            """SELECT r.source AS source, r.value AS value FROM raw_metrics r
               JOIN (SELECT source AS s_, asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                     WHERE metric='price_usd' AND asset_id=?
                     AND source IN ('coinbase','binance')
                     GROUP BY source, asset_id) m
               ON r.source=m.s_ AND r.asset_id=m.a_ AND r.ts=m.mts
               WHERE r.metric='price_usd' AND r.asset_id=?""",
            (asset["id"], asset["id"]),
        )
        if len(df) < 2:
            continue
        prices = dict(zip(df["source"], df["value"]))
        cb = prices.get("coinbase")
        bn = prices.get("binance")
        if not cb or not bn or bn == 0:
            continue
        premium = (cb - bn) / bn
        if premium >= extreme:
            signal = 1
        elif premium <= -extreme:
            signal = -1
        else:
            signal = 0
        rows.append({
            "ts": ts, "asset_id": asset["id"], "factor": FACTOR,
            "raw_value": premium, "zscore": None, "signal": signal,
            "confidence": 0.8,
            "meta": json.dumps({"cb_price": cb, "binance_price": bn}),
        })
    return rows
