"""因子计算层: 输入 raw_metrics, 输出 factors 表行。"""
from . import funding_composite, coinbase_premium, stablecoin_mint, fear_greed_reversal, etf_flow

ALL_FACTORS = {
    "funding_composite": funding_composite,
    "coinbase_premium": coinbase_premium,
    "stablecoin_mint_7d": stablecoin_mint,
    "fear_greed_reversal": fear_greed_reversal,
    "etf_flow_5d": etf_flow,
}
