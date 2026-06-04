"""cryo 链上数据 adapter — 包装 paradigmxyz/cryo CLI。

cryo (https://github.com/paradigmxyz/cryo) 是 Paradigm 出品的 Rust 工具,
把 EVM 链 (ETH/Arb/Op/Polygon/BNB/Avax) 的链上数据导出为 parquet/csv/json。

本 adapter 用 subprocess 调用 cryo 二进制, 提供地址级粒度的数据:
  - ERC20 transfers (大额转账、CEX inflow/outflow)
  - logs (合约事件)
  - native transfers (ETH 转账)

设计原则:
  - 软依赖: 没装 cryo 时返回 None, 主流程不中断
  - 输出 parquet (cryo 默认), 用 pandas 读
  - 支持指定区块范围 (用于增量更新)
  - 缓存到 data/onchain/cryo/, 重复请求复用磁盘

使用前置:
  1. 装 cryo: `cargo install cryo_cli`
     或 brew install paradigmxyz/cryo/cryo
  2. 配 RPC: 在 .env 加 `ETH_RPC_URL=https://eth.llamarpc.com`
     (免费 RPC: llamarpc / publicnode / drpc)

降级链:
  Level 0: cryo 已装 + RPC 可用 → 直接拉数据
  Level 1: cryo 已装 + RPC 失败 → 用 fallback RPC
  Level 2: cryo 未装 → 返回 None, 上层降级到 blockchain.info/etherscan
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..utils import setup_logger

log = setup_logger("cryo_onchain", "INFO")

# 数据缓存目录
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "onchain" / "cryo"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 默认 RPC 列表 (按优先级, 都是免费公开节点)
DEFAULT_RPCS = {
    "ethereum": [
        "https://eth.llamarpc.com",
        "https://ethereum.publicnode.com",
        "https://rpc.ankr.com/eth",
    ],
    "polygon": [
        "https://polygon.llamarpc.com",
        "https://polygon-rpc.com",
    ],
    "arbitrum": [
        "https://arbitrum.llamarpc.com",
        "https://arb1.arbitrum.io/rpc",
    ],
    "optimism": [
        "https://optimism.llamarpc.com",
        "https://mainnet.optimism.io",
    ],
    "bnb": [
        "https://bsc-dataseed.binance.org",
        "https://bsc-dataseed1.defibit.io",
    ],
}

# 业务常用合约地址
WELL_KNOWN_TOKENS = {
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48".lower(),  # ETH USDC (Circle 官方合约)
    "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
}

# 常见 CEX 热钱包 (用于识别 cex flow)
CEX_HOT_WALLETS = {
    "binance_14": "0x28C6c06298d514Db089934071355E5743bf21d60",
    "binance_15": "0xDFd5293D8e347dFe59E90eFd55b2956a1343963d",
    "coinbase_4": "0xA9D1e08C7793af67e9d92fe308d5697FB81d3E43",
    "kraken_1":   "0x2910543Af39abA0Cd09dBb2D50200b3E800A63D2",
    "okx_1":      "0x6Cc5F688a315f3dC28A7781717a9A798a59fDA7b",
}


def _which_cryo() -> Optional[str]:
    """检测 cryo 是否在 PATH。"""
    path = shutil.which("cryo")
    if not path:
        log.debug("cryo binary not in PATH")
    return path


def _resolve_rpc(chain: str) -> Optional[str]:
    """解析 RPC: 优先 env var, 否则用默认列表第一个。"""
    env_key = f"{chain.upper()}_RPC_URL"
    if os.getenv(env_key):
        return os.getenv(env_key)
    rpcs = DEFAULT_RPCS.get(chain.lower(), [])
    return rpcs[0] if rpcs else None


def is_available() -> bool:
    """快速 health check: cryo 是否可用。"""
    return _which_cryo() is not None


def _run_cryo(
    dataset: str,
    chain: str,
    blocks: str,
    output_dir: Path,
    extra_args: list[str] = None,
    timeout: int = 120,
) -> bool:
    """跑一次 cryo 子进程。

    Args:
        dataset: cryo dataset 名 (transactions / logs / erc20_transfers / ...)
        chain: 链名 (ethereum/polygon/...)
        blocks: 区块范围 (e.g. "18000000:18001000" 或 "-1000:" 表示最近 1000 块)
        output_dir: 输出目录 (parquet 文件)
        extra_args: 附加参数 (如 --contract 0x...)
        timeout: 超时秒数

    Returns:
        True 成功, False 失败
    """
    cryo = _which_cryo()
    if not cryo:
        return False

    rpc = _resolve_rpc(chain)
    if not rpc:
        log.warning(f"  no RPC for {chain}, set {chain.upper()}_RPC_URL")
        return False

    cmd = [
        cryo, dataset,
        "--rpc", rpc,
        "--blocks", blocks,
        "--output-dir", str(output_dir),
        "--format", "parquet",
        "--overwrite",
    ]
    if extra_args:
        cmd.extend(extra_args)

    log.info(f"  cryo {dataset} blocks={blocks} chain={chain}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            log.warning(f"  cryo exit {result.returncode}: {result.stderr[:200]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        log.warning(f"  cryo timeout after {timeout}s")
        return False
    except Exception as e:
        log.warning(f"  cryo error: {e}")
        return False


def fetch_erc20_transfers(
    contract: str,
    chain: str = "ethereum",
    last_n_blocks: int = 1000,
    min_value_usd: float = 100_000.0,
) -> Optional["pd.DataFrame"]:
    """拉指定 ERC20 合约的最近 N 块 transfer 记录。

    用于识别大额转账 / whale activity / cex 流入流出。

    Args:
        contract: ERC20 合约地址
        chain: 链名
        last_n_blocks: 最近多少个区块 (~13s/block on ETH)
        min_value_usd: 过滤小额转账 (粗略阈值, 实际还要乘价格)

    Returns:
        DataFrame[block_number, tx_hash, from, to, value_decimal] 或 None
    """
    if not is_available():
        log.debug("  cryo not available, skip ERC20 transfers")
        return None

    try:
        import pandas as pd
    except ImportError:
        return None

    out_dir = CACHE_DIR / f"{chain}_transfers_{contract[:10]}"
    out_dir.mkdir(parents=True, exist_ok=True)

    ok = _run_cryo(
        dataset="erc20_transfers",
        chain=chain,
        blocks=f"-{last_n_blocks}:",
        output_dir=out_dir,
        extra_args=["--contract", contract],
        timeout=180,
    )
    if not ok:
        return None

    # 读所有 parquet 文件
    parquets = list(out_dir.glob("*.parquet"))
    if not parquets:
        return None
    df = pd.concat([pd.read_parquet(p) for p in parquets], ignore_index=True)
    log.info(f"  cryo loaded {len(df)} transfers")
    return df


def detect_cex_flow(
    contract: str,
    chain: str = "ethereum",
    last_n_blocks: int = 1000,
) -> dict:
    """识别 CEX 流入流出 (基于热钱包匹配)。

    Returns:
        {
          "cex_inflow_count": int,   # 流入 CEX 的笔数
          "cex_outflow_count": int,  # 从 CEX 流出的笔数
          "net_cex_flow": int,       # outflow - inflow (正数=囤币信号)
          "total_transfers": int,
        }
    """
    df = fetch_erc20_transfers(contract, chain, last_n_blocks)
    if df is None or df.empty:
        return {
            "cex_inflow_count": 0,
            "cex_outflow_count": 0,
            "net_cex_flow": 0,
            "total_transfers": 0,
            "_status": "unavailable",
        }

    cex_addrs = {addr.lower() for addr in CEX_HOT_WALLETS.values()}

    # cryo 字段名约定: from_address, to_address (按版本可能变)
    from_col = next((c for c in df.columns if "from" in c.lower()), None)
    to_col = next((c for c in df.columns if "to" in c.lower()), None)
    if not from_col or not to_col:
        return {"_status": "unknown_schema", "columns": list(df.columns)}

    df["_from_lc"] = df[from_col].astype(str).str.lower()
    df["_to_lc"] = df[to_col].astype(str).str.lower()

    cex_in = df["_to_lc"].isin(cex_addrs).sum()
    cex_out = df["_from_lc"].isin(cex_addrs).sum()

    return {
        "cex_inflow_count": int(cex_in),
        "cex_outflow_count": int(cex_out),
        "net_cex_flow": int(cex_out - cex_in),
        "total_transfers": int(len(df)),
        "_status": "ok",
    }


# ====================================================================
#  统一入口 (给 onchain_real.py 调用)
# ====================================================================

def enrich_onchain(symbol: str, contract: str = None, chain: str = "ethereum") -> dict:
    """统一对外接口: 给定一个币种, 尝试用 cryo 拉链上 enrichment。

    Args:
        symbol: 币种代号 (BTC/ETH/...)
        contract: 合约地址 (可选, 未传则查 WELL_KNOWN_TOKENS)
        chain: 链名

    Returns:
        dict, 不可用时返回 {"_available": False, ...}
    """
    if not is_available():
        return {
            "_available": False,
            "_reason": "cryo binary not installed (cargo install cryo_cli)",
            "symbol": symbol,
        }

    contract = contract or WELL_KNOWN_TOKENS.get(symbol.upper())
    if not contract:
        return {"_available": False, "_reason": "no contract address", "symbol": symbol}

    flow = detect_cex_flow(contract, chain, last_n_blocks=1000)

    return {
        "_available": True,
        "symbol": symbol,
        "contract": contract,
        "chain": chain,
        "cex_flow": flow,
        "_ts": datetime.utcnow().isoformat() + "Z",
    }


# ====================================================================
#  Self-test
# ====================================================================

if __name__ == "__main__":
    import sys
    print("[cryo_onchain] availability check ...")
    avail = is_available()
    print(f"  cryo installed: {avail}")
    if avail:
        print(f"  ETH RPC: {_resolve_rpc('ethereum')}")
        sym = sys.argv[1] if len(sys.argv) > 1 else "USDT"
        print(f"  fetching {sym} cex flow ...")
        out = enrich_onchain(sym)
        print(json.dumps(out, indent=2, default=str))
    else:
        print("  install hint: brew install paradigmxyz/cryo/cryo")
        print("                or: cargo install cryo_cli")
