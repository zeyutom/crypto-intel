"""链上 Whale Alert — 大额转账监控 + 飞书推送。

数据源 (全部免费, 无需 key):
  1. Blockchain.info 最近 BTC 大额交易 (unconfirmed pool)
  2. Etherscan 公开 API 最近区块 ETH 大额转账 (需要免费 key, 无也可)
  3. DeFiLlama bridges / large-txns (如有)

阈值:
  - BTC: > 100 BTC (~$10M+)
  - ETH: > 5000 ETH (~$15M+)

与 watchdog 联动: whale_alert 是 watchdog 的一个 check 来源

输出:
  - 最近大额转账列表
  - 飞书告警 (如果超过阈值)
  - 因子信号: large_flow_score (可纳入 screener)
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("whale_alert", "INFO")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _get(url: str, params: dict = None, headers: dict = None,
         timeout: int = 15):
    import httpx
    try:
        r = httpx.get(url, params=params, headers={
            "User-Agent": "CryptoIntel/2.0",
            **(headers or {}),
        }, timeout=timeout, follow_redirects=True)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"  请求失败: {e}")
    return None


# ====================================================================
#  BTC 大额交易 (blockchain.info unconfirmed pool)
# ====================================================================

def fetch_btc_large_txns(min_btc: float = 100) -> list[dict]:
    """从 blockchain.info 获取最近大额 BTC 交易。"""
    log.info(f"  BTC 大额交易 (>{min_btc} BTC)...")

    # 最近区块
    data = _get("https://blockchain.info/latestblock")
    if not data:
        return []

    block_hash = data.get("hash", "")
    if not block_hash:
        return []

    # 获取区块交易
    block = _get(f"https://blockchain.info/rawblock/{block_hash}")
    if not block:
        return []

    large_txns = []
    for tx in block.get("tx", []):
        total_out = sum(o.get("value", 0) for o in tx.get("out", []))
        btc_amount = total_out / 1e8
        if btc_amount >= min_btc:
            # 估算 USD (用 blockchain.info 的市场价)
            large_txns.append({
                "chain": "BTC",
                "hash": tx.get("hash", "")[:16] + "...",
                "amount": round(btc_amount, 2),
                "unit": "BTC",
                "timestamp": tx.get("time", 0),
                "n_inputs": len(tx.get("inputs", [])),
                "n_outputs": len(tx.get("out", [])),
            })

    large_txns.sort(key=lambda x: x["amount"], reverse=True)
    log.info(f"    ✓ {len(large_txns)} 笔大额 BTC 交易")
    return large_txns[:20]


# ====================================================================
#  ETH 大额交易 (Etherscan 公开 API)
# ====================================================================

def fetch_eth_large_txns(min_eth: float = 5000) -> list[dict]:
    """从 Etherscan 获取最近大额 ETH 交易。"""
    import os
    log.info(f"  ETH 大额交易 (>{min_eth} ETH)...")

    api_key = os.environ.get("ETHERSCAN_API_KEY", "")
    params = {
        "module": "proxy",
        "action": "eth_getBlockByNumber",
        "tag": "latest",
        "boolean": "true",
    }
    if api_key:
        params["apikey"] = api_key

    data = _get("https://api.etherscan.io/api", params=params, timeout=20)
    if not data or not data.get("result"):
        return []

    block = data["result"]
    large_txns = []

    for tx in block.get("transactions", []):
        value_hex = tx.get("value", "0x0")
        try:
            value_wei = int(value_hex, 16)
            eth_amount = value_wei / 1e18
        except (ValueError, TypeError):
            continue

        if eth_amount >= min_eth:
            large_txns.append({
                "chain": "ETH",
                "hash": tx.get("hash", "")[:16] + "...",
                "amount": round(eth_amount, 2),
                "unit": "ETH",
                "from": tx.get("from", "")[:10] + "...",
                "to": tx.get("to", "")[:10] + "..." if tx.get("to") else "contract_create",
            })

    large_txns.sort(key=lambda x: x["amount"], reverse=True)
    log.info(f"    ✓ {len(large_txns)} 笔大额 ETH 交易")
    return large_txns[:20]


# ====================================================================
#  综合 Whale Alert
# ====================================================================

def run_whale_check() -> dict:
    """运行一轮 Whale Alert 检查。

    Returns: {
        ok, btc_large: [...], eth_large: [...],
        total_alerts, btc_flow_btc, eth_flow_eth,
    }
    """
    log.info("[Whale Alert] 扫描大额转账...")

    btc_txns = fetch_btc_large_txns(min_btc=100)
    time.sleep(1)
    eth_txns = fetch_eth_large_txns(min_eth=5000)

    btc_total = sum(t["amount"] for t in btc_txns)
    eth_total = sum(t["amount"] for t in eth_txns)

    result = {
        "ok": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "btc_large": btc_txns[:10],
        "eth_large": eth_txns[:10],
        "btc_flow_btc": round(btc_total, 2),
        "eth_flow_eth": round(eth_total, 2),
        "total_alerts": len(btc_txns) + len(eth_txns),
    }

    if btc_txns or eth_txns:
        log.info(f"  ⚡ BTC: {len(btc_txns)} 大额 ({btc_total:.0f} BTC)")
        log.info(f"  ⚡ ETH: {len(eth_txns)} 大额 ({eth_total:.0f} ETH)")

    # 保存到日志
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_path = DATA_DIR / "whale_log.jsonl"
    with open(log_path, "a") as f:
        f.write(json.dumps({
            "ts": result["timestamp"],
            "btc_n": len(btc_txns), "btc_total": btc_total,
            "eth_n": len(eth_txns), "eth_total": eth_total,
        }, ensure_ascii=False) + "\n")

    return result


def push_whale_alert_feishu(whale_result: dict) -> dict:
    """大额转账告警推送到飞书。"""
    if whale_result.get("total_alerts", 0) == 0:
        return {"ok": True, "skipped": True, "reason": "no_alerts"}

    try:
        from ..notifier import _load_groups
        import httpx
    except Exception as e:
        return {"ok": False, "error": str(e)}

    groups = _load_groups()
    if not groups:
        return {"ok": False, "error": "未配置飞书群"}

    # 构建消息
    btc_txns = whale_result.get("btc_large", [])
    eth_txns = whale_result.get("eth_large", [])

    lines = ["🐋 **Whale Alert** — 大额转账监控\n"]
    if btc_txns:
        lines.append(f"**BTC** ({len(btc_txns)} 笔, 共 {whale_result['btc_flow_btc']:.0f} BTC):")
        for t in btc_txns[:5]:
            lines.append(f"  • {t['amount']:.0f} BTC ({t['hash']})")

    if eth_txns:
        lines.append(f"\n**ETH** ({len(eth_txns)} 笔, 共 {whale_result['eth_flow_eth']:.0f} ETH):")
        for t in eth_txns[:5]:
            lines.append(f"  • {t['amount']:.0f} ETH ({t['hash']})")

    text = "\n".join(lines)

    # 推送
    for g in groups:
        try:
            httpx.post(g["url"], json={
                "msg_type": "text",
                "content": {"text": text},
            }, timeout=10)
        except Exception:
            pass

    return {"ok": True, "groups": len(groups)}


def calc_whale_flow_score() -> float:
    """基于最近 whale 日志计算异常流量评分 (0-1)。

    用于纳入因子系统: 大额转账异常多 → 高分 (市场可能有大行情)
    """
    log_path = DATA_DIR / "whale_log.jsonl"
    if not log_path.exists():
        return 0.0

    entries = []
    for line in log_path.read_text().strip().split("\n")[-24:]:  # 最近 24 条
        try:
            entries.append(json.loads(line))
        except Exception:
            continue

    if not entries:
        return 0.0

    # 平均大额交易数
    avg_n = sum(e.get("btc_n", 0) + e.get("eth_n", 0) for e in entries) / len(entries)
    # 标准化: 5 笔以上算高
    return round(min(1.0, avg_n / 5), 3)
