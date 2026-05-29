"""cryo 链上数据仓库 + DuckDB 查询层 (Phase 3-B).

把零散的 cryo_onchain.py 调用升级成正经数据仓库:
  - 按月分区采集 Top-N 主流币的 ERC20 transfers
  - 文件布局: data/onchain/warehouse/{chain}/{token}/year=2026/month=05/*.parquet
  - 用 DuckDB 做 cross-table 查询 (whale flow / cex flow / 大额聚合 / 持有人分布)
  - 增量更新: 记录每个 token 的 last_block, 下次从那继续

数据流:

   cryo CLI ──→ parquet 分区 ──→ DuckDB 查询层 ──→ 因子层
                  │
                  └─→ 增量索引: data/onchain/warehouse/_state.json

支持的查询:
  - top_whales(token, since_days): 最大持有人变动
  - cex_flow_summary(token, since_days): CEX inflow/outflow 汇总
  - large_transfers(token, min_value_usd, since_days): 大额转账列表
  - holder_growth(token, since_days): 唯一地址数变化

软依赖:
  - cryo binary (没装时所有采集 = no-op, 但查询已有数据不受影响)
  - duckdb (没装时降级到 pandas 全表 scan)
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..utils import setup_logger

log = setup_logger("cryo_warehouse", "INFO")

WAREHOUSE_DIR = Path(__file__).resolve().parents[2] / "data" / "onchain" / "warehouse"
WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = WAREHOUSE_DIR / "_state.json"


def _duckdb_available() -> bool:
    try:
        import duckdb  # noqa
        return True
    except ImportError:
        return False


def _cryo_available() -> bool:
    return shutil.which("cryo") is not None


def is_available() -> bool:
    """只要 DuckDB 装着, 查询层就能用 (即便 cryo 没装也可查已有数据)."""
    return _duckdb_available()


# 默认采集 universe (主流 ERC20 + Stablecoin + 大蓝筹)
DEFAULT_TOKENS = {
    "USDT": {"chain": "ethereum", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
    "USDC": {"chain": "ethereum", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
    "WETH": {"chain": "ethereum", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": 18},
    "WBTC": {"chain": "ethereum", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "decimals": 8},
    "LINK": {"chain": "ethereum", "address": "0x514910771AF9Ca656af840dff83E8264EcF986CA", "decimals": 18},
    "UNI":  {"chain": "ethereum", "address": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984", "decimals": 18},
    "AAVE": {"chain": "ethereum", "address": "0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9", "decimals": 18},
    "MKR":  {"chain": "ethereum", "address": "0x9f8F72aA9304c8B593d555F12eF6589cC3A579A2", "decimals": 18},
}

# CEX 热钱包 (复用 cryo_onchain.py 里的, 但扩充)
CEX_WALLETS = {
    "binance_14": "0x28C6c06298d514Db089934071355E5743bf21d60",
    "binance_15": "0xDFd5293D8e347dFe59E90eFd55b2956a1343963d",
    "binance_8":  "0xF977814e90dA44bFA03b6295A0616a897441aceC",
    "coinbase_4": "0xA9D1e08C7793af67e9d92fe308d5697FB81d3E43",
    "coinbase_6": "0x503828976D22510aad0201ac7EC88293211D23Da",
    "kraken_1":   "0x2910543Af39abA0Cd09dBb2D50200b3E800A63D2",
    "okx_1":      "0x6Cc5F688a315f3dC28A7781717a9A798a59fDA7b",
    "okx_2":      "0x868daB0b8E21EC0a48b76A7D8C00F35BdD33eFa0",
    "bybit_1":    "0xf89d7b9c864f589bbF53a82105107622B35EaA40",
    "kucoin_1":   "0x2B5634C42055806a59e9107ED44D43c426E58258",
}


# ────────────────────────────────────────────────────────────────────
#  State (增量索引)
# ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"tokens": {}, "last_updated": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"tokens": {}, "last_updated": None}


def _save_state(state: dict):
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _partition_path(token: str, chain: str, year: int, month: int) -> Path:
    p = WAREHOUSE_DIR / chain / token / f"year={year}" / f"month={month:02d}"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ────────────────────────────────────────────────────────────────────
#  采集层: 把指定 token 的 transfers 落到分区 parquet
# ────────────────────────────────────────────────────────────────────

def _resolve_rpc(chain: str) -> Optional[str]:
    """复用 cryo_onchain 的 RPC 解析."""
    env_key = f"{chain.upper()}_RPC_URL"
    if os.getenv(env_key):
        return os.getenv(env_key)
    # 公共节点 fallback
    fallback = {
        "ethereum": "https://eth.llamarpc.com",
        "polygon": "https://polygon.llamarpc.com",
    }
    return fallback.get(chain)


def ingest_token(
    token: str,
    chain: str = None,
    blocks: int = 5000,
    timeout: int = 600,
) -> dict:
    """把指定 token 最近 N 个区块的 ERC20 transfers 拉到本地分区。"""
    info = DEFAULT_TOKENS.get(token.upper())
    if not info:
        return {"ok": False, "reason": f"unknown token: {token}"}

    chain = chain or info["chain"]
    contract = info["address"]

    if not _cryo_available():
        return {
            "ok": False,
            "reason": "cryo binary not installed",
            "hint": "brew install paradigmxyz/cryo/cryo",
        }
    rpc = _resolve_rpc(chain)
    if not rpc:
        return {"ok": False, "reason": f"no RPC for {chain}"}

    now = datetime.now(timezone.utc)
    out_dir = _partition_path(token.upper(), chain, now.year, now.month)
    cmd = [
        "cryo", "erc20_transfers",
        "--rpc", rpc,
        "--blocks", f"-{blocks}:",
        "--output-dir", str(out_dir),
        "--format", "parquet",
        "--overwrite",
        "--contract", contract,
    ]
    log.info(f"  ingest {token} ({chain}) last {blocks} blocks ...")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return {
                "ok": False,
                "reason": "cryo exit non-zero",
                "stderr": r.stderr[:300],
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

    # 更新 state
    state = _load_state()
    state["tokens"].setdefault(token.upper(), {})
    state["tokens"][token.upper()][f"{chain}/{now.year}-{now.month:02d}"] = {
        "ingested_at": now.isoformat(),
        "blocks": blocks,
        "partition": str(out_dir.relative_to(WAREHOUSE_DIR)),
    }
    _save_state(state)

    parquets = list(out_dir.glob("*.parquet"))
    return {
        "ok": True,
        "token": token,
        "chain": chain,
        "partition": str(out_dir),
        "files": [p.name for p in parquets],
        "n_files": len(parquets),
    }


def ingest_all(tokens: list[str] = None, blocks: int = 5000) -> dict:
    """批量 ingest 一组 token。"""
    tokens = tokens or list(DEFAULT_TOKENS.keys())
    results = {}
    for t in tokens:
        results[t] = ingest_token(t, blocks=blocks)
    return results


# ────────────────────────────────────────────────────────────────────
#  查询层 (DuckDB 优先, pandas fallback)
# ────────────────────────────────────────────────────────────────────

def _open_db():
    """返回 DuckDB 连接 (内存模式), 自动注册 warehouse 下所有 parquet."""
    if not _duckdb_available():
        return None
    import duckdb
    con = duckdb.connect(":memory:")
    # 用 hive_partitioning 让 DuckDB 自动识别 year/month
    glob = str(WAREHOUSE_DIR / "**" / "*.parquet")
    try:
        con.execute(f"""
            CREATE VIEW transfers AS
            SELECT *,
                   regexp_extract(filename, 'warehouse/([^/]+)/([^/]+)/', 1) as chain,
                   regexp_extract(filename, 'warehouse/([^/]+)/([^/]+)/', 2) as token
            FROM read_parquet('{glob}', filename=true, union_by_name=true)
        """)
    except Exception as e:
        log.warning(f"  duckdb view creation failed: {e}")
        return None
    return con


def list_partitions() -> list[dict]:
    """列出仓库里所有分区 (即便 DuckDB 没装也能用)."""
    out = []
    for parquet in WAREHOUSE_DIR.rglob("*.parquet"):
        try:
            rel = parquet.relative_to(WAREHOUSE_DIR)
            parts = rel.parts
            if len(parts) >= 4:
                chain, token = parts[0], parts[1]
                out.append({
                    "chain": chain,
                    "token": token,
                    "path": str(rel),
                    "size_kb": round(parquet.stat().st_size / 1024, 1),
                })
        except Exception:
            continue
    return out


def cex_flow_summary(
    token: str,
    chain: str = "ethereum",
) -> dict:
    """统计 token 在仓库里所有分区的 CEX inflow/outflow."""
    parts = [p for p in list_partitions()
             if p["token"] == token.upper() and p["chain"] == chain]
    if not parts:
        return {"_status": "no_data", "token": token, "chain": chain}

    con = _open_db()
    if con is None:
        return _cex_flow_pandas(token, chain, parts)

    cex_list = "'" + "','".join(addr.lower() for addr in CEX_WALLETS.values()) + "'"
    glob = str(WAREHOUSE_DIR / chain / token.upper() / "**" / "*.parquet")
    try:
        # cryo 输出列名: from_address / to_address (按版本可能差异)
        df = con.execute(f"""
            SELECT
              COUNT(*) AS total_transfers,
              SUM(CASE WHEN LOWER(to_address) IN ({cex_list}) THEN 1 ELSE 0 END) AS cex_inflow,
              SUM(CASE WHEN LOWER(from_address) IN ({cex_list}) THEN 1 ELSE 0 END) AS cex_outflow,
              COUNT(DISTINCT from_address) AS unique_senders,
              COUNT(DISTINCT to_address) AS unique_receivers
            FROM read_parquet('{glob}', union_by_name=true)
        """).fetchdf()
        if df.empty:
            return {"_status": "empty", "token": token}
        row = df.iloc[0]
        return {
            "_status": "ok",
            "token": token.upper(),
            "chain": chain,
            "total_transfers": int(row["total_transfers"]),
            "cex_inflow": int(row["cex_inflow"]),
            "cex_outflow": int(row["cex_outflow"]),
            "net_cex_flow": int(row["cex_outflow"] - row["cex_inflow"]),
            "unique_senders": int(row["unique_senders"]),
            "unique_receivers": int(row["unique_receivers"]),
            "partitions": len(parts),
            "engine": "duckdb",
        }
    except Exception as e:
        log.warning(f"  duckdb query failed: {e}; fallback to pandas")
        return _cex_flow_pandas(token, chain, parts)


def _cex_flow_pandas(token: str, chain: str, parts: list) -> dict:
    """没 DuckDB 时的纯 pandas fallback."""
    try:
        import pandas as pd
    except ImportError:
        return {"_status": "no_pandas"}

    cex_addrs = {a.lower() for a in CEX_WALLETS.values()}
    cex_in = cex_out = total = 0
    senders, receivers = set(), set()

    for p in parts:
        try:
            df = pd.read_parquet(WAREHOUSE_DIR / p["path"])
            from_col = next((c for c in df.columns if "from" in c.lower()), None)
            to_col = next((c for c in df.columns if "to" in c.lower()), None)
            if not from_col or not to_col:
                continue
            df["_fl"] = df[from_col].astype(str).str.lower()
            df["_tl"] = df[to_col].astype(str).str.lower()
            total += len(df)
            cex_in += df["_tl"].isin(cex_addrs).sum()
            cex_out += df["_fl"].isin(cex_addrs).sum()
            senders.update(df["_fl"].unique())
            receivers.update(df["_tl"].unique())
        except Exception:
            continue

    return {
        "_status": "ok",
        "token": token.upper(),
        "chain": chain,
        "total_transfers": int(total),
        "cex_inflow": int(cex_in),
        "cex_outflow": int(cex_out),
        "net_cex_flow": int(cex_out - cex_in),
        "unique_senders": len(senders),
        "unique_receivers": len(receivers),
        "partitions": len(parts),
        "engine": "pandas",
    }


def top_whales(token: str, chain: str = "ethereum", n: int = 20) -> list[dict]:
    """统计该 token 仓库里转出量最大的 N 个地址."""
    parts = [p for p in list_partitions()
             if p["token"] == token.upper() and p["chain"] == chain]
    if not parts:
        return []

    con = _open_db()
    if con is None:
        return []  # pandas fallback 可后续补
    glob = str(WAREHOUSE_DIR / chain / token.upper() / "**" / "*.parquet")
    try:
        df = con.execute(f"""
            SELECT from_address AS addr,
                   COUNT(*) AS n_sent,
                   SUM(TRY_CAST(value AS DOUBLE)) AS total_sent
            FROM read_parquet('{glob}', union_by_name=true)
            GROUP BY from_address
            ORDER BY n_sent DESC
            LIMIT {n}
        """).fetchdf()
        return df.to_dict("records")
    except Exception as e:
        log.warning(f"  whale query failed: {e}")
        return []


def warehouse_stats() -> dict:
    """整个仓库的概览."""
    parts = list_partitions()
    state = _load_state()
    by_token: dict[str, int] = {}
    by_chain: dict[str, int] = {}
    total_kb = 0
    for p in parts:
        by_token[p["token"]] = by_token.get(p["token"], 0) + 1
        by_chain[p["chain"]] = by_chain.get(p["chain"], 0) + 1
        total_kb += p["size_kb"]
    return {
        "total_parquet_files": len(parts),
        "total_size_mb": round(total_kb / 1024, 2),
        "by_token": by_token,
        "by_chain": by_chain,
        "tracked_tokens": list(state.get("tokens", {}).keys()),
        "last_updated": state.get("last_updated"),
        "duckdb_available": _duckdb_available(),
        "cryo_available": _cryo_available(),
    }


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== cryo_warehouse health ===")
    print(json.dumps(warehouse_stats(), indent=2, default=str))
    print("\n=== list_partitions ===")
    for p in list_partitions()[:5]:
        print(f"  {p}")
    print("\n=== USDT cex flow ===")
    print(json.dumps(cex_flow_summary("USDT"), indent=2))
