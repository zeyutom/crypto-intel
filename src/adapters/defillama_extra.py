"""DefiLlama 扩展: 全市场 TVL、链 TVL、Top 协议收入。

补充 stablecoin 之外的 DeFi 维度。
"""
from ..config import CFG
from ..utils import http_get_json, now_iso, setup_logger

log = setup_logger("defillama_extra", "WARNING")


def fetch() -> list[dict]:
    if not CFG["sources"].get("defillama", {}).get("enabled", True):
        return []
    base = CFG["sources"]["defillama"]["main_base"]
    ts = now_iso()
    rows: list[dict] = []

    # 1. 全市场 TVL (历史)
    try:
        data = http_get_json(f"{base}/v2/historicalChainTvl")
        if isinstance(data, list) and data:
            # 取最新一条 + 过去 7 / 30 天
            for it in data[-30:]:
                d = it.get("date")
                tvl = it.get("tvl")
                if d and tvl is not None:
                    from datetime import datetime, timezone
                    ts_iso = datetime.fromtimestamp(int(d), tz=timezone.utc) \
                        .strftime("%Y-%m-%dT%H:%M:%SZ")
                    rows.append({
                        "ts": ts_iso, "source": "defillama",
                        "asset_id": "defi_total",
                        "metric": "tvl_usd", "value": float(tvl), "value_text": None,
                    })
    except Exception as e:
        log.warning("DefiLlama TVL fetch failed: %s", e)

    # 2. 主链 TVL
    try:
        chains = http_get_json(f"{base}/v2/chains")
        if isinstance(chains, list):
            for ch in chains:
                if not isinstance(ch, dict):
                    continue
                name = ch.get("name", "").lower()
                if name not in ("ethereum", "tron", "bsc", "solana", "arbitrum", "base", "polygon"):
                    continue
                tvl = ch.get("tvl")
                if tvl is None:
                    continue
                rows.append({
                    "ts": ts, "source": "defillama",
                    "asset_id": f"chain_{name}",
                    "metric": "chain_tvl_usd", "value": float(tvl), "value_text": None,
                })
    except Exception as e:
        log.warning("DefiLlama chains fetch failed: %s", e)

    # 3. Top 5 协议费用 (近 24h)
    try:
        fees = http_get_json(f"{base}/overview/fees", params={"excludeTotalDataChart": "true"})
        if isinstance(fees, dict):
            protocols = fees.get("protocols", [])[:5]
            for p in protocols:
                slug = p.get("slug") or p.get("name", "").lower().replace(" ", "_")
                fee_24h = p.get("total24h")
                if fee_24h is not None:
                    rows.append({
                        "ts": ts, "source": "defillama",
                        "asset_id": f"protocol_{slug}",
                        "metric": "fees_24h_usd", "value": float(fee_24h), "value_text": None,
                    })
    except Exception as e:
        log.warning("DefiLlama fees fetch failed: %s", e)

    return rows
