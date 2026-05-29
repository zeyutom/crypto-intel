"""因子元学习引擎 — 持续优化因子池。

核心机制:
  1. 每次筛选后保存 snapshot (因子值 + 排名)
  2. N 天后回测: 计算每个因子的 IC (Information Coefficient)
     IC = rank_correlation(因子值, 未来N天收益率)
  3. 根据 IC 滚动窗口自动调整因子权重 (IC-weighted)
  4. 衰减 IC 低 / 持续为负的因子, 放大 IC 高的因子
  5. 支持自动淘汰弱因子、引入新因子候选

数据存储: data/meta/ 目录, JSON 格式
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("meta_learner", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"

# ── 默认因子配置 ──
DEFAULT_FACTORS = {
    # 因子名: {weight, min_weight, max_weight, ic_history: []}
    "momentum_30d":    {"weight": 0.18, "min": 0.05, "max": 0.30},
    "momentum_7d":     {"weight": 0.10, "min": 0.03, "max": 0.20},
    "ath_drawdown":    {"weight": 0.10, "min": 0.03, "max": 0.20},
    "volume_turnover": {"weight": 0.10, "min": 0.03, "max": 0.20},
    "tvl_mcap":        {"weight": 0.15, "min": 0.05, "max": 0.30},
    "market_cap_size": {"weight": 0.05, "min": 0.02, "max": 0.15},
    # 新增因子
    "onchain_activity":{"weight": 0.10, "min": 0.03, "max": 0.25},
    "dev_activity":    {"weight": 0.07, "min": 0.02, "max": 0.20},
    "funding_rate":    {"weight": 0.07, "min": 0.02, "max": 0.15},
    "narrative_heat":  {"weight": 0.08, "min": 0.02, "max": 0.20},
}

# 回测周期 (天)
LOOKBACK_DAYS = [7, 14, 30]
# IC 衰减因子 (越老的 IC 权重越低)
IC_DECAY = 0.85
# 最少需要多少次 IC 记录才开始自动调权
MIN_IC_RECORDS = 3


def _ensure_dir():
    META_DIR.mkdir(parents=True, exist_ok=True)


def load_factor_config() -> dict:
    """加载因子配置 (权重 + IC 历史)。首次使用时初始化。"""
    _ensure_dir()
    config_path = META_DIR / "factor_config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            # 合并新因子 (如果代码里加了新因子但 config 里还没有)
            for fname, fdef in DEFAULT_FACTORS.items():
                if fname not in cfg["factors"]:
                    cfg["factors"][fname] = {**fdef, "ic_history": []}
                    log.info(f"  新因子加入池: {fname}")
            return cfg
        except Exception as e:
            log.warning(f"加载 factor_config 失败: {e}, 重新初始化")

    # 初始化
    cfg = {
        "version": 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "updated": datetime.now(timezone.utc).isoformat(),
        "regime": "unknown",
        "factors": {},
    }
    for fname, fdef in DEFAULT_FACTORS.items():
        cfg["factors"][fname] = {**fdef, "ic_history": []}
    save_factor_config(cfg)
    return cfg


def save_factor_config(cfg: dict):
    """持久化因子配置。"""
    _ensure_dir()
    cfg["updated"] = datetime.now(timezone.utc).isoformat()
    path = META_DIR / "factor_config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def get_current_weights(cfg: dict = None) -> dict[str, float]:
    """返回当前生效的因子权重 (归一化到 sum=1)。"""
    if cfg is None:
        cfg = load_factor_config()
    raw = {k: v["weight"] for k, v in cfg["factors"].items()}
    total = sum(raw.values())
    if total <= 0:
        total = 1
    return {k: v / total for k, v in raw.items()}


# ====================================================================
#  快照: 每次筛选后保存
# ====================================================================

def save_snapshot(scored_coins: list[dict], factor_weights: dict):
    """保存本次筛选快照, 用于未来回测。"""
    _ensure_dir()
    ts = datetime.now(timezone.utc)
    snapshot = {
        "timestamp": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
        "factor_weights": factor_weights,
        "coins": [],
    }
    for c in scored_coins[:200]:  # 只保存 top 200 节省空间
        snapshot["coins"].append({
            "symbol": c["symbol"],
            "price": c["price"],
            "market_cap": c["market_cap"],
            "composite_score": c["composite_score"],
            # 保存各因子原始值
            **{k: v for k, v in c.items()
               if k.startswith("f_")},
        })
    path = META_DIR / f"snapshot_{ts.strftime('%Y%m%d_%H%M')}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    log.info(f"快照已保存: {path.name} ({len(snapshot['coins'])} coins)")
    return path


# ====================================================================
#  回测: 计算因子 IC
# ====================================================================

def _spearman_rank_corr(x: list[float], y: list[float]) -> float:
    """Spearman 秩相关系数 (不依赖 scipy)。"""
    n = len(x)
    if n < 5:
        return 0.0

    def _rank(arr):
        indexed = sorted(enumerate(arr), key=lambda t: t[1])
        ranks = [0.0] * n
        for rank_val, (orig_idx, _) in enumerate(indexed):
            ranks[orig_idx] = rank_val + 1
        return ranks

    rx = _rank(x)
    ry = _rank(y)
    d_sq = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - (6 * d_sq) / (n * (n * n - 1))


def run_ic_backtest(lookback_days: int = 7) -> dict:
    """回测: 找到 N 天前的快照, 对比当前价格, 计算每个因子的 IC。

    IC = spearman(因子值_t, 收益率_t+N)
    """
    _ensure_dir()
    target_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # 找最近的快照
    snapshots = sorted(META_DIR.glob("snapshot_*.json"))
    if not snapshots:
        return {"ok": False, "error": "无历史快照"}

    # 找距离 target_date 最近的快照
    best_snap = None
    best_diff = float("inf")
    for sp in snapshots:
        try:
            data = json.loads(sp.read_text())
            snap_date = datetime.fromisoformat(data["timestamp"])
            diff = abs((snap_date - target_date).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_snap = data
        except Exception:
            continue

    if not best_snap or best_diff > 3 * 86400:  # 容差 3 天
        return {"ok": False, "error": f"未找到 {lookback_days} 天前的快照"}

    # 需要当前价格来计算收益率 — 从最新快照取
    # (实际使用时应从 API 获取, 这里用最新快照近似)
    latest_snap = None
    try:
        latest_snap = json.loads(snapshots[-1].read_text())
    except Exception:
        return {"ok": False, "error": "无法读取最新快照"}

    current_prices = {c["symbol"]: c["price"]
                      for c in latest_snap.get("coins", [])}

    # 计算每个因子的 IC
    factor_names = [k for k in best_snap["coins"][0].keys()
                    if k.startswith("f_")]
    ic_results = {}

    for fname in factor_names:
        factor_vals = []
        returns = []
        for coin in best_snap["coins"]:
            sym = coin["symbol"]
            old_price = coin.get("price", 0)
            new_price = current_prices.get(sym, 0)
            if old_price > 0 and new_price > 0:
                ret = (new_price - old_price) / old_price
                fval = coin.get(fname, 0)
                factor_vals.append(fval)
                returns.append(ret)

        if len(factor_vals) >= 10:
            ic = _spearman_rank_corr(factor_vals, returns)
            ic_results[fname] = round(ic, 4)

    return {
        "ok": True,
        "lookback_days": lookback_days,
        "snapshot_date": best_snap.get("date"),
        "matched_coins": len(factor_vals) if factor_vals else 0,
        "factor_ic": ic_results,
    }


# ====================================================================
#  元学习: 自动调权
# ====================================================================

def update_weights_from_ic(ic_result: dict) -> dict:
    """根据回测 IC 更新因子权重。

    策略:
    - IC > 0: 因子有效, 按 IC 大小分配权重
    - IC < 0: 因子反向有效 (考虑反转或降权)
    - IC ≈ 0: 因子无效, 缩减权重
    - 加入 IC 衰减: 越早的 IC 影响越小
    """
    if not ic_result.get("ok"):
        return {"ok": False, "error": ic_result.get("error")}

    cfg = load_factor_config()
    factor_ic = ic_result.get("factor_ic", {})

    # 映射因子名: f_momentum_30d → momentum_30d
    ic_map = {}
    for raw_name, ic_val in factor_ic.items():
        clean = raw_name.replace("f_", "", 1)
        ic_map[clean] = ic_val

    updated_count = 0
    for fname, fdata in cfg["factors"].items():
        ic_val = ic_map.get(fname)
        if ic_val is None:
            continue

        # 记录 IC 历史
        fdata.setdefault("ic_history", [])
        fdata["ic_history"].append({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "ic": ic_val,
            "lookback": ic_result.get("lookback_days", 7),
        })
        # 只保留最近 30 条
        fdata["ic_history"] = fdata["ic_history"][-30:]

        # 计算加权平均 IC (指数衰减)
        history = fdata["ic_history"]
        if len(history) < MIN_IC_RECORDS:
            continue

        weighted_ic = 0.0
        weight_sum = 0.0
        for i, h in enumerate(reversed(history)):
            w = IC_DECAY ** i
            weighted_ic += h["ic"] * w
            weight_sum += w
        avg_ic = weighted_ic / weight_sum if weight_sum > 0 else 0

        # 根据 IC 调整权重
        old_w = fdata["weight"]
        # IC → 权重调整: IC=0.3 → 大幅加权, IC=0 → 不变, IC=-0.1 → 降权
        adjustment = 1.0 + avg_ic * 0.5  # IC=0.3 → 1.15x, IC=-0.2 → 0.9x
        new_w = old_w * adjustment
        # clamp
        new_w = max(fdata["min"], min(fdata["max"], new_w))
        fdata["weight"] = round(new_w, 4)

        if abs(new_w - old_w) > 0.001:
            log.info(f"  {fname}: IC={avg_ic:+.3f} → 权重 {old_w:.3f} → {new_w:.3f}")
            updated_count += 1

    # 归一化
    total = sum(f["weight"] for f in cfg["factors"].values())
    for f in cfg["factors"].values():
        f["weight"] = round(f["weight"] / total, 4)

    save_factor_config(cfg)

    return {
        "ok": True,
        "factors_updated": updated_count,
        "new_weights": get_current_weights(cfg),
    }


# ====================================================================
#  Regime Detection: 市场状态识别
# ====================================================================

def detect_regime(btc_data: dict = None) -> str:
    """根据 BTC 数据判断市场 regime。

    返回: "bull" / "bear" / "sideways" / "volatile"
    """
    if not btc_data:
        return "unknown"

    chg_30d = btc_data.get("change_30d", 0)
    chg_7d = btc_data.get("change_7d", 0)
    # 简单判断逻辑
    if chg_30d > 15 and chg_7d > 3:
        regime = "bull"
    elif chg_30d < -15 and chg_7d < -3:
        regime = "bear"
    elif abs(chg_30d) < 8 and abs(chg_7d) < 3:
        regime = "sideways"
    else:
        regime = "volatile"

    return regime


def apply_regime_adjustment(weights: dict[str, float],
                            regime: str) -> dict[str, float]:
    """根据市场 regime 微调因子权重。

    - bull: 动量因子加强, TVL 因子减弱
    - bear: TVL/drawdown 因子加强, 动量减弱
    - sideways: 均衡
    - volatile: 成交量因子加强
    """
    if regime == "unknown":
        return weights

    # regime → 因子乘数
    regime_mults = {
        "bull": {
            "momentum_30d": 1.3, "momentum_7d": 1.2,
            "tvl_mcap": 0.8, "ath_drawdown": 0.7,
            "narrative_heat": 1.2,
        },
        "bear": {
            "momentum_30d": 0.6, "momentum_7d": 0.7,
            "tvl_mcap": 1.4, "ath_drawdown": 1.3,
            "onchain_activity": 1.2, "dev_activity": 1.2,
        },
        "sideways": {
            "tvl_mcap": 1.1, "volume_turnover": 1.1,
            "dev_activity": 1.1,
        },
        "volatile": {
            "volume_turnover": 1.3, "funding_rate": 1.3,
            "momentum_30d": 0.9, "momentum_7d": 0.9,
        },
    }

    mults = regime_mults.get(regime, {})
    adjusted = {}
    for k, v in weights.items():
        adjusted[k] = v * mults.get(k, 1.0)

    # 归一化
    total = sum(adjusted.values())
    return {k: round(v / total, 4) for k, v in adjusted.items()}


# ====================================================================
#  因子健康报告
# ====================================================================

def generate_factor_report() -> dict:
    """生成因子池健康状态报告。"""
    cfg = load_factor_config()
    report = {
        "regime": cfg.get("regime", "unknown"),
        "total_factors": len(cfg["factors"]),
        "factors": {},
    }

    for fname, fdata in cfg["factors"].items():
        history = fdata.get("ic_history", [])
        avg_ic = 0
        if history:
            avg_ic = sum(h["ic"] for h in history[-10:]) / len(history[-10:])

        status = "healthy"
        if len(history) >= MIN_IC_RECORDS:
            if avg_ic < -0.05:
                status = "weak_negative"
            elif abs(avg_ic) < 0.02:
                status = "noisy"
            elif avg_ic > 0.15:
                status = "strong"

        report["factors"][fname] = {
            "weight": fdata["weight"],
            "avg_ic_10": round(avg_ic, 4),
            "ic_records": len(history),
            "status": status,
        }

    return report
