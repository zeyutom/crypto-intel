"""因子: Fear & Greed Reversal。

Alternative.me FGI 0-100;< 25 抄底信号,> 75 减仓信号。
"""
import json
from ..config import CFG
from ..db import query_df
from ..utils import now_iso

FACTOR = "fear_greed_reversal"


def compute() -> list[dict]:
    buy = CFG["factors"]["fear_greed_reversal"]["buy_below"]
    sell = CFG["factors"]["fear_greed_reversal"]["sell_above"]
    df = query_df(
        """SELECT ts, value, value_text FROM raw_metrics
           WHERE source='feargreed' AND metric='fear_greed_index'
           ORDER BY ts DESC LIMIT 1"""
    )
    if df.empty:
        return []
    v = float(df.iloc[0]["value"])
    cls = df.iloc[0]["value_text"]
    if v <= buy:
        signal = 1       # 恐慌抄底
    elif v >= sell:
        signal = -1      # 贪婪减仓
    else:
        signal = 0
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": FACTOR,
        "raw_value": v, "zscore": None, "signal": signal,
        "confidence": 0.7,
        "meta": json.dumps({"classification": cls, "buy_below": buy, "sell_above": sell}),
    }]
