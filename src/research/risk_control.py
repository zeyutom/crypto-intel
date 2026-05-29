"""组合风控 + 因子正交化。

功能:
  1. 因子正交化: 检测因子间共线性 (VIF / 相关矩阵) → 去冗余
  2. 仓位约束: 单币最大权重、板块集中度、市值分布
  3. 波动率预算: 按历史波动率调整权重 (风险平价思想)
  4. 回撤保护: 组合连续回撤 → 自动降仓
  5. 黑名单: 异常代币自动过滤

设计:
  - 所有功能都是可选的过滤/调整层, 不影响主筛选流程
  - 作为 post-processing 应用到筛选结果上
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("risk_control", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"


# ====================================================================
#  因子正交化: 相关性分析 + 去冗余
# ====================================================================

def calc_correlation_matrix(scored_coins: list[dict],
                             factor_keys: list[str] = None) -> dict:
    """计算因子间的 Pearson 相关系数矩阵。

    Returns: {
        "matrix": {f1: {f2: corr}},
        "high_corr_pairs": [(f1, f2, corr)],  # |corr| > 0.7
        "vif": {factor: vif_value},
    }
    """
    if factor_keys is None:
        factor_keys = [
            "f_momentum_30d", "f_momentum_7d", "f_ath_drawdown",
            "f_volume_turnover", "f_tvl_mcap", "f_market_cap_size",
            "f_onchain_activity", "f_dev_activity", "f_funding_rate",
            "f_narrative_heat",
        ]

    n = len(scored_coins)
    if n < 10:
        return {"matrix": {}, "high_corr_pairs": [], "vif": {}}

    # 提取因子值矩阵
    factor_data = {}
    for key in factor_keys:
        vals = [c.get(key, 0) or 0 for c in scored_coins]
        factor_data[key] = vals

    # Pearson 相关系数
    def _pearson(x, y):
        n = len(x)
        if n < 3:
            return 0
        mx = sum(x) / n
        my = sum(y) / n
        sx = math.sqrt(sum((xi - mx) ** 2 for xi in x) / max(n - 1, 1))
        sy = math.sqrt(sum((yi - my) ** 2 for yi in y) / max(n - 1, 1))
        if sx == 0 or sy == 0:
            return 0
        cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / (n - 1)
        return cov / (sx * sy)

    matrix = {}
    high_corr = []

    for f1 in factor_keys:
        matrix[f1] = {}
        for f2 in factor_keys:
            if f1 == f2:
                matrix[f1][f2] = 1.0
            else:
                corr = _pearson(factor_data[f1], factor_data[f2])
                matrix[f1][f2] = round(corr, 3)
                if abs(corr) > 0.7 and f1 < f2:  # 避免重复
                    high_corr.append((f1, f2, round(corr, 3)))

    # 简化 VIF (使用 R² 近似)
    vif = {}
    for target in factor_keys:
        others = [f for f in factor_keys if f != target]
        if not others:
            vif[target] = 1.0
            continue
        # R² ≈ max(corr²) with any other factor (简化版)
        max_r2 = max(matrix[target].get(f, 0) ** 2 for f in others)
        vif[target] = round(1 / max(1 - max_r2, 0.01), 2)

    high_corr.sort(key=lambda x: abs(x[2]), reverse=True)

    return {
        "matrix": matrix,
        "high_corr_pairs": high_corr,
        "vif": vif,
    }


def suggest_factor_pruning(corr_result: dict, vif_threshold: float = 5.0,
                            corr_threshold: float = 0.75) -> list[str]:
    """建议剔除冗余因子。

    规则:
      - VIF > threshold 的因子标记为冗余
      - 高相关对中, 保留 IC 更高的一个 (如有 IC 数据)
    """
    prune = []
    vif = corr_result.get("vif", {})

    for fname, v in vif.items():
        if v > vif_threshold:
            prune.append(fname)
            log.info(f"  ⚠️ 高 VIF: {fname} = {v:.1f} (建议检查)")

    high_pairs = corr_result.get("high_corr_pairs", [])
    for f1, f2, corr in high_pairs:
        if abs(corr) > corr_threshold:
            log.info(f"  ⚠️ 高相关: {f1} ↔ {f2} = {corr:.3f}")

    return prune


# ====================================================================
#  仓位约束
# ====================================================================

# 板块分类 (粗略)
SECTOR_MAP = {
    "BTC": "store_of_value", "ETH": "smart_contract",
    "BNB": "exchange", "SOL": "smart_contract", "ADA": "smart_contract",
    "XRP": "payment", "DOT": "smart_contract", "AVAX": "smart_contract",
    "DOGE": "meme", "SHIB": "meme", "PEPE": "meme", "WIF": "meme",
    "FLOKI": "meme", "BONK": "meme",
    "LINK": "oracle", "UNI": "defi", "AAVE": "defi", "MKR": "defi",
    "CRV": "defi", "SNX": "defi", "COMP": "defi", "SUSHI": "defi",
    "LDO": "defi", "RPL": "defi", "FXS": "defi",
    "ARB": "l2", "OP": "l2", "MATIC": "l2", "POL": "l2",
    "IMX": "l2", "STRK": "l2", "ZK": "l2",
    "FIL": "storage", "AR": "storage",
    "RNDR": "ai", "FET": "ai", "OCEAN": "ai", "AGIX": "ai",
    "TAO": "ai", "WLD": "ai",
    "SUI": "smart_contract", "APT": "smart_contract", "SEI": "smart_contract",
    "NEAR": "smart_contract", "TON": "smart_contract",
    "TRX": "payment", "XLM": "payment",
    "ATOM": "interop", "INJ": "interop",
}


def apply_position_limits(
    scored_coins: list[dict],
    max_single_weight: float = 0.15,      # 单币最大 15%
    max_sector_weight: float = 0.35,       # 单板块最大 35%
    max_meme_weight: float = 0.15,         # Meme 板块最大 15%
    min_market_cap: float = 1e8,           # 最低市值 $100M
    top_n: int = 30,
) -> list[dict]:
    """应用仓位约束, 返回过滤后的列表。"""
    log.info(f"[风控] 应用仓位约束 (top_n={top_n})...")

    filtered = []
    sector_count = {}
    sector_limit = max(1, int(top_n * max_sector_weight))
    meme_limit = max(1, int(top_n * max_meme_weight))

    for coin in scored_coins:
        sym = coin.get("symbol", "")
        mcap = coin.get("market_cap", 0) or 0

        # 最低市值过滤
        if mcap < min_market_cap:
            continue

        # 板块集中度
        sector = SECTOR_MAP.get(sym, "other")
        current_count = sector_count.get(sector, 0)

        if sector == "meme" and current_count >= meme_limit:
            log.info(f"  ⚡ Meme 上限: {sym} 被跳过")
            continue
        if current_count >= sector_limit:
            log.info(f"  ⚡ 板块上限 ({sector}): {sym} 被跳过")
            continue

        sector_count[sector] = current_count + 1
        filtered.append(coin)

        if len(filtered) >= top_n:
            break

    log.info(f"  ✓ 仓位约束后: {len(filtered)}/{len(scored_coins)} 代币")
    return filtered


# ====================================================================
#  波动率预算 (风险平价)
# ====================================================================

def calc_volatility_weights(scored_coins: list[dict]) -> dict[str, float]:
    """基于历史波动率计算风险平价权重。

    波动率高的币降低权重, 波动率低的币提高权重。
    """
    vol_data = {}
    for coin in scored_coins:
        sym = coin.get("symbol", "")
        # 用 30d 波动幅度作为波动率代理
        change_30d = abs(coin.get("change_30d", 0) or 0)
        change_7d = abs(coin.get("change_7d", 0) or 0)
        # 粗略年化波动率: 30d range * sqrt(12)
        vol_30d = change_30d / 100 * math.sqrt(12)
        vol_7d = change_7d / 100 * math.sqrt(52)
        vol = max(vol_30d, vol_7d, 0.01)  # 避免除零
        vol_data[sym] = vol

    if not vol_data:
        return {}

    # 反比例权重: 1/vol
    inv_vols = {sym: 1 / v for sym, v in vol_data.items()}
    total_inv = sum(inv_vols.values())

    weights = {sym: round(iv / total_inv, 4) for sym, iv in inv_vols.items()}
    return weights


# ====================================================================
#  回撤保护
# ====================================================================

def check_drawdown_protection(lookback_days: int = 7) -> dict:
    """检查组合近期回撤, 决定是否降仓。

    Returns: {
        "action": "full" | "reduce" | "hedge",
        "drawdown": float,
        "position_multiplier": float (0.5 ~ 1.0),
    }
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    snap_files = sorted(META_DIR.glob("snapshot_*.json"))

    if len(snap_files) < 2:
        return {"action": "full", "drawdown": 0, "position_multiplier": 1.0}

    # 加载最近的快照
    recent = []
    for sf in snap_files[-lookback_days:]:
        try:
            data = json.loads(sf.read_text())
            recent.append(data)
        except Exception:
            continue

    if len(recent) < 2:
        return {"action": "full", "drawdown": 0, "position_multiplier": 1.0}

    # 计算 Top-10 的平均价格变化
    first_snap = recent[0]
    last_snap = recent[-1]

    first_prices = {c["symbol"]: c["price"] for c in first_snap.get("coins", [])[:30]}
    last_prices = {c["symbol"]: c["price"] for c in last_snap.get("coins", [])[:30]}

    returns = []
    for sym, old_p in first_prices.items():
        new_p = last_prices.get(sym, 0)
        if old_p > 0 and new_p > 0:
            returns.append((new_p - old_p) / old_p)

    if not returns:
        return {"action": "full", "drawdown": 0, "position_multiplier": 1.0}

    avg_return = sum(returns) / len(returns)

    # 回撤判断
    if avg_return < -0.15:
        # 严重回撤 (>15%): 降仓到 50%
        return {
            "action": "reduce",
            "drawdown": round(avg_return, 4),
            "position_multiplier": 0.5,
            "reason": f"Top-30 平均回撤 {avg_return:.1%}, 触发降仓保护",
        }
    elif avg_return < -0.08:
        # 中等回撤 (>8%): 降仓到 75%
        return {
            "action": "reduce",
            "drawdown": round(avg_return, 4),
            "position_multiplier": 0.75,
            "reason": f"Top-30 平均回撤 {avg_return:.1%}, 适度降仓",
        }
    else:
        return {
            "action": "full",
            "drawdown": round(avg_return, 4),
            "position_multiplier": 1.0,
        }


# ====================================================================
#  黑名单管理
# ====================================================================

BLACKLIST_PATH = META_DIR / "blacklist.json"


def load_blacklist() -> dict:
    """加载黑名单。"""
    if BLACKLIST_PATH.exists():
        try:
            return json.loads(BLACKLIST_PATH.read_text())
        except Exception:
            pass
    return {"symbols": {}, "updated": None}


def add_to_blacklist(symbol: str, reason: str, duration_days: int = 30):
    """添加代币到黑名单。"""
    bl = load_blacklist()
    bl["symbols"][symbol] = {
        "reason": reason,
        "added": datetime.now(timezone.utc).isoformat(),
        "expires": (datetime.now(timezone.utc).timestamp() +
                    duration_days * 86400),
    }
    bl["updated"] = datetime.now(timezone.utc).isoformat()
    META_DIR.mkdir(parents=True, exist_ok=True)
    BLACKLIST_PATH.write_text(json.dumps(bl, ensure_ascii=False, indent=2))
    log.info(f"  ⛔ 黑名单: {symbol} ({reason})")


def filter_blacklist(scored_coins: list[dict]) -> list[dict]:
    """从结果中移除黑名单代币。"""
    bl = load_blacklist()
    now = datetime.now(timezone.utc).timestamp()

    blocked = set()
    for sym, info in bl.get("symbols", {}).items():
        if info.get("expires", 0) > now:
            blocked.add(sym)

    if not blocked:
        return scored_coins

    filtered = [c for c in scored_coins if c.get("symbol", "") not in blocked]
    removed = len(scored_coins) - len(filtered)
    if removed > 0:
        log.info(f"  ⛔ 黑名单过滤: 移除 {removed} 个代币")
    return filtered


# ====================================================================
#  综合风控 Pipeline
# ====================================================================

def apply_risk_controls(
    scored_coins: list[dict],
    top_n: int = 30,
    enable_position_limits: bool = True,
    enable_volatility_budget: bool = True,
    enable_drawdown_protection: bool = True,
    enable_blacklist: bool = True,
) -> dict:
    """一键应用所有风控措施。

    Returns: {
        "coins": filtered_coins,
        "risk_report": {...},
        "position_multiplier": float,
        "vol_weights": {sym: weight},
    }
    """
    log.info("[风控] 综合风控 Pipeline...")

    report = {
        "original_count": len(scored_coins),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    coins = scored_coins[:]

    # 1. 黑名单过滤
    if enable_blacklist:
        coins = filter_blacklist(coins)
        report["after_blacklist"] = len(coins)

    # 2. 仓位约束
    if enable_position_limits:
        coins = apply_position_limits(coins, top_n=top_n)
        report["after_position_limits"] = len(coins)

    # 3. 因子正交化分析 (仅报告, 不修改)
    corr = calc_correlation_matrix(coins)
    report["high_corr_pairs"] = corr.get("high_corr_pairs", [])[:5]
    report["vif_warnings"] = {k: v for k, v in corr.get("vif", {}).items() if v > 5}
    prune_suggestions = suggest_factor_pruning(corr)
    report["prune_suggestions"] = prune_suggestions

    # 4. 波动率权重
    vol_weights = {}
    if enable_volatility_budget:
        vol_weights = calc_volatility_weights(coins)
        report["vol_weights_range"] = {
            "min": min(vol_weights.values()) if vol_weights else 0,
            "max": max(vol_weights.values()) if vol_weights else 0,
        }

    # 5. 回撤保护
    position_multiplier = 1.0
    if enable_drawdown_protection:
        dd = check_drawdown_protection()
        position_multiplier = dd["position_multiplier"]
        report["drawdown"] = dd

    report["final_count"] = len(coins)

    log.info(f"  ✓ 风控完成: {report['original_count']} → {len(coins)} 代币, "
             f"仓位乘数={position_multiplier}")

    return {
        "coins": coins,
        "risk_report": report,
        "position_multiplier": position_multiplier,
        "vol_weights": vol_weights,
    }


def generate_risk_report_text(risk_result: dict) -> str:
    """生成风控报告文本摘要。"""
    rr = risk_result.get("risk_report", {})
    lines = [
        "═══ 风控报告 ═══",
        f"原始: {rr.get('original_count', 0)} → 最终: {rr.get('final_count', 0)} 代币",
    ]

    dd = rr.get("drawdown", {})
    if dd.get("action") != "full":
        lines.append(f"⚠️ 回撤保护: {dd.get('reason', '')}")
        lines.append(f"   仓位乘数: {dd.get('position_multiplier', 1):.0%}")

    pairs = rr.get("high_corr_pairs", [])
    if pairs:
        lines.append(f"📊 高相关因子对: {len(pairs)} 组")
        for f1, f2, corr in pairs[:3]:
            f1_short = f1.replace("f_", "")
            f2_short = f2.replace("f_", "")
            lines.append(f"   {f1_short} ↔ {f2_short}: {corr:.3f}")

    vif_warns = rr.get("vif_warnings", {})
    if vif_warns:
        lines.append(f"⚠️ 高 VIF 因子: {', '.join(k.replace('f_', '') for k in vif_warns)}")

    return "\n".join(lines)
