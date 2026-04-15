"""多因子合成 → 每个 asset 一个方向性信号 + 置信度 + 归因。

策略:
  composite = Σ_i (signal_i × confidence_i × weight_i) / Σ_i (confidence_i × weight_i)
  direction = BULL if composite > 0.2 else BEAR if composite < -0.2 else NEUTRAL
  regime = 简单规则: ETF 持续正 + F&G < 60 → BULL;F&G > 80 → CRISIS 等
"""
import json
from ..db import query_df, latest_factors, upsert_signal
from ..utils import now_iso


# 默认因子权重 (v0.3 手工设定, 作 fallback)
DEFAULT_WEIGHTS = {
    "funding_composite": 1.0,
    "coinbase_premium": 1.2,
    "stablecoin_mint_7d": 1.5,
    "fear_greed_reversal": 0.8,
    "etf_flow_5d": 1.8,
    # v0.4 新因子 (默认权重保守, 等数据多了由 IR 接管)
    "open_interest_change": 0.8,
    "liquidation_heat": 0.6,
    "long_short_ratio": 0.5,
    "btc_dominance_trend": 0.8,
    "total_mcap_momentum": 1.0,
    "defi_tvl_momentum": 0.6,
    "trending_score": 0.4,
    "btc_nasdaq_corr": 0.5,
    "btc_gold_corr": 0.4,
    "dxy_inverse": 0.7,
}


def _effective_weights() -> dict:
    """v0.5: 优先用 backtest 得到的 IR 权重, 其次用默认权重。
       每个因子取 max(default, IR_weight)。"""
    try:
        from ..review.backtest import get_latest_ir_weights
        ir_w = get_latest_ir_weights()
    except Exception:
        ir_w = {}
    if not ir_w:
        return dict(DEFAULT_WEIGHTS)
    # 每个因子: 对所有 asset 的 IR 取平均, 与 default 取 max
    merged = dict(DEFAULT_WEIGHTS)
    from collections import defaultdict
    factor_irs = defaultdict(list)
    for (factor, _aid), w in ir_w.items():
        factor_irs[factor].append(w)
    for factor, irs in factor_irs.items():
        avg_ir = sum(irs) / len(irs) if irs else 0
        # IR > 0.5 = 显著有效, 加权; IR < 0 会被 filter 掉 (get_latest_ir_weights 已过滤)
        merged[factor] = max(merged.get(factor, 0.3), avg_ir)
    return merged


# 向后兼容
FACTOR_WEIGHTS = DEFAULT_WEIGHTS


def _detect_regime() -> str:
    """轻量 regime 识别: 基于 F&G + ETF 5d。"""
    df = query_df(
        """SELECT factor, raw_value FROM factors f
           WHERE (factor='fear_greed_reversal' OR factor='etf_flow_5d')
             AND ts = (SELECT MAX(ts) FROM factors f2 WHERE f2.factor=f.factor)"""
    )
    if df.empty:
        return "UNKNOWN"
    m = dict(zip(df["factor"], df["raw_value"]))
    fg = m.get("fear_greed_reversal")
    etf = m.get("etf_flow_5d", 0)
    if fg is None:
        return "UNKNOWN"
    if fg < 25 and etf > 0:
        return "BEAR_BOUNCE"
    if fg > 75 and etf < 0:
        return "CRISIS"
    if etf > 500 and 40 <= fg <= 75:
        return "BULL"
    if etf < -500:
        return "BEAR"
    return "CHOP"


def compose() -> list[dict]:
    df = latest_factors()
    if df.empty:
        return []

    ts = now_iso()
    regime = _detect_regime()
    weights = _effective_weights()   # v0.5: 动态从 backtest 拉, 缺省用 DEFAULT
    results = []

    # 按 asset_id 聚合
    for asset_id, grp in df.groupby(df["asset_id"].fillna("market")):
        num = 0.0
        denom = 0.0
        breakdown = {}
        for _, row in grp.iterrows():
            fname = row["factor"]
            w = weights.get(fname, 0.5)
            sig = row["signal"] or 0
            conf = row["confidence"] or 0.5
            num += sig * conf * w
            denom += conf * w
            breakdown[fname] = {
                "signal": int(sig), "confidence": round(float(conf), 3),
                "raw_value": float(row["raw_value"]) if row["raw_value"] is not None else None,
                "weight": w,
            }
        composite = num / denom if denom else 0
        if composite > 0.2:
            direction = "BULL"
        elif composite < -0.2:
            direction = "BEAR"
        else:
            direction = "NEUTRAL"
        confidence = min(1.0, denom / sum(FACTOR_WEIGHTS.values()))

        results.append({
            "ts": ts, "asset_id": asset_id if asset_id != "market" else None,
            "composite": round(composite, 4),
            "direction": direction,
            "confidence": round(confidence, 3),
            "regime": regime,
            "factor_breakdown": json.dumps(breakdown, ensure_ascii=False),
        })
    upsert_signal(results)
    return results
