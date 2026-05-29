"""DefiLlama 免费 API — 完整 31 端点覆盖.

参考: https://api-docs.defillama.com/

设计:
  - 零成本: 全部用 https://api.llama.fi (免费, 无 key)
  - 三类基底 url: TVL/Fees/DEX 走 api.llama.fi, coins/prices 走 coins.llama.fi,
    stablecoins 走 stablecoins.llama.fi, yields 走 yields.llama.fi
  - 单文件 self-contained: 内置 cache + 重试 + 优雅降级
  - 函数返回结构化数据 (dict/DataFrame), 不只是 raw JSON

覆盖的 31 个端点:

  TVL (6):
    /protocols                          → list_protocols
    /protocol/{slug}                    → protocol_history
    /v2/historicalChainTvl              → all_chains_tvl_history
    /v2/historicalChainTvl/{chain}      → chain_tvl_history
    /tvl/{slug}                         → protocol_current_tvl
    /v2/chains                          → list_chains

  Coins/Prices (7):
    /prices/current/{coins}             → current_prices
    /prices/historical/{ts}/{coins}     → historical_prices
    /batchHistorical                    → batch_historical_prices
    /chart/{coins}                      → price_chart
    /percentage/{coins}                 → price_percentage
    /prices/first/{coins}               → first_price
    /block/{chain}/{ts}                 → closest_block

  Stablecoins (6):
    /stablecoins                        → list_stablecoins
    /stablecoincharts/all               → all_stables_history
    /stablecoincharts/{chain}           → chain_stables_history
    /stablecoin/{id}                    → stable_detail
    /stablecoinchains                   → stables_by_chain
    /stablecoinprices                   → stable_prices

  Yields (2):
    /pools                              → list_yield_pools
    /chart/{pool_id}                    → pool_apy_history

  DEX (6):
    /overview/dexs                      → dex_overview
    /overview/dexs/{chain}              → dex_overview_chain
    /summary/dexs/{slug}                → dex_summary
    /overview/options                   → options_overview
    /overview/options/{chain}           → options_overview_chain
    /summary/options/{slug}             → options_summary

  Perp/OI (1):
    /overview/open-interest             → open_interest_overview

  Fees & Revenue (3):
    /overview/fees                      → fees_overview
    /overview/fees/{chain}              → fees_overview_chain
    /summary/fees/{slug}                → fees_summary

使用:
  from src.adapters.defillama_full import dlf
  dlf.list_protocols()
  dlf.protocol_history("uniswap")
  dlf.current_prices(["coingecko:ethereum", "ethereum:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"])
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from ..utils import setup_logger

log = setup_logger("defillama_full", "INFO")

# 不同子服务的 base URL
BASES = {
    "tvl": "https://api.llama.fi",            # protocols / chains / fees / dexs / options
    "coins": "https://coins.llama.fi",        # prices / chart / block
    "stables": "https://stablecoins.llama.fi", # stablecoins
    "yields": "https://yields.llama.fi",       # pools / chart
}

# 缓存目录
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache" / "defillama"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 缓存 TTL (秒)
TTL_HOT = 60         # current 类: 1 分钟
TTL_HIST = 3600      # historical 类: 1 小时
TTL_DAILY = 86400    # 极慢变化 (protocols 列表): 1 天

# Module-level 内存缓存
_mem_cache: dict[str, tuple[float, Any]] = {}


# ────────────────────────────────────────────────────────────────────
#  HTTP 工具
# ────────────────────────────────────────────────────────────────────

def _cache_key(url: str, params: dict = None) -> str:
    p = "_".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
    safe = (url + "?" + p).replace("/", "_").replace(":", "").replace("?", "_")
    return safe[:200]


def _disk_cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def _get_cached(key: str, ttl: int) -> Optional[Any]:
    # 1) 内存
    if key in _mem_cache:
        ts, val = _mem_cache[key]
        if time.time() - ts < ttl:
            return val
    # 2) 磁盘
    p = _disk_cache_path(key)
    if p.exists():
        age = time.time() - p.stat().st_mtime
        if age < ttl:
            try:
                data = json.loads(p.read_text())
                _mem_cache[key] = (time.time(), data)
                return data
            except Exception:
                pass
    return None


def _set_cached(key: str, value: Any):
    _mem_cache[key] = (time.time(), value)
    try:
        _disk_cache_path(key).write_text(json.dumps(value, default=str))
    except Exception:
        pass


def _http_get(
    base: str,
    path: str,
    params: dict = None,
    ttl: int = TTL_HOT,
    timeout: int = 20,
) -> Optional[Any]:
    """v0.9: forward 到统一 HttpClient.

    保留模块级 cache 主要为兼容性 (clear_cache/cache_files API),
    但实际 cache 走 src.http_client.http 的全局双层缓存.
    """
    from ..http_client import http
    url = f"{base}{path}"
    return http.get_json(url, params=params, timeout=timeout, ttl=int(ttl))


# ────────────────────────────────────────────────────────────────────
#  TVL 端点 (6)
# ────────────────────────────────────────────────────────────────────

def list_protocols() -> Optional[list[dict]]:
    """所有协议 + 最新 TVL. 一天更新一次, 缓存 1h."""
    return _http_get(BASES["tvl"], "/protocols", ttl=TTL_HIST)


def protocol_history(slug: str) -> Optional[dict]:
    """单协议历史 TVL + 按 token/chain 分解."""
    return _http_get(BASES["tvl"], f"/protocol/{slug}", ttl=TTL_HIST)


def protocol_current_tvl(slug: str) -> Optional[float]:
    """单协议当前 TVL (USD). 极轻量, /tvl/{slug} 直接返回数字."""
    data = _http_get(BASES["tvl"], f"/tvl/{slug}", ttl=TTL_HOT)
    try:
        return float(data) if data is not None else None
    except (ValueError, TypeError):
        return None


def all_chains_tvl_history() -> Optional[list[dict]]:
    """所有链聚合的 TVL 历史 (剔除 LSD 双计)."""
    return _http_get(BASES["tvl"], "/v2/historicalChainTvl", ttl=TTL_HIST)


def chain_tvl_history(chain: str) -> Optional[list[dict]]:
    """单链 TVL 历史. chain 如 'Ethereum', 'Solana', 'Arbitrum'."""
    return _http_get(BASES["tvl"], f"/v2/historicalChainTvl/{chain}", ttl=TTL_HIST)


def list_chains() -> Optional[list[dict]]:
    """所有链当前 TVL."""
    return _http_get(BASES["tvl"], "/v2/chains", ttl=TTL_HIST)


# ────────────────────────────────────────────────────────────────────
#  Coins/Prices 端点 (7)
# ────────────────────────────────────────────────────────────────────

def current_prices(coins: list[str]) -> Optional[dict]:
    """多 token 当前价格.

    coins 格式: 'coingecko:ethereum' / 'ethereum:0xc02aaa...' / 'solana:So111...'
    返回: {coins: {coin_id: {price, symbol, timestamp, ...}}}
    """
    if not coins:
        return {"coins": {}}
    coin_str = ",".join(coins)
    return _http_get(BASES["coins"], f"/prices/current/{coin_str}", ttl=TTL_HOT)


def historical_prices(timestamp: int, coins: list[str]) -> Optional[dict]:
    """指定时间戳的历史价格 (Unix 秒)."""
    if not coins:
        return {"coins": {}}
    coin_str = ",".join(coins)
    return _http_get(BASES["coins"], f"/prices/historical/{timestamp}/{coin_str}",
                     ttl=TTL_DAILY)


def batch_historical_prices(coins_ts_map: dict[str, list[int]]) -> Optional[dict]:
    """多 token 多时间戳一次拿完 (POST 风格但官方实现是 GET with body).

    coins_ts_map: {"coingecko:ethereum": [ts1, ts2, ts3], ...}
    """
    # 官方端点用 GET + JSON body, 这里用 json string 形式
    body = json.dumps(coins_ts_map)
    return _http_get(BASES["coins"], "/batchHistorical",
                     params={"coins": body}, ttl=TTL_DAILY)


def price_chart(coins: list[str], start: int = None, span: int = 100,
                period: str = "1d") -> Optional[dict]:
    """token 价格历史序列.

    period: '1h' | '4h' | '1d' | '1w'
    span: 多少个数据点
    """
    if not coins:
        return None
    coin_str = ",".join(coins)
    params = {"span": span, "period": period}
    if start:
        params["start"] = start
    return _http_get(BASES["coins"], f"/chart/{coin_str}", params=params, ttl=TTL_HIST)


def price_percentage(coins: list[str], lookforward: bool = False,
                     period: str = "24h") -> Optional[dict]:
    """token 在 period 内的价格变化百分比.

    period: '1h' | '24h' | '7d' | '30d' 等
    """
    if not coins:
        return None
    coin_str = ",".join(coins)
    params = {"period": period}
    if lookforward:
        params["lookForward"] = "true"
    return _http_get(BASES["coins"], f"/percentage/{coin_str}", params=params,
                     ttl=TTL_HOT)


def first_price(coins: list[str]) -> Optional[dict]:
    """token 最早有记录的时间戳和价格."""
    if not coins:
        return None
    coin_str = ",".join(coins)
    return _http_get(BASES["coins"], f"/prices/first/{coin_str}", ttl=TTL_DAILY)


def closest_block(chain: str, timestamp: int) -> Optional[dict]:
    """给定时间戳, 返回该链最近的区块号."""
    return _http_get(BASES["coins"], f"/block/{chain}/{timestamp}", ttl=TTL_DAILY)


# ────────────────────────────────────────────────────────────────────
#  Stablecoins 端点 (6)
# ────────────────────────────────────────────────────────────────────

def list_stablecoins(include_prices: bool = True) -> Optional[dict]:
    """所有稳定币 + 各链流通量."""
    params = {"includePrices": "true" if include_prices else "false"}
    return _http_get(BASES["stables"], "/stablecoins", params=params, ttl=TTL_HOT)


def all_stables_history() -> Optional[list[dict]]:
    """所有稳定币聚合市值历史."""
    return _http_get(BASES["stables"], "/stablecoincharts/all", ttl=TTL_HIST)


def chain_stables_history(chain: str) -> Optional[list[dict]]:
    """单链上稳定币市值历史."""
    return _http_get(BASES["stables"], f"/stablecoincharts/{chain}", ttl=TTL_HIST)


def stable_detail(stable_id: Union[int, str]) -> Optional[dict]:
    """单个稳定币详情 (历史 + 各链分布).

    stable_id 是 DefiLlama 内部 ID, 比如 USDT=1, USDC=2
    """
    return _http_get(BASES["stables"], f"/stablecoin/{stable_id}", ttl=TTL_HIST)


def stables_by_chain() -> Optional[list[dict]]:
    """各链上所有稳定币的当前市值."""
    return _http_get(BASES["stables"], "/stablecoinchains", ttl=TTL_HOT)


def stable_prices() -> Optional[list[dict]]:
    """所有稳定币的历史脱锚价格 (用于监测脱锚事件)."""
    return _http_get(BASES["stables"], "/stablecoinprices", ttl=TTL_HIST)


# ────────────────────────────────────────────────────────────────────
#  Yields 端点 (2)
# ────────────────────────────────────────────────────────────────────

def list_yield_pools() -> Optional[dict]:
    """所有 DeFi 池子的当前 APY + TVL + 预测 IL 等."""
    return _http_get(BASES["yields"], "/pools", ttl=TTL_HOT)


def pool_apy_history(pool_id: str) -> Optional[dict]:
    """单个池子的 APY 历史."""
    return _http_get(BASES["yields"], f"/chart/{pool_id}", ttl=TTL_HIST)


# ────────────────────────────────────────────────────────────────────
#  DEX & Options 端点 (6)
# ────────────────────────────────────────────────────────────────────

def dex_overview(exclude_chart: bool = False) -> Optional[dict]:
    """所有 DEX 的成交量概览."""
    params = {"excludeTotalDataChart": "true"} if exclude_chart else {}
    return _http_get(BASES["tvl"], "/overview/dexs", params=params, ttl=TTL_HOT)


def dex_overview_chain(chain: str, exclude_chart: bool = False) -> Optional[dict]:
    """指定链上 DEX 成交量."""
    params = {"excludeTotalDataChart": "true"} if exclude_chart else {}
    return _http_get(BASES["tvl"], f"/overview/dexs/{chain}",
                     params=params, ttl=TTL_HOT)


def dex_summary(slug: str) -> Optional[dict]:
    """单 DEX 历史成交量."""
    return _http_get(BASES["tvl"], f"/summary/dexs/{slug}", ttl=TTL_HIST)


def options_overview() -> Optional[dict]:
    """所有期权 DEX 概览."""
    return _http_get(BASES["tvl"], "/overview/options", ttl=TTL_HOT)


def options_overview_chain(chain: str) -> Optional[dict]:
    """单链期权成交概览."""
    return _http_get(BASES["tvl"], f"/overview/options/{chain}", ttl=TTL_HOT)


def options_summary(slug: str) -> Optional[dict]:
    """单期权协议历史."""
    return _http_get(BASES["tvl"], f"/summary/options/{slug}", ttl=TTL_HIST)


# ────────────────────────────────────────────────────────────────────
#  Perp Open Interest (1)
# ────────────────────────────────────────────────────────────────────

def open_interest_overview() -> Optional[dict]:
    """所有 perp DEX 的持仓量概览."""
    return _http_get(BASES["tvl"], "/overview/open-interest", ttl=TTL_HOT)


# ────────────────────────────────────────────────────────────────────
#  Fees & Revenue (3)
# ────────────────────────────────────────────────────────────────────

def fees_overview(exclude_chart: bool = True) -> Optional[dict]:
    """所有协议的费用 + 收入概览."""
    params = {"excludeTotalDataChart": "true"} if exclude_chart else {}
    return _http_get(BASES["tvl"], "/overview/fees", params=params, ttl=TTL_HOT)


def fees_overview_chain(chain: str, exclude_chart: bool = True) -> Optional[dict]:
    """单链费用概览."""
    params = {"excludeTotalDataChart": "true"} if exclude_chart else {}
    return _http_get(BASES["tvl"], f"/overview/fees/{chain}",
                     params=params, ttl=TTL_HOT)


def fees_summary(slug: str) -> Optional[dict]:
    """单协议费用历史."""
    return _http_get(BASES["tvl"], f"/summary/fees/{slug}", ttl=TTL_HIST)


# ────────────────────────────────────────────────────────────────────
#  高级聚合 API (给因子层用)
# ────────────────────────────────────────────────────────────────────

def get_top_protocols_by_tvl(n: int = 50) -> list[dict]:
    """Top N 协议 (按 TVL)."""
    data = list_protocols()
    if not data:
        return []
    sorted_protocols = sorted(
        data,
        key=lambda p: float(p.get("tvl", 0) or 0),
        reverse=True,
    )
    return sorted_protocols[:n]


def get_protocol_tvl_change(slug: str, days: int = 7) -> Optional[dict]:
    """单协议 N 天 TVL 变化 + 年化."""
    hist = protocol_history(slug)
    if not hist:
        return None
    tvl_list = hist.get("tvl", [])
    if len(tvl_list) < 2:
        return None
    now_tvl = float(tvl_list[-1].get("totalLiquidityUSD", 0))
    target_ts = int(time.time()) - days * 86400
    past_tvl = None
    for entry in reversed(tvl_list):
        ts = int(entry.get("date", 0))
        if ts <= target_ts:
            past_tvl = float(entry.get("totalLiquidityUSD", 0))
            break
    if past_tvl is None or past_tvl == 0:
        return None
    change_pct = (now_tvl - past_tvl) / past_tvl
    return {
        "slug": slug,
        "now_tvl": now_tvl,
        "past_tvl": past_tvl,
        "days": days,
        "change_pct": round(change_pct, 4),
        "change_pct_annualized": round(change_pct * 365.0 / days, 4),
    }


def get_chain_dex_volume_share() -> dict[str, float]:
    """各链 DEX 成交量市占率 (近 24h)."""
    data = dex_overview(exclude_chart=True)
    if not data:
        return {}
    total = float(data.get("total24h", 0))
    if total <= 0:
        return {}
    protocols = data.get("protocols", [])
    chain_vol: dict[str, float] = {}
    for p in protocols:
        chains = p.get("chains", []) or []
        vol_24h = float(p.get("total24h", 0) or 0)
        if not chains or vol_24h == 0:
            continue
        per_chain = vol_24h / len(chains)
        for c in chains:
            chain_vol[c] = chain_vol.get(c, 0) + per_chain
    return {c: round(v / total, 4) for c, v in chain_vol.items()}


def get_stable_peg_health() -> dict[str, dict]:
    """所有主流稳定币当前脱锚情况.

    返回: {symbol: {price, deviation_pct, status}}
    """
    data = list_stablecoins(include_prices=True)
    if not data:
        return {}
    out = {}
    peggeds = data.get("peggedAssets", []) or []
    for p in peggeds:
        sym = p.get("symbol")
        price = p.get("price")
        circ = p.get("circulating", {}).get("peggedUSD", 0)
        if not sym or price is None or circ < 1e7:  # 跳过小盘
            continue
        try:
            price_f = float(price)
        except (ValueError, TypeError):
            continue
        dev = price_f - 1.0
        status = "ok" if abs(dev) < 0.005 else (
            "depegged" if abs(dev) > 0.02 else "deviating"
        )
        out[sym] = {
            "price": round(price_f, 5),
            "deviation_pct": round(dev * 100, 3),
            "circulating_usd": float(circ),
            "status": status,
        }
    return dict(sorted(out.items(),
                       key=lambda kv: abs(kv[1]["deviation_pct"]),
                       reverse=True))


def get_top_yield_opportunities(min_tvl: float = 1e7,
                                max_il_risk: str = "no",
                                max_apy: float = 1000.0,
                                stable_only: bool = False) -> list[dict]:
    """高 APY 的低风险池子.

    Args:
        min_tvl: 最小 TVL 阈值 ($10M 默认)
        max_il_risk: 'no' (only) or 'yes' (include IL pools)
        max_apy: 上限 (>1000% 通常是临时奖励刷量, 不可持续, 默认过滤)
        stable_only: 只看稳定币池子
    """
    data = list_yield_pools()
    if not data:
        return []
    pools = data.get("data", []) if isinstance(data, dict) else data
    out = []
    for p in pools or []:
        if not isinstance(p, dict):
            continue
        tvl = p.get("tvlUsd", 0)
        if tvl < min_tvl:
            continue
        if max_il_risk == "no" and p.get("ilRisk", "no") == "yes":
            continue
        if stable_only and not p.get("stablecoin"):
            continue
        apy = p.get("apy", 0)
        if apy is None or apy <= 0 or apy > max_apy:
            continue
        out.append({
            "pool": p.get("pool"),
            "project": p.get("project"),
            "chain": p.get("chain"),
            "symbol": p.get("symbol"),
            "apy": round(apy, 2),
            "apy_base": p.get("apyBase"),
            "apy_reward": p.get("apyReward"),
            "tvl_usd": round(tvl, 0),
            "stable": p.get("stablecoin"),
            "il_risk": p.get("ilRisk", "no"),
        })
    return sorted(out, key=lambda x: x["apy"], reverse=True)


def get_perp_oi_by_protocol() -> list[dict]:
    """各 perp DEX 当前持仓量 (按降序)."""
    data = open_interest_overview()
    if not data:
        return []
    protocols = data.get("protocols", []) if isinstance(data, dict) else []
    out = []
    for p in protocols:
        oi = p.get("openInterestAtEnd") or p.get("total24h") or 0
        if not oi:
            continue
        out.append({
            "name": p.get("name"),
            "open_interest_usd": float(oi),
            "change_24h": p.get("change_1d"),
            "change_7d": p.get("change_7d"),
        })
    return sorted(out, key=lambda x: x["open_interest_usd"], reverse=True)


def health() -> dict:
    """诊断: 各 base url 是否可达 + 缓存大小."""
    import httpx
    out = {"bases": {}, "cache_files": 0, "cache_mb": 0.0}
    for name, base in BASES.items():
        try:
            r = httpx.get(base + "/protocols" if name == "tvl"
                         else base + "/stablecoins" if name == "stables"
                         else base + "/pools" if name == "yields"
                         else base + "/prices/current/coingecko:bitcoin",
                          timeout=5)
            out["bases"][name] = f"ok ({r.status_code})"
        except Exception as e:
            out["bases"][name] = f"err: {str(e)[:60]}"

    if CACHE_DIR.exists():
        files = list(CACHE_DIR.glob("*.json"))
        out["cache_files"] = len(files)
        out["cache_mb"] = round(sum(f.stat().st_size for f in files) / 1024 / 1024, 2)
    return out


def clear_cache():
    """清空缓存 (内存 + 磁盘)."""
    _mem_cache.clear()
    if CACHE_DIR.exists():
        for f in CACHE_DIR.glob("*.json"):
            f.unlink()
    log.info("cache cleared")


def is_available() -> bool:
    """该模块只依赖 httpx, 总是可用."""
    try:
        import httpx  # noqa
        return True
    except ImportError:
        return False


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("[defillama_full] health check ...")
    print(json.dumps(health(), indent=2))

    print("\n[defillama_full] sampling 5 endpoints...")
    print(f"  list_chains       -> {len(list_chains() or [])} chains")
    print(f"  list_protocols    -> {len(list_protocols() or [])} protocols")
    print(f"  list_stablecoins  -> peggedAssets={len((list_stablecoins() or {}).get('peggedAssets', []))}")
    print(f"  dex_overview      -> {len((dex_overview() or {}).get('protocols', []))} DEXs")
    print(f"  stable_peg_health -> {list(get_stable_peg_health().items())[:3]}")
