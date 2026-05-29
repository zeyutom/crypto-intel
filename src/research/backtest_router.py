"""回测引擎路由 (W2-S2 facade).

把 portfolio_backtest (legacy 纯 Python) 和 portfolio_backtest_vbt (vectorbt)
两个引擎收敛到一个入口:
  - 优先 vectorbt (装了的话, 速度 10-100x)
  - 否则 fallback 到 legacy
  - 返回字段统一 (映射差异化字段)

用户视角:
  from src.research.backtest_router import run_backtest, run_sweep
  res = run_backtest(top_n=10, rebalance_days=7)
  # res["engine"] 告诉你实际用了哪个

弃用警告:
  legacy 引擎仍可独立调 (CLI backtest-wf), 但建议走 router.
  vectorbt 引擎也仍可独立调 (CLI backtest-vbt), 但 router 更智能.
"""
from __future__ import annotations
import warnings
from typing import Literal

from ..utils import setup_logger

log = setup_logger("backtest_router", "INFO")


def detect_engine() -> Literal["vbt", "legacy"]:
    """检测哪个引擎可用. 优先 vectorbt."""
    try:
        import vectorbt  # noqa
        return "vbt"
    except ImportError:
        return "legacy"


# 字段映射: 把 legacy/vbt 各自的返回字段统一到一个 schema
UNIFIED_FIELDS = [
    "ok", "engine", "total_return", "annual_return", "sharpe", "max_drawdown",
    "calmar", "win_rate", "vs_btc_excess", "top_n", "rebalance_days",
    "n_snapshots", "n_periods", "final_capital",
    "start_date", "end_date",
]


def _normalize(result: dict, engine: str) -> dict:
    """把 result 投射到 UNIFIED_FIELDS, 缺失字段补 None."""
    out = {}
    for k in UNIFIED_FIELDS:
        if k == "engine":
            out[k] = engine    # 用 router 选的引擎名, 不被 result 覆盖
        elif k in result:
            out[k] = result[k]
        elif k == "n_periods" and "n_rebalances" in result:
            out[k] = result["n_rebalances"]
        elif k == "final_capital":
            out[k] = result.get("final_capital") or result.get("final_equity")
        elif k == "start_date":
            dr = result.get("date_range", "")
            out[k] = dr.split(" ~ ")[0] if " ~ " in dr else result.get("start_date")
        elif k == "end_date":
            dr = result.get("date_range", "")
            out[k] = dr.split(" ~ ")[-1] if " ~ " in dr else result.get("end_date")
        else:
            out[k] = result.get(k)
    # 透传一些详细字段 (legacy 有 equity_curve, vbt 也有)
    if "equity_curve" in result:
        out["equity_curve"] = result["equity_curve"]
    if "overfit" in result:
        out["overfit"] = result["overfit"]
    return out


def run_backtest(
    top_n: int = 10,
    rebalance_days: int = 7,
    initial_capital: float = 10000.0,
    engine: Literal["auto", "vbt", "legacy"] = "auto",
    fee_rate: float = 0.001,
) -> dict:
    """统一回测入口.

    Args:
        engine: "auto" 自动检测 vbt 优先, "vbt" 强制 vectorbt, "legacy" 强制旧引擎
        fee_rate: 仅 vbt 引擎支持; legacy 引擎忽略此参数

    Returns:
        统一字段 dict + engine 字段标明用了哪个
    """
    chosen = engine
    if chosen == "auto":
        chosen = detect_engine()

    if chosen == "vbt":
        from .portfolio_backtest_vbt import run_walkforward_backtest_vbt, is_available
        if not is_available():
            log.warning("vbt 引擎不可用, fallback 到 legacy")
            chosen = "legacy"
        else:
            res = run_walkforward_backtest_vbt(
                top_n=top_n, rebalance_days=rebalance_days,
                initial_capital=initial_capital, fee_rate=fee_rate,
            )
            return _normalize(res, "vectorbt")

    # legacy 路径
    from .portfolio_backtest import run_walkforward_backtest
    res = run_walkforward_backtest(
        top_n=top_n, rebalance_days=rebalance_days,
        initial_capital=initial_capital,
    )
    return _normalize(res, "legacy")


def run_sweep(
    engine: Literal["auto", "vbt", "legacy"] = "auto",
    top_n_grid: list[int] = None,
    rebalance_grid: list[int] = None,
    fee_rate: float = 0.001,
    diagnose_overfitting: bool = True,
) -> dict:
    """统一参数扫描入口."""
    chosen = engine
    if chosen == "auto":
        chosen = detect_engine()

    if chosen == "vbt":
        from .portfolio_backtest_vbt import (
            run_parameter_sweep_vbt, is_available,
        )
        if is_available():
            return run_parameter_sweep_vbt(
                top_n_grid=top_n_grid, rebalance_grid=rebalance_grid,
                fee_rate=fee_rate, diagnose_overfitting=diagnose_overfitting,
            )
        log.warning("vbt 不可用, fallback 到 legacy")

    from .portfolio_backtest import run_parameter_sweep
    return run_parameter_sweep()


def is_available() -> bool:
    """oss-check 友好接口."""
    return True   # router 总是可用 (至少 legacy 引擎能跑)


def health() -> dict:
    return {
        "available_engines": {
            "legacy": True,
            "vectorbt": detect_engine() == "vbt",
        },
        "auto_chosen": detect_engine(),
    }


# ────────────────────────────────────────────────────────────────────
#  弃用警告 (老入口直接调时提示一次)
# ────────────────────────────────────────────────────────────────────

_DEPRECATION_WARNED = set()


def _warn_legacy_call(caller: str):
    """让老调用方知道有 router 可用 (只提示一次, 不打扰)."""
    if caller in _DEPRECATION_WARNED:
        return
    _DEPRECATION_WARNED.add(caller)
    warnings.warn(
        f"{caller}: 建议改用 src.research.backtest_router.run_backtest() "
        "以自动选择最快引擎. 当前仍直连旧 API, 不影响功能.",
        DeprecationWarning, stacklevel=3,
    )
