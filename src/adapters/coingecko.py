"""CoinGecko 免费 API: 全市场价格快照 (作为价格真实性交叉验证的基准之一)。"""
from ..config import CFG
from ..utils import http_get_json, now_iso


def fetch() -> list[dict]:
    if not CFG["sources"]["coingecko"]["enabled"]:
        return []
    base = CFG["sources"]["coingecko"]["base_url"]
    ids = ",".join(a["coingecko"] for a in CFG["universe"] if a.get("coingecko"))
    data = http_get_json(
        f"{base}/simple/price",
        params={"ids": ids, "vs_currencies": "usd",
                "include_market_cap": "true", "include_24hr_vol": "true",
                "include_24hr_change": "true"},
    )
    ts = now_iso()
    rows = []
    for asset in CFG["universe"]:
        cg = asset.get("coingecko")
        d = data.get(cg or "", {})
        if not d:
            continue
        rows.extend([
            {"ts": ts, "source": "coingecko", "asset_id": asset["id"],
             "metric": "price_usd", "value": d.get("usd"), "value_text": None},
            {"ts": ts, "source": "coingecko", "asset_id": asset["id"],
             "metric": "market_cap_usd", "value": d.get("usd_market_cap"), "value_text": None},
            {"ts": ts, "source": "coingecko", "asset_id": asset["id"],
             "metric": "volume_24h_usd", "value": d.get("usd_24h_vol"), "value_text": None},
            {"ts": ts, "source": "coingecko", "asset_id": asset["id"],
             "metric": "change_24h_pct", "value": d.get("usd_24h_change"), "value_text": None},
        ])
    return rows
