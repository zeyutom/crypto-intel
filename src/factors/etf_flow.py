"""因子: ETF Net Flow (5d cumulative) — BTC 现货 ETF 日净流入累计。

正 → 机构资金持续流入, 利多 BTC。
"""
import json
import pandas as pd
from ..config import CFG
from ..db import query_df
from ..utils import now_iso

FACTOR = "etf_flow_5d"


def compute() -> list[dict]:
    win = CFG["factors"]["etf_flow"]["sum_window_days"]
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='farside_etf' AND asset_id='bitcoin'
             AND metric='etf_net_flow_musd'
           ORDER BY ts DESC LIMIT 60"""
    )
    if df.empty:
        return []
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").drop_duplicates("ts")
    recent = df.tail(win)
    if len(recent) == 0:
        return []
    cum = float(recent["value"].sum())
    # 方向
    if cum > 500:       # 5d 累计 > +5 亿美元
        signal = 1
    elif cum < -500:
        signal = -1
    else:
        signal = 0
    return [{
        "ts": now_iso(), "asset_id": "bitcoin", "factor": FACTOR,
        "raw_value": cum, "zscore": None, "signal": signal,
        "confidence": min(1.0, len(recent) / win),
        "meta": json.dumps({
            "window_days": win,
            "daily_flows_musd": recent.set_index(recent["ts"].dt.strftime("%Y-%m-%d"))["value"].to_dict(),
        }),
    }]
