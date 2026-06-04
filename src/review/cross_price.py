"""价格交叉验证: 每个 asset 比较 CoinGecko / Binance / Coinbase 的最新价格,
偏差超过 max_deviation_pct 即告警。"""
import json
import logging
import pandas as pd
from ..config import CFG
from ..db import query_df, upsert_review
from ..utils import now_iso

log = logging.getLogger(__name__)


def run() -> list[dict]:
    max_dev = CFG["review"]["price_cross_check"]["max_deviation_pct"]
    ts = now_iso()
    results = []

    for asset in CFG["universe"]:
        try:
            df = query_df(
                """SELECT r.source AS source, r.value AS value FROM raw_metrics r
                   JOIN (SELECT source AS s_, asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                         WHERE metric='price_usd' AND asset_id=?
                         GROUP BY source, asset_id) m
                   ON r.source=m.s_ AND r.asset_id=m.a_ AND r.ts=m.mts
                   WHERE r.metric='price_usd' AND r.asset_id=?""",
                (asset["id"], asset["id"]),
            )
            if len(df) < 2:
                continue
            # drop NULL/NaN/non-positive prices so one bad row can't poison the mean
            prices = {s: float(p) for s, p in zip(df["source"], df["value"])
                      if p is not None and pd.notna(p) and float(p) > 0}
            if len(prices) < 2:
                continue
            mean = sum(prices.values()) / len(prices)
            if mean <= 0:
                continue
            devs = {s: (p - mean) / mean for s, p in prices.items()}
            max_d = max(abs(d) for d in devs.values())

            if max_d > max_dev * 3:
                severity = "ALERT"
            elif max_d > max_dev:
                severity = "WARN"
            else:
                severity = "OK"

            results.append({
                "ts": ts, "check_name": "price_cross", "subject": asset["id"],
                "severity": severity,
                "detail": json.dumps({
                    "prices": prices, "mean": mean,
                    "max_deviation_pct": round(max_d * 100, 4),
                    "threshold_pct": max_dev * 100,
                }),
            })
        except Exception as e:
            log.warning("price_cross skip %s: %s", asset["id"], e)
            continue

    upsert_review(results)
    return results
