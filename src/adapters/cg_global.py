"""CoinGecko Global API: 全市场宏观指标。

提供:
- BTC 占比 (Dominance)
- 全市场总市值
- 24h 总成交量
- 已上线币种数
"""
from ..config import CFG
from ..utils import http_get_json, now_iso


def fetch() -> list[dict]:
    if not CFG["sources"].get("cg_global", {}).get("enabled", True):
        return []
    base = CFG["sources"]["coingecko"]["base_url"]
    try:
        data = http_get_json(f"{base}/global")
    except Exception:
        return []

    g = data.get("data", {})
    ts = now_iso()
    rows: list[dict] = []

    if "market_cap_percentage" in g:
        btc_dom = float(g["market_cap_percentage"].get("btc", 0))
        eth_dom = float(g["market_cap_percentage"].get("eth", 0))
        rows.extend([
            {"ts": ts, "source": "cg_global", "asset_id": "market",
             "metric": "btc_dominance_pct", "value": btc_dom, "value_text": None},
            {"ts": ts, "source": "cg_global", "asset_id": "market",
             "metric": "eth_dominance_pct", "value": eth_dom, "value_text": None},
        ])

    if "total_market_cap" in g:
        total = float(g["total_market_cap"].get("usd", 0))
        rows.append({"ts": ts, "source": "cg_global", "asset_id": "market",
                     "metric": "total_mcap_usd", "value": total, "value_text": None})

    if "total_volume" in g:
        vol = float(g["total_volume"].get("usd", 0))
        rows.append({"ts": ts, "source": "cg_global", "asset_id": "market",
                     "metric": "total_volume_24h_usd", "value": vol, "value_text": None})

    if "active_cryptocurrencies" in g:
        n = float(g.get("active_cryptocurrencies", 0))
        rows.append({"ts": ts, "source": "cg_global", "asset_id": "market",
                     "metric": "active_currencies", "value": n, "value_text": None})

    return rows
