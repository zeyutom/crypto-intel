"""因子: Funding Rate Composite (多币种加权资金费率)。

信号方向: 极端正 → 多头过度拥挤(负信号);极端负 → 空头拥挤(正信号反转)
置信度: 基于 4 个币种的非空值数量。
"""
import json
from ..config import CFG
from ..db import query_df
from ..utils import now_iso


FACTOR = "funding_composite"


def compute() -> list[dict]:
    weights = CFG["factors"]["funding_composite"]["weights"]
    hi = CFG["factors"]["funding_composite"]["extreme_high"]
    lo = CFG["factors"]["funding_composite"]["extreme_low"]

    # 取每个币种最近一笔 funding_rate_8h
    # 数据源优先级: binance > okx (binance 在 US 机房会被 451, OKX 是替代源)
    df = query_df(
        """SELECT r.asset_id AS asset_id, r.value AS value, r.source AS src
           FROM raw_metrics r
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                 WHERE source IN ('binance','okx') AND metric='funding_rate_8h'
                 GROUP BY asset_id) m
           ON r.asset_id = m.a_ AND r.ts = m.mts
           WHERE r.source IN ('binance','okx') AND r.metric='funding_rate_8h'"""
    )
    # 同 asset 多源时, 优先 binance, 其次 okx
    if not df.empty:
        df = df.sort_values("src", ascending=True)  # 'binance' < 'okx' alphabetically
        df = df.drop_duplicates(subset=["asset_id"], keep="first")
    if df.empty:
        return []

    # 映射回 symbol
    sym_map = {a["id"]: a["binance"] for a in CFG["universe"] if a.get("binance")}
    weighted_sum = 0.0
    total_w = 0.0
    breakdown = {}
    for _, row in df.iterrows():
        sym = sym_map.get(row["asset_id"])
        if not sym:
            continue
        w = weights.get(sym, 0)
        weighted_sum += w * row["value"]
        total_w += w
        breakdown[sym] = row["value"]
    if total_w == 0:
        return []
    composite = weighted_sum / total_w

    # 方向: 极端反转
    if composite >= hi:
        signal = -1  # 过热,警惕回调
    elif composite <= lo:
        signal = 1   # 过冷, bounce 信号
    else:
        signal = 0

    confidence = min(1.0, len(breakdown) / len(weights))
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": FACTOR,
        "raw_value": composite,
        "zscore": None,  # 需要历史序列做 z 化, PoC 阶段先留空
        "signal": signal,
        "confidence": confidence,
        "meta": json.dumps({"breakdown": breakdown, "hi": hi, "lo": lo}),
    }]
