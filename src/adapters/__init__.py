"""数据源适配器层: 每个 adapter 负责 fetch + 返回统一 schema 的 raw rows。"""
from . import coingecko, binance, coinbase, defillama, feargreed, farside

ALL_ADAPTERS = {
    "coingecko": coingecko,
    "binance": binance,
    "coinbase": coinbase,
    "defillama": defillama,
    "feargreed": feargreed,
    "farside_etf": farside,
}
