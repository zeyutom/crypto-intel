"""Vectorbt-backed Walk-forward 回测 (Phase 2-A).

替换 portfolio_backtest.py 的纯 Python 循环, 用 vectorbt 的向量化引擎,
速度 10-100x 提升, 支持秒级参数扫描。

API 与 portfolio_backtest 保持兼容:
  - run_walkforward_backtest_vbt(top_n, rebalance_days) -> dict
  - run_parameter_sweep_vbt() -> dict
  - 返回字段一致 (ok/total_return/sharpe/max_drawdown/...)

设计:
  1. 读 data/meta/snapshot_*.json (与现有 portfolio_backtest 共用)
  2. 拼出 close/weight 矩阵 (T 天 x N 币)
  3. 调 vbt.Portfolio.from_orders 或 from_holding (优先 from_orders 支持手续费)
  4. 提取 stats() 输出, 映射到现有字段

软依赖:
  没装 vectorbt → 返回 {"ok": False, "error": "vectorbt not installed", "fallback": "portfolio_backtest"}
  调用方应捕获 fallback 并 fall through to 旧引擎。
"""
from __future__ import annotations
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..utils import setup_logger

log = setup_logger("portfolio_backtest_vbt", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"


def _vbt_available() -> bool:
    try:
        import vectorbt  # noqa
        return True
    except ImportError:
        return False


def is_available() -> bool:
    """供 oss-check 用。"""
    return _vbt_available()


def _load_snapshots() -> list[dict]:
    META_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for sf in sorted(META_DIR.glob("snapshot_*.json")):
        try:
            d = json.loads(sf.read_text())
            if d.get("coins") and d.get("date"):
                out.append(d)
        except Exception:
            continue
    return out


def _build_price_matrix(snapshots: list[dict]) -> "tuple[pd.DataFrame, pd.DataFrame]":
    """从快照构建 (close_df, score_df) 矩阵: rows=date, cols=symbol.

    snapshots 必须有 'date' 和 'coins'(含 symbol/price/composite_score)。
    """
    import pandas as pd

    rows_close = {}
    rows_score = {}
    all_symbols: set[str] = set()

    for snap in snapshots:
        date = snap["date"]
        cmap = {}
        smap = {}
        for c in snap.get("coins", []):
            sym = c.get("symbol")
            price = c.get("price")
            score = c.get("composite_score")
            if not sym or price is None or price <= 0:
                continue
            cmap[sym] = float(price)
            smap[sym] = float(score) if score is not None else 0.0
            all_symbols.add(sym)
        rows_close[date] = cmap
        rows_score[date] = smap

    syms = sorted(all_symbols)
    close = pd.DataFrame(rows_close).T.reindex(columns=syms)
    score = pd.DataFrame(rows_score).T.reindex(columns=syms)
    close.index = pd.to_datetime(close.index)
    score.index = pd.to_datetime(score.index)
    close = close.sort_index()
    score = score.sort_index()
    return close, score


def _top_n_weights(score: "pd.DataFrame", top_n: int) -> "pd.DataFrame":
    """每天选 Top-N 并等权 (NaN 视为 -inf)。

    返回 weight DataFrame, 同 shape as score, 每行非零总和 = 1。
    """
    import numpy as np
    import pandas as pd

    weights = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    sc = score.fillna(-1e9)
    for dt, row in sc.iterrows():
        topk = row.nlargest(top_n)
        topk = topk[topk > -1e8]  # 排除 NaN 占位
        if len(topk) == 0:
            continue
        w = 1.0 / len(topk)
        weights.loc[dt, topk.index] = w
    return weights


def _apply_rebalance_schedule(
    weights: "pd.DataFrame",
    rebalance_days: int,
) -> "pd.DataFrame":
    """只在 rebalance 日给出目标权重, 其他日 NaN (vectorbt 视为 hold)。

    用 ffill 把上一次 rebalance 的权重延续下来, 但首次 rebalance 之前为 0。
    """
    import numpy as np
    import pandas as pd

    if weights.empty:
        return weights

    dates = weights.index
    rb_idx = [0]
    last_dt = dates[0]
    for i, d in enumerate(dates[1:], start=1):
        if (d - last_dt).days >= rebalance_days:
            rb_idx.append(i)
            last_dt = d

    masked = pd.DataFrame(np.nan, index=weights.index, columns=weights.columns)
    for i in rb_idx:
        masked.iloc[i] = weights.iloc[i]
    return masked


def run_walkforward_backtest_vbt(
    top_n: int = 10,
    rebalance_days: int = 7,
    initial_capital: float = 10000.0,
    fee_rate: float = 0.001,  # 10 bps 滑点+手续费
) -> dict:
    """vectorbt 版本的 walk-forward 回测。

    Returns: 与 portfolio_backtest.run_walkforward_backtest 字段对齐
    """
    if not _vbt_available():
        return {
            "ok": False,
            "error": "vectorbt not installed",
            "fallback": "portfolio_backtest",
            "hint": "pip install vectorbt",
        }

    import numpy as np
    import pandas as pd
    import vectorbt as vbt

    snapshots = _load_snapshots()
    if len(snapshots) < 2:
        return {"ok": False, "error": f"快照不足 ({len(snapshots)} 个), 需 ≥2"}

    close, score = _build_price_matrix(snapshots)
    if close.empty or close.shape[1] == 0:
        return {"ok": False, "error": "价格矩阵为空"}

    # forward-fill 价格 (中间快照缺币时不破)
    close = close.ffill()

    # 选币 + 应用 rebalance 节奏
    target_w = _top_n_weights(score, top_n)
    schedule = _apply_rebalance_schedule(target_w, rebalance_days)

    # 用 from_orders: 给定每天目标权重的差额作为下单量
    # vectorbt 对 from_holding 的支持依版本差异较大, 这里用更鲁棒的 from_orders
    # 思路: 在 rebalance 日, 把上期持仓清零, 按新目标权重买入
    # 用实际采样间隔做年化 (快照索引有多天跳空, 不是连续日频)
    span_days = (close.index[-1] - close.index[0]).days
    n_rows = len(close.index)
    avg_gap_days = span_days / (n_rows - 1) if n_rows > 1 and span_days > 0 else 1.0

    portfolio = vbt.Portfolio.from_orders(
        close=close,
        size=schedule,                 # NaN(非 rebalance 日)= hold; 只在 rebalance 日按目标权重下单
        size_type="targetpercent",     # 把 size 解读成目标权重
        group_by=True,                 # 多列视为一个组合
        cash_sharing=True,
        fees=fee_rate,
        slippage=0.0,
        init_cash=initial_capital,
        freq=pd.Timedelta(days=avg_gap_days),  # 按真实平均间隔, vbt 年化用 365/avg_gap
    )

    stats = portfolio.stats()  # Series
    equity = portfolio.value().rename("equity")
    total_return = float(portfolio.total_return())

    # 年化 (按实际天数)
    days = (close.index[-1] - close.index[0]).days
    if days > 0:
        annual_return = (1 + total_return) ** (365.0 / days) - 1
    else:
        annual_return = 0.0

    sharpe = float(stats.get("Sharpe Ratio", 0.0)) if not math.isnan(stats.get("Sharpe Ratio", float("nan"))) else 0.0
    max_dd = float(stats.get("Max Drawdown [%]", 0.0)) / 100.0

    # 胜率: 按 rebalance 周期收益正占比 (与 legacy 引擎口径一致),
    # 而非 vbt 的 per-trade Win Rate [%] (按单笔成交统计, 两者不可比)。
    rb_mask = schedule.notna().any(axis=1)
    eq_rb = equity[rb_mask]
    period_rets = eq_rb.pct_change().dropna()
    win_rate = float((period_rets > 0).mean()) if len(period_rets) else 0.5

    # vs BTC
    vs_btc = None
    if "BTC" in close.columns:
        btc = close["BTC"].dropna()
        if len(btc) >= 2:
            btc_ret = btc.iloc[-1] / btc.iloc[0] - 1
            vs_btc = total_return - btc_ret

    # 计算 calmar
    calmar = (annual_return / abs(max_dd)) if max_dd != 0 else 0.0

    return {
        "ok": True,
        "engine": "vectorbt",
        "top_n": top_n,
        "rebalance_days": rebalance_days,
        "fee_rate": fee_rate,
        "total_return": round(total_return, 6),
        "annual_return": round(annual_return, 6),
        "sharpe": round(sharpe, 4),
        "max_drawdown": round(max_dd, 6),
        "calmar": round(calmar, 4),
        "win_rate": round(win_rate, 4),
        "vs_btc_excess": round(vs_btc, 6) if vs_btc is not None else None,
        "n_snapshots": len(snapshots),
        "n_periods": int((schedule.notna().any(axis=1)).sum()),
        "start_date": str(close.index[0].date()),
        "end_date": str(close.index[-1].date()),
        "final_equity": round(float(equity.iloc[-1]), 2),
        "equity_curve": [
            {"date": str(d.date()), "equity": round(float(v), 2)}
            for d, v in equity.items()
        ],
    }


def run_parameter_sweep_vbt(
    top_n_grid: list[int] = None,
    rebalance_grid: list[int] = None,
    fee_rate: float = 0.001,
    diagnose_overfitting: bool = True,
) -> dict:
    """秒级参数扫描 (vectorbt 内部并行化好, 但我们简单串行调用).

    Phase 2.5 新增: diagnose_overfitting=True 会对所有配置的收益矩阵
    跑 PBO + DSR + 多重检验诊断, 把 best_config 标上过拟合 verdict.
    """
    if not _vbt_available():
        return {
            "ok": False,
            "error": "vectorbt not installed",
            "fallback": "portfolio_backtest.run_parameter_sweep",
        }

    top_n_grid = top_n_grid or [5, 10, 15, 20, 30]
    rebalance_grid = rebalance_grid or [3, 7, 14]

    import time
    t0 = time.time()
    results = []
    equity_curves: list[list[float]] = []   # 每个配置一条 equity 曲线 (用于 PBO)
    for top_n in top_n_grid:
        for rb in rebalance_grid:
            r = run_walkforward_backtest_vbt(
                top_n=top_n, rebalance_days=rb, fee_rate=fee_rate
            )
            if r.get("ok"):
                results.append({
                    "top_n": top_n,
                    "rebalance_days": rb,
                    "total_return": r["total_return"],
                    "annual_return": r["annual_return"],
                    "sharpe": r["sharpe"],
                    "max_drawdown": r["max_drawdown"],
                    "calmar": r["calmar"],
                    "win_rate": r["win_rate"],
                    "vs_btc": r.get("vs_btc_excess"),
                })
                # 提取 equity 曲线 → 转 daily return
                eq = r.get("equity_curve", [])
                if eq:
                    equity_curves.append([row["equity"] for row in eq])
    elapsed = time.time() - t0

    if not results:
        return {"ok": False, "error": "无有效结果"}

    results.sort(key=lambda x: x["sharpe"], reverse=True)

    # ─── 过拟合诊断 ───────────────────────────────────────────────────
    overfit = None
    if diagnose_overfitting and len(equity_curves) >= 2:
        try:
            import numpy as np
            from . import overfitting as of_mod
            # 把每条 equity 转 daily return; 不同曲线长度对齐到最短
            min_len = min(len(c) for c in equity_curves)
            if min_len >= 5:
                rets = []
                for c in equity_curves:
                    arr = np.asarray(c[:min_len], dtype=float)
                    r = np.diff(arr) / arr[:-1]
                    rets.append(r)
                R = np.column_stack(rets)  # (T-1, N_configs)
                overfit = of_mod.diagnose_backtest(R, n_splits=min(16, R.shape[0] // 2))
        except Exception as e:
            log.warning(f"overfit diagnosis failed: {e}")
            overfit = {"error": str(e)}

    out = {
        "ok": True,
        "engine": "vectorbt",
        "configs_tested": len(results),
        "elapsed_seconds": round(elapsed, 2),
        "best_config": results[0],
        "all_results": results,
    }
    if overfit is not None:
        out["overfit"] = overfit
        # 在 best_config 上盖一个 warning 标记
        out["best_config"]["overfit_verdict"] = overfit.get("overall_verdict", "n/a")
    return out


# ====================================================================
#  Self-test
# ====================================================================

if __name__ == "__main__":
    print(f"[portfolio_backtest_vbt] vectorbt available: {_vbt_available()}")
    if _vbt_available():
        r = run_walkforward_backtest_vbt(top_n=10, rebalance_days=7)
        print(json.dumps({k: v for k, v in r.items() if k != "equity_curve"},
                        indent=2, ensure_ascii=False, default=str))
    else:
        print("install: pip install vectorbt --break-system-packages")
