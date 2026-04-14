"""CoinGecko Trending API: 7 个热门搜索币种 + 5 个热门 NFT。

代表"散户/社区在搜什么", 是叙事/Mindshare 的代理指标。
"""
import json
from ..config import CFG
from ..utils import http_get_json, now_iso


def fetch() -> list[dict]:
    if not CFG["sources"].get("cg_trending", {}).get("enabled", True):
        return []
    base = CFG["sources"]["coingecko"]["base_url"]
    try:
        data = http_get_json(f"{base}/search/trending")
    except Exception:
        return []

    ts = now_iso()
    rows: list[dict] = []

    coins = data.get("coins", []) or []
    # 把整个 trending 列表当一个 metric (text 字段存 JSON)
    if coins:
        names = []
        for entry in coins[:7]:
            it = entry.get("item", {})
            names.append({
                "id": it.get("id"),
                "symbol": it.get("symbol"),
                "name": it.get("name"),
                "rank": it.get("market_cap_rank"),
                "score": it.get("score"),
                "price_usd": (it.get("data", {}) or {}).get("price"),
                "change_24h": (it.get("data", {}) or {}).get("price_change_percentage_24h", {}).get("usd"),
            })
        rows.append({
            "ts": ts, "source": "cg_trending", "asset_id": "market",
            "metric": "trending_coins",
            "value": float(len(names)),
            "value_text": json.dumps(names, ensure_ascii=False),
        })

    return rows
