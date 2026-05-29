"""Alternative.me Crypto Fear & Greed Index (免费).

v0.9: 顶层 http_get_json 失败时 graceful.
"""
from ..config import CFG
from ..utils import http_get_json, setup_logger
from datetime import datetime, timezone

log = setup_logger("feargreed_adapter", "WARNING")


def fetch() -> list[dict]:
    if not CFG["sources"]["feargreed"]["enabled"]:
        return []
    base = CFG["sources"]["feargreed"]["base_url"]
    try:
        data = http_get_json(base, params={"limit": 30, "format": "json"})
    except Exception as e:
        log.warning("Fear&Greed fetch 失败: %s", str(e)[:100])
        return []
    if not isinstance(data, dict):
        return []
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
