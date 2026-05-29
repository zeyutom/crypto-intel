"""因子桥接: 把 pipeline 的市场级因子注入 screener 的评分体系。

Pipeline 因子 (src/factors/) 产出的是市场级信号 (signal ∈ {-1, 0, 1}),
存储在 DB 的 factors 表中。这些因子不是逐币计算的,而是整体市场的情绪/宏观指标。

桥接策略:
  1. 从 DB 读取最新 pipeline 因子 (funding_composite, fear_greed_reversal, etc.)
  2. 计算一个 "market_overlay" 乘数 (0.85 ~ 1.15)
  3. 在 screener 打分后, 对每个币的 composite_score 乘以此乘数
  4. 特定因子 (如 funding_composite) 可以覆盖/增强 screener 的同名因子

这样两套系统互补: screener 做逐币精细打分, pipeline 做宏观情绪叠加。
"""
from __future__ import annotations
import json
from ..utils import setup_logger

log = setup_logger("factor_bridge", "INFO")


# ── 市场级因子 → overlay 权重 ──
MARKET_FACTORS = {
    # factor_name: (weight_in_overlay, description)
    "fear_greed_reversal":    (0.25, "恐贪指数反转"),
    "funding_composite":      (0.20, "多币种加权资金费率"),
    "liquidation_heat":       (0.15, "清算热力"),
    "btc_dominance_trend":    (0.10, "BTC 主导率趋势"),
    "total_mcap_momentum":    (0.10, "全市场总市值动量"),
    "defi_tvl_momentum":      (0.10, "DeFi TVL 动量"),
    "open_interest_change":   (0.10, "OI 变化率"),
}


def load_pipeline_signals() -> dict:
    """从 DB 读取最新 pipeline 因子值。

    返回: {factor_name: {signal, raw_value, confidence, meta}}
    """
    try:
        from ..db import query_df
        df = query_df(
            """SELECT f.factor, f.signal, f.raw_value, f.confidence, f.meta
               FROM factors f
               JOIN (SELECT factor AS f_, MAX(ts) AS mts FROM factors GROUP BY factor) m
               ON f.factor = m.f_ AND f.ts = m.mts"""
        )
        if df.empty:
            log.info("  Pipeline 因子表为空 (可能未运行过 ingest+factors)")
            return {}

        result = {}
        for _, row in df.iterrows():
            meta = {}
            try:
                meta = json.loads(row.get("meta", "{}") or "{}")
            except Exception:
                pass
            result[row["factor"]] = {
                "signal": int(row.get("signal", 0)),
                "raw_value": float(row.get("raw_value", 0)),
                "confidence": float(row.get("confidence", 0.5)),
                "meta": meta,
            }
        log.info(f"  加载 {len(result)} 个 pipeline 因子")
        return result
    except Exception as e:
        log.warning(f"  Pipeline 因子加载失败 (DB 可能未初始化): {e}")
        return {}


def calc_market_overlay(pipeline_signals: dict) -> float:
    """计算市场级叠加乘数。

    返回: 0.85 ~ 1.15 的乘数
    - > 1.0: 市场情绪偏多 (应该更积极选币)
    - < 1.0: 市场情绪偏空 (应该更保守)
    - = 1.0: 中性或数据不足
    """
    if not pipeline_signals:
        return 1.0

    weighted_signal = 0.0
    total_weight = 0.0

    for fname, (weight, desc) in MARKET_FACTORS.items():
        fdata = pipeline_signals.get(fname)
        if fdata is None:
            continue
        sig = fdata["signal"]         # -1, 0, +1
        conf = fdata["confidence"]    # 0-1
        weighted_signal += sig * conf * weight
        total_weight += weight

    if total_weight == 0:
        return 1.0

    # 归一化到 -1 ~ +1
    norm_signal = weighted_signal / total_weight

    # 映射到 0.85 ~ 1.15
    overlay = 1.0 + norm_signal * 0.15

    log.info(f"  Market overlay = {overlay:.3f} "
             f"(signal={norm_signal:+.3f}, "
             f"{sum(1 for f in MARKET_FACTORS if f in pipeline_signals)}/{len(MARKET_FACTORS)} 因子)")

    return round(overlay, 4)


def apply_market_overlay(scored_coins: list[dict],
                         overlay: float) -> list[dict]:
    """对所有币的 composite_score 应用市场叠加乘数, 然后重新排序。"""
    if abs(overlay - 1.0) < 0.001:
        return scored_coins

    for c in scored_coins:
        c["composite_score_raw"] = c["composite_score"]
        c["composite_score"] = round(c["composite_score"] * overlay, 4)
        c["market_overlay"] = overlay

    scored_coins.sort(key=lambda x: x["composite_score"], reverse=True)
    return scored_coins


def get_pipeline_summary(pipeline_signals: dict) -> list[dict]:
    """把 pipeline 因子整理成可读的摘要 (用于报告展示)。"""
    summary = []
    signal_labels = {1: "📈 看多", 0: "➡️ 中性", -1: "📉 看空"}
    for fname, (weight, desc) in MARKET_FACTORS.items():
        fdata = pipeline_signals.get(fname)
        if fdata is None:
            summary.append({
                "name": fname, "desc": desc,
                "signal_label": "❓ 无数据", "signal": 0,
                "raw_value": None, "confidence": 0,
            })
        else:
            summary.append({
                "name": fname, "desc": desc,
                "signal_label": signal_labels.get(fdata["signal"], "➡️ 中性"),
                "signal": fdata["signal"],
                "raw_value": fdata["raw_value"],
                "confidence": fdata["confidence"],
            })
    return summary
