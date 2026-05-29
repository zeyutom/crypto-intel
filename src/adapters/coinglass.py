"""Coinglass 公开 API: 衍生品综合数据 (OI / Funding / Liquidations).

⚠️ v0.9 状态更新 (2026-05-19):
  Coinglass 把所有 public endpoint 收紧为 API-key only (openInterest/v3/chart,
  futures/liquidation/info, futures/longShortChart 均返回 401/404 或空 data).

  本 adapter 现在会优雅返回空列表 [].

  推荐替代:
    OI                → src.adapters.defillama_full.open_interest_overview()
    清算/Funding/LS   → ccxt fetch_funding_rate (需 pip install ccxt)
  或申请 CoinGlass key 后在 .env 配 COINGLASS_API_KEY, 改用 open-api 域名.
"""
from datetime import datetime, timezone
from ..config import CFG
from ..utils import setup_logger, now_iso
from ..http_client import http

log = setup_logger("coinglass", "WARNING")


def _safe_get(url: str, params: dict = None, headers: dict = None):
    """优雅版 HTTP GET — 失败返回 None 不抛."""
    return http.get_json(url, params=params, headers=headers,
                         timeout=15, ttl="hot")

BASE = "https://fapi.coinglass.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Origin": "https://www.coinglass.com",
    "Referer": "https://www.coinglass.com/",
}

_SYMBOL_MAP = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "bnb": "BNB",
}


def fetch() -> list[dict]:
    """返回:
       - oi_total_usd: 全交易所未平仓合约总和 (BTC/ETH 等)
       - liquidations_24h_usd: 过去 24h 全网清算
       - long_short_ratio: 多空账户比 (主流交易所平均)
    """
    if not CFG["sources"].get("coinglass", {}).get("enabled", True):
        return []
    ts = now_iso()
    rows: list[dict] = []

    # 1. Open Interest 历史 (近期点 = 当前 OI)
    for asset_id, sym in _SYMBOL_MAP.items():
        try:
            data = _safe_get(
                f"{BASE}/api/openInterest/v3/chart",
                params={"symbol": sym, "timeType": "h1", "exchangeName": "All"},
                headers=HEADERS,
            )
            if data and isinstance(data, dict) and data.get("code") in ("0", 0, "success"):
                points = data.get("data", {}).get("priceList", [])
                oi_points = data.get("data", {}).get("dataMap", {})
                # 取最新 OI 总和
                if isinstance(oi_points, dict):
                    last_oi = 0
                    for ex_name, ex_series in oi_points.items():
                        if isinstance(ex_series, list) and ex_series:
                            last_oi += float(ex_series[-1] or 0)
                    if last_oi > 0:
                        rows.append({
                            "ts": ts, "source": "coinglass", "asset_id": asset_id,
                            "metric": "oi_total_usd",
                            "value": last_oi, "value_text": None,
                        })
        except Exception as e:
            log.warning("Coinglass OI fetch failed for %s: %s", sym, e)

    # 2. 24h 全网清算
    try:
        data = _safe_get(
            f"{BASE}/api/futures/liquidation/info",
            params={"timeType": "1", "symbol": "all"},
            headers=HEADERS,
        )
        if data.get("code") in ("0", 0, "success"):
            d = data.get("data", {})
            longs = float(d.get("longVolUsd", 0) or 0)
            shorts = float(d.get("shortVolUsd", 0) or 0)
            total = longs + shorts
            if total > 0:
                rows.extend([
                    {"ts": ts, "source": "coinglass", "asset_id": "market",
                     "metric": "liquidations_24h_usd", "value": total, "value_text": None},
                    {"ts": ts, "source": "coinglass", "asset_id": "market",
                     "metric": "liquidations_long_24h_usd", "value": longs, "value_text": None},
                    {"ts": ts, "source": "coinglass", "asset_id": "market",
                     "metric": "liquidations_short_24h_usd", "value": shorts, "value_text": None},
                ])
    except Exception as e:
        log.warning("Coinglass liquidations fetch failed: %s", e)

    # 3. 多空比 (BTC)
    try:
        data = _safe_get(
            f"{BASE}/api/futures/longShortChart",
            params={"symbol": "BTC", "timeType": "h1"},
            headers=HEADERS,
        )
        if data.get("code") in ("0", 0, "success"):
            d = data.get("data", {})
            ratios = d.get("longShortRatios", []) if isinstance(d, dict) else []
            if ratios:
                last = float(ratios[-1])
                rows.append({
                    "ts": ts, "source": "coinglass", "asset_id": "bitcoin",
                    "metric": "long_short_ratio", "value": last, "value_text": None,
                })
    except Exception as e:
        log.warning("Coinglass long/short fetch failed: %s", e)

    # ── v0.9: Coinglass 大部分端点已死 → 用 DefiLlama OI 兜底 ──
    if not rows:
        rows = _fallback_defillama_oi(ts)

    return rows


def _fallback_defillama_oi(ts: str) -> list[dict]:
    """Coinglass 失败时, 用 DefiLlama 的 open_interest_overview 兜底.

    DefiLlama 给的是 perp DEX 聚合 OI (Hyperliquid/dYdX 等), 比 Coinglass
    覆盖少但完全免费. 字段映射到 oi_total_usd, asset_id=market.
    """
    try:
        from . import defillama_full as dlf
        data = dlf.open_interest_overview()
        if not data or not isinstance(data, dict):
            return []
        total = data.get("total24h") or data.get("totalOpenInterest")
        if total is None:
            # 累加 protocols 的 openInterestAtEnd
            total = sum(
                float(p.get("openInterestAtEnd") or 0)
                for p in data.get("protocols", []) or []
            )
        if total and total > 0:
            log.info(
                "Coinglass 失败 → DefiLlama perp OI fallback: $%.1fB",
                total / 1e9,
            )
            return [{
                "ts": ts, "source": "coinglass",  # 保留 source 名以兼容因子层
                "asset_id": "market", "metric": "oi_total_usd",
                "value": float(total), "value_text": "via_defillama_fallback",
            }]
    except Exception as e:
        log.warning("DefiLlama OI fallback also failed: %s", e)
    return []
