"""Binance 公开 API: 现货价格 + 永续资金费率。"""
from ..config import CFG
from ..utils import http_get_json, now_iso


def fetch() -> list[dict]:
    if not CFG["sources"]["binance"]["enabled"]:
        return []
    spot = CFG["sources"]["binance"]["spot_base"]
    fapi = CFG["sources"]["binance"]["fapi_base"]
    ts = now_iso()
    rows = []

    # 1. 现货价格
    prices = {p["symbol"]: float(p["price"])
              for p in http_get_json(f"{spot}/api/v3/ticker/price")}

    # 2. 永续资金费率(最近一笔)
    fundings = {}
    for asset in CFG["universe"]:
        sym = asset.get("binance")
        if not sym:
            continue
        data = http_get_json(f"{fapi}/fapi/v1/fundingRate",
                             params={"symbol": sym, "limit": 1})
        if data:
            fundings[sym] = float(data[-1]["fundingRate"])

    # 3. 24h 聚合: 价格变化 / 成交量
    stats = {s["symbol"]: s for s in http_get_json(f"{spot}/api/v3/ticker/24hr")
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
