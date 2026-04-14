"""因子计算层 (v0.4 - 15 个因子)。"""
from types import SimpleNamespace
from . import funding_composite, coinbase_premium, stablecoin_mint, fear_greed_reversal, etf_flow
from . import _v04_factors as v4

# v0.4 因子: 包成与原 module 一样的接口 (有 .compute() 函数)
def _wrap(fn):
    return SimpleNamespace(compute=fn)

ALL_FACTORS = {
    # === v0.1–v0.3 原有 5 个 ===
    "funding_composite":     funding_composite,
    "coinbase_premium":      coinbase_premium,
    "stablecoin_mint_7d":    stablecoin_mint,
    "fear_greed_reversal":   fear_greed_reversal,
    "etf_flow_5d":           etf_flow,

    # === v0.4 新增 10 个 ===
    # 衍生品
    "open_interest_change":  _wrap(v4.compute_open_interest_change),
    "liquidation_heat":      _wrap(v4.compute_liquidation_heat),
    "long_short_ratio":      _wrap(v4.compute_long_short_ratio),
    # 全市场宏观
    "btc_dominance_trend":   _wrap(v4.compute_btc_dominance_trend),
    "total_mcap_momentum":   _wrap(v4.compute_total_mcap_momentum),
    # DeFi
    "defi_tvl_momentum":     _wrap(v4.compute_defi_tvl_momentum),
    # 叙事
    "trending_score":        _wrap(v4.compute_trending_score),
    # 宏观联动
    "btc_nasdaq_corr":       _wrap(v4.compute_btc_nasdaq_corr),
    "btc_gold_corr":         _wrap(v4.compute_btc_gold_corr),
    "dxy_inverse":           _wrap(v4.compute_dxy_inverse),
}
