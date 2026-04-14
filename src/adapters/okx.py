"""OKX 公开 API: 现货价格 + 永续资金费率。

OKX 在多数地区(包括美国)可访问, 用作 Binance (US 地区被 451 屏蔽) 的替代源。
公开 API 不需要 key, 限速宽松。
"""
from ..config import CFG
from ..utils import http_get_json, now_iso, setup_logger

log = setup_logger("okx", "WARNING")

OKX_BASE = "https://www.okx.com"

# OKX 用的 instId 命名: BTC-USDT (现货), BTC-USDT-SWAP (永续)
_SPOT_MAP = {
    "bitcoin": "BTC-USDT",
    "ethereum": "ETH-USDT",
    "solana": "SOL-USDT",
    "bnb": "BNB-USDT",
}
_SWAP_MAP = {k: v + "-SWAP" for k, v in _SPOT_MAP.items()}


def fetch() -> list[dict]:
    """返回:
       - price_usd (来自 spot ticker)
       - change_24h_pct (基于 spot ticker 的 sodUtc0 vs last)
       - volume_24h_usd (vol24h * last 估算)
       - funding_rate_8h (来自 swap funding-rate)
    """
    if not CFG["sources"].get("okx", {}).get("enabled", True):
        return []
    ts = now_iso()
    rows: list[dict] = []

    # 1. 现货价格 + 24h 变化
    try:
        spot = http_get_json(f"{OKX_BASE}/api/v5/market/tickers", params={"instType": "SPOT"})
        if spot.get("code") == "0":
            ticker_map = {t["instId"]: t for t in spot["data"]}
            for asset_id, inst in _SPOT_MAP.items():
                t = ticker_map.get(inst)
                if not t:
                    continue
                last = float(t["last"])
                sod = float(t.get("sodUtc0") or t.get("open24h") or last)
                change = (last - sod) / sod * 100 if sod else 0
                vol_24h = float(t.get("volCcy24h") or 0)  # 计价币种成交量 (USDT)
                rows.extend([
                    {"ts": ts, "source": "okx", "asset_id": asset_id,
                     "metric": "price_usd", "value": last, "value_text": None},
                    {"ts": ts, "source": "okx", "asset_id": asset_id,
                     "metric": "change_24h_pct", "value": change, "value_text": None},
                    {"ts": ts, "source": "okx", "asset_id": asset_id,
                     "metric": "volume_24h_usd", "value": vol_24h, "value_text": None},
                ])
    except Exception as e:
        log.warning("OKX spot ticker fetch failed: %s", e)

    # 2. 永续资金费率
    for asset_id, inst in _SWAP_MAP.items():
        try:
            data = http_get_json(f"{OKX_BASE}/api/v5/public/funding-rate",
                                 params={"instId": inst})
            if data.get("code") == "0" and data.get("data"):
                fr = float(data["data"][0]["fundingRate"])
                rows.append({"ts": ts, "source": "okx", "asset_id": asset_id,
                             "metric": "funding_rate_8h", "value": fr, "value_text": None})
        except Exception as e:
            log.warning("OKX funding fetch failed for %s: %s", inst, e)

    return rows
