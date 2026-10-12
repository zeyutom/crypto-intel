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
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("meta_learner", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"

# ── 默认因子配置 ──
DEFAULT_FACTORS = {
    # 因子名: {weight, min_weight, max_weight, enabled, ic_history: []}
    # enabled=False 的因子 (死因子: 截面常数) 权重强制 0, 不参与归一化
    # locked=True 的因子是人工关闭 (如方向为负, 而权重只能为正), 不会被自动复活
    "momentum_30d":    {"weight": 0.18, "min": 0.05, "max": 0.30, "enabled": True},
    "momentum_7d":     {"weight": 0.10, "min": 0.03, "max": 0.20, "enabled": True},
    "ath_drawdown":    {"weight": 0.10, "min": 0.03, "max": 0.20, "enabled": True},
    "volume_turnover": {"weight": 0.10, "min": 0.03, "max": 0.20, "enabled": True},
    "tvl_mcap":        {"weight": 0.15, "min": 0.05, "max": 0.30, "enabled": True},
    "market_cap_size": {"weight": 0.05, "min": 0.02, "max": 0.15, "enabled": True},
    # 新增因子
    "onchain_activity":{"weight": 0.10, "min": 0.03, "max": 0.25, "enabled": True},
    "dev_activity":    {"weight": 0.07, "min": 0.02, "max": 0.20, "enabled": True},
    "funding_rate":    {"weight": 0.07, "min": 0.02, "max": 0.15, "enabled": True},
    "narrative_heat":  {"weight": 0.08, "min": 0.02, "max": 0.20, "enabled": True},
}

# 回测周期 (天)
LOOKBACK_DAYS = [7, 14, 30]
# IC 衰减因子 (越老的 IC 权重越低)
IC_DECAY = 0.85
# 最少需要多少次 IC 记录才开始自动调权
MIN_IC_RECORDS = 3
# 显著性地板: |加权 IC| 低于此值视作统计噪声, 不触发调权
# (常数/并列因子的 IC 为 NaN → 记为 degenerate 不进历史, 这里再挡一层弱信号)
IC_SIGNIFICANCE_FLOOR = 0.02
# IC 回测只用"真实"快照: 币数不足 / 回填合成的快照会引入幸存者偏差与 null 因子
MIN_SNAPSHOT_COINS = 100
# 旧快照里有多少比例的币在最新快照中拿不到价格 → 超过则告警 (截断/脱落偏差)
DROPOUT_WARN_RATE = 0.15
# 权重投影到 [min,max] 的最大迭代次数
WEIGHT_PROJECT_ITERS = 20
# 快照 schema 版本 (2 = 全宇宙 + coin_id + _meta)
SNAPSHOT_SCHEMA = 2
# 单一权重写入方: 只有设置了该环境变量 (=1) 的环境才会改写 factor_config.json 的权重。
# 曾经本地 launchd 和云端 nightly 各自调权, 两套权重分叉 (同日 Top-20 只重合 9/20),
# 每天推送的榜单在两套模型之间来回切换。云端 nightly.yml 设置该变量, 本地默认只读。
WEIGHT_WRITER_ENV = "CRYPTO_INTEL_WEIGHT_WRITER"


def is_weight_writer() -> bool:
    """当前环境是否允许改写因子权重 (见 WEIGHT_WRITER_ENV)。"""
    return os.environ.get(WEIGHT_WRITER_ENV, "").strip().lower() in ("1", "true", "yes")


def _is_num(v) -> bool:
    """有限数值 (排除 None / bool / NaN / inf)。"""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _json_num(v):
    """NaN / inf 落盘前转 null, 其它原样返回 (JSON 不允许 NaN)。"""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _json_sanitize(obj):
    """递归把 dict / list 里的 NaN / inf 换成 null (调用方 meta、权重也可能带 NaN)。"""
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_sanitize(v) for v in obj]
    return _json_num(obj)


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
            # 旧版 config 没有 enabled 字段 → 回填为 True
            for fdata in cfg["factors"].values():
                fdata.setdefault("enabled", True)
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
    """返回当前生效的因子权重 (归一化到 sum=1)。

    被禁用 (enabled=False) 的死因子权重为 0, 且不参与归一化。
    """
    if cfg is None:
        cfg = load_factor_config()
    raw = {k: (v["weight"] if v.get("enabled", True) else 0.0)
           for k, v in cfg["factors"].items()}
    total = sum(raw.values())
    if total <= 0:
        total = 1
    return {k: v / total for k, v in raw.items()}


# ====================================================================
#  快照: 每次筛选后保存
# ====================================================================

def _factor_stats(coins: list[dict]) -> dict:
    """每个 f_* 因子的截面诊断: 取值个数 + 非零占比 (用于发现死因子)。

    nonzero_share 的分母是该因子有数值的币数 (null 不计)。
    """
    stats = {}
    names = sorted({k for c in coins for k in c if k.startswith("f_")})
    for raw in names:
        vals = [c.get(raw) for c in coins]
        vals = [v for v in vals if _is_num(v)]
        nonzero = sum(1 for v in vals if v != 0)
        stats[raw.replace("f_", "", 1)] = {
            "nunique": len(set(vals)),
            "nonzero_share": round(nonzero / len(vals), 4) if vals else 0.0,
        }
    return stats


def save_snapshot(scored_coins: list[dict], factor_weights: dict,
                  meta: dict | None = None):
    """保存本次筛选快照, 用于未来回测。

    保存**完整**评分宇宙 (不截断): 只存 top-200 时, 掉出前 200 的币在
    后续快照里没有价格, IC / Top-N 评估会因截断产生幸存者偏差。
    """
    _ensure_dir()
    ts = datetime.now(timezone.utc)
    snapshot = {
        "timestamp": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
        "factor_weights": factor_weights,
        "_meta": {},
        "coins": [],
    }
    for c in scored_coins:
        snapshot["coins"].append({
            "symbol": c["symbol"],
            "coin_id": c.get("coin_id", ""),
            "price": _json_num(c["price"]),
            "market_cap": _json_num(c["market_cap"]),
            "composite_score": _json_num(c["composite_score"]),
            # 原始涨跌幅 (此前漏存, 导致 dashboard 的 30d 列恒为 +0.0%)
            "change_24h": _json_num(c.get("change_24h")),
            "change_7d": _json_num(c.get("change_7d")),
            "change_30d": _json_num(c.get("change_30d")),
            # 保存各因子原始值
            **{k: _json_num(v) for k, v in c.items()
               if k.startswith("f_")},
        })
    # 调用方附加的 meta 先放入, 再写入本函数负责的权威字段
    snapshot["_meta"].update(meta or {})
    snapshot["_meta"].update({
        "universe_size": len(snapshot["coins"]),
        "truncated": False,
        "schema": SNAPSHOT_SCHEMA,
        "factor_stats": _factor_stats(snapshot["coins"]),
    })
    path = META_DIR / f"snapshot_{ts.strftime('%Y%m%d_%H%M')}.json"
    # allow_nan=False 是结构性保险: 任何漏网的 NaN 都会在这里抛错而不是写成非法 JSON
    path.write_text(json.dumps(_json_sanitize(snapshot), ensure_ascii=False,
                               indent=2, allow_nan=False),
                    encoding="utf-8")
    log.info(f"快照已保存: {path.name} ({len(snapshot['coins'])} coins)")
    return path


# ====================================================================
#  回测: 计算因子 IC
# ====================================================================

def _spearman_rank_corr(x: list[float], y: list[float]) -> float:
    """Spearman 秩相关系数 (不依赖 scipy)。

    使用平均秩处理并列值 (tie-aware)。零方差 (常数列) 返回 NaN:
    Spearman 在此情形下未定义, 返回 0.0 会把死因子伪装成"无信息但正常",
    随后归一化时被动抬高其权重 (funding_rate/dev_activity 曾占到 ~30%)。

    含 None 的配对 (如回填快照缺因子值) 成对剔除后再计算; 配对 < 5 返回 0.0。
    """
    pairs = [(a, b) for a, b in zip(x, y) if a is not None and b is not None]
    if len(pairs) != len(x):
        x = [a for a, _ in pairs]
        y = [b for _, b in pairs]
    n = len(x)
    if n < 5:
        return 0.0

    def _rank(arr):
        order = sorted(range(n), key=lambda i: arr[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and arr[order[j + 1]] == arr[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0   # average of tied positions (1-based)
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx = _rank(x)
    ry = _rank(y)
    # zero-variance guard: a constant column has no rank spread -> undefined corr
    if len(set(rx)) < 2 or len(set(ry)) < 2:
        return float("nan")
    # Pearson correlation of average ranks (correct, tie-aware Spearman)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    denom = (vx * vy) ** 0.5
    return cov / denom if denom > 0 else 0.0


def _load_valid_snapshot(path: Path) -> dict | None:
    """读取一份可用于 IC 回测的快照; 不合格返回 None。

    跳过: 回填合成快照 (文件名 *_0000.json, 或 _meta.synthetic /
    _meta.source == "backfill"), 币数 < MIN_SNAPSHOT_COINS 的残缺快照,
    以及前 50 个币里任一 f_* 为 null 的快照 (因子缺失会扭曲截面 IC)。
    """
    if path.name.endswith("_0000.json"):
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    meta = data.get("_meta") or {}
    if meta.get("synthetic") or meta.get("source") == "backfill":
        return None
    coins = data.get("coins") or []
    if len(coins) < MIN_SNAPSHOT_COINS:
        return None
    for c in coins[:50]:
        for k, v in c.items():
            if k.startswith("f_") and not _is_num(v):
                return None
    return data


def run_ic_backtest(lookback_days: int = 7) -> dict:
    """回测: 找到 N 天前的快照, 对比当前价格, 计算每个因子的 IC。

    IC = spearman(因子值_t, 收益率_t+N)

    仅使用真实、完整的快照 (见 _load_valid_snapshot), 旧快照与价格源
    ("latest") 快照都做同样过滤。

    返回的 factor_ic 只含有限数值 (下游 cli 直接 abs(ic) / ic > 0.05, 放 None
    会崩); IC 未定义 (NaN: 因子列或收益率列常数) 的因子不进 factor_ic, 而是
    列在 degenerate_factors 里; 旧快照全截面常数的因子另列在 constant_factors,
    供 update_weights_from_ic 判定死因子。
    """
    _ensure_dir()
    target_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # 找最近的快照 (只保留合格的)
    all_files = sorted(META_DIR.glob("snapshot_*.json"))
    if not all_files:
        return {"ok": False, "error": "无历史快照"}
    snapshots = []   # [(path, data, ts)]
    for sp in all_files:
        data = _load_valid_snapshot(sp)
        if data is None:
            continue
        try:
            snap_ts = datetime.fromisoformat(data["timestamp"])
        except Exception:
            continue
        # naive 时间戳按 UTC 处理 (与 returns_tracker 一致), 否则与 target_date 相减会 TypeError
        if snap_ts.tzinfo is None:
            snap_ts = snap_ts.replace(tzinfo=timezone.utc)
        snapshots.append((sp, data, snap_ts))
    skipped = len(all_files) - len(snapshots)
    if skipped:
        log.info(f"IC 回测跳过 {skipped} 份合成/残缺快照")
    if not snapshots:
        return {"ok": False, "error": "无合格的真实快照 (全部为回填/残缺)"}

    # 需要当前价格来计算收益率 — 从最新合格快照取
    # (实际使用时应从 API 获取, 这里用最新快照近似)
    latest_file, latest_snap, latest_ts = snapshots[-1]
    current_prices = {c["symbol"]: c.get("price")
                      for c in latest_snap.get("coins", [])}

    # 找距离 target_date 最近的快照 (排除最新快照本身, 避免自配对 old==new)
    best_snap = None
    best_diff = float("inf")
    best_ts = None
    best_file = None
    for sp, data, snap_ts in snapshots[:-1]:
        diff = abs((snap_ts - target_date).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best_snap = data
            best_ts = snap_ts
            best_file = sp

    if not best_snap or best_diff > 3 * 86400:  # 容差 3 天
        return {"ok": False, "error": f"未找到 {lookback_days} 天前的快照"}

    # 校验实际持有期: 价格源快照必须比因子快照明显更新
    realized_days = (latest_ts - best_ts).total_seconds() / 86400.0
    if realized_days < 0.5 * lookback_days:
        return {"ok": False,
                "error": f"实际跨度 {realized_days:.1f}d 远小于 {lookback_days}d, 拒绝自配对"}

    # 配对: 旧快照每个币在最新快照里的价格 → 收益率
    old_coins = best_snap["coins"]
    matched = []   # [(coin, ret)]
    for coin in old_coins:
        old_price = coin.get("price")
        new_price = current_prices.get(coin["symbol"])
        if _is_num(old_price) and _is_num(new_price) and old_price > 0 and new_price > 0:
            matched.append((coin, (new_price - old_price) / old_price))
    dropout_rate = (1.0 - len(matched) / len(old_coins)) if old_coins else 1.0
    if dropout_rate > DROPOUT_WARN_RATE:
        log.warning(f"IC 回测脱落率 {dropout_rate:.1%} > {DROPOUT_WARN_RATE:.0%}: "
                    f"{best_file.name} 中有大量币在 {latest_file.name} 里无价格, "
                    f"IC 可能带幸存者偏差")

    # 计算每个因子的 IC
    factor_names = [k for k in old_coins[0].keys() if k.startswith("f_")]
    ic_results = {}
    constant_factors = []
    degenerate_factors = []

    for fname in factor_names:
        # 旧快照全截面是否常数 (死因子判据, 与是否配对无关)
        all_vals = {coin.get(fname) for coin in old_coins
                    if _is_num(coin.get(fname))}
        if len(all_vals) < 2:
            constant_factors.append(fname)

        factor_vals = []
        returns = []
        for coin, ret in matched:
            fval = coin.get(fname)
            # 回填的合成快照里缺失因子是 null → 跳过, 不参与 IC
            if not _is_num(fval):
                continue
            factor_vals.append(fval)
            returns.append(ret)

        if len(factor_vals) >= 10:
            ic = _spearman_rank_corr(factor_vals, returns)
            # 常数列 → NaN → 不进 factor_ic (JSON 不写 NaN, 下游也不用防 None), 单独列出
            if math.isnan(ic):
                degenerate_factors.append(fname)
            else:
                ic_results[fname] = round(ic, 4)

    return {
        "ok": True,
        "lookback_days": lookback_days,
        "snapshot_date": best_snap.get("date"),
        "snapshot_file": best_file.name,
        "latest_file": latest_file.name,
        "matched_coins": len(matched),
        "dropout_rate": round(dropout_rate, 4),
        "skipped_snapshots": skipped,
        "factor_ic": ic_results,
        "degenerate_factors": degenerate_factors,
        "constant_factors": constant_factors,
    }


# ====================================================================
#  元学习: 自动调权
# ====================================================================

def _normalize_and_project(factors: dict) -> bool:
    """对 enabled 因子: 归一化到 sum=1, 再迭代投影到各自 [min,max]。

    旧实现先 clamp 再归一化, 归一化会把权重推出边界 (实测 117 次违约)。
    这里反过来: 归一化 → clamp 越界者 → 未被 clamp 的按比例重分配使总和为 1,
    重复直到无新越界 (最多 WEIGHT_PROJECT_ITERS 轮)。被禁用的因子权重保持 0。

    返回边界是否在 1e-6 内成立 (不成立时只告警, 不抛异常)。
    """
    enabled = {k: v for k, v in factors.items() if v.get("enabled", True)}
    for k, v in factors.items():
        if k not in enabled:
            v["weight"] = 0.0
    if not enabled:
        log.warning("所有因子均被禁用, 无法归一化权重")
        return False

    total = sum(v["weight"] for v in enabled.values())
    if total <= 0:
        w = {k: 1.0 / len(enabled) for k in enabled}
    else:
        w = {k: v["weight"] / total for k, v in enabled.items()}

    # 每轮: clamp 全部 → 算缺口 (1 - sum) → 把缺口按比例分给还有余量的因子
    # (缺口为正只分给未顶到 max 的, 为负只分给未跌到 min 的), 直到缺口归零
    eps = 1e-12
    for _ in range(WEIGHT_PROJECT_ITERS):
        for k, v in enabled.items():
            w[k] = max(v["min"], min(v["max"], w[k]))
        deficit = 1.0 - sum(w.values())
        if abs(deficit) < 1e-9:
            break
        if deficit > 0:
            cand = [k for k, v in enabled.items() if w[k] < v["max"] - eps]
        else:
            cand = [k for k, v in enabled.items() if w[k] > v["min"] + eps]
        if not cand:
            break   # 所有因子都顶在边界上, min/max 组合不可行
        base = sum(w[k] for k in cand)
        for k in cand:
            share = w[k] / base if base > 0 else 1.0 / len(cand)
            w[k] += deficit * share

    ok = (abs(sum(w.values()) - 1.0) < 1e-6 and
          all(v["min"] - 1e-6 <= w[k] <= v["max"] + 1e-6
              for k, v in enabled.items()))
    if not ok:
        log.warning(f"权重投影后仍越界/不归一 (min/max 组合不可行?): "
                    f"{ {k: round(x, 4) for k, x in w.items()} }")
    for k in enabled:
        enabled[k]["weight"] = round(w[k], 6)
    return ok


def update_weights_from_ic(ic_result: dict, force: bool = False) -> dict:
    """根据回测 IC 更新因子权重。

    策略:
    - IC > 0: 因子有效, 按 IC 大小分配权重
    - IC < 0: 因子反向有效 (考虑反转或降权)
    - IC ≈ 0: 因子无效, 缩减权重
    - 加入 IC 衰减: 越早的 IC 影响越小
    - IC 未定义 (NaN: 常数列) 的因子本轮 degenerate: 不进 IC 历史, 若同时在
      旧快照里是截面常数 → 判定死因子, 权重 0 + enabled=False; 恢复变化后
      以 min 权重重新启用 (归一化/投影后保证 ≥ min, 不一定恰等于 min)
    - 每 UTC 日最多调一次权重 (daily-screen / adaptive_trainer / nightly
      都会调用本函数, 多次复利会让权重震荡); 同日同 lookback 的 IC 记录覆盖
      而非追加。force=True 跳过当日守卫

    degenerate 判据 (三者任一): 在 ic_result["degenerate_factors"] 里 /
    在 ic_result["constant_factors"] 里 / factor_ic 里的值是 None 或 NaN
    (run_ic_backtest 自身不会在 factor_ic 放 None, 兼容手工构造的结果)。

    非权重写入方 (未设置 WEIGHT_WRITER_ENV) 直接返回 skipped="not_weight_writer",
    不读写 factor_config.json; force 不能绕过这一条。
    locked=True 的因子始终保持关闭, 不会因 IC 恢复而被复活。
    """
    if not ic_result.get("ok"):
        return {"ok": False, "error": ic_result.get("error")}

    if not is_weight_writer():
        cfg = load_factor_config()
        log.info(f"本机不是权重写入方 ({WEIGHT_WRITER_ENV} 未设置), 不改写因子权重")
        return {
            "ok": True,
            "skipped": "not_weight_writer",
            "factors_updated": 0,
            "disabled": [k for k, v in cfg["factors"].items()
                         if not v.get("enabled", True)],
            "new_weights": get_current_weights(cfg),
        }

    cfg = load_factor_config()
    factor_ic = ic_result.get("factor_ic", {})
    lookback = ic_result.get("lookback_days", 7)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 映射因子名: f_momentum_30d → momentum_30d (只保留有效数值 IC)
    ic_map = {}
    degenerate = set()
    for raw_name, ic_val in factor_ic.items():
        clean = raw_name.replace("f_", "", 1)
        if _is_num(ic_val):
            ic_map[clean] = ic_val
        else:
            degenerate.add(clean)
    constant = {raw.replace("f_", "", 1)
                for raw in ic_result.get("constant_factors", [])}
    degenerate |= {raw.replace("f_", "", 1)
                   for raw in ic_result.get("degenerate_factors", [])}
    degenerate |= constant
    # 同一因子既有有效 IC 又被标 degenerate 是矛盾输入, 以 degenerate 为准
    for fname in degenerate:
        ic_map.pop(fname, None)

    # ── Phase A: 刷新 IC 历史 (无论是否触发调权) ──
    for fname, fdata in cfg["factors"].items():
        fdata.setdefault("ic_history", [])
        fdata.setdefault("enabled", True)
        fdata.setdefault("degenerate_runs", 0)
        if fname not in ic_map:
            continue
        ic_val = ic_map[fname]
        fdata["degenerate_runs"] = 0

        # 记录 IC 历史: 同日同 lookback 覆盖, 避免一天多次调用堆积重复记录
        entry = {"date": today, "ic": ic_val, "lookback": lookback}
        history = fdata["ic_history"]
        for i, h in enumerate(history):
            if h.get("date") == today and h.get("lookback") == lookback:
                history[i] = entry
                break
        else:
            history.append(entry)
        # 只保留最近 30 条
        fdata["ic_history"] = history[-30:]

    # ── 当日守卫: 今天已调过权重 → 只刷新 IC 记录, 不再动权重 ──
    if cfg.get("last_weight_update_date") == today and not force:
        save_factor_config(cfg)
        log.info(f"权重今日 ({today}) 已更新过, 本次仅刷新 IC 记录")
        return {
            "ok": True,
            "skipped": "already_updated_today",
            "factors_updated": 0,
            "disabled": [k for k, v in cfg["factors"].items()
                         if not v.get("enabled", True)],
            "new_weights": get_current_weights(cfg),
        }

    # ── Phase B: 死因子禁用 / 复活 ──
    # old_weights 在禁用之前取, 这样被清零的因子也计入 factors_updated
    old_weights = {k: v["weight"] for k, v in cfg["factors"].items()}
    for fname, fdata in cfg["factors"].items():
        if fdata.get("locked", False):
            # 人工锁定的因子: 无论 IC 如何都保持关闭
            fdata["enabled"] = False
            fdata["weight"] = 0.0
            continue
        if fname in degenerate:
            # 守卫之后才计数 → 按"调权轮次"而非按调用次数 (一天可被调 3 次)
            fdata["degenerate_runs"] += 1
            # 本轮 degenerate 且旧快照截面常数 → 死因子, 权重强制 0
            if fname in constant and fdata.get("enabled", True):
                log.warning(f"  {fname}: 截面常数 (IC 未定义), 禁用并清零权重")
            if fname in constant:
                fdata["weight"] = 0.0
                fdata["enabled"] = False
        elif fname in ic_map and not fdata.get("enabled", True):
            # 因子重新有变化 (IC 有效) → 以 min 权重复活
            fdata["enabled"] = True
            fdata["weight"] = fdata["min"]
            log.info(f"  {fname}: 恢复变化, 以 min 权重 {fdata['min']:.3f} 重新启用")

    # ── Phase C: IC 乘数 → 归一化 → 投影到 [min,max] ──
    for fname, fdata in cfg["factors"].items():
        if not fdata.get("enabled", True) or fname not in ic_map:
            continue

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

        # 显著性门槛: 弱于噪声地板的 IC 不调权 (防止无信息因子被随机 IC 推动)
        if abs(avg_ic) < IC_SIGNIFICANCE_FLOOR:
            continue

        # 根据 IC 调整权重 (边界由归一化后的投影统一处理)
        # IC → 权重调整: IC=0.3 → 大幅加权, IC=0 → 不变, IC=-0.1 → 降权
        adjustment = 1.0 + avg_ic * 0.5  # IC=0.3 → 1.15x, IC=-0.2 → 0.9x
        fdata["weight"] = fdata["weight"] * adjustment
        log.info(f"  {fname}: IC={avg_ic:+.3f} → 乘数 x{adjustment:.3f}")

    _normalize_and_project(cfg["factors"])

    updated_count = 0
    for fname, fdata in cfg["factors"].items():
        if abs(fdata["weight"] - old_weights[fname]) > 0.001:
            log.info(f"  {fname}: 权重 {old_weights[fname]:.3f} → {fdata['weight']:.3f}")
            updated_count += 1

    cfg["last_weight_update_date"] = today
    save_factor_config(cfg)

    return {
        "ok": True,
        "factors_updated": updated_count,
        "disabled": [k for k, v in cfg["factors"].items()
                     if not v.get("enabled", True)],
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
        if fdata.get("locked", False):
            status = "locked"     # 人工关闭, 不会自动复活
        elif not fdata.get("enabled", True):
            status = "disabled"   # 死因子 (截面常数), 权重 0

        report["factors"][fname] = {
            "weight": fdata["weight"],
            "enabled": fdata.get("enabled", True),
            "locked": fdata.get("locked", False),
            "avg_ic_10": round(avg_ic, 4),
            "ic_records": len(history),
            "status": status,
        }

    return report
