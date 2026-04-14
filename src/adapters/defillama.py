"""DefiLlama 免费 API: 稳定币流通量 (USDT+USDC) + 主流协议 TVL。"""
from ..config import CFG
from ..utils import http_get_json, now_iso


def fetch() -> list[dict]:
    if not CFG["sources"]["defillama"]["enabled"]:
        return []
    stable_base = CFG["sources"]["defillama"]["stablecoins_base"]
    ts = now_iso()
    rows: list[dict] = []

    # 稳定币流通历史 (全链聚合);用于 Stablecoin Mint 7d 因子
    try:
        data = http_get_json(f"{stable_base}/stablecoincharts/all")
        # data: list[{date, totalCirculatingUSD: {peggedUSD, peggedVar..}}]
        # 取最近 30 条快照
        for row in data[-30:]:
            date = row.get("date")
            total = row.get("totalCirculatingUSD", {})
            usd = total.get("peggedUSD") if isinstance(total, dict) else None
            if date and usd is not None:
                rows.append({
                    "ts": __iso_from_epoch(date),
                    "source": "defillama", "asset_id": "stablecoins",
                    "metric": "total_circulating_usd",
                    "value": float(usd), "value_text": None,
                })
    except Exception:
        pass

    # 分币种: USDT / USDC
    try:
        lst = http_get_json(f"{stable_base}/stablecoins?includePrices=true")
        peggeds = lst.get("peggedAssets", []) if isinstance(lst, dict) else []
        for p in peggeds:
            sym = p.get("symbol")
            if sym not in CFG["factors"]["stablecoin_mint"]["assets"]:
                continue
            circulating = p.get("circulating", {}).get("peggedUSD")
            if circulating is None:
                continue
            rows.append({"ts": ts, "source": "defillama",
                         "asset_id": f"stable_{sym.lower()}",
                         "metric": "circulating_usd",
                         "value": float(circulating), "value_text": None})
    except Exception:
        pass

    return rows


def __iso_from_epoch(epoch_str):
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(epoch_str), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return epoch_str


def fetch_stable_history() -> list[dict]:
    """单独暴露一个函数:拉取稳定币流通历史(日级),用于算 7d mint。"""
    return [r for r in fetch() if r.get("metric") == "total_circulating_usd"]
