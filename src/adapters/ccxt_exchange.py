"""统一交易所 adapter via ccxt (Phase 2-C).

替换/补充 binance.py / okx.py / coinbase.py 三个独立 adapter,
用 ccxt 一个 API 调任意支持的交易所, 配带 fallback 链:

  Primary: binance → fallback: okx → fallback: coinbase
  Spot 价 / OHLCV / 资金费率 都用统一接口

设计:
  - 软依赖 ccxt (没装时返回空数据 + 提示)
  - 默认 exchange 列表: ["binance", "okx", "coinbase", "bybit", "kucoin"]
  - 每个 method 自动尝试 primary → fallback, 第一个 succeed 即返回
  - 提供与现有 binance/okx adapter 兼容的 fetch() 入口

API:
  - fetch_ticker(symbol, exchanges=None) -> dict
  - fetch_ohlcv(symbol, timeframe="1d", limit=200, exchanges=None) -> DataFrame
  - fetch_funding_rate(symbol, exchanges=None) -> float
  - fetch_all_tickers(exchanges=None) -> dict[symbol -> price]
  - health() -> dict
"""
from __future__ import annotations
import time
from typing import Optional

from ..utils import setup_logger

log = setup_logger("ccxt_exchange", "INFO")

# 默认交易所优先级 (按数据质量+稳定性)
DEFAULT_EXCHANGES = ["binance", "okx", "coinbase", "bybit", "kucoin"]

# Module-level cache: ccxt instances
_exchange_cache: dict = {}


def _ccxt_available() -> bool:
    try:
        import ccxt  # noqa
        return True
    except ImportError:
        return False


def is_available() -> bool:
    return _ccxt_available()


def _get_exchange(name: str):
    """惰性创建并缓存 ccxt exchange 实例。"""
    if name in _exchange_cache:
        return _exchange_cache[name]
    if not _ccxt_available():
        return None
    import ccxt
    try:
        cls = getattr(ccxt, name, None)
        if cls is None:
            return None
        inst = cls({
            "timeout": 15000,
            "enableRateLimit": True,
        })
        _exchange_cache[name] = inst
        return inst
    except Exception as e:
        log.warning(f"  init {name} failed: {e}")
        return None


def _normalize_symbol(symbol: str, market_type: str = "spot") -> str:
    """把 'BTC' / 'BTCUSDT' 规范成 ccxt 的 'BTC/USDT' 格式。"""
    s = symbol.upper().replace("-", "/").replace("_", "/")
    if "/" in s:
        return s
    if s.endswith("USDT") and len(s) > 4:
        return f"{s[:-4]}/USDT"
    if s.endswith("USD") and len(s) > 3:
        return f"{s[:-3]}/USD"
    if s.endswith("BTC") and len(s) > 3:
        return f"{s[:-3]}/BTC"
    # 默认拼 USDT
    return f"{s}/USDT"


# ====================================================================
#  Ticker (单个币种最新价 + 24h 数据)
# ====================================================================

def fetch_ticker(symbol: str, exchanges: list[str] = None) -> Optional[dict]:
    """获取最新 ticker, 自动 fallback。

    Returns: {
      "exchange": str, "symbol": str, "price": float,
      "bid": float, "ask": float, "volume_24h": float,
      "change_pct_24h": float, "high_24h": float, "low_24h": float,
    } 或 None
    """
    if not _ccxt_available():
        return None
    exchanges = exchanges or DEFAULT_EXCHANGES
    sym = _normalize_symbol(symbol)

    for name in exchanges:
        ex = _get_exchange(name)
        if ex is None:
            continue
        try:
            t = ex.fetch_ticker(sym)
            return {
                "exchange": name,
                "symbol": sym,
                "price": float(t.get("last") or 0),
                "bid": float(t.get("bid") or 0),
                "ask": float(t.get("ask") or 0),
                "volume_24h": float(t.get("baseVolume") or 0),
                "quote_volume_24h": float(t.get("quoteVolume") or 0),
                "change_pct_24h": float(t.get("percentage") or 0),
                "high_24h": float(t.get("high") or 0),
                "low_24h": float(t.get("low") or 0),
                "_ts": int(time.time()),
            }
        except Exception as e:
            log.debug(f"  {name}.fetch_ticker({sym}) failed: {e}")
            continue
    return None


# ====================================================================
#  OHLCV (K线历史, 给 Alpha158 准备)
# ====================================================================

def fetch_ohlcv(
    symbol: str,
    timeframe: str = "1d",
    limit: int = 200,
    exchanges: list[str] = None,
) -> Optional["pd.DataFrame"]:
    """获取 OHLCV K 线, 返回 pandas DataFrame。

    Columns: [open, high, low, close, volume], index = datetime
    """
    if not _ccxt_available():
        return None

    try:
        import pandas as pd
    except ImportError:
        return None

    exchanges = exchanges or DEFAULT_EXCHANGES
    sym = _normalize_symbol(symbol)

    for name in exchanges:
        ex = _get_exchange(name)
        if ex is None:
            continue
        try:
            raw = ex.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
            if not raw:
                continue
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["datetime"] = pd.to_datetime(df["ts"], unit="ms")
            df = df.set_index("datetime").drop(columns=["ts"])
            df.attrs["exchange"] = name
            df.attrs["symbol"] = sym
            return df
        except Exception as e:
            log.debug(f"  {name}.fetch_ohlcv({sym}) failed: {e}")
            continue
    return None


# ====================================================================
#  Funding rate (永续合约资金费率)
# ====================================================================

def fetch_funding_rate(
    symbol: str,
    exchanges: list[str] = None,
) -> Optional[dict]:
    """获取最新资金费率 (perp futures)."""
    if not _ccxt_available():
        return None
    # 只用支持 perp 的交易所
    exchanges = exchanges or ["binance", "okx", "bybit"]
    sym = _normalize_symbol(symbol)
    perp = sym.replace("USDT", "USDT:USDT")  # ccxt perp 标记

    for name in exchanges:
        ex = _get_exchange(name)
        if ex is None or not ex.has.get("fetchFundingRate"):
            continue
        try:
            fr = ex.fetch_funding_rate(perp)
            return {
                "exchange": name,
                "symbol": perp,
                "rate": float(fr.get("fundingRate") or 0),
                "next_funding": fr.get("fundingDatetime"),
                "_ts": int(time.time()),
            }
        except Exception as e:
            log.debug(f"  {name}.fetch_funding_rate({perp}) failed: {e}")
            continue
    return None


# ====================================================================
#  批量: 一次拿所有 ticker (用于 universe 扫描)
# ====================================================================

def fetch_all_tickers(
    exchanges: list[str] = None,
    quote: str = "USDT",
) -> dict[str, float]:
    """获取一个交易所所有 quote-pair 的最新价 → {BTC: 67890, ETH: 3500, ...}"""
    if not _ccxt_available():
        return {}
    exchanges = exchanges or DEFAULT_EXCHANGES

    for name in exchanges:
        ex = _get_exchange(name)
        if ex is None:
            continue
        try:
            tickers = ex.fetch_tickers()
            out = {}
            for sym, t in tickers.items():
                if not sym.endswith(f"/{quote}"):
                    continue
                base = sym.replace(f"/{quote}", "")
                last = t.get("last")
                if last:
                    out[base] = float(last)
            if out:
                log.info(f"  {name}.fetch_tickers -> {len(out)} {quote} pairs")
                return out
        except Exception as e:
            log.debug(f"  {name}.fetch_tickers failed: {e}")
            continue
    return {}


# ====================================================================
#  Health check
# ====================================================================

def health() -> dict:
    """诊断: ccxt 是否可用 + 每个 exchange 能否 ping。"""
    if not _ccxt_available():
        return {"installed": False, "hint": "pip install ccxt"}
    import ccxt
    out = {
        "installed": True,
        "version": ccxt.__version__,
        "exchanges": {},
    }
    for name in DEFAULT_EXCHANGES:
        ex = _get_exchange(name)
        if ex is None:
            out["exchanges"][name] = "init_failed"
            continue
        try:
            t = ex.fetch_ticker("BTC/USDT")
            out["exchanges"][name] = f"ok (BTC=${t.get('last', '?')})"
        except Exception as e:
            out["exchanges"][name] = f"err: {str(e)[:60]}"
    return out


# ====================================================================
#  兼容入口: 模仿 binance.fetch() 签名 (drop-in 替换)
# ====================================================================

def fetch() -> list[dict]:
    """兼容现有 adapters/binance.py 风格的入口。

    返回行格式与原 binance.fetch() 类似, 供 pipeline 直接消费。
    """
    if not _ccxt_available():
        return []
    try:
        from ..config import CFG
    except Exception:
        log.warning("  config not loadable, returning empty")
        return []

    from ..utils import now_iso
    ts = now_iso()
    rows = []
    universe = CFG.get("universe", [])
    if not universe:
        log.warning("  config.universe empty")
        return []

    # 批量拿一次 ticker (single API call), 比单调快
    all_t = fetch_all_tickers()
    for asset in universe:
        sym = asset.get("symbol") or asset.get("binance", "")
        sym = sym.replace("USDT", "")
        price = all_t.get(sym)
        if price is None:
            continue
        rows.append({
            "ts": ts, "source": "ccxt", "asset_id": sym,
            "metric": "spot_price", "value": price,
        })

    log.info(f"  ccxt.fetch -> {len(rows)} rows (covers {len(all_t)} pairs)")
    return rows


# ====================================================================
#  Self-test
# ====================================================================

if __name__ == "__main__":
    import json
    print(f"[ccxt_exchange] available: {_ccxt_available()}")
    if _ccxt_available():
        print(json.dumps(health(), indent=2))
        print("\n--- BTC ticker ---")
        print(json.dumps(fetch_ticker("BTC"), indent=2, default=str))
        print("\n--- ETH funding rate ---")
        print(json.dumps(fetch_funding_rate("ETH"), indent=2, default=str))
    else:
        print("install: pip install ccxt --break-system-packages")
