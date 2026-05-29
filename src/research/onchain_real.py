"""真链上数据源 — 活跃地址、大额转账、DEX交易量 (v0.9: 与 cryo 分层).

链上数据分三层 (职责分离, 不互相替代):
  src.research.onchain_real    ← 这里, 公开 API 聚合 (BTC/ETH/DEX 整体级别)
  src.adapters.cryo_onchain    ← subprocess 调 cryo binary, 地址级粒度 (CEX 流向)
  src.adapters.cryo_warehouse  ← parquet 仓库 + DuckDB 跨表查询

何时用哪个:
  - 想要"BTC 24h 交易数"等聚合指标 → onchain_real
  - 想要"某 token 最近 1000 块的 CEX inflow/outflow" → cryo_onchain
  - 想要"过去一个月内 USDT 的大额 whale" → cryo_warehouse



数据源优先级:
  1. Blockchain.info (BTC 免费, 无需 key)
  2. Etherscan 公开统计 (ETH 免费)
  3. DeFiLlama DEX volume API (免费)
  4. Dune Analytics Echo API (免费 community queries)
  5. Flipside Crypto API (免费 tier, 需注册但不收费)

设计原则:
  - 全部免费 API, 无需 key 即可获取核心指标
  - 有 key 时自动增强 (Dune/Flipside 提供更丰富数据)
  - 失败时优雅降级, 不影响主流程
"""
from __future__ import annotations
import os
import time
from datetime import datetime, timezone
from ..utils import setup_logger

log = setup_logger("onchain_real", "INFO")

# 可选: cryo adapter (Paradigm 链上数据工具)
try:
    from ..adapters import cryo_onchain  # noqa
    _CRYO_AVAILABLE = cryo_onchain.is_available()
except Exception:
    cryo_onchain = None
    _CRYO_AVAILABLE = False


def _get(url: str, params: dict = None, headers: dict = None,
         retries: int = 2, backoff: float = 5.0, timeout: int = 20):
    """v0.9: forward 到统一 HttpClient (rate limit + cache + retry 统一管理)."""
    from ..http_client import http
    return http.get_json(url, params=params, headers=headers,
                         timeout=timeout, ttl="hot", retries=retries)


# ====================================================================
#  BTC 链上 (blockchain.info — 完全免费, 无需 key)
# ====================================================================

def fetch_btc_onchain() -> dict:
    """BTC 核心链上指标 (blockchain.info 免费 API)。

    返回: {active_addresses_24h, transactions_24h, hash_rate, avg_tx_value}
    """
    log.info("  BTC 链上数据 (blockchain.info)...")
    result = {}

    # 活跃地址数 (估算: 用 n_unique_addresses)
    data = _get("https://api.blockchain.info/stats")
    if data:
        result["transactions_24h"] = data.get("n_tx", 0)
        result["hash_rate_th"] = data.get("hash_rate", 0) / 1e6  # TH/s → EH/s approx
        result["difficulty"] = data.get("difficulty", 0)
        result["market_price_usd"] = data.get("market_price_usd", 0)
        result["miners_revenue_btc"] = data.get("miners_revenue_btc", 0)
        result["total_btc_sent"] = data.get("total_btc_sent", 0) / 1e8  # satoshi → BTC
        result["avg_block_size"] = data.get("avg_block_size", 0)
        log.info(f"    ✓ BTC: {result.get('transactions_24h', 0):,} txs/day")

    # 活跃地址 (最近值, 单独 endpoint)
    addr_data = _get("https://api.blockchain.info/charts/n-unique-addresses",
                     params={"timespan": "2days", "format": "json"})
    if addr_data and addr_data.get("values"):
        vals = addr_data["values"]
        result["active_addresses_24h"] = int(vals[-1].get("y", 0)) if vals else 0
        log.info(f"    ✓ BTC active addresses: {result.get('active_addresses_24h', 0):,}")

    return result


# ====================================================================
#  DeFiLlama DEX Volume (免费, 无需 key)
# ====================================================================

def fetch_dex_volumes() -> dict[str, float]:
    """各链 DEX 24h 交易量 (DeFiLlama)。

    返回: {chain_name: volume_24h_usd}
    """
    log.info("  DEX 交易量 (DeFiLlama)...")
    data = _get("https://api.llama.fi/overview/dexs",
                params={"excludeTotalDataChart": "true",
                        "excludeTotalDataChartBreakdown": "true"})
    if not data:
        log.warning("    ✗ DeFiLlama DEX API 不可用")
        return {}

    # 按链聚合
    chain_volumes = {}
    protocols = data.get("protocols", [])
    for p in protocols:
        chains = p.get("chains", [])
        vol = p.get("total24h") or 0
        if vol > 0 and chains:
            # 简单按链数均分 (粗略, 但免费 API 无法拿到逐链拆分)
            per_chain = vol / len(chains)
            for chain in chains:
                chain_volumes[chain] = chain_volumes.get(chain, 0) + per_chain

    # 也取总量
    total_vol = data.get("total24h") or sum(chain_volumes.values())
    chain_volumes["_total"] = total_vol
    log.info(f"    ✓ DEX total 24h: ${total_vol/1e9:.2f}B across {len(chain_volumes)-1} chains")
    return chain_volumes


# ====================================================================
#  Dune Analytics Echo API (免费 community queries)
# ====================================================================

def fetch_dune_query(query_id: int) -> list[dict] | None:
    """执行 Dune Analytics 公开查询 (需要 DUNE_API_KEY, 免费注册即可)。

    常用 query_id:
      - 3521777: ETH daily active addresses
      - 3521800: Top chains daily active addresses
      - 3521850: Whale transfers (>$1M)
    """
    api_key = os.environ.get("DUNE_API_KEY", "")
    if not api_key:
        return None

    log.info(f"  Dune query #{query_id}...")
    data = _get(
        f"https://api.dune.com/api/v1/query/{query_id}/results",
        headers={"X-Dune-API-Key": api_key},
        timeout=30,
    )
    if data and data.get("result"):
        rows = data["result"].get("rows", [])
        log.info(f"    ✓ Dune #{query_id}: {len(rows)} rows")
        return rows
    return None


# ====================================================================
#  Flipside Crypto (免费 tier)
# ====================================================================

def fetch_flipside_query(query: str) -> list[dict] | None:
    """执行 Flipside SQL 查询 (需要 FLIPSIDE_API_KEY, 免费注册)。"""
    api_key = os.environ.get("FLIPSIDE_API_KEY", "")
    if not api_key:
        return None

    import httpx
    log.info("  Flipside query...")
    try:
        # Create query
        r = httpx.post(
            "https://flipsidecrypto.xyz/api/v1/queries",
            json={"sql": query, "ttlMinutes": 60},
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            timeout=30,
        )
        if r.status_code != 200:
            return None
        token = r.json().get("token")
        if not token:
            return None

        # Poll for results (max 30s)
        for _ in range(6):
            time.sleep(5)
            r2 = httpx.get(
                f"https://flipsidecrypto.xyz/api/v1/queries/{token}",
                headers={"x-api-key": api_key},
                timeout=15,
            )
            if r2.status_code == 200:
                resp = r2.json()
                if resp.get("status") == "finished":
                    rows = resp.get("results", [])
                    log.info(f"    ✓ Flipside: {len(rows)} rows")
                    return rows
        return None
    except Exception as e:
        log.warning(f"    ✗ Flipside 失败: {e}")
        return None


# ====================================================================
#  综合链上指标聚合
# ====================================================================

def fetch_real_onchain_data() -> dict:
    """聚合所有链上数据源, 返回综合结果。

    返回: {
        "btc": {...},
        "dex_volumes": {chain: vol},
        "dune_active_addresses": [...] or None,
        "whale_transfers": [...] or None,
        "timestamp": "...",
    }
    """
    log.info("[链上数据] 聚合真实链上指标...")
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "btc": {},
        "dex_volumes": {},
        "dune_active_addresses": None,
        "whale_transfers": None,
    }

    # 1. BTC 链上 (总是有)
    result["btc"] = fetch_btc_onchain()

    # 2. DEX 交易量 (总是有)
    result["dex_volumes"] = fetch_dex_volumes()

    # 3. Dune — ETH active addresses (如果有 key)
    if os.environ.get("DUNE_API_KEY"):
        result["dune_active_addresses"] = fetch_dune_query(3521777)

    # 4. Dune — Whale transfers (如果有 key)
    if os.environ.get("DUNE_API_KEY"):
        result["whale_transfers"] = fetch_dune_query(3521850)

    return result


def calc_real_onchain_score(symbol: str, onchain_data: dict) -> float:
    """基于真实链上数据计算链上活跃度评分 (0-1)。

    BTC/ETH: 用链上 tx 数据
    L1 chains: 用 DEX volume 作为代理
    其他: 返回 0 (数据不足)
    """
    if not onchain_data:
        return 0.0

    dex_vols = onchain_data.get("dex_volumes", {})
    btc = onchain_data.get("btc", {})

    # BTC 特殊处理
    if symbol == "BTC":
        txs = btc.get("transactions_24h", 0)
        active = btc.get("active_addresses_24h", 0)
        # BTC 正常日交易量 ~300k-600k, 活跃地址 ~700k-1M
        tx_score = min(1.0, txs / 500_000) if txs > 0 else 0
        addr_score = min(1.0, active / 900_000) if active > 0 else 0
        return round((tx_score * 0.5 + addr_score * 0.5), 3)

    # ETH 用 DEX volume 作为活跃度代理
    if symbol == "ETH":
        eth_dex = dex_vols.get("Ethereum", 0)
        # ETH 正常 DEX 日交易量 ~$1-5B
        return round(min(1.0, eth_dex / 3e9), 3) if eth_dex > 0 else 0.0

    # 其他 L1 — 按 DEX volume 占比
    # 映射 symbol → chain name
    sym_to_chain = {
        "SOL": "Solana", "BNB": "BSC", "AVAX": "Avalanche",
        "ARB": "Arbitrum", "OP": "Optimism", "BASE": "Base",
        "MATIC": "Polygon", "POL": "Polygon",
        "SUI": "Sui", "APT": "Aptos", "FTM": "Fantom",
        "NEAR": "Near", "TON": "TON", "TRX": "Tron",
    }
    chain = sym_to_chain.get(symbol, "")
    if chain and chain in dex_vols:
        vol = dex_vols[chain]
        total = dex_vols.get("_total", 1)
        # 用 DEX volume 占比作为活跃度 (ETH 约 50-60%, SOL 约 15-20%)
        share = vol / total if total > 0 else 0
        return round(min(1.0, share * 5), 3)  # 20%+ → 1.0

    return 0.0


# ====================================================================
#  cryo 增强 — 地址级 CEX 流向 (可选, 软依赖)
# ====================================================================

def fetch_cex_flow_via_cryo(symbol: str) -> dict | None:
    """如果 cryo 可用, 拉指定币种最近 1000 块的 CEX inflow/outflow。

    数据来源: paradigmxyz/cryo (Rust 链上数据工具)
    未装 cryo 时返回 None, 不影响主流程。

    Returns:
        {
          "cex_inflow_count": int,
          "cex_outflow_count": int,
          "net_cex_flow": int,     # >0 表示资金从 CEX 流出 (囤币信号)
          "total_transfers": int,
        }  或 None
    """
    if not _CRYO_AVAILABLE or cryo_onchain is None:
        return None
    try:
        enriched = cryo_onchain.enrich_onchain(symbol)
        if enriched.get("_available"):
            return enriched.get("cex_flow")
        return None
    except Exception as e:
        log.warning(f"  cryo enrich failed for {symbol}: {e}")
        return None


def cryo_health() -> dict:
    """Health check for cryo adapter (供 CLI 诊断用)."""
    if cryo_onchain is None:
        return {"installed": False, "reason": "adapter not loaded"}
    return {
        "installed": _CRYO_AVAILABLE,
        "binary_path": cryo_onchain._which_cryo() if _CRYO_AVAILABLE else None,
        "eth_rpc": cryo_onchain._resolve_rpc("ethereum") if _CRYO_AVAILABLE else None,
    }
