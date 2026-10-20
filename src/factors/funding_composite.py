"""因子: Funding Rate Composite (多币种加权资金费率)。

信号方向: 极端正 → 多头过度拥挤(负信号);极端负 → 空头拥挤(正信号反转)
置信度: 基于 4 个币种的非空值数量。
"""
import json
from ..config import CFG
from ..db import query_df
from ..utils import now_iso


FACTOR = "funding_composite"


def _pick_fresh_funding(df, window_hours: float = 6.0):
    """每个 asset 一行: 最新一笔 ts 的 window_hours 内按 binance > okx 选, 更旧的源不参与."""
    import pandas as pd
    d = df.copy()
    d["_ts"] = pd.to_datetime(d["ts"], utc=True, errors="coerce")
    newest = d.groupby("asset_id")["_ts"].transform("max")
    d = d[d["_ts"] >= newest - pd.Timedelta(hours=window_hours)]
    d["_rank"] = d["src"].map({"binance": 0, "okx": 1}).fillna(9)
    d = d.sort_values(["asset_id", "_rank", "_ts"], ascending=[True, True, False])
    return d.drop_duplicates(subset=["asset_id"], keep="first").drop(columns=["_ts", "_rank"])


def compute() -> list[dict]:
    weights = CFG["factors"]["funding_composite"]["weights"]
    hi = CFG["factors"]["funding_composite"]["extreme_high"]
    lo = CFG["factors"]["funding_composite"]["extreme_low"]

    # 取每个币种最近一笔 funding_rate_8h
    # 数据源优先级: binance > okx (binance 在 US 机房会被 451, OKX 是替代源)
    df = query_df(
        """SELECT r.asset_id AS asset_id, r.value AS value, r.source AS src, r.ts AS ts
           FROM raw_metrics r
           JOIN (SELECT asset_id AS a_, source AS s_, MAX(ts) AS mts FROM raw_metrics
                 WHERE source IN ('binance','okx') AND metric='funding_rate_8h'
                 GROUP BY asset_id, source) m
           ON r.asset_id = m.a_ AND r.source = m.s_ AND r.ts = m.mts
           WHERE r.source IN ('binance','okx') AND r.metric='funding_rate_8h'"""
    )
    # 同 asset 多源时: 先看新鲜度, 只在该资产最新一笔的 6h 内才按 binance > okx 挑。
    # 以前只按源字母序挑: 云端 binance 常年 451, 6 月的旧 binance 费率一直压过每天的 okx
    if not df.empty:
        df = _pick_fresh_funding(df)
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
