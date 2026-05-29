"""LLM 驱动的 Alpha 因子自动发现引擎。

灵感: AlphaAgent (arxiv 2502.16789), Qlib RD-Agent, Hubble

核心流程:
  1. 基于当前因子 IC 表现, 让 LLM 生成 N 个候选因子表达式
  2. 候选因子是纯数学公式, 用已有数据列计算 (无需新数据源)
  3. 对候选因子做 IC 回测 (用历史快照数据)
  4. IC > 阈值的因子进入 "候选池"
  5. 候选池因子连续 K 次 IC 为正 → 自动晋升为正式因子
  6. 正式因子连续 M 次 IC 为负 → 自动降级淘汰

因子表达式语法 (安全沙箱):
  变量: momentum_30d, momentum_7d, ath_drawdown, volume_turnover,
        tvl_mcap, market_cap_size, onchain_activity, dev_activity,
        funding_rate, narrative_heat, change_24h, change_7d, change_30d,
        turnover, tvl_mcap_ratio, market_cap, price
  运算: +, -, *, /, **, abs(), min(), max(), log(), sqrt(), sigmoid()
  示例: "momentum_30d * 0.6 + funding_rate * 0.4 - abs(ath_drawdown) * 0.2"
"""
from __future__ import annotations
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("alpha_discovery", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"
DISCOVERY_DIR = Path(__file__).resolve().parents[2] / "data" / "alpha_candidates"

# 允许在因子表达式中使用的变量
ALLOWED_VARS = {
    "momentum_30d", "momentum_7d", "ath_drawdown", "volume_turnover",
    "tvl_mcap", "market_cap_size", "onchain_activity", "dev_activity",
    "funding_rate", "narrative_heat", "change_24h", "change_7d",
    "change_30d", "turnover", "tvl_mcap_ratio", "market_cap", "price",
}

# 安全沙箱: 只允许这些函数
SAFE_FUNCTIONS = {
    "abs": abs,
    "min": min,
    "max": max,
    "log": lambda x: math.log(max(x, 1e-10)),
    "sqrt": lambda x: math.sqrt(max(x, 0)),
    "sigmoid": lambda x: 1 / (1 + math.exp(-max(min(x, 20), -20))),
    "sign": lambda x: 1 if x > 0 else (-1 if x < 0 else 0),
    "clip": lambda x, lo, hi: max(lo, min(hi, x)),
    "pow": lambda x, n: x ** n,
}

# 晋升/淘汰阈值
PROMOTE_IC_THRESHOLD = 0.03      # IC > 3% 才算有效
PROMOTE_CONSECUTIVE = 2          # 连续 2 次 IC 为正 → 晋升
DEMOTE_IC_THRESHOLD = -0.02      # IC < -2% 算反向
DEMOTE_CONSECUTIVE = 3           # 连续 3 次 IC 为负 → 淘汰


def _ensure_dirs():
    META_DIR.mkdir(parents=True, exist_ok=True)
    DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)


# ====================================================================
#  因子表达式安全执行
# ====================================================================

def eval_factor_expr(expr: str, coin: dict) -> float:
    """在安全沙箱中执行因子表达式。

    coin 是一个 scored_coin dict, 包含 f_* 字段和原始字段。
    """
    # 构建变量映射
    variables = {}
    for var_name in ALLOWED_VARS:
        # 尝试从 f_ 前缀字段取值
        val = coin.get(f"f_{var_name}", coin.get(var_name, 0))
        if val is None:
            val = 0
        variables[var_name] = float(val)

    # 安全 eval
    safe_env = {**SAFE_FUNCTIONS, **variables, "__builtins__": {}}
    try:
        result = eval(expr, safe_env)
        if isinstance(result, (int, float)) and math.isfinite(result):
            return result
        return 0.0
    except Exception:
        return 0.0


# ====================================================================
#  候选因子管理
# ====================================================================

def load_candidates() -> dict:
    """加载候选因子池。"""
    _ensure_dirs()
    path = DISCOVERY_DIR / "candidates.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"version": 1, "candidates": {}, "graduated": [], "retired": []}


def save_candidates(data: dict):
    _ensure_dirs()
    data["updated"] = datetime.now(timezone.utc).isoformat()
    path = DISCOVERY_DIR / "candidates.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ====================================================================
#  LLM 因子生成 (离线模式: 不调 API, 用模板)
# ====================================================================

# 预定义的因子变异策略 (不需要 LLM 也能运行)
MUTATION_TEMPLATES = [
    # 组合因子: 两个因子的线性组合
    ("combo_{a}_{b}", "{a} * 0.6 + {b} * 0.4"),
    ("diff_{a}_{b}", "{a} - {b}"),
    ("product_{a}_{b}", "{a} * {b}"),
    # 非线性变换
    ("sigmoid_{a}", "sigmoid({a} * 3)"),
    ("log_{a}", "log(abs({a}) + 1) * sign({a})"),
    ("sq_{a}", "sign({a}) * pow(abs({a}), 0.5)"),
    # 三因子组合
    ("trio_{a}_{b}_{c}", "{a} * 0.4 + {b} * 0.3 + {c} * 0.3"),
    # 条件因子
    ("gated_{a}_{b}", "{a} * sigmoid({b} * 5)"),
    # 动量增强
    ("accel_{a}_{b}", "({a} - {b}) * abs({a})"),
]

FACTOR_NAMES = list(ALLOWED_VARS - {"change_24h", "change_7d", "change_30d",
                                      "turnover", "tvl_mcap_ratio", "market_cap", "price"})


def generate_candidates_offline(n: int = 20) -> list[dict]:
    """不依赖 LLM, 用组合变异策略生成候选因子。"""
    import random
    candidates = []
    used_names = set()

    for _ in range(n * 3):  # 多生成一些, 去重后取 n 个
        if len(candidates) >= n:
            break

        template_name, template_expr = random.choice(MUTATION_TEMPLATES)
        factors = random.sample(FACTOR_NAMES, min(3, len(FACTOR_NAMES)))

        name = template_name
        expr = template_expr
        for i, placeholder in enumerate(["a", "b", "c"]):
            if f"{{{placeholder}}}" in name and i < len(factors):
                name = name.replace(f"{{{placeholder}}}", factors[i])
                expr = expr.replace(f"{{{placeholder}}}", factors[i])

        if name in used_names:
            continue
        used_names.add(name)

        # 验证表达式能运行
        test_coin = {f"f_{v}": 0.5 for v in ALLOWED_VARS}
        test_coin.update({v: 0.5 for v in ALLOWED_VARS})
        try:
            val = eval_factor_expr(expr, test_coin)
            if math.isfinite(val):
                candidates.append({
                    "name": f"alpha_{name}",
                    "expr": expr,
                    "origin": "mutation",
                    "created": datetime.now(timezone.utc).isoformat(),
                })
        except Exception:
            continue

    return candidates[:n]


def generate_candidates_llm(current_ic: dict = None) -> list[dict]:
    """用 Claude CLI 生成候选因子表达式。

    如果 Claude CLI 不可用, 自动降级到 offline 模式。
    """
    try:
        from ..evolution._claude_runner import run_claude
    except Exception:
        log.info("Claude CLI 不可用, 使用离线变异策略")
        return generate_candidates_offline(20)

    ic_info = ""
    if current_ic:
        ic_info = "当前因子 IC 表现:\n"
        for fname, ic_val in sorted(current_ic.items(), key=lambda x: abs(x[1]), reverse=True):
            ic_info += f"  {fname}: IC = {ic_val:+.4f}\n"

    prompt = f"""你是量化因子发现专家。基于以下因子表现，生成 10 个新的候选因子表达式。

{ic_info}

可用变量: {', '.join(sorted(FACTOR_NAMES))}
可用函数: abs, min, max, log, sqrt, sigmoid, sign, clip, pow

要求:
1. 每个因子是一个数学表达式（Python 语法）
2. 寻找因子之间的非线性组合、交互项、条件关系
3. 尝试捕捉: 动量反转信号、价值与成长的交集、链上异动与价格的背离
4. 表达式要简洁（不超过 80 字符）

输出严格 JSON 格式:
[
  {{"name": "alpha_xxx", "expr": "表达式", "rationale": "一句话解释逻辑"}},
  ...
]
只输出 JSON, 不要其他内容。"""

    result = run_claude(prompt, system="你是量化因子表达式生成器。只输出 JSON。", timeout=120)
    if not result.get("ok"):
        log.warning(f"LLM 生成失败: {result.get('error')}, 降级到离线模式")
        return generate_candidates_offline(20)

    # 解析 LLM 输出
    md = result.get("markdown", "")
    # 提取 JSON
    json_match = re.search(r'\[[\s\S]*\]', md)
    if not json_match:
        log.warning("LLM 输出无法解析为 JSON, 降级到离线模式")
        return generate_candidates_offline(20)

    try:
        raw = json.loads(json_match.group())
    except json.JSONDecodeError:
        log.warning("JSON 解析失败, 降级到离线模式")
        return generate_candidates_offline(20)

    # 验证每个表达式
    candidates = []
    test_coin = {f"f_{v}": 0.5 for v in ALLOWED_VARS}
    test_coin.update({v: 0.5 for v in ALLOWED_VARS})

    for item in raw:
        name = item.get("name", "").strip()
        expr = item.get("expr", "").strip()
        if not name or not expr:
            continue
        try:
            val = eval_factor_expr(expr, test_coin)
            if math.isfinite(val):
                candidates.append({
                    "name": name if name.startswith("alpha_") else f"alpha_{name}",
                    "expr": expr,
                    "rationale": item.get("rationale", ""),
                    "origin": "llm",
                    "created": datetime.now(timezone.utc).isoformat(),
                })
        except Exception:
            continue

    log.info(f"LLM 生成 {len(candidates)}/{len(raw)} 个有效候选因子")

    # 如果 LLM 产出太少, 用离线补充
    if len(candidates) < 5:
        candidates.extend(generate_candidates_offline(15 - len(candidates)))

    return candidates


# ====================================================================
#  候选因子 IC 评估
# ====================================================================

def _spearman(x: list[float], y: list[float]) -> float:
    """Spearman rank correlation."""
    n = len(x)
    if n < 5:
        return 0.0
    def _rank(arr):
        indexed = sorted(enumerate(arr), key=lambda t: t[1])
        ranks = [0.0] * n
        for rank_val, (orig_idx, _) in enumerate(indexed):
            ranks[orig_idx] = rank_val + 1
        return ranks
    rx, ry = _rank(x), _rank(y)
    d_sq = sum((a - b) ** 2 for a, b in zip(rx, ry))
    return 1 - (6 * d_sq) / (n * (n * n - 1))


def evaluate_candidates(candidates: list[dict],
                        snapshots: list[dict] = None) -> list[dict]:
    """对候选因子做 IC 回测。

    用历史快照: 计算候选因子值 at T, 对比收益 at T+N → IC
    """
    _ensure_dirs()

    if not snapshots:
        # 加载所有历史快照
        snap_files = sorted(META_DIR.glob("snapshot_*.json"))
        if len(snap_files) < 2:
            log.warning("快照不足 (<2), 无法评估候选因子")
            return candidates  # 返回原样, 标记为未评估

        snapshots = []
        for sf in snap_files[-10:]:  # 最近 10 个快照
            try:
                snapshots.append(json.loads(sf.read_text()))
            except Exception:
                continue

    if len(snapshots) < 2:
        for c in candidates:
            c["ic"] = None
            c["status"] = "pending_data"
        return candidates

    # 用最早的快照计算因子值, 最新的快照提供收益
    old_snap = snapshots[0]
    new_snap = snapshots[-1]

    # 当前价格映射
    new_prices = {c["symbol"]: c["price"] for c in new_snap.get("coins", [])}

    for candidate in candidates:
        expr = candidate["expr"]
        factor_vals = []
        returns = []

        for coin in old_snap.get("coins", []):
            sym = coin["symbol"]
            old_price = coin.get("price", 0)
            new_price = new_prices.get(sym, 0)
            if old_price <= 0 or new_price <= 0:
                continue

            ret = (new_price - old_price) / old_price
            fval = eval_factor_expr(expr, coin)
            if math.isfinite(fval):
                factor_vals.append(fval)
                returns.append(ret)

        if len(factor_vals) >= 10:
            ic = _spearman(factor_vals, returns)
            candidate["ic"] = round(ic, 4)
            candidate["n_coins"] = len(factor_vals)
            candidate["status"] = "evaluated"
        else:
            candidate["ic"] = None
            candidate["status"] = "insufficient_data"

    # 按 IC 排序
    candidates.sort(key=lambda x: abs(x.get("ic") or 0), reverse=True)

    # ── Phase 2.5: 多重检验校正 ──────────────────────────────────
    # 把每个候选打上 "after-Bonferroni 是否显著" 的 flag
    try:
        from . import overfitting as of_mod
        n_tests = max(1, len(candidates))
        mt = of_mod.multiple_testing_threshold(n_tests, alpha=0.05)
        for c in candidates:
            ic = c.get("ic")
            n = c.get("n_coins", 0)
            if ic is None or n < 10:
                c["significant_after_bonferroni"] = False
                continue
            # 该候选实际需要超过的 IC 阈值
            import math as _m
            ic_thresh = mt["z_threshold"] / _m.sqrt(max(n, 1))
            c["ic_threshold_bonferroni"] = round(ic_thresh, 4)
            c["significant_after_bonferroni"] = abs(ic) >= ic_thresh
        # 在 candidates 头部记录全局 metadata
        if candidates:
            candidates[0]["_meta_n_tests"] = n_tests
            candidates[0]["_meta_bonferroni_z"] = mt["z_threshold"]
    except Exception:
        pass

    return candidates


# ====================================================================
#  因子进化: 晋升 / 淘汰
# ====================================================================

def run_evolution_cycle(use_llm: bool = True) -> dict:
    """运行一轮因子进化。

    1. 生成候选因子
    2. IC 评估
    3. 更新候选池 (新增/晋升/淘汰)
    """
    _ensure_dirs()
    pool = load_candidates()

    # 获取当前正式因子的 IC
    from .meta_learner import load_factor_config
    cfg = load_factor_config()
    current_ic = {}
    for fname, fdata in cfg.get("factors", {}).items():
        history = fdata.get("ic_history", [])
        if history:
            current_ic[fname] = history[-1].get("ic", 0)

    # Step 1: 生成候选因子
    log.info("Step 1: 生成候选因子...")
    if use_llm:
        new_candidates = generate_candidates_llm(current_ic)
    else:
        new_candidates = generate_candidates_offline(20)
    log.info(f"  生成 {len(new_candidates)} 个候选")

    # Step 2: IC 评估
    log.info("Step 2: IC 评估...")
    evaluated = evaluate_candidates(new_candidates)

    # Step 3: 更新候选池
    log.info("Step 3: 更新候选池...")
    new_count = 0
    promote_count = 0
    retire_count = 0

    for c in evaluated:
        name = c["name"]
        ic = c.get("ic")

        if ic is None:
            continue

        if name not in pool["candidates"]:
            # 新候选
            pool["candidates"][name] = {
                "expr": c["expr"],
                "origin": c.get("origin", "unknown"),
                "rationale": c.get("rationale", ""),
                "created": c.get("created", datetime.now(timezone.utc).isoformat()),
                "ic_history": [{"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "ic": ic}],
                "status": "candidate",
            }
            new_count += 1
        else:
            # 已有候选 — 追加 IC 记录
            pool["candidates"][name]["ic_history"].append({
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "ic": ic,
            })
            # 只保留最近 20 条
            pool["candidates"][name]["ic_history"] = pool["candidates"][name]["ic_history"][-20:]

    # 检查晋升/淘汰
    to_remove = []
    for name, cdata in pool["candidates"].items():
        history = cdata.get("ic_history", [])
        if len(history) < 2:
            continue

        recent_ics = [h["ic"] for h in history[-PROMOTE_CONSECUTIVE:]]

        # 晋升检查: 连续 N 次 IC > 阈值 + Bonferroni 校正后显著
        # Phase 2.5: 加上多重检验门槛, 防止假阳性
        ic_thresh_dyn = PROMOTE_IC_THRESHOLD
        try:
            from . import overfitting as of_mod
            mt = of_mod.multiple_testing_threshold(
                max(len(pool["candidates"]), 1), alpha=0.05
            )
            # 用 n=60 (常见候选评估窗口) 算 IC 阈值
            ic_thresh_bonf = mt["ic_floor"]["n=60"]
            ic_thresh_dyn = max(PROMOTE_IC_THRESHOLD, ic_thresh_bonf)
        except Exception:
            pass

        if (len(recent_ics) >= PROMOTE_CONSECUTIVE and
                all(ic > ic_thresh_dyn for ic in recent_ics)):
            avg_ic = sum(recent_ics) / len(recent_ics)
            pool["graduated"].append({
                "name": name,
                "expr": cdata["expr"],
                "avg_ic": round(avg_ic, 4),
                "ic_threshold_used": round(ic_thresh_dyn, 4),
                "graduated_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "origin": cdata.get("origin", ""),
            })
            to_remove.append(name)
            promote_count += 1
            log.info(f"  🎓 晋升: {name} (avg IC={avg_ic:+.4f} > {ic_thresh_dyn:.4f} Bonferroni)")

        # 淘汰检查
        recent_neg = [h["ic"] for h in history[-DEMOTE_CONSECUTIVE:]]
        if (len(recent_neg) >= DEMOTE_CONSECUTIVE and
                all(ic < DEMOTE_IC_THRESHOLD for ic in recent_neg)):
            pool["retired"].append({
                "name": name,
                "reason": "consecutive_negative_ic",
                "retired_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            })
            to_remove.append(name)
            retire_count += 1
            log.info(f"  ❌ 淘汰: {name}")

    for name in to_remove:
        pool["candidates"].pop(name, None)

    # 只保留最近 50 个 retired 记录
    pool["retired"] = pool["retired"][-50:]

    save_candidates(pool)

    result = {
        "ok": True,
        "generated": len(new_candidates),
        "new_added": new_count,
        "promoted": promote_count,
        "retired": retire_count,
        "pool_size": len(pool["candidates"]),
        "graduated_total": len(pool["graduated"]),
        "top_candidates": [],
    }

    # 输出 top 5 候选 (按最新 IC 排)
    ranked = sorted(
        pool["candidates"].items(),
        key=lambda x: (x[1].get("ic_history", [{}])[-1].get("ic", 0)
                        if x[1].get("ic_history") else 0),
        reverse=True
    )
    for name, cdata in ranked[:5]:
        last_ic = cdata["ic_history"][-1]["ic"] if cdata.get("ic_history") else 0
        result["top_candidates"].append({
            "name": name,
            "expr": cdata["expr"][:60],
            "ic": last_ic,
            "records": len(cdata.get("ic_history", [])),
        })

    log.info(f"进化完成: +{new_count} 候选, {promote_count} 晋升, {retire_count} 淘汰, "
             f"池中 {len(pool['candidates'])} 个")
    return result


def get_discovery_report() -> dict:
    """获取因子发现状态报告。"""
    pool = load_candidates()
    return {
        "pool_size": len(pool["candidates"]),
        "graduated": pool.get("graduated", []),
        "retired_count": len(pool.get("retired", [])),
        "candidates": {
            name: {
                "expr": cdata["expr"],
                "last_ic": cdata["ic_history"][-1]["ic"] if cdata.get("ic_history") else None,
                "records": len(cdata.get("ic_history", [])),
                "origin": cdata.get("origin", ""),
            }
            for name, cdata in pool["candidates"].items()
        },
    }
