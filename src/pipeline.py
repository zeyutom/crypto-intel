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


def run_report():
    path = generate_report()
    log.info("[report] generated: %s", path)
    return path


def run_all_once() -> dict:
    """一键跑: ingest → factor → review → report。"""
    ing = run_ingest_all()
    fac = run_factors_all()
    rev = run_reviews_all()
    path = run_report()
    return {"ingest": ing, "factor": fac, "review": rev, "report_path": str(path)}
