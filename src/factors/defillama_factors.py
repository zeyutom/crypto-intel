"""DefiLlama 衍生因子 — 4 个链上原生信号.

这 4 个因子用 defillama_full.py 拿数据, 现有数据源 (CoinGecko/Binance/CEX)
拿不到这种粒度的链上经济活动指标.

  1. tvl_momentum_7d      — Top 协议 TVL 7 天增速 (符号: 协议变化 → 持有币种)
                            原理: TVL 上升 = 资金真实涌入, 比交易量更扎实

  2. dex_volume_growth    — DEX 24h vs 7d 平均的成交量比, 衡量"是否突然活跃"
                            原理: DEX 突然放量预示 narrative 或 incident

  3. stable_peg_deviation — 稳定币脱锚分: 多稳定币加权偏离 + 方向性
                            原理: USDT/USDC 微脱锚是市场流动性紧张信号
                                  USDe/PYUSD 等新币脱锚 → 全市场避险

  4. yield_spike          — 主流 yield pool APY 异常上升
                            原理: 借贷利率/lp 收益率突变 = 资金成本 / 套利窗口

输出: dict[symbol -> {factor: float, raw: dict}]
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import Optional

from ..utils import setup_logger

log = setup_logger("defillama_factors", "INFO")


def _safe_dlf():
    """惰性 import + 优雅降级."""
    try:
        from ..adapters import defillama_full as dlf
        return dlf
    except ImportError:
        return None


# ────────────────────────────────────────────────────────────────────
#  Factor 1: TVL Momentum 7d
# ────────────────────────────────────────────────────────────────────

def compute_tvl_momentum(
    protocol_slugs: list[str] = None,
    days: int = 7,
) -> dict[str, dict]:
    """计算指定协议的 TVL N 天增速.

    Args:
        protocol_slugs: 如 ['uniswap', 'aave', 'curve-dex', 'lido']
                        默认取 Top 30 by current TVL

    Returns:
        {slug: {tvl_change_pct, tvl_now, tvl_past, momentum_score, raw}}
        momentum_score ∈ [-1, 1] 归一化分
    """
    dlf = _safe_dlf()
    if dlf is None:
        return {}

    if not protocol_slugs:
        top = dlf.get_top_protocols_by_tvl(30) or []
        protocol_slugs = [p.get("slug") for p in top if p.get("slug")]

    out = {}
    for slug in protocol_slugs:
        if not slug:
            continue
        change = dlf.get_protocol_tvl_change(slug, days=days)
        if not change:
            continue
        pct = change["change_pct"]
        # 归一化: ±30% 映射到 ±1, 超出 cap
        score = max(-1.0, min(1.0, pct / 0.30))
        out[slug] = {
            "tvl_change_pct": pct,
            "tvl_now": change["now_tvl"],
            "tvl_past": change["past_tvl"],
            "days": days,
            "momentum_score": round(score, 4),
            "raw": change,
        }
    return dict(sorted(out.items(),
                       key=lambda kv: kv[1]["momentum_score"], reverse=True))


# ────────────────────────────────────────────────────────────────────
#  Factor 2: DEX Volume Growth (24h vs 7d avg)
# ────────────────────────────────────────────────────────────────────

def compute_dex_volume_growth() -> dict[str, dict]:
    """DEX 24h 成交量 vs 7d 日均的比值.

    Returns:
        {protocol_name: {volume_24h, volume_7d_avg, growth_ratio, score, raw}}
        growth_ratio > 1.5 = 突然放量
    """
    dlf = _safe_dlf()
    if dlf is None:
        return {}

    data = dlf.dex_overview(exclude_chart=False)
    if not data:
        return {}

    out = {}
    for p in data.get("protocols", []) or []:
        if not isinstance(p, dict):
            continue
        name = p.get("name") or p.get("slug")
        vol_24h = p.get("total24h") or 0
        vol_7d = p.get("total7d") or 0
        if not name or vol_24h <= 0 or vol_7d <= 0:
            continue
        vol_7d_avg = vol_7d / 7.0
        if vol_7d_avg <= 0:
            continue
        ratio = vol_24h / vol_7d_avg
        # score: 1.0 → 0, 2.0 → +1, 0.5 → -1 (log scale)
        score = max(-1.0, min(1.0, math.log2(ratio)))
        out[name] = {
            "volume_24h_usd": float(vol_24h),
            "volume_7d_avg_usd": float(vol_7d_avg),
            "growth_ratio": round(ratio, 3),
            "score": round(score, 4),
        }
    return dict(sorted(out.items(),
                       key=lambda kv: kv[1]["score"], reverse=True))


# ────────────────────────────────────────────────────────────────────
#  Factor 3: Stablecoin Peg Deviation
# ────────────────────────────────────────────────────────────────────

# 主流稳定币 (按市值权重大致排序)
MAJOR_STABLES = {
    "USDT": 0.45, "USDC": 0.30, "DAI": 0.08,
    "USDE": 0.05, "PYUSD": 0.02, "FDUSD": 0.05,
    "USDS": 0.03, "FRAX": 0.02,
}


def compute_stable_peg_deviation() -> dict:
    """市场级稳定币脱锚分数.

    Returns:
        {
          "market_peg_score": float,    # ∈ [-1, 0], 0=完全锚定, -1=严重脱锚
          "weighted_deviation_bps": float,  # 加权脱锚 (bps)
          "depegged_count": int,         # 偏离 > 50bps 的数量
          "per_stable": {sym: {...}},
          "interpretation": str,
        }
    """
    dlf = _safe_dlf()
    if dlf is None:
        return {"_status": "unavailable"}

    pegs = dlf.get_stable_peg_health()
    if not pegs:
        return {"_status": "no_data"}

    # 只看主流稳定币的加权偏离
    weighted_dev = 0.0
    total_weight = 0.0
    depegged = 0
    per_stable = {}
    for sym, w in MAJOR_STABLES.items():
        if sym not in pegs:
            continue
        d = pegs[sym]
        dev_pct = abs(d["deviation_pct"]) / 100.0  # 转回 decimal
        weighted_dev += dev_pct * w
        total_weight += w
        per_stable[sym] = d
        if dev_pct > 0.005:
            depegged += 1

    if total_weight == 0:
        return {"_status": "no_major_stables"}

    avg_dev = weighted_dev / total_weight
    bps = avg_dev * 10000

    # 评分: ±5bps 算正常 (score=0), ±100bps 算严重 (score=-1)
    score = -min(1.0, max(0.0, (avg_dev * 10000 - 5) / 95))

    if abs(bps) < 5:
        interp = f"稳定币锚定健康 (加权偏离 {bps:.1f}bps)"
    elif abs(bps) < 20:
        interp = f"轻度偏离 ({bps:.1f}bps), 关注流动性"
    elif abs(bps) < 50:
        interp = f"明显偏离 ({bps:.1f}bps), 谨慎"
    else:
        interp = f"严重脱锚 ({bps:.1f}bps), 警报! 系统性流动性紧张"

    return {
        "market_peg_score": round(score, 4),
        "weighted_deviation_bps": round(bps, 2),
        "depegged_count": depegged,
        "per_stable": per_stable,
        "interpretation": interp,
        "_status": "ok",
    }


# ────────────────────────────────────────────────────────────────────
#  Factor 4: Yield Spike (头部 yield APY 异动)
# ────────────────────────────────────────────────────────────────────

def compute_yield_spike(top_n: int = 30) -> dict:
    """Top-N 主流稳定币池子的 APY 中位数 vs 周历史.

    Returns:
        {
          "median_stable_apy_pct": float,
          "p75_stable_apy_pct": float,
          "p90_stable_apy_pct": float,
          "n_pools_above_10pct": int,    # >10% APY 的池子数
          "regime": "tight" | "normal" | "loose",
          "interpretation": str,
        }
    """
    dlf = _safe_dlf()
    if dlf is None:
        return {"_status": "unavailable"}

    pools = dlf.get_top_yield_opportunities(
        min_tvl=1e7, max_apy=200, stable_only=True
    )
    if not pools:
        return {"_status": "no_data"}

    apys = sorted([p["apy"] for p in pools[:top_n] if p["apy"] > 0])
    if len(apys) < 3:
        return {"_status": "too_few_pools", "n": len(apys)}

    import statistics
    median = statistics.median(apys)
    p75 = apys[int(len(apys) * 0.75)] if len(apys) > 3 else apys[-1]
    p90 = apys[int(len(apys) * 0.90)] if len(apys) > 9 else apys[-1]
    above_10 = sum(1 for a in apys if a > 10)

    # 判断 regime: 稳定币池子加权 yield 中位数
    # < 4%: 资金过剩, 流动性宽松
    # 4-8%: 正常
    # > 8%: 资金紧张
    if median < 4:
        regime = "loose"
        interp = f"稳定币池子 yield 中位 {median:.1f}%, 流动性宽松, 风险偏好高"
    elif median < 8:
        regime = "normal"
        interp = f"稳定币池子 yield 中位 {median:.1f}%, 流动性正常"
    else:
        regime = "tight"
        interp = f"稳定币池子 yield 中位 {median:.1f}%, 流动性紧张, 借贷需求高"

    return {
        "median_stable_apy_pct": round(median, 2),
        "p75_stable_apy_pct": round(p75, 2),
        "p90_stable_apy_pct": round(p90, 2),
        "n_pools_above_10pct": above_10,
        "n_pools_sampled": len(apys),
        "regime": regime,
        "interpretation": interp,
        "_status": "ok",
    }


# ────────────────────────────────────────────────────────────────────
#  汇总入口 — 给 pipeline 调用
# ────────────────────────────────────────────────────────────────────

def compute_all_defillama_factors() -> dict:
    """一次性算全部 4 个因子."""
    return {
        "tvl_momentum": compute_tvl_momentum(),
        "dex_volume_growth": compute_dex_volume_growth(),
        "stable_peg_deviation": compute_stable_peg_deviation(),
        "yield_spike": compute_yield_spike(),
        "_ts": datetime.utcnow().isoformat() + "Z",
    }


def is_available() -> bool:
    dlf = _safe_dlf()
    return dlf is not None and dlf.is_available()


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    print("=== TVL Momentum (Top 10) ===")
    mom = compute_tvl_momentum()
    for slug, d in list(mom.items())[:10]:
        sign = "↑" if d["tvl_change_pct"] > 0 else "↓"
        print(f"  {slug:25s}  {sign} {d['tvl_change_pct']*100:+6.2f}% "
              f"score={d['momentum_score']:+.3f}")

    print("\n=== DEX Volume Growth (Top 5) ===")
    dv = compute_dex_volume_growth()
    for name, d in list(dv.items())[:5]:
        print(f"  {name:25s}  ratio={d['growth_ratio']:.2f}  "
              f"score={d['score']:+.3f}")

    print("\n=== Stable Peg Deviation ===")
    peg = compute_stable_peg_deviation()
    print(f"  score: {peg.get('market_peg_score')}")
    print(f"  bps:   {peg.get('weighted_deviation_bps')}")
    print(f"  {peg.get('interpretation')}")

    print("\n=== Yield Spike ===")
    ys = compute_yield_spike()
    print(f"  median APY: {ys.get('median_stable_apy_pct')}%")
    print(f"  regime: {ys.get('regime')}")
    print(f"  {ys.get('interpretation')}")
