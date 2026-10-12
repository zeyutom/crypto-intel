"""多因子合成 → 每个 asset 一个方向性信号 + 置信度 + 归因。

策略:
  composite = Σ_i (signal_i × confidence_i × weight_i) / Σ_i (confidence_i × weight_i)
  direction = BULL if composite > 0.2 else BEAR if composite < -0.2 else NEUTRAL
  regime = 简单规则: ETF 持续正 + F&G < 60 → BULL;F&G > 80 → CRISIS 等
           ETF 因子缺失时 (farside 源被封) 退化为 BTC 动量 regime (见 _detect_regime)
"""
import json
from datetime import timedelta
import pandas as pd
from ..db import query_df, latest_factors, upsert_signal
from ..utils import now_iso


# IR 动态权重: weight = default × clamp(1 + IR_SLOPE × mean_ir, IR_MULT_MIN, IR_MULT_MAX)
IR_SLOPE = 0.25
IR_MULT_MIN = 0.5
IR_MULT_MAX = 1.5


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
    """v0.5: 用 backtest 的 IR 对默认权重做有界缩放, 无数据时退回 DEFAULT_WEIGHTS。

    公式 (每个因子, mean_ir = 该因子跨 asset 的 IR 均值, IR 已被 clip 到 [-2, 2]):
        weight = default × clamp(1 + 0.25 × mean_ir, 0.5, 1.5)
    → IR = +2 时 1.5× default, IR = -2 时 0.5× default, IR = 0 时不变。
    修复 2026-10 回测发现的问题: 旧版 max(default, avg_ir) 只会加权 (funding_composite
    IR 13.9 → 权重 13.9), 负 IR 因子 (coinbase_premium / stablecoin_mint_7d /
    fear_greed_reversal) 仍保留满额默认权重。
    """
    try:
        from ..review.backtest import get_latest_ir_weights
        ir_w = get_latest_ir_weights()
    except Exception:
        ir_w = {}
    if not ir_w:
        return dict(DEFAULT_WEIGHTS)
    merged = dict(DEFAULT_WEIGHTS)
    from collections import defaultdict
    factor_irs = defaultdict(list)
    for (factor, _aid), ir in ir_w.items():
        factor_irs[factor].append(ir)
    for factor, irs in factor_irs.items():
        mean_ir = sum(irs) / len(irs) if irs else 0.0
        mult = min(IR_MULT_MAX, max(IR_MULT_MIN, 1.0 + IR_SLOPE * mean_ir))
        # 未列入 DEFAULT_WEIGHTS 的因子, 基准与 compose() 的 fallback (0.5) 一致
        merged[factor] = round(merged.get(factor, 0.5) * mult, 4)
    return merged


# 向后兼容
FACTOR_WEIGHTS = DEFAULT_WEIGHTS


def _btc_price_changes() -> dict | None:
    """从 raw_metrics 的 BTC 日级价格算 change_7d / change_30d (百分比)。

    价格优先级 binance > okx > coingecko (与 backtest._get_asset_price_history 一致)。
    基准价取「最新日期往前 N 天 (含) 之前最近的一天」; 历史不足 30 天 → None。
    """
    df = query_df(
        """SELECT ts, value AS price, source FROM raw_metrics
           WHERE asset_id='bitcoin' AND metric='price_usd'
             AND source IN ('binance','okx','coingecko') AND value > 0
           ORDER BY ts"""
    )
    if df is None or df.empty:
        return None
    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts", "price"])
    if df.empty:
        return None
    df["date"] = df["ts"].dt.normalize()
    df["pref"] = df["source"].map({"binance": 1, "okx": 2, "coingecko": 3}).fillna(9)
    # 每日取一个价: 优先级高的源, 同源取当日最晚一条
    daily = (df.sort_values(["date", "pref", "ts"], ascending=[True, True, False])
               .drop_duplicates("date", keep="first")[["date", "price"]]
               .reset_index(drop=True))
    last_date = daily["date"].iloc[-1]
    p_now = float(daily["price"].iloc[-1])

    def _chg(days: int) -> float | None:
        base = daily[daily["date"] <= last_date - timedelta(days=days)]
        if base.empty:
            return None
        p0 = float(base["price"].iloc[-1])
        return (p_now / p0 - 1.0) * 100.0 if p0 > 0 else None

    chg_7d, chg_30d = _chg(7), _chg(30)
    if chg_7d is None or chg_30d is None:
        return None
    return {"change_7d": chg_7d, "change_30d": chg_30d}


# meta_learner.detect_regime 的小写 regime → 信号层大写 regime
_MOMENTUM_REGIME_MAP = {
    "bull": "BULL", "bear": "BEAR", "volatile": "VOLATILE", "sideways": "CHOP",
}


def _detect_regime() -> tuple[str, str]:
    """轻量 regime 识别, 返回 (regime, regime_source)。

    1. F&G + ETF 5d 都在 → 原规则 (source="etf+fg")
    2. etf_flow_5d 缺失 (farside 源被封, 该因子从未入库) → 不再默认 0,
       改用 BTC 7d/30d 动量, 阈值复用 research.meta_learner.detect_regime
       (source="btc_momentum")
    3. 两者都没有 → "UNKNOWN" (source="none")
    修复 2026-10 回测发现 etf 缺失被当成 0 → 1770/1770 行全是 CHOP 的问题。
    """
    df = query_df(
        """SELECT factor, raw_value FROM factors f
           WHERE (factor='fear_greed_reversal' OR factor='etf_flow_5d')
             AND ts = (SELECT MAX(ts) FROM factors f2 WHERE f2.factor=f.factor)"""
    )
    m = {} if df is None or df.empty else dict(zip(df["factor"], df["raw_value"]))
    fg = m.get("fear_greed_reversal")
    etf = m.get("etf_flow_5d")
    fg = None if fg is None or pd.isna(fg) else float(fg)
    etf = None if etf is None or pd.isna(etf) else float(etf)

    if etf is not None and fg is not None:
        if fg < 25 and etf > 0:
            return "BEAR_BOUNCE", "etf+fg"
        if fg > 75 and etf < 0:
            return "CRISIS", "etf+fg"
        if etf > 500 and 40 <= fg <= 75:
            return "BULL", "etf+fg"
        if etf < -500:
            return "BEAR", "etf+fg"
        return "CHOP", "etf+fg"

    # ETF 缺失 → BTC 动量兜底
    try:
        btc = _btc_price_changes()
    except Exception:
        btc = None
    if btc:
        from ..research.meta_learner import detect_regime
        mapped = _MOMENTUM_REGIME_MAP.get(detect_regime(btc))
        if mapped:
            return mapped, "btc_momentum"
    return "UNKNOWN", "none"


def compose() -> list[dict]:
    df = latest_factors()
    if df.empty:
        return []

    ts = now_iso()
    regime, regime_source = _detect_regime()
    weights = _effective_weights()   # v0.5: 动态从 backtest 拉, 缺省用 DEFAULT
    results = []

    # 按 asset_id 聚合
    for asset_id, grp in df.groupby(df["asset_id"].fillna("market")):
        num = 0.0
        denom = 0.0
        wsum = 0.0
        breakdown = {}
        for _, row in grp.iterrows():
            fname = row["factor"]
            w = weights.get(fname, 0.5)
            sig = 0 if pd.isna(row["signal"]) else int(row["signal"])
            conf = 0.5 if pd.isna(row["confidence"]) else float(row["confidence"])
            num += sig * conf * w
            denom += conf * w
            wsum += w
            breakdown[fname] = {
                "signal": int(sig), "confidence": round(float(conf), 3),
                "raw_value": None if pd.isna(row["raw_value"]) else float(row["raw_value"]),
                "weight": w,
            }
        composite = num / denom if denom else 0
        if composite > 0.2:
            direction = "BULL"
        elif composite < -0.2:
            direction = "BEAR"
        else:
            direction = "NEUTRAL"
        confidence = min(1.0, denom / wsum) if wsum else 0.0

        # signals 表没有专门列, regime_source 放进 factor_breakdown JSON 的 "_meta" 键;
        # 带 signal=0 是为了让 report.daily 的 top_drivers 过滤 (signal != 0) 跳过它
        breakdown["_meta"] = {
            "signal": 0, "confidence": 0.0, "raw_value": None, "weight": 0.0,
            "regime_source": regime_source,
        }
        results.append({
            "ts": ts, "asset_id": asset_id if asset_id != "market" else None,
            "composite": round(composite, 4),
            "direction": direction,
            "confidence": round(confidence, 3),
            "regime": regime,
            "regime_source": regime_source,   # 仅内存字段, upsert_signal 按名绑定会忽略
            "factor_breakdown": json.dumps(breakdown, ensure_ascii=False),
        })
    upsert_signal(results)
    return results
