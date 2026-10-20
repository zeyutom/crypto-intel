"""因子元学习引擎 — 持续优化因子池。

核心机制:
  1. 每次筛选后保存 snapshot (全宇宙因子值 + 排名, schema 2)
  2. N 天后回测: 计算每个因子的 IC (Information Coefficient)
     IC = rank_correlation(因子值, 未来N天收益率), 按 coin_id 配对
  3. 每条 IC 记录带来源标签 (asof/base/base_schema/n), 只有"旧快照为全宇宙"
     (base_schema ≥ 2) 且 30 天内的记录参与学习, 按日历天数衰减
  4. 权重 = 向"目标权重"平滑靠拢, 而不是每天复利:
       目标 = prior × max(0, 1 + K × 收缩后的 IC 信号), 投影到 [min,max]
       新权重 = (1-α)·旧权重 + α·目标, α = 1/lookback
     IC 信号不显著 (样本少 / |信号| < 地板) 时目标就是 prior, 权重缓慢回到 prior
  5. 权重只能为正, 不会自动翻转方向。死因子 (截面常数) / 低覆盖因子 (绝大多数币
     无数据) 自动关闭; 不重叠窗口 IC 的 t 值持续 < -2 的因子自动关闭; 条件恢复后
     以 min 权重重新启用。人工 locked 的因子永不自动复活。

数据存储: data/meta/ 目录, JSON 格式
"""
from __future__ import annotations
import json
import math
import os
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("meta_learner", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"

# ── 默认因子配置 ──
DEFAULT_FACTORS = {
    # 因子名: {weight, min_weight, max_weight, enabled, ic_history: []}
    # weight 同时是新因子的 prior (无证据时权重回归的目标)
    # enabled=False 的因子权重强制 0, 不参与归一化; disabled_reason 记录原因
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

# ── 学习规则参数 (2026-10 审计后重写, 见 update_weights_from_ic) ──
# 最少需要多少条"合格" IC 记录才产生信号 (不足时目标 = prior)
MIN_IC_RECORDS = 3
# 显著性地板: |收缩后 IC 信号| 低于此值视作噪声, 目标 = prior
IC_SIGNIFICANCE_FLOOR = 0.02
# IC 记录按日历天数衰减 (半衰期约 6.6 天)。旧实现按条目位置衰减, 6 月的 24 条
# 截断偏差记录隔了 4 个月仍占 37% 的衰减权重, 把动量因子压到下限。
IC_DECAY_PER_DAY = 0.90
# 超过此天数的 IC 记录不参与学习
IC_MAX_AGE_DAYS = 30
# IC 历史保留天数 / 条数上限 (超过的直接从 ic_history 删除)
IC_HISTORY_KEEP_DAYS = 45
IC_HISTORY_MAX_ENTRIES = 60
# 只有"旧快照"为全宇宙 schema 2 的 IC 记录参与学习 (schema 1 只存前 200 → 截断偏差)
IC_MIN_BASE_SCHEMA = 2
# 收缩: 信号 = 加权平均 IC × n_eff/(n_eff+K), n_eff = 记录覆盖的独立 (不重叠) 窗口数。
# 7 日前瞻收益每天评一次, 相邻记录 6/7 重叠, 7 条日记录只相当于 ~1.9 个独立观测。
IC_SHRINK_K = 3.0
# 目标权重 = prior × max(0, 1 + K × 信号): 信号 +0.05 → 1.2x, -0.1 → 0.6x, -0.25 → 0
IC_TO_WEIGHT_K = 4.0
# 负 IC 自动关闭: 不重叠窗口数 ≥ 4 (约一个月) 且 t < -2 → 关闭; t > -1 → 以 min 复活
NEG_IC_MIN_WINDOWS = 4
NEG_IC_T_DISABLE = -2.0
NEG_IC_T_REENABLE = -1.0
# 低覆盖因子: 旧快照里有非零数值的币数 < max(30, 20% 宇宙) → 本轮视作退化, 自动关闭
# (onchain_activity 只有 11/400 个币非零、narrative_heat 37/400, IC 实际在测"是不是头部 L1")
MIN_COVERAGE_COINS = 30
MIN_COVERAGE_SHARE = 0.20
# IC 回测只用"真实"快照: 币数不足 / 回填合成的快照会引入幸存者偏差与 null 因子
MIN_SNAPSHOT_COINS = 100
# 旧快照里有多少比例的币在最新快照中拿不到价格 → 超过则告警 (截断/脱落偏差)
DROPOUT_WARN_RATE = 0.15
# 权重投影到 [min,max] 的最大迭代次数
WEIGHT_PROJECT_ITERS = 20
# 快照 schema 版本 (2 = 全宇宙 + coin_id + _meta)
SNAPSHOT_SCHEMA = 2
# factor_config 版本: 2 = IC 记录带来源标签 + 每个因子有 prior + disabled_reason
CONFIG_VERSION = 2
# v2 迁移时使用的 prior: 2026-10-12 人工整理的权重 (commit 1df0424, PR #1)。
# 此后到迁移前的漂移来自被污染的 IC 历史, 迁移时权重重置到这里。
# 当时为 0 的因子 (人工锁定 / 已关闭) 用 DEFAULT_FACTORS 的权重作 prior。
MIGRATION_PRIORS = {
    "momentum_30d": 0.086438, "momentum_7d": 0.051827, "ath_drawdown": 0.186187,
    "tvl_mcap": 0.170568, "market_cap_size": 0.15, "onchain_activity": 0.163113,
    "funding_rate": 0.035498, "narrative_heat": 0.156369,
}
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



def _default_prior(fname: str, fdata: dict | None = None) -> float:
    """因子的 prior (无显著 IC 证据时权重回归的目标)。"""
    if fname in DEFAULT_FACTORS:
        return DEFAULT_FACTORS[fname]["weight"]
    return float((fdata or {}).get("min", 0.02))


def _migrate_to_v2(cfg: dict) -> bool:
    """v1 → v2 一次性迁移 (2026-10-20 审计), 已是 v2 返回 False。

    - 把不带来源标签的旧 IC 记录 (6 月的 top-200 截断记录, 以及 10-13..10-19
      用 schema 1 旧快照算出的记录) 移到 ic_history_archive, 不再参与学习;
    - 每个因子写入 prior (MIGRATION_PRIORS = 2026-10-12 人工整理的权重);
    - 启用因子的权重重置到 prior: 10-12 之后的漂移 (动量被压到下限、tvl_mcap +41%)
      来自被污染的 IC 历史;
    - 已关闭因子补 disabled_reason; 删除从未被写过的 cfg["regime"]。
    """
    if int(cfg.get("version", 1) or 1) >= CONFIG_VERSION:
        return False
    archive = cfg.setdefault("ic_history_archive", {})
    n_archived = 0
    for fname, fdata in cfg["factors"].items():
        hist = fdata.get("ic_history") or []
        legacy = [h for h in hist if "base_schema" not in h]
        if legacy:
            archive.setdefault(fname, []).extend(legacy)
            n_archived += len(legacy)
        fdata["ic_history"] = [h for h in hist if "base_schema" in h]
        prior = MIGRATION_PRIORS.get(fname) or _default_prior(fname, fdata)
        fdata["prior"] = prior
        fdata.setdefault("enabled", True)
        if fdata.get("locked", False):
            fdata["enabled"] = False
            fdata["disabled_reason"] = "locked"
            fdata["weight"] = 0.0
        elif not fdata["enabled"]:
            fdata.setdefault("disabled_reason", "constant")
            fdata["weight"] = 0.0
        else:
            fdata.pop("disabled_reason", None)
            fdata["weight"] = prior
    _normalize_and_project(cfg["factors"], total=_prior_mass(cfg["factors"]))
    cfg.pop("regime", None)
    cfg["version"] = CONFIG_VERSION
    cfg.setdefault("migrations", []).append({
        "to": CONFIG_VERSION,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "archived_ic_records": n_archived,
        "note": "IC 记录加来源标签, 旧记录归档; 权重重置到 2026-10-12 的 prior; 学习规则改为目标权重平滑",
    })
    log.warning(f"factor_config 已迁移到 v{CONFIG_VERSION}: 归档 {n_archived} 条旧 IC 记录, "
                f"权重重置到 prior")
    return True


def load_factor_config() -> dict:
    """加载因子配置 (权重 + IC 历史)。首次使用时初始化。

    旧版 (v1) 配置在这里一次性迁移到 v2; 只有权重写入方会把迁移结果落盘,
    只读环境在内存里得到同样的视图。
    """
    _ensure_dir()
    config_path = META_DIR / "factor_config.json"
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            # 合并新因子 (如果代码里加了新因子但 config 里还没有)
            for fname, fdef in DEFAULT_FACTORS.items():
                if fname not in cfg["factors"]:
                    cfg["factors"][fname] = {**fdef, "ic_history": [],
                                             "prior": fdef["weight"]}
                    log.info(f"  新因子加入池: {fname}")
            # 旧版 config 没有 enabled 字段 → 回填为 True
            for fdata in cfg["factors"].values():
                fdata.setdefault("enabled", True)
            if _migrate_to_v2(cfg) and is_weight_writer():
                save_factor_config(cfg)
            for fname, fdata in cfg["factors"].items():
                fdata.setdefault("prior", _default_prior(fname, fdata))
            return cfg
        except Exception as e:
            log.warning(f"加载 factor_config 失败: {e}, 重新初始化")

    # 初始化
    cfg = {
        "version": CONFIG_VERSION,
        "created": datetime.now(timezone.utc).isoformat(),
        "updated": datetime.now(timezone.utc).isoformat(),
        "factors": {},
    }
    for fname, fdef in DEFAULT_FACTORS.items():
        cfg["factors"][fname] = {**fdef, "ic_history": [], "prior": fdef["weight"]}
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

    被禁用 (enabled=False) 的因子权重为 0, 且不参与归一化。
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



def _snapshot_schema(snap: dict) -> int:
    """快照 schema: 有 _meta.schema 用之, 否则视作 1 (只存前 200 的旧格式)。"""
    try:
        return int((snap.get("_meta") or {}).get("schema") or 1)
    except (TypeError, ValueError):
        return 1


def _price_lookup(latest_coins: list[dict]):
    """返回 lookup(coin) → 最新价格。

    优先按 coin_id 配对 (schema 2 快照都带 coin_id); 没有 coin_id 的行只在
    symbol 在最新快照里唯一时按 symbol 配对。曾经只按 symbol 配: VELO 同时是
    velodrome-finance 和 velo, 最后一行覆盖前一行, 一个币被记成 -88% 的收益。
    """
    by_id: dict[str, float] = {}
    by_sym: dict[str, list] = {}
    for c in latest_coins:
        cid = c.get("coin_id")
        if cid:
            by_id[cid] = c.get("price")
        by_sym.setdefault(c.get("symbol"), []).append(c.get("price"))

    def lookup(coin: dict):
        cid = coin.get("coin_id")
        if cid:
            return by_id.get(cid)
        prices = by_sym.get(coin.get("symbol")) or []
        return prices[0] if len(prices) == 1 else None
    return lookup


def run_ic_backtest(lookback_days: int = 7) -> dict:
    """回测: 找到 N 天前的快照, 对比当前价格, 计算每个因子的 IC。

    IC = spearman(因子值_t, 收益率_t+N)

    仅使用真实、完整的快照 (见 _load_valid_snapshot), 旧快照与价格源
    ("latest") 快照都做同样过滤。币按 coin_id 配对 (见 _price_lookup)。

    返回的 factor_ic 只含有限数值 (下游 cli 直接 abs(ic) / ic > 0.05, 放 None
    会崩)。不进 factor_ic 的因子分三类列出, 供 update_weights_from_ic 判定:
      - constant_factors: 旧快照全截面常数 (死因子)
      - low_coverage_factors: 旧快照里有非零数值的币太少 (见 MIN_COVERAGE_*)
      - degenerate_factors: IC 未定义 (NaN, 因子列或收益率列常数)
    base_schema / latest_date 用来给 IC 记录打来源标签。
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
    latest_file, latest_snap, latest_ts = snapshots[-1]
    lookup = _price_lookup(latest_snap.get("coins", []))

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
        new_price = lookup(coin)
        if _is_num(old_price) and _is_num(new_price) and old_price > 0 and new_price > 0:
            matched.append((coin, (new_price - old_price) / old_price))
    dropout_rate = (1.0 - len(matched) / len(old_coins)) if old_coins else 1.0
    if dropout_rate > DROPOUT_WARN_RATE:
        log.warning(f"IC 回测脱落率 {dropout_rate:.1%} > {DROPOUT_WARN_RATE:.0%}: "
                    f"{best_file.name} 中有大量币在 {latest_file.name} 里无价格, "
                    f"IC 可能带幸存者偏差")

    # 计算每个因子的 IC
    factor_names = [k for k in old_coins[0].keys() if k.startswith("f_")]
    min_cover = max(MIN_COVERAGE_COINS, int(MIN_COVERAGE_SHARE * len(old_coins)))
    ic_results = {}
    constant_factors = []
    low_coverage_factors = []
    degenerate_factors = []
    coverage = {}

    for fname in factor_names:
        # 旧快照全截面是否常数 (死因子判据, 与是否配对无关)
        nums = [coin.get(fname) for coin in old_coins if _is_num(coin.get(fname))]
        if len(set(nums)) < 2:
            constant_factors.append(fname)
            degenerate_factors.append(fname)   # IC 未定义, 与旧实现一致两边都列
            continue
        # 覆盖率: 有非零数值的币数 (缺数据在打分时被记成 0)
        n_cover = sum(1 for v in nums if v != 0)
        coverage[fname] = n_cover
        if n_cover < min_cover:
            low_coverage_factors.append(fname)
            continue

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

    if low_coverage_factors:
        log.info(f"低覆盖因子 (非零币数 < {min_cover}): "
                 f"{ {f: coverage.get(f) for f in low_coverage_factors} }")

    return {
        "ok": True,
        "lookback_days": lookback_days,
        "snapshot_date": best_snap.get("date"),
        "snapshot_file": best_file.name,
        "latest_file": latest_file.name,
        "latest_date": latest_ts.strftime("%Y-%m-%d"),
        "base_schema": _snapshot_schema(best_snap),
        "latest_schema": _snapshot_schema(latest_snap),
        "realized_days": round(realized_days, 2),
        "matched_coins": len(matched),
        "dropout_rate": round(dropout_rate, 4),
        "skipped_snapshots": skipped,
        "factor_ic": ic_results,
        "degenerate_factors": degenerate_factors,
        "constant_factors": constant_factors,
        "low_coverage_factors": low_coverage_factors,
        "coverage": coverage,
    }



# ====================================================================
#  元学习: 自动调权
# ====================================================================

def _project(w: dict, factors: dict, total: float) -> tuple[dict, bool]:
    """把 w (只含启用因子) 归一化到 sum=total, 再迭代投影到各自 [min,max]。

    每轮: clamp 全部 → 算缺口 (total - sum) → 把缺口按比例分给还有余量的因子
    (缺口为正只分给未顶到 max 的, 为负只分给未跌到 min 的), 直到缺口归零。
    返回 (投影后的权重, 约束是否在 1e-6 内成立)。
    """
    if not w:
        return {}, False
    s = sum(w.values())
    if s <= 0:
        w = {k: total / len(w) for k in w}
    else:
        w = {k: v * total / s for k, v in w.items()}
    eps = 1e-12
    for _ in range(WEIGHT_PROJECT_ITERS):
        for k in w:
            w[k] = max(factors[k]["min"], min(factors[k]["max"], w[k]))
        deficit = total - sum(w.values())
        if abs(deficit) < 1e-9:
            break
        if deficit > 0:
            cand = [k for k in w if w[k] < factors[k]["max"] - eps]
        else:
            cand = [k for k in w if w[k] > factors[k]["min"] + eps]
        if not cand:
            break   # 所有因子都顶在边界上, min/max 组合不可行
        base = sum(w[k] for k in cand)
        for k in cand:
            share = w[k] / base if base > 0 else 1.0 / len(cand)
            w[k] += deficit * share
    ok = (abs(sum(w.values()) - total) < 1e-6 and
          all(factors[k]["min"] - 1e-6 <= w[k] <= factors[k]["max"] + 1e-6 for k in w))
    return w, ok


def _prior_mass(factors: dict) -> float:
    """启用因子的 prior 之和 (调权时投影的目标总和, 见 _normalize_and_project)。"""
    return sum(float(v.get("prior", _default_prior(k, v)))
               for k, v in factors.items() if v.get("enabled", True))


def _normalize_and_project(factors: dict, total: float = 1.0) -> bool:
    """对 enabled 因子: 归一化到 sum=total, 再迭代投影到各自 [min,max]。

    旧实现先 clamp 再归一化, 归一化会把权重推出边界 (实测 117 次违约)。
    被禁用的因子权重保持 0。

    total 默认 1; 调权时传入启用因子的 prior 之和。min/max 是相对"全因子池"
    的尺度: 关掉两个共占 32% 的低覆盖因子后若仍归一化到 1, ath/size/tvl 会被
    顶到 max, 多出的权重全部挤进动量。打分用 get_current_weights 归一化后的
    相对权重, 总和本身不影响排名。

    返回边界是否在 1e-6 内成立 (不成立时只告警, 不抛异常)。
    """
    enabled = {k: v for k, v in factors.items() if v.get("enabled", True)}
    for k, v in factors.items():
        if k not in enabled:
            v["weight"] = 0.0
    if not enabled:
        log.warning("所有因子均被禁用, 无法归一化权重")
        return False
    if not total or total <= 0:
        total = 1.0
    w, ok = _project({k: v["weight"] for k, v in enabled.items()}, enabled, total)
    if not ok:
        log.warning(f"权重投影后仍越界/不归一 (min/max 组合不可行?): "
                    f"{ {k: round(x, 4) for k, x in w.items()} }")
    for k in enabled:
        enabled[k]["weight"] = round(w[k], 6)
    return ok


def _parse_day(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def _eligible_ic(history: list, lookback: int, today: date) -> list[tuple[int, float]]:
    """参与学习的 IC 记录 → [(age_days, ic)], 按 age 升序 (最新在前)。

    条件: 同一 lookback; 带来源标签且旧快照 schema ≥ IC_MIN_BASE_SCHEMA
    (schema 1 快照只存前 200, 截断偏差); 日历年龄 ≤ IC_MAX_AGE_DAYS。
    """
    out = []
    for h in history or []:
        if h.get("lookback") != lookback or not _is_num(h.get("ic")):
            continue
        try:
            schema = int(h.get("base_schema") or 0)
        except (TypeError, ValueError):
            schema = 0
        if schema < IC_MIN_BASE_SCHEMA:
            continue
        d = _parse_day(h.get("date"))
        if d is None:
            continue
        age = max(0, (today - d).days)
        if age > IC_MAX_AGE_DAYS:
            continue
        out.append((age, float(h["ic"])))
    out.sort(key=lambda x: x[0])
    return out


def _ic_signal(eligible: list[tuple[int, float]], lookback: int) -> dict:
    """合格 IC 记录 → 收缩后的信号。

    avg_ic: 按日历天数衰减的加权平均 (IC_DECAY_PER_DAY ** age)
    n_eff:  记录覆盖的独立 (不重叠) 窗口数 = 1 + 跨度天数 / lookback
    signal: avg_ic × n_eff/(n_eff+IC_SHRINK_K); |signal| < 地板 → 0
    记录不足 MIN_IC_RECORDS 时 signal = 0 (目标权重 = prior)。
    """
    n = len(eligible)
    if n < MIN_IC_RECORDS:
        return {"n": n, "avg_ic": None, "n_eff": 0.0, "shrink": 0.0, "signal": 0.0}
    ws = [IC_DECAY_PER_DAY ** age for age, _ in eligible]
    avg = sum(w * ic for w, (_, ic) in zip(ws, eligible)) / sum(ws)
    span = eligible[-1][0] - eligible[0][0]
    n_eff = 1.0 + span / max(1, lookback)
    shrink = n_eff / (n_eff + IC_SHRINK_K)
    sig = avg * shrink
    if abs(sig) < IC_SIGNIFICANCE_FLOOR:
        sig = 0.0
    return {"n": n, "avg_ic": round(avg, 4), "n_eff": round(n_eff, 2),
            "shrink": round(shrink, 3), "signal": round(sig, 4)}


def _nonoverlap_tstat(eligible: list[tuple[int, float]],
                      lookback: int) -> tuple[float | None, int]:
    """不重叠窗口 IC 的 t 值: 从最新记录往回, 只取与上一条相隔 ≥ lookback 天的记录。

    返回 (t, 窗口数); 窗口数 < 2 时 t 为 None。
    """
    picked = []
    last = None
    for age, ic in eligible:
        if last is None or age - last >= lookback:
            picked.append(ic)
            last = age
    n = len(picked)
    if n < 2:
        return None, n
    mean = sum(picked) / n
    var = sum((x - mean) ** 2 for x in picked) / (n - 1)
    if var <= 0:
        return (None if mean == 0 else math.copysign(math.inf, mean)), n
    return mean / math.sqrt(var / n), n


def update_weights_from_ic(ic_result: dict, force: bool = False) -> dict:
    """根据回测 IC 更新因子权重。

    学习规则 (2026-10-20 重写; 旧规则每天把 1+0.5×IC 复利到权重上, 同一周的
    收益噪声被重复计入约 7 次, 加上按条目位置衰减让 4 个月前的截断偏差 IC 仍占
    主导, 动量因子被压到下限):

    1. Phase A 记录 IC: 每条记录带来源标签 {date, ic, lookback, asof, base,
       base_schema, n}; 同一价格快照 (asof) + lookback 覆盖而非追加;
       超过 IC_HISTORY_KEEP_DAYS 天的记录删除。
    2. 每个价格快照 (asof = 最新快照文件名) 最多调一次权重 (daily-screen /
       adaptive_trainer / nightly 都会调用本函数)。以快照而非 UTC 日期为键:
       夜间任务被 GitHub 延迟到 UTC 次日时, 不会占掉第二天的名额。
       force=True 跳过守卫。
    3. Phase B 开关 (disabled_reason 记录原因):
       - locked=True → 永远关闭 (人工), 不会自动复活
       - 旧快照截面常数 → "constant"; 有非零数值的币太少 → "low_coverage";
         之后 IC 重新有效 → 以 min 权重复活
       - 不重叠窗口 IC 的 t < NEG_IC_T_DISABLE 且窗口数 ≥ NEG_IC_MIN_WINDOWS
         → "negative_ic"; t > NEG_IC_T_REENABLE → 以 min 复活。
       权重只能为正, 系统不会自动翻转因子方向; 持续为负的因子被关闭而不是反用。
    4. Phase C 目标权重 (只用合格记录, 见 _eligible_ic / _ic_signal):
         target = prior × max(0, 1 + IC_TO_WEIGHT_K × signal), 投影到 [min,max]
         new    = (1-α)·old + α·target (α = 1/lookback), 再投影
       投影总和 = 启用因子 prior 之和 (见 _normalize_and_project)。
       信号为 0 (记录不足 / 不显著) 时目标就是 prior: 没有证据就慢慢回到 prior。

    degenerate 判据 (任一): 在 degenerate_factors / constant_factors /
    low_coverage_factors 里, 或 factor_ic 里的值是 None / NaN。

    非权重写入方 (未设置 WEIGHT_WRITER_ENV) 直接返回 skipped="not_weight_writer",
    不读写 factor_config.json; force 不能绕过这一条。
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
    lookback = int(ic_result.get("lookback_days", 7) or 7)
    today = datetime.now(timezone.utc).date()
    asof = ic_result.get("latest_file") or today.isoformat()
    entry_date = ic_result.get("latest_date") or today.isoformat()

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
    low_cov = {raw.replace("f_", "", 1)
               for raw in ic_result.get("low_coverage_factors", [])}
    degenerate |= {raw.replace("f_", "", 1)
                   for raw in ic_result.get("degenerate_factors", [])}
    degenerate |= constant | low_cov
    # 同一因子既有有效 IC 又被标 degenerate 是矛盾输入, 以 degenerate 为准
    for fname in degenerate:
        ic_map.pop(fname, None)

    # ── Phase A: 刷新 IC 历史 (无论是否触发调权) ──
    keep_after = today - timedelta(days=IC_HISTORY_KEEP_DAYS)
    for fname, fdata in cfg["factors"].items():
        fdata.setdefault("enabled", True)
        fdata.setdefault("degenerate_runs", 0)
        fdata.setdefault("prior", _default_prior(fname, fdata))
        history = [h for h in fdata.get("ic_history") or []
                   if (_parse_day(h.get("date")) or today) >= keep_after]
        if fname in ic_map:
            fdata["degenerate_runs"] = 0
            entry = {"date": entry_date, "ic": ic_map[fname], "lookback": lookback,
                     "asof": asof, "base": ic_result.get("snapshot_file"),
                     "base_schema": ic_result.get("base_schema"),
                     "n": ic_result.get("matched_coins")}
            for i, h in enumerate(history):
                if h.get("asof") == asof and h.get("lookback") == lookback:
                    history[i] = entry
                    break
            else:
                history.append(entry)
        fdata["ic_history"] = history[-IC_HISTORY_MAX_ENTRIES:]

    # ── 守卫: 这个价格快照已调过权重 → 只刷新 IC 记录, 不再动权重 ──
    if cfg.get("last_weight_update_asof") == asof and not force:
        save_factor_config(cfg)
        log.info(f"权重已按 {asof} 更新过, 本次仅刷新 IC 记录")
        return {
            "ok": True,
            "skipped": "already_updated_for_snapshot",
            "factors_updated": 0,
            "disabled": [k for k, v in cfg["factors"].items()
                         if not v.get("enabled", True)],
            "new_weights": get_current_weights(cfg),
        }

    old_weights = {k: v["weight"] for k, v in cfg["factors"].items()}
    eligible = {f: _eligible_ic(fd["ic_history"], lookback, today)
                for f, fd in cfg["factors"].items()}

    # ── Phase B: 关闭 / 复活 ──
    for fname, fdata in cfg["factors"].items():
        if fdata.get("locked", False):
            # 人工锁定的因子: 无论 IC 如何都保持关闭
            fdata.update(enabled=False, weight=0.0, disabled_reason="locked")
            continue
        reason = fdata.get("disabled_reason")
        if fname in degenerate:
            # 守卫之后才计数 → 按"调权轮次"而非按调用次数
            fdata["degenerate_runs"] += 1
            dead = ("constant" if fname in constant
                    else "low_coverage" if fname in low_cov else None)
            if dead:
                if fdata.get("enabled", True) or reason != dead:
                    what = "截面常数 (IC 未定义)" if dead == "constant" else "覆盖不足 (绝大多数币无数据)"
                    log.warning(f"  {fname}: {what}, 禁用并清零权重")
                fdata.update(enabled=False, weight=0.0, disabled_reason=dead)
            continue
        if fname not in ic_map:
            continue
        t, n_win = _nonoverlap_tstat(eligible[fname], lookback)
        if fdata.get("enabled", True):
            if n_win >= NEG_IC_MIN_WINDOWS and t is not None and t < NEG_IC_T_DISABLE:
                log.warning(f"  {fname}: 不重叠窗口 IC t={t:.2f} (n={n_win}) 持续为负, 自动关闭")
                fdata.update(enabled=False, weight=0.0, disabled_reason="negative_ic")
        else:
            revive = reason in (None, "constant", "low_coverage") or (
                reason == "negative_ic" and n_win >= NEG_IC_MIN_WINDOWS
                and t is not None and t > NEG_IC_T_REENABLE)
            if revive:
                fdata.update(enabled=True, weight=fdata["min"])
                fdata.pop("disabled_reason", None)
                log.info(f"  {fname}: 恢复, 以 min 权重 {fdata['min']:.3f} 重新启用")

    # ── Phase C: 目标权重 → 平滑 → 投影 ──
    enabled = {k: v for k, v in cfg["factors"].items() if v.get("enabled", True)}
    for k, v in cfg["factors"].items():
        if k not in enabled:
            v["weight"] = 0.0
    signals: dict[str, dict] = {}
    targets: dict[str, float] = {}
    if enabled:
        mass = _prior_mass(cfg["factors"])
        for fname, fdata in enabled.items():
            sig = _ic_signal(eligible[fname], lookback)
            signals[fname] = sig
            targets[fname] = fdata["prior"] * max(0.0, 1.0 + IC_TO_WEIGHT_K * sig["signal"])
        targets, _ = _project(targets, enabled, mass)
        alpha = 1.0 / max(1, lookback)
        blended = {k: (1 - alpha) * enabled[k]["weight"] + alpha * targets[k]
                   for k in enabled}
        blended, ok = _project(blended, enabled, mass)
        if not ok:
            log.warning(f"权重投影后仍越界/不归一 (min/max 组合不可行?): "
                        f"{ {k: round(x, 4) for k, x in blended.items()} }")
        for k in enabled:
            enabled[k]["weight"] = round(blended[k], 6)
        for fname, sig in signals.items():
            if sig["avg_ic"] is not None:
                log.info(f"  {fname}: IC={sig['avg_ic']:+.3f} (n={sig['n']}, "
                         f"n_eff={sig['n_eff']}) → 信号 {sig['signal']:+.3f} → "
                         f"目标 {targets[fname]:.3f}")
    else:
        log.warning("所有因子均被禁用, 无法归一化权重")

    updated_count = 0
    for fname, fdata in cfg["factors"].items():
        if abs(fdata["weight"] - old_weights[fname]) > 0.001:
            log.info(f"  {fname}: 权重 {old_weights[fname]:.3f} → {fdata['weight']:.3f}")
            updated_count += 1

    cfg["last_weight_update_date"] = today.isoformat()
    cfg["last_weight_update_asof"] = asof
    save_factor_config(cfg)

    return {
        "ok": True,
        "factors_updated": updated_count,
        "disabled": [k for k, v in cfg["factors"].items()
                     if not v.get("enabled", True)],
        "disabled_reasons": {k: v.get("disabled_reason") for k, v in cfg["factors"].items()
                             if not v.get("enabled", True)},
        "signals": signals,
        "targets": {k: round(v, 6) for k, v in targets.items()},
        "new_weights": get_current_weights(cfg),
    }


# ====================================================================
#  Regime Detection: 市场状态识别
# ====================================================================

_REGIMES = ("bull", "bear", "sideways", "volatile")
# 迟滞: 只有在 BTC 7d/30d 涨跌各 ±1pp 的范围内都判成新 regime 才切换,
# 否则沿用上一次 (曾经 30d 涨跌 ±0.3pp 就让飞书卡片在"高波动/震荡"间来回跳)
REGIME_HYSTERESIS_PP = 1.0


def _classify_regime(chg_30d: float, chg_7d: float) -> str:
    if chg_30d > 15 and chg_7d > 3:
        return "bull"
    if chg_30d < -15 and chg_7d < -3:
        return "bear"
    # "volatile" 必须有一周的大幅波动; 以前它是兜底桶, 30d +9% / 7d +1% 这种
    # 平静行情也会被叫成高波动
    if abs(chg_7d) >= 5:
        return "volatile"
    return "sideways"


def detect_regime(btc_data: dict = None, prev_regime: str | None = None) -> str:
    """根据 BTC 7d/30d 涨跌判断市场 regime。

    返回: "bull" / "bear" / "sideways" / "volatile" ("unknown" = 无数据)
      bull:     30d > +15% 且 7d > +3%
      bear:     30d < -15% 且 7d < -3%
      volatile: 非牛非熊, 但 |7d| ≥ 5%
      sideways: 其余
    prev_regime: 上一次的 regime; 给出时加迟滞 (见 REGIME_HYSTERESIS_PP)。
    """
    if not btc_data:
        return "unknown"

    chg_30d = btc_data.get("change_30d") or 0
    chg_7d = btc_data.get("change_7d") or 0
    regime = _classify_regime(chg_30d, chg_7d)
    if prev_regime in _REGIMES and prev_regime != regime:
        m = REGIME_HYSTERESIS_PP
        corners = {_classify_regime(chg_30d + a, chg_7d + b)
                   for a in (-m, m) for b in (-m, m)}
        if corners != {regime}:
            return prev_regime
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

def latest_snapshot_meta() -> dict:
    """最新一份真实 (非回填合成) 快照的 _meta; 没有则 {}。

    regime 只记录在快照里 (factor_config 不保存 regime, 单一写入方之外的环境
    也能读到)。旧 schema 1 快照没有 _meta 时退回顶层 regime 字段。
    """
    try:
        files = sorted(META_DIR.glob("snapshot_*.json"), reverse=True)
    except Exception:
        return {}
    for p in files:
        if p.name.endswith("_0000.json"):
            continue
        try:
            d = json.loads(p.read_text())
        except Exception:
            continue
        meta = d.get("_meta") or {}
        if meta.get("synthetic") or meta.get("source") == "backfill":
            continue
        if not meta and d.get("regime"):
            return {"regime": d.get("regime")}
        return meta
    return {}


def generate_factor_report(regime: str | None = None, lookback: int = 7) -> dict:
    """生成因子池健康状态报告。

    regime: 调用方刚算出的 regime (screen); 不给时取最新真实快照的 _meta.regime。
    IC 统计只用参与学习的合格记录 (见 _eligible_ic), 与调权口径一致。
    """
    cfg = load_factor_config()
    if regime is None:
        regime = latest_snapshot_meta().get("regime") or "unknown"
    today = datetime.now(timezone.utc).date()
    report = {
        "regime": regime,
        "total_factors": len(cfg["factors"]),
        "config_version": cfg.get("version", 1),
        "last_weight_update": cfg.get("last_weight_update_asof")
        or cfg.get("last_weight_update_date"),
        "factors": {},
    }

    for fname, fdata in cfg["factors"].items():
        history = fdata.get("ic_history", [])
        eligible = _eligible_ic(history, lookback, today)
        sig = _ic_signal(eligible, lookback)
        avg_ic = sig["avg_ic"] if sig["avg_ic"] is not None else 0.0

        status = "insufficient_data"
        if len(eligible) >= MIN_IC_RECORDS:
            status = "healthy"
            if avg_ic < -0.05:
                status = "weak_negative"
            elif abs(avg_ic) < 0.02:
                status = "noisy"
            elif avg_ic > 0.15:
                status = "strong"
        if fdata.get("locked", False):
            status = "locked"     # 人工关闭, 不会自动复活
        elif not fdata.get("enabled", True):
            status = "disabled"   # 自动关闭, 原因见 disabled_reason

        report["factors"][fname] = {
            "weight": fdata["weight"],
            "prior": fdata.get("prior"),
            "enabled": fdata.get("enabled", True),
            "locked": fdata.get("locked", False),
            "disabled_reason": fdata.get("disabled_reason"),
            # 键名沿用 avg_ic_10 (下游在用); 值是合格记录的日历衰减平均 IC
            "avg_ic_10": round(avg_ic, 4),
            "signal": sig["signal"],
            "ic_records": len(eligible),
            "ic_records_total": len(history),
            "degenerate_runs": fdata.get("degenerate_runs", 0),
            "status": status,
        }

    return report
