"""Coinbase Exchange 公开 API: 价格 (用于计算 Coinbase Premium)。"""
from ..config import CFG
from ..utils import http_get_json, now_iso


def fetch() -> list[dict]:
    if not CFG["sources"]["coinbase"]["enabled"]:
        return []
    base = CFG["sources"]["coinbase"]["base_url"]
    ts = now_iso()
    rows = []
    for asset in CFG["universe"]:
        product = asset.get("coinbase")
        if not product:
            continue
        try:
            data = http_get_json(f"{base}/products/{product}/ticker")
        except Exception:
            continue
        price = float(data.get("price", 0) or 0)
        if price > 0:
            rows.append({"ts": ts, "source": "coinbase", "asset_id": asset["id"],
                         "metric": "price_usd", "value": price, "value_text": None})
    return rows
