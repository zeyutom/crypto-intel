"""Coinglass 公开 API: 衍生品综合数据 (OI / Funding / Liquidations)。

Coinglass 提供丰富的衍生品数据, 部分公开端点不需 key。
注: 部分 endpoint 限速, 失败时返回空列表不影响其他源。
"""
from datetime import datetime, timezone
from ..config import CFG
from ..utils import http_get_json, now_iso, setup_logger

log = setup_logger("coinglass", "WARNING")

BASE = "https://fapi.coinglass.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.coinglass.com",
    "Referer": "https://www.coinglass.com/",
}

_SYMBOL_MAP = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "bnb": "BNB",
}


def fetch() -> list[dict]:
    """返回:
       - oi_total_usd: 全交易所未平仓合约总和 (BTC/ETH 等)
       - liquidations_24h_usd: 过去 24h 全网清算
       - long_short_ratio: 多空账户比 (主流交易所平均)
    """
    if not CFG["sources"].get("coinglass", {}).get("enabled", True):
        return []
    ts = now_iso()
    rows: list[dict] = []

    # 1. Open Interest 历史 (近期点 = 当前 OI)
    for asset_id, sym in _SYMBOL_MAP.items():
        try:
            data = http_get_json(
                f"{BASE}/api/openInterest/v3/chart",
                params={"symbol": sym, "timeType": "h1", "exchangeName": "All"},
                headers=HEADERS, timeout=15,
            )
            if data.get("code") in ("0", 0, "success"):
                points = data.get("data", {}).get("priceList", [])
                oi_points = data.get("data", {}).get("dataMap", {})
                # 取最新 OI 总和
                if isinstance(oi_points, dict):
                    last_oi = 0
                    for ex_name, ex_series in oi_points.items():
                        if isinstance(ex_series, list) and ex_series:
                            last_oi += float(ex_series[-1] or 0)
                    if last_oi > 0:
                        rows.append({
                            "ts": ts, "source": "coinglass", "asset_id": asset_id,
                            "metric": "oi_total_usd",
                            "value": last_oi, "value_text": None,
                        })
        except Exception as e:
            log.warning("Coinglass OI fetch failed for %s: %s", sym, e)

    # 2. 24h 全网清算
    try:
        data = http_get_json(
            f"{BASE}/api/futures/liquidation/info",
            params={"timeType": "1", "symbol": "all"},
            headers=HEADERS, timeout=15,
        )
        if data.get("code") in ("0", 0, "success"):
            d = data.get("data", {})
            longs = float(d.get("longVolUsd", 0) or 0)
            shorts = float(d.get("shortVolUsd", 0) or 0)
            total = longs + shorts
            if total > 0:
                rows.extend([
                    {"ts": ts, "source": "coinglass", "asset_id": "market",
                     "metric": "liquidations_24h_usd", "value": total, "value_text": None},
                    {"ts": ts, "source": "coinglass", "asset_id": "market",
                     "metric": "liquidations_long_24h_usd", "value": longs, "value_text": None},
                    {"ts": ts, "source": "coinglass", "asset_id": "market",
                     "metric": "liquidations_short_24h_usd", "value": shorts, "value_text": None},
                ])
    except Exception as e:
        log.warning("Coinglass liquidations fetch failed: %s", e)

    # 3. 多空比 (BTC)
    try:
        data = http_get_json(
            f"{BASE}/api/futures/longShortChart",
            params={"symbol": "BTC", "timeType": "h1"},
            headers=HEADERS, timeout=15,
        )
        if data.get("code") in ("0", 0, "success"):
            d = data.get("data", {})
            ratios = d.get("longShortRatios", []) if isinstance(d, dict) else []
            if ratios:
                last = float(ratios[-1])
                rows.append({
                    "ts": ts, "source": "coinglass", "asset_id": "bitcoin",
                    "metric": "long_short_ratio", "value": last, "value_text": None,
                })
    except Exception as e:
        log.warning("Coinglass long/short fetch failed: %s", e)

    return rows
