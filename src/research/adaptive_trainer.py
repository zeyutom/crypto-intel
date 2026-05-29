"""FreqAI 风格自适应再训练引擎。

灵感: FreqAI (freqtrade) 的 adaptive retraining + self-evolving策略

核心: 每日快照后自动执行:
  1. Rolling IC 回测 (最近 N 天窗口)
  2. 因子权重指数衰减更新
  3. Agent 权重回测 + 更新 (如果启用 Swarm)
  4. Alpha 因子池巡检 (晋升/淘汰)
  5. 过拟合检测 (如果快照 >= 16 天)
  6. 持久化训练日志

自动化: 被 daily-screen pipeline 自动调用, 无需手动触发

设计:
  - 渐进式: 快照不足时跳过高级步骤, 不报错
  - 幂等: 同一天跑多次不会重复训练
  - 日志: 每次训练记录到 data/meta/training_log.json
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("adaptive_trainer", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"
TRAIN_LOG = META_DIR / "training_log.json"


def _load_train_log() -> list[dict]:
    if TRAIN_LOG.exists():
        try:
            return json.loads(TRAIN_LOG.read_text())
        except Exception:
            pass
    return []


def _save_train_log(entries: list):
    META_DIR.mkdir(parents=True, exist_ok=True)
    # 只保留最近 90 天
    TRAIN_LOG.write_text(json.dumps(entries[-90:], ensure_ascii=False, indent=2))


def _count_snapshots() -> int:
    META_DIR.mkdir(parents=True, exist_ok=True)
    return len(list(META_DIR.glob("snapshot_*.json")))


def run_adaptive_training(force: bool = False) -> dict:
    """运行一轮自适应再训练。

    Returns: {
        ok, steps_run, ic_updated, weights_changed, alpha_evolved,
        pbo_checked, training_entry
    }
    """
    today = datetime.now().strftime("%Y-%m-%d")
    n_snaps = _count_snapshots()

    log.info(f"[自适应训练] {today}, {n_snaps} 个快照...")

    # 检查今天是否已训练 (幂等)
    train_log = _load_train_log()
    if not force and train_log and train_log[-1].get("date") == today:
        log.info("  今日已训练, 跳过 (use force=True 强制)")
        return {"ok": True, "skipped": True, "reason": "already_trained_today"}

    entry = {
        "date": today,
        "n_snapshots": n_snaps,
        "steps": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    steps_run = 0

    # ── Step 1: Rolling IC 回测 + 权重更新 (需要 >= 2 快照) ──
    ic_updated = False
    weights_changed = 0
    if n_snaps >= 2:
        try:
            from .meta_learner import run_ic_backtest, update_weights_from_ic
            # 尝试多个窗口, 取最佳
            best_ic = None
            for days in [7, 14, 30]:
                ic_r = run_ic_backtest(lookback_days=days)
                if ic_r.get("ok"):
                    best_ic = ic_r
                    break

            if best_ic:
                ur = update_weights_from_ic(best_ic)
                if ur.get("ok"):
                    weights_changed = ur.get("factors_updated", 0)
                    ic_updated = True
                    entry["steps"].append({
                        "name": "ic_update",
                        "lookback": days,
                        "factors_updated": weights_changed,
                    })
                    log.info(f"  ✓ IC 回测 ({days}d) → {weights_changed} 因子权重更新")
                    steps_run += 1
        except Exception as e:
            log.warning(f"  ✗ IC 回测失败: {e}")
    else:
        log.info(f"  跳过 IC 回测 (快照 {n_snaps} < 2)")

    # ── Step 2: Alpha 因子池巡检 (需要 >= 2 快照) ──
    alpha_evolved = False
    if n_snaps >= 2:
        try:
            from .alpha_discovery import run_evolution_cycle
            evo = run_evolution_cycle(use_llm=False)  # 日常用离线模式, 快
            if evo.get("ok"):
                alpha_evolved = True
                entry["steps"].append({
                    "name": "alpha_evolution",
                    "generated": evo.get("generated", 0),
                    "promoted": evo.get("promoted", 0),
                    "retired": evo.get("retired", 0),
                    "pool_size": evo.get("pool_size", 0),
                })
                log.info(f"  ✓ Alpha 进化: +{evo.get('new_added',0)} 候选, "
                         f"{evo.get('promoted',0)} 晋升, {evo.get('retired',0)} 淘汰")
                steps_run += 1
        except Exception as e:
            log.warning(f"  ✗ Alpha 进化失败: {e}")

    # ── Step 3: 过拟合检测 (需要 >= 16 快照) ──
    pbo_checked = False
    if n_snaps >= 16:
        try:
            from .overfitting import pbo_cscv
            # 构建收益矩阵 (需要从快照中提取)
            snap_files = sorted(META_DIR.glob("snapshot_*.json"))
            returns_data = _build_returns_matrix(snap_files[-20:])
            if returns_data is not None and len(returns_data) >= 16:
                pbo_result = pbo_cscv(returns_data)
                pbo_checked = True
                entry["steps"].append({
                    "name": "pbo_check",
                    "pbo": pbo_result.get("pbo", None),
                    "verdict": pbo_result.get("verdict", "unknown"),
                })
                log.info(f"  ✓ PBO 检测: {pbo_result.get('verdict', 'N/A')} "
                         f"(PBO={pbo_result.get('pbo', 0):.2%})")
                steps_run += 1
        except Exception as e:
            log.warning(f"  ✗ PBO 检测失败: {e}")
    else:
        log.info(f"  跳过 PBO (快照 {n_snaps} < 16)")

    # ── 保存训练日志 ──
    entry["steps_run"] = steps_run
    train_log.append(entry)
    _save_train_log(train_log)

    result = {
        "ok": True,
        "steps_run": steps_run,
        "ic_updated": ic_updated,
        "weights_changed": weights_changed,
        "alpha_evolved": alpha_evolved,
        "pbo_checked": pbo_checked,
        "n_snapshots": n_snaps,
        "training_entry": entry,
    }

    log.info(f"  ✅ 自适应训练完成: {steps_run} 步")
    return result


def _build_returns_matrix(snap_files: list) -> list | None:
    """从快照文件构建收益率矩阵 (用于 PBO)。

    Returns: list of list (T-1 periods x N coins)
    """
    if len(snap_files) < 2:
        return None

    snapshots = []
    for sf in snap_files:
        try:
            data = json.loads(sf.read_text())
            prices = {c["symbol"]: c["price"] for c in data.get("coins", [])[:30]
                      if c.get("price", 0) > 0}
            snapshots.append(prices)
        except Exception:
            continue

    if len(snapshots) < 2:
        return None

    # 找到所有快照共有的 symbols
    common = set(snapshots[0].keys())
    for s in snapshots[1:]:
        common &= set(s.keys())

    if len(common) < 5:
        return None

    syms = sorted(common)
    returns_matrix = []
    for i in range(1, len(snapshots)):
        period_returns = []
        for sym in syms:
            old = snapshots[i - 1].get(sym, 0)
            new = snapshots[i].get(sym, 0)
            if old > 0 and new > 0:
                period_returns.append((new - old) / old)
            else:
                period_returns.append(0)
        returns_matrix.append(period_returns)

    return returns_matrix


def get_training_summary(last_n: int = 7) -> dict:
    """获取最近 N 天的训练摘要。"""
    log_entries = _load_train_log()
    recent = log_entries[-last_n:]

    return {
        "total_entries": len(log_entries),
        "recent": recent,
        "total_ic_updates": sum(1 for e in recent if any(
            s.get("name") == "ic_update" for s in e.get("steps", [])
        )),
        "total_alpha_evolutions": sum(1 for e in recent if any(
            s.get("name") == "alpha_evolution" for s in e.get("steps", [])
        )),
    }
