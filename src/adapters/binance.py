"""Binance 公开 API: 现货价格 + 永续资金费率.

v0.9: 451/403 等被屏蔽时优雅返回 [] (美国机房常见, OKX adapter 兜底).
"""
from ..config import CFG
from ..utils import now_iso, setup_logger
from ..http_client import http

log = setup_logger("binance_adapter", "INFO")


def _safe_get(url: str, params: dict = None):
    """优雅版 http_get_json — 失败返回 None 不抛."""
    return http.get_json(url, params=params, ttl="hot")


def fetch() -> list[dict]:
    if not CFG["sources"]["binance"]["enabled"]:
        return []
    spot = CFG["sources"]["binance"]["spot_base"]
    fapi = CFG["sources"]["binance"]["fapi_base"]
    ts = now_iso()
    rows = []

    # 1. 现货价格 — 被屏蔽 (451) 时返回空, 不抛, 让 OKX 兜底
    price_data = _safe_get(f"{spot}/api/v3/ticker/price")
    if not price_data:
        log.warning("Binance spot 不可达 (451/网络问题), 跳过. 用 OKX adapter 兜底")
        return []
    prices = {p["symbol"]: float(p["price"]) for p in price_data}

    # 2. 永续资金费率
    fundings = {}
    for asset in CFG["universe"]:
        sym = asset.get("binance")
        if not sym:
            continue
        data = _safe_get(f"{fapi}/fapi/v1/fundingRate",
                         params={"symbol": sym, "limit": 1})
        if data:
            fundings[sym] = float(data[-1]["fundingRate"])

    # 3. 24h 聚合
    stats_data = _safe_get(f"{spot}/api/v3/ticker/24hr")
    if stats_data is None:
        stats_data = []
    stats = {s["symbol"]: s for s in stats_data
             if s["symbol"] in {a["binance"] for a in CFG["universe"] if a.get("binance")}}

    for asset in CFG["universe"]:
        sym = asset.get("binance")
        if not sym:
            continue
        price = prices.get(sym)
        st = stats.get(sym, {})
        funding = fundings.get(sym)
        rows.extend([
            {"ts": ts, "source": "binance", "asset_id": asset["id"],
             "metric": "price_usd", "value": price, "value_text": None},
            {"ts": ts, "source": "binance", "asset_id": asset["id"],
             "metric": "change_24h_pct", "value": float(st.get("priceChangePercent", 0) or 0),
             "value_text": None},
            {"ts": ts, "source": "binance", "asset_id": asset["id"],
             "metric": "volume_24h_usd", "value": float(st.get("quoteVolume", 0) or 0),
             "value_text": None},
        ])
        if funding is not None:
            rows.append({"ts": ts, "source": "binance", "asset_id": asset["id"],
                         "metric": "funding_rate_8h", "value": funding, "value_text": None})
    return rows
