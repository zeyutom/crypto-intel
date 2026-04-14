"""Alternative.me Crypto Fear & Greed Index (免费)。"""
from ..config import CFG
from ..utils import http_get_json
from datetime import datetime, timezone


def fetch() -> list[dict]:
    if not CFG["sources"]["feargreed"]["enabled"]:
        return []
    base = CFG["sources"]["feargreed"]["base_url"]
    data = http_get_json(base, params={"limit": 30, "format": "json"})
    items = data.get("data", [])
    rows = []
    for item in items:
        try:
            ts_epoch = int(item["timestamp"])
            ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            continue
        rows.append({
            "ts": ts, "source": "feargreed", "asset_id": "market",
            "metric": "fear_greed_index",
            "value": float(item["value"]),
            "value_text": item.get("value_classification"),
        })
    return rows
