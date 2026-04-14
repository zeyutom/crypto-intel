"""因子: Stablecoin Net Mint 7d(宏观流动性代理)。

定义: 稳定币总流通(USD) 最新值 - 7 天前值;正值 → 新增购买力。
"""
import json
import pandas as pd
from ..config import CFG
from ..db import query_df
from ..utils import now_iso

FACTOR = "stablecoin_mint_7d"


def compute() -> list[dict]:
    win = CFG["factors"]["stablecoin_mint"]["window_days"]
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='defillama' AND asset_id='stablecoins'
             AND metric='total_circulating_usd'
           ORDER BY ts"""
    )
    if len(df) < 2:
        return []
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.drop_duplicates("ts").sort_values("ts").set_index("ts")
    latest = df.iloc[-1]
    cutoff = latest.name - pd.Timedelta(days=win)
    past = df[df.index <= cutoff]
    if past.empty:
        # 数据不够 7 天: 用最早一条做近似
        past_value = df.iloc[0]["value"]
    else:
        past_value = past.iloc[-1]["value"]
    mint = latest["value"] - past_value
    # 方向: 7d 净 mint > 0 看多, < 0 警惕流动性收缩
    if mint > 1e9:       # > 10 亿美元
        signal = 1
    elif mint < -1e9:
        signal = -1
    else:
        signal = 0
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": FACTOR,
        "raw_value": mint, "zscore": None, "signal": signal,
        "confidence": 0.85 if len(df) >= win else 0.5,
        "meta": json.dumps({
            "latest_usd": float(latest["value"]),
            "past_usd": float(past_value),
            "window_days": win,
        }),
    }]
