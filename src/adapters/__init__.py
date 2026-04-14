"""数据源适配器层: 每个 adapter 负责 fetch + 返回统一 schema 的 raw rows。"""
from . import coingecko, binance, coinbase, defillama, feargreed, farside, okx

ALL_ADAPTERS = {
    "coingecko": coingecko,
    "binance": binance,        # 注: 在美国机房 (如 GitHub Actions) 会被 451 屏蔽,会优雅失败
    "okx": okx,                # 替代源, 提供 price + funding rate, 不屏蔽美国
    "coinbase": coinbase,
    "defillama": defillama,
    "feargreed": feargreed,
    "farside_etf": farside,    # 主源 Farside, 失败时自动 fallback CoinGlass
}
