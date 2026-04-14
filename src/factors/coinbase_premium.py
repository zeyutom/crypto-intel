"""因子: Coinbase Premium = (CB价 - 国际所价) / 国际所价。

美区资金主导识别。国际所优先 Binance, 没有则用 OKX (覆盖云端 Binance 451 场景)。
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
        # 取 coinbase + (binance|okx) 最近价格
        df = query_df(
            """SELECT r.source AS source, r.value AS value FROM raw_metrics r
               JOIN (SELECT source AS s_, asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                     WHERE metric='price_usd' AND asset_id=?
                     AND source IN ('coinbase','binance','okx')
                     GROUP BY source, asset_id) m
               ON r.source=m.s_ AND r.asset_id=m.a_ AND r.ts=m.mts
               WHERE r.metric='price_usd' AND r.asset_id=?""",
            (asset["id"], asset["id"]),
        )
        if df.empty:
            continue
        prices = dict(zip(df["source"], df["value"]))
        cb = prices.get("coinbase")
        # 国际所: 优先 binance, 没有则 okx
        intl = prices.get("binance") or prices.get("okx")
        intl_name = "binance" if prices.get("binance") else "okx"
        if not cb or not intl or intl == 0:
            continue
        premium = (cb - intl) / intl
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
            "meta": json.dumps({"cb_price": cb, "intl_price": intl,
                                 "intl_source": intl_name}),
        })
    return rows
