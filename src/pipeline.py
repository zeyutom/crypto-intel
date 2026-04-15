"""工作流编排: 对外暴露 run_* 入口,供 CLI 与 Scheduler 共用。"""
from .utils import setup_logger
from .config import CFG
from .db import init_db, upsert_raw
from .adapters import ALL_ADAPTERS
from .factors import ALL_FACTORS
from .db import upsert_factor
from .signals.composite import compose
from .review import cross_price, ic_monitor
from .report.daily import generate as generate_report


log = setup_logger("pipeline", CFG["output"]["log_level"])


def run_ingest_all() -> dict[str, int]:
    """运行所有启用的 adapter, 返回每个 source 的入库行数。"""
    init_db()
    stats = {}
    for name, mod in ALL_ADAPTERS.items():
        try:
            rows = mod.fetch()
            n = upsert_raw(rows)
            stats[name] = n
            log.info("[ingest] %-14s %d rows", name, n)
        except Exception as e:
            log.warning("[ingest] %-14s FAILED: %s", name, e)
            stats[name] = -1
    return stats


def run_factors_all() -> dict[str, int]:
    """运行所有因子计算。"""
    stats = {}
    for name, mod in ALL_FACTORS.items():
        try:
            rows = mod.compute()
            n = upsert_factor(rows)
            stats[name] = n
            log.info("[factor] %-24s %d rows", name, n)
        except Exception as e:
            log.warning("[factor] %-24s FAILED: %s", name, e)
            stats[name] = -1
    # 合成信号
    try:
        sigs = compose()
        log.info("[signal] composed %d assets", len(sigs))
    except Exception as e:
        log.warning("[signal] FAILED: %s", e)
    return stats


def run_reviews_all() -> dict[str, int]:
    stats = {}
    try:
        stats["price_cross"] = len(cross_price.run())
        log.info("[review] price_cross %d", stats["price_cross"])
    except Exception as e:
        log.warning("[review] price_cross FAILED: %s", e)
    try:
        stats["ic_monitor"] = len(ic_monitor.run())
        log.info("[review] ic_monitor %d", stats["ic_monitor"])
    except Exception as e:
        log.warning("[review] ic_monitor FAILED: %s", e)
    return stats


def run_snapshot() -> int:
    """v0.5: 每日因子快照 (为 IC/IR 回测提供基础)。"""
    try:
        from .snapshot import take_daily_snapshot
        n = take_daily_snapshot()
        log.info("[snapshot] took %d factor snapshots today", n)
        return n
    except Exception as e:
        log.warning("[snapshot] FAILED: %s", e)
        return 0


def run_backtest() -> int:
    """v0.5: 跑 IC/IR 回测 (通常在 snapshot 之后)。"""
    try:
        from .review.backtest import run_backtest_all
        n = run_backtest_all()
        log.info("[backtest] computed %d performance rows", n)
        return n
    except Exception as e:
        log.warning("[backtest] FAILED: %s", e)
        return 0


def run_report():
    path = generate_report()
    log.info("[report] generated: %s", path)
    return path


def run_llm_brief() -> dict:
    """生成 Claude Opus 智能简报 (需要 ANTHROPIC_API_KEY)。"""
    try:
        from .llm_brief import generate_brief, save_brief
    except Exception as e:
        log.warning("[llm] import failed: %s", e)
        return {"ok": False, "error": str(e)}
    try:
        brief = generate_brief()
        if brief.get("ok"):
            save_brief(brief)
            log.info("[llm] brief saved (%d output tokens)",
                     brief["usage"].get("output_tokens", 0))
        else:
            log.warning("[llm] brief failed: %s", brief.get("error"))
        return brief
    except Exception as e:
        log.warning("[llm] FAILED: %s", e)
        return {"ok": False, "error": str(e)}


def run_all_once(skip_llm: bool = False) -> dict:
    """一键跑: ingest → factor → snapshot → backtest → review → llm brief → report。"""
    ing = run_ingest_all()
    fac = run_factors_all()
    snap = run_snapshot()        # v0.5: 每日快照
    bt = run_backtest()          # v0.5: 真实 IC/IR
    rev = run_reviews_all()
    llm_meta = {"skipped": True}
    if not skip_llm:
        llm = run_llm_brief()
        llm_meta = {
            "ok": llm.get("ok", False),
            "model": llm.get("model"),
            "tokens_in": llm.get("usage", {}).get("input_tokens"),
            "tokens_out": llm.get("usage", {}).get("output_tokens"),
            "error": llm.get("error"),
        }
    path = run_report()
    return {"ingest": ing, "factor": fac, "snapshot": snap, "backtest": bt,
            "review": rev, "llm_brief": llm_meta, "report_path": str(path)}
