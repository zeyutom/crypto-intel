#!/usr/bin/env python3
"""Backfill 历史快照 — 让元学习有原料启动.

从 DefiLlama 价格 API 拉 Top-N 币种 90 天日线 → 反推每天的合成 snapshot,
写到 data/meta/snapshot_YYYYMMDD_0000.json (与现有 schema 完全兼容).

之所以需要 backfill:
  - 元学习需要 ≥ 7 个连续快照才能开始 IC 加权
  - PBO 诊断需要 T (时间) × N (策略) 矩阵, T 太小估计不准
  - 当前只有 9 个真实快照, 跨度短

合成 snapshot 不能完整复制实时筛选 (没有 funding rate / TVL / ETF flow 历史),
但能提供这 4 个因子的近似:
  - f_momentum_30d  → 30d 累计收益率 (归一化)
  - f_momentum_7d   → 7d 累计收益率 (归一化)
  - f_ath_drawdown  → close 距 90d 高点的距离
  - f_volume_turnover → 24h 成交额 / 市值

剩余 7 个因子用 None 占位, alpha_discovery 算因子时会跳过 None → 0.

用法:
  python scripts/backfill_snapshots.py             # backfill Top 30 币, 60 天
  python scripts/backfill_snapshots.py --days 90 --top 50
  python scripts/backfill_snapshots.py --dry-run   # 不写文件, 只显示
"""
from __future__ import annotations
import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.utils import setup_logger
from src.adapters import defillama_full as dlf

log = setup_logger("backfill", "INFO")

META_DIR = ROOT / "data" / "meta"
META_DIR.mkdir(parents=True, exist_ok=True)


# ────────────────────────────────────────────────────────────────────
#  发现要 backfill 的 universe (从最近一个快照取 Top symbols)
# ────────────────────────────────────────────────────────────────────

# CoinGecko id ↔ symbol 映射 (DefiLlama 需要 coingecko:id 格式)
DEFAULT_UNIVERSE: list[tuple[str, str]] = [
    ("BTC", "coingecko:bitcoin"),
    ("ETH", "coingecko:ethereum"),
    ("SOL", "coingecko:solana"),
    ("BNB", "coingecko:binancecoin"),
    ("XRP", "coingecko:ripple"),
    ("ADA", "coingecko:cardano"),
    ("DOGE", "coingecko:dogecoin"),
    ("AVAX", "coingecko:avalanche-2"),
    ("DOT", "coingecko:polkadot"),
    ("LINK", "coingecko:chainlink"),
    ("MATIC", "coingecko:matic-network"),
    ("TRX", "coingecko:tron"),
    ("LTC", "coingecko:litecoin"),
    ("UNI", "coingecko:uniswap"),
    ("ATOM", "coingecko:cosmos"),
    ("ETC", "coingecko:ethereum-classic"),
    ("XLM", "coingecko:stellar"),
    ("BCH", "coingecko:bitcoin-cash"),
    ("NEAR", "coingecko:near"),
    ("APT", "coingecko:aptos"),
    ("FIL", "coingecko:filecoin"),
    ("ARB", "coingecko:arbitrum"),
    ("OP", "coingecko:optimism"),
    ("SUI", "coingecko:sui"),
    ("INJ", "coingecko:injective-protocol"),
    ("RNDR", "coingecko:render-token"),
    ("HBAR", "coingecko:hedera-hashgraph"),
    ("AAVE", "coingecko:aave"),
    ("MKR", "coingecko:maker"),
    ("LDO", "coingecko:lido-dao"),
]


def load_universe(n: int = 30) -> list[tuple[str, str]]:
    """优先从最近 snapshot 读 Top-N, 否则用 DEFAULT_UNIVERSE."""
    snaps = sorted(META_DIR.glob("snapshot_*.json"))
    if snaps:
        try:
            d = json.loads(snaps[-1].read_text())
            symbols = [c["symbol"] for c in d.get("coins", [])[:n]]
            # 把 symbol 映射回 coingecko id
            sym_to_cg = {s: cg for s, cg in DEFAULT_UNIVERSE}
            universe = [(s, sym_to_cg[s]) for s in symbols if s in sym_to_cg]
            if len(universe) >= 10:
                log.info(f"从最近快照取 {len(universe)} 个币种 (与 DEFAULT 交集)")
                return universe
        except Exception as e:
            log.warning(f"读快照失败 fallback to default: {e}")
    return DEFAULT_UNIVERSE[:n]


# ────────────────────────────────────────────────────────────────────
#  拉历史价格
# ────────────────────────────────────────────────────────────────────

PRICE_CACHE = ROOT / "data" / "cache" / "backfill_prices.json"


def fetch_history(
    universe: list[tuple[str, str]],
    days: int = 60,
    use_cache: bool = True,
) -> dict[str, list[dict]]:
    """从 DefiLlama 拉每个币 N 天的日级价格, 带磁盘缓存.

    Returns: {symbol: [{ts, price}, ...]}

    Bug fix v0.9: 之前 cache 只存 {symbol: prices} 不记 days,
    跨次跑 --days 90 时若 cache 是 30 天的 → 误以为已有数据 → 用户拿不到结果.
    现在 cache 改成 {"_meta": {days, ts}, "_data": {symbol: prices}}, 不够长强制重拉.
    """
    PRICE_CACHE.parent.mkdir(parents=True, exist_ok=True)

    # 尝试读 cache (≤ 24h 算新鲜, 且 days 不小于这次需要)
    cached: dict[str, list[dict]] = {}
    if use_cache and PRICE_CACHE.exists():
        try:
            age_hr = (time.time() - PRICE_CACHE.stat().st_mtime) / 3600
            raw = json.loads(PRICE_CACHE.read_text())
            # 兼容新老格式
            if isinstance(raw, dict) and "_meta" in raw:
                cache_days = raw["_meta"].get("days", 0)
                cached = raw.get("_data", {})
            else:
                # 老格式: 假设是 60 天 (写脚本时的默认值)
                cache_days = 60
                cached = raw
            if age_hr >= 24:
                log.info(f"  缓存过期 ({age_hr:.1f}h), 重新拉")
                cached = {}
            elif cache_days < days:
                log.info(f"  缓存只有 {cache_days} 天 < 需要 {days} 天, 强制重拉")
                cached = {}
            else:
                log.info(f"  ✓ 缓存命中 ({age_hr:.1f}h old, {cache_days}d, "
                         f"{len(cached)} 个币种)")
        except Exception:
            cached = {}

    out: dict[str, list[dict]] = dict(cached)
    needed = [(s, c) for s, c in universe if s not in out]
    if not needed:
        log.info(f"  全部 {len(universe)} 个币种命中缓存, 跳过 API")
        return out

    log.info(f"  缓存里缺 {len(needed)} 个币种, 调 API ...")
    for i, (sym, coin_id) in enumerate(needed):
        log.info(f"  [{i+1}/{len(needed)}] {sym} ({coin_id}) ...")
        data = dlf.price_chart([coin_id], start=None, span=days + 5, period="1d")
        if not data or "coins" not in data:
            log.warning(f"    {sym}: 无数据")
            continue
        coin_data = data["coins"].get(coin_id, {})
        prices = coin_data.get("prices", [])
        if not prices:
            log.warning(f"    {sym}: prices 为空")
            continue
        out[sym] = prices
        log.info(f"    ✓ {len(prices)} 个数据点")
        # 每 5 个币种增量写一次缓存 (中断也不丢)
        if (i + 1) % 5 == 0:
            try:
                PRICE_CACHE.write_text(json.dumps(
                    {"_meta": {"days": days, "ts": time.time()},
                     "_data": out}, default=str))
            except Exception:
                pass
        time.sleep(0.3)

    # 最终写一次完整缓存
    try:
        PRICE_CACHE.write_text(json.dumps(
            {"_meta": {"days": days, "ts": time.time()},
             "_data": out}, default=str))
        log.info(f"  ✓ 价格缓存已存 {PRICE_CACHE.name} (days={days})")
    except Exception as e:
        log.warning(f"  写缓存失败: {e}")
    return out


# ────────────────────────────────────────────────────────────────────
#  从历史价格反推因子
# ────────────────────────────────────────────────────────────────────

def _safe_pct(curr: float, past: float) -> float:
    if past is None or past == 0 or curr is None:
        return 0.0
    return (curr - past) / past


def _normalize(value: float, scale: float = 1.0) -> float:
    """把 ±scale 内的值映射到 [-1, +1], 超出 cap."""
    if not math.isfinite(value):
        return 0.0
    return max(-1.0, min(1.0, value / scale))


def compute_coin_factors(
    prices: list[dict],
    end_idx: int,
    full_lookback: int = 90,
) -> dict:
    """从某个币的价格序列反推该 end_idx 时点的因子.

    Args:
        prices: [{timestamp, price}, ...] 升序
        end_idx: 计算因子的"今天"在序列里的下标
        full_lookback: 算 ATH 时的回望窗口

    Returns: {f_momentum_30d, f_momentum_7d, f_ath_drawdown, ...}
    """
    if end_idx < 1 or end_idx >= len(prices):
        return {}

    curr = prices[end_idx].get("price")
    if not curr or curr <= 0:
        return {}

    # momentum_7d / momentum_30d
    p_7d = prices[end_idx - 7].get("price") if end_idx >= 7 else None
    p_30d = prices[end_idx - 30].get("price") if end_idx >= 30 else None
    mom_7 = _safe_pct(curr, p_7d)
    mom_30 = _safe_pct(curr, p_30d)

    # ATH drawdown: curr 距过去 90 天最高价
    start = max(0, end_idx - full_lookback)
    window_prices = [p["price"] for p in prices[start:end_idx + 1] if p.get("price")]
    ath = max(window_prices) if window_prices else curr
    ath_dd = _safe_pct(curr, ath)   # 负值 (跌幅)

    return {
        "f_momentum_30d": _normalize(mom_30, scale=1.0),    # ±100% → ±1
        "f_momentum_7d": _normalize(mom_7, scale=0.3),      # ±30% → ±1
        "f_ath_drawdown": ath_dd,                           # 直接用 (∈ [-1, 0])
        # 占位 (用 None 让 alpha_discovery 视为 0)
        "f_volume_turnover": None,
        "f_funding_rate": None,
        "f_tvl_mcap": None,
        "f_dev_activity": None,
        "f_onchain_activity": None,
        "f_narrative_heat": None,
        "f_market_cap_size": None,
    }


def compute_composite(factors: dict, weights: dict = None) -> float:
    """简单等权合成 (没有元学习权重时的占位)."""
    if not weights:
        weights = {
            "f_momentum_30d": 0.35,
            "f_momentum_7d": 0.25,
            "f_ath_drawdown": -0.20,  # 反向: 跌得越多, 越像低吸机会 → 但这里取负数, 让大跌→低分
        }
    score = 0.0
    total_w = 0.0
    for fname, w in weights.items():
        v = factors.get(fname)
        if v is None:
            continue
        score += v * w
        total_w += abs(w)
    return round(score / max(total_w, 1e-6), 4)


# ────────────────────────────────────────────────────────────────────
#  写合成 snapshot
# ────────────────────────────────────────────────────────────────────

def build_snapshot_for_date(
    target_date: datetime,
    universe: list[tuple[str, str]],
    history: dict[str, list[dict]],
) -> dict | None:
    """对给定日期构造一个合成 snapshot."""
    target_ts = int(target_date.timestamp())
    coins = []
    for sym, _coin_id in universe:
        prices = history.get(sym)
        if not prices:
            continue
        # 找到该日期对应的 idx (price 数组里 timestamp <= target_ts 的最大)
        idx = None
        for i, p in enumerate(prices):
            if p.get("timestamp", 0) <= target_ts:
                idx = i
            else:
                break
        if idx is None or idx < 30:
            continue  # 需要至少 30 天历史才能算 momentum_30d
        factors = compute_coin_factors(prices, idx)
        if not factors:
            continue
        coins.append({
            "symbol": sym,
            "price": float(prices[idx]["price"]),
            "market_cap": None,  # 没拉, 留 None
            **factors,
            "composite_score": compute_composite(factors),
        })

    if len(coins) < 5:
        return None

    # 按 composite_score 排序
    coins.sort(key=lambda c: c.get("composite_score", 0), reverse=True)

    return {
        "timestamp": target_date.replace(tzinfo=timezone.utc).isoformat(),
        "date": target_date.strftime("%Y-%m-%d"),
        "factor_weights": {
            "f_momentum_30d": 0.35,
            "f_momentum_7d": 0.25,
            "f_ath_drawdown": -0.20,
        },
        "coins": coins,
        "_meta": {
            "source": "backfill",
            "synthetic": True,
            "n_factors": 3,  # 真正有数据的因子数 (非 None)
            "backfill_run": datetime.utcnow().isoformat() + "Z",
        },
    }


# ────────────────────────────────────────────────────────────────────
#  主入口
# ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backfill 合成历史快照")
    parser.add_argument("--days", type=int, default=60,
                        help="向前 backfill 多少天 (默认 60)")
    parser.add_argument("--top", type=int, default=30,
                        help="universe 大小 (默认 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="不写文件, 只显示要做什么")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖已存在的 backfill 快照 (不影响真实快照)")
    args = parser.parse_args()

    universe = load_universe(args.top)
    log.info(f"universe: {len(universe)} 个币种")
    log.info(f"  前 5 个: {[s for s, _ in universe[:5]]}")

    # 算 momentum_30d 需要每个 target 之前再有 30 天历史
    # 算 ATH drawdown 需要 90 天回望
    # 所以总共拉 days + 90 缓冲, 才能 backfill 完 days 天
    lookback_buffer = 90
    fetch_days = args.days + lookback_buffer
    log.info(f"\n拉历史价格 (DefiLlama price_chart, span={fetch_days + 5}d) ...")
    history = fetch_history(universe, days=fetch_days)
    if not history:
        log.error("没有任何币种拿到历史价格")
        sys.exit(1)
    log.info(f"成功获取 {len(history)} 个币种")

    # 对过去 N 天的每一天构造一个 snapshot
    log.info(f"\n构造 {args.days} 天合成快照 ...")
    written = 0
    skipped = 0
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(args.days, 0, -1):
        target = today - timedelta(days=offset)
        date_str = target.strftime("%Y%m%d_0000")
        out_path = META_DIR / f"snapshot_{date_str}.json"

        # 跳过真实快照 (没有 _meta.source=backfill 标记的)
        if out_path.exists():
            if args.overwrite:
                pass
            else:
                try:
                    existing = json.loads(out_path.read_text())
                    if not (existing.get("_meta", {}) or {}).get("source") == "backfill":
                        log.info(f"  {target.date()} 已有真实快照, 跳过")
                        skipped += 1
                        continue
                except Exception:
                    pass

        snap = build_snapshot_for_date(target, universe, history)
        if snap is None:
            skipped += 1
            continue

        if args.dry_run:
            log.info(f"  [dry-run] would write {out_path.name} "
                     f"({len(snap['coins'])} coins, top: {snap['coins'][0]['symbol']})")
        else:
            out_path.write_text(json.dumps(snap, ensure_ascii=False, indent=2))
        written += 1

    log.info(f"\n[backfill 完成]")
    log.info(f"  写入: {written}")
    log.info(f"  跳过: {skipped}")
    log.info(f"  总快照数 (含 backfill): {len(list(META_DIR.glob('snapshot_*.json')))}")


if __name__ == "__main__":
    main()
