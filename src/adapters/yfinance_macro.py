"""yfinance 宏观联动: 纳指 (^IXIC) / 道指 (^DJI) / 黄金 (GC=F) / 美元指数 (DX-Y.NYB)。

用作 BTC 宏观 Beta 因子的输入。yfinance 是免费的 Yahoo Finance 包装。
"""
from datetime import datetime, timedelta, timezone
from ..config import CFG
from ..utils import setup_logger

log = setup_logger("yfinance_macro", "WARNING")

_TICKERS = {
    "ixic": "^IXIC",       # 纳斯达克综合
    "dji":  "^DJI",        # 道琼斯
    "spx":  "^GSPC",       # S&P 500
    "gold": "GC=F",        # 黄金期货
    "dxy":  "DX-Y.NYB",    # 美元指数
}


def fetch() -> list[dict]:
    if not CFG["sources"].get("yfinance_macro", {}).get("enabled", True):
        return []
    try:
        import yfinance as yf
    except ImportError:
        log.warning("yfinance not installed, skip")
        return []

    rows: list[dict] = []
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)

    for asset_id, ticker in _TICKERS.items():
        try:
            t = yf.Ticker(ticker)
            hist = t.history(start=start.strftime("%Y-%m-%d"),
                             end=end.strftime("%Y-%m-%d"))
            if hist.empty:
                continue
            # 写入近 90 个交易日的收盘价 (供因子算相关性)
            for ts, row in hist.iterrows():
                close = float(row["Close"])
                ts_iso = ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ") \
                    if ts.tz else ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                rows.append({
                    "ts": ts_iso, "source": "yfinance_macro",
                    "asset_id": asset_id,
                    "metric": "close_price", "value": close, "value_text": None,
                })
        except Exception as e:
            log.warning("yfinance fetch failed for %s: %s", ticker, e)
            continue

    return rows
