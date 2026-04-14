"""数据源适配器层 (v0.4 - 11 个源)。

每个 adapter 负责 fetch + 返回统一 schema 的 raw rows。
"""
from . import (
    coingecko, binance, okx, coinbase, defillama, feargreed, farside,
    coinglass, cg_global, cg_trending, defillama_extra, yfinance_macro,
)

ALL_ADAPTERS = {
    # === 行情 ===
    "coingecko":      coingecko,         # 全市场快照 (备用价格)
    "binance":        binance,           # 主交易所 (US 机房会 451, 优雅失败)
    "okx":            okx,               # 替代主交易所 (不屏蔽美国)
    "coinbase":       coinbase,          # 美区合规交易所 (用于 Coinbase Premium)
    # === 全市场宏观 ===
    "cg_global":      cg_global,         # BTC 占比 / 总市值 / 总成交量
    "cg_trending":    cg_trending,       # 热门搜索币种 (叙事代理)
    # === DeFi ===
    "defillama":      defillama,         # 稳定币流通
    "defillama_extra": defillama_extra,  # 全市场 TVL / 主链 TVL / 协议收入
    # === 衍生品 ===
    "coinglass":      coinglass,         # OI / 清算 / 多空比
    # === 情绪 ===
    "feargreed":      feargreed,         # F&G index
    # === 事件/资金流 ===
    "farside_etf":    farside,           # BTC ETF 净流入 (主源 + Coinglass fallback)
    # === 宏观联动 ===
    "yfinance_macro": yfinance_macro,    # 纳指/道指/SPX/黄金/DXY
}
