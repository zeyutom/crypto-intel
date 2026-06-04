"""Walk-forward 组合回测框架。

模拟策略:
  1. 每个 rebalance 周期, 取筛选 Top-N 等权构建组合
  2. 持仓到下一个 rebalance 日, 按当时快照重新选币
  3. 计算: 累计收益、年化收益、Sharpe、MaxDD、Calmar、胜率、vs BTC 超额

数据源:
  - data/meta/snapshot_*.json (每日筛选快照, 含 price + composite_score)
  - CoinGecko 当前价格 (最后一段的收益)

设计:
  - 纯离线回测, 不需要 API (除了最后一天可选取当前价)
  - 支持 Top-N 参数 (默认 10)
  - 支持 rebalance 周期 (默认 7 天)
  - 输出 JSON + 可选 HTML 报告
"""
from __future__ import annotations
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("portfolio_backtest", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"


def _load_snapshots() -> list[dict]:
    """加载所有历史快照, 按日期排序。"""
    META_DIR.mkdir(parents=True, exist_ok=True)
    snap_files = sorted(META_DIR.glob("snapshot_*.json"))
    snapshots = []
    for sf in snap_files:
        try:
            data = json.loads(sf.read_text())
            # 确保有日期和 coins
            if data.get("coins") and data.get("date"):
                snapshots.append(data)
        except Exception:
            continue
    return snapshots


def _price_map(snap: dict) -> dict[str, float]:
    """从快照构建 symbol → price 映射。"""
    return {c["symbol"]: c["price"] for c in snap.get("coins", [])
            if c.get("price", 0) > 0}


def _top_n_symbols(snap: dict, n: int) -> list[str]:
    """从快照取 Top-N (按 composite_score 排序)。"""
    coins = snap.get("coins", [])
    ranked = sorted(coins, key=lambda c: c.get("composite_score", 0), reverse=True)
    return [c["symbol"] for c in ranked[:n] if c.get("price", 0) > 0]


# ====================================================================
#  核心回测引擎
# ====================================================================

def run_walkforward_backtest(
    top_n: int = 10,
    rebalance_days: int = 7,
    initial_capital: float = 10000.0,
) -> dict:
    """Walk-forward 回测 (legacy 纯 Python 引擎).

    NOTE (v0.9): 推荐改用 src.research.backtest_router.run_backtest() —
    它会自动检测 vectorbt 可用性, 优先用快的引擎; legacy 仍保留作为 fallback.

    Returns: {
        ok, total_return, annual_return, sharpe, max_drawdown, calmar,
        win_rate, vs_btc_excess, trades, equity_curve, period_returns,
        n_snapshots, n_rebalances, holdings_history
    }
    """
    snapshots = _load_snapshots()
    if len(snapshots) < 2:
        return {"ok": False, "error": f"快照不足 ({len(snapshots)}个), 需要至少 2 个快照才能回测"}

    log.info(f"Walk-forward 回测: {len(snapshots)} 快照, Top-{top_n}, "
             f"rebalance={rebalance_days}d, capital=${initial_capital:,.0f}")

    # 确定 rebalance 日期
    all_dates = [s["date"] for s in snapshots]
    rebalance_dates = [all_dates[0]]
    for d in all_dates[1:]:
        last_rb = rebalance_dates[-1]
        try:
            dt_last = datetime.strptime(last_rb, "%Y-%m-%d")
            dt_curr = datetime.strptime(d, "%Y-%m-%d")
            if (dt_curr - dt_last).days >= rebalance_days:
                rebalance_dates.append(d)
        except ValueError:
            continue

    # 确保最后一天也在列表中 (用于计算最后一段收益)
    if all_dates[-1] not in rebalance_dates:
        rebalance_dates.append(all_dates[-1])

    # 快照按日期索引
    snap_by_date = {s["date"]: s for s in snapshots}

    # 回测状态
    capital = initial_capital
    equity_curve = []       # [{date, equity, return_pct}]
    period_returns = []     # 每个 period 的收益率
    holdings_history = []   # [{date, symbols, weights, entry_prices}]
    trades = []             # [{date, action, symbols}]
    btc_start_price = None
    btc_end_price = None

    # 遍历 rebalance 周期
    for i in range(len(rebalance_dates) - 1):
        rb_date = rebalance_dates[i]
        next_date = rebalance_dates[i + 1]

        snap_entry = snap_by_date.get(rb_date)
        snap_exit = snap_by_date.get(next_date)
        if not snap_entry or not snap_exit:
            continue

        # 选币
        selected = _top_n_symbols(snap_entry, top_n)
        if not selected:
            continue

        entry_prices = _price_map(snap_entry)
        exit_prices = _price_map(snap_exit)

        # BTC 基准 (从第一个含 BTC 的快照开始计, 避免早期快照缺 BTC 时锁死为 0)
        if not btc_start_price and "BTC" in entry_prices:
            btc_start_price = entry_prices["BTC"]
        if "BTC" in exit_prices:
            btc_end_price = exit_prices["BTC"]

        # 等权分配
        weight = 1.0 / len(selected)
        period_return = 0.0
        n_valid = 0

        for sym in selected:
            ep = entry_prices.get(sym, 0)
            xp = exit_prices.get(sym, 0)
            if ep > 0 and xp > 0:
                coin_ret = (xp - ep) / ep
                period_return += coin_ret * weight
                n_valid += 1

        if n_valid < len(selected) and n_valid > 0:
            # 调整权重 (有些币没有价格)
            period_return *= len(selected) / n_valid

        capital *= (1 + period_return)
        period_returns.append(period_return)

        equity_curve.append({
            "date": next_date,
            "equity": round(capital, 2),
            "period_return": round(period_return, 4),
        })

        holdings_history.append({
            "entry_date": rb_date,
            "exit_date": next_date,
            "symbols": selected,
            "period_return": round(period_return, 4),
        })

        trades.append({
            "date": rb_date,
            "action": "rebalance",
            "n_coins": len(selected),
            "symbols": selected[:5],  # 只记前 5 个
        })

        log.info(f"  {rb_date} → {next_date}: Top-{len(selected)}, "
                 f"return={period_return:+.2%}, equity=${capital:,.0f}")

    # ── 统计指标 ──
    n_periods = len(period_returns)
    if n_periods == 0:
        return {"ok": False, "error": "无有效回测周期"}

    total_return = (capital - initial_capital) / initial_capital

    # 年化 (用真实日历跨度, 而非假设每期都恰好是 rebalance_days 天)
    try:
        _d0 = datetime.strptime(rebalance_dates[0], "%Y-%m-%d")
        _d1 = datetime.strptime(rebalance_dates[-1], "%Y-%m-%d")
        total_days = max((_d1 - _d0).days, 1)
    except (ValueError, IndexError):
        total_days = n_periods * rebalance_days
    annual_return = ((1 + total_return) ** (365 / max(total_days, 1))) - 1 if total_days > 0 else 0

    # Sharpe (年化, 假设无风险利率 = 4%)
    avg_ret = sum(period_returns) / n_periods
    std_ret = math.sqrt(sum((r - avg_ret) ** 2 for r in period_returns) / max(n_periods - 1, 1))
    avg_period_days = total_days / max(n_periods, 1)
    periods_per_year = 365 / avg_period_days if avg_period_days > 0 else 365 / rebalance_days
    risk_free_per_period = 0.04 / periods_per_year
    sharpe = ((avg_ret - risk_free_per_period) / std_ret * math.sqrt(periods_per_year)
              if std_ret > 0 else 0)

    # Max Drawdown
    peak = initial_capital
    max_dd = 0
    running = initial_capital
    for pr in period_returns:
        running *= (1 + pr)
        if running > peak:
            peak = running
        dd = (peak - running) / peak
        if dd > max_dd:
            max_dd = dd

    # Calmar
    calmar = annual_return / max_dd if max_dd > 0 else 0

    # Win Rate
    wins = sum(1 for r in period_returns if r > 0)
    win_rate = wins / n_periods

    # vs BTC
    btc_return = ((btc_end_price - btc_start_price) / btc_start_price
                  if btc_start_price and btc_start_price > 0 else 0)
    vs_btc = total_return - btc_return

    # Sortino (下行标准差)
    downside = [r for r in period_returns if r < 0]
    down_std = (math.sqrt(sum(r ** 2 for r in downside) / max(len(downside), 1))
                if downside else 0)
    sortino = ((avg_ret - risk_free_per_period) / down_std * math.sqrt(periods_per_year)
               if down_std > 0 else 0)

    result = {
        "ok": True,
        "initial_capital": initial_capital,
        "final_capital": round(capital, 2),
        "total_return": round(total_return, 4),
        "annual_return": round(annual_return, 4),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_drawdown": round(max_dd, 4),
        "calmar": round(calmar, 2),
        "win_rate": round(win_rate, 4),
        "avg_period_return": round(avg_ret, 4),
        "std_period_return": round(std_ret, 4),
        "btc_return": round(btc_return, 4),
        "vs_btc_excess": round(vs_btc, 4),
        "n_snapshots": len(snapshots),
        "n_rebalances": n_periods,
        "total_days": total_days,
        "top_n": top_n,
        "rebalance_days": rebalance_days,
        "date_range": f"{all_dates[0]} ~ {all_dates[-1]}",
        "equity_curve": equity_curve,
        "period_returns": [round(r, 4) for r in period_returns],
        "holdings_history": holdings_history[-10:],  # 最近 10 期
        "trades": trades[-10:],
    }

    log.info(f"\n{'='*60}")
    log.info(f"回测结果: {all_dates[0]} ~ {all_dates[-1]} ({total_days}d)")
    log.info(f"  总收益: {total_return:+.2%}  年化: {annual_return:+.2%}")
    log.info(f"  Sharpe: {sharpe:.2f}  Sortino: {sortino:.2f}  MaxDD: {max_dd:.2%}")
    log.info(f"  Calmar: {calmar:.2f}  胜率: {win_rate:.0%}")
    log.info(f"  vs BTC: {vs_btc:+.2%} (BTC: {btc_return:+.2%})")
    log.info(f"{'='*60}")

    return result


# ====================================================================
#  HTML 报告生成
# ====================================================================

def generate_backtest_report(result: dict) -> Path:
    """生成回测 HTML 报告。"""
    META_DIR.mkdir(parents=True, exist_ok=True)
    report_dir = META_DIR.parent / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    if not result.get("ok"):
        return None

    # 收益曲线数据
    ec = result.get("equity_curve", [])
    dates_js = json.dumps([e["date"] for e in ec])
    equity_js = json.dumps([e["equity"] for e in ec])
    returns_js = json.dumps([e["period_return"] * 100 for e in ec])

    # 持仓历史
    holdings = result.get("holdings_history", [])
    holdings_html = ""
    for h in reversed(holdings):
        ret = h["period_return"]
        color = "#22c55e" if ret > 0 else "#ef4444"
        syms = ", ".join(h["symbols"][:8])
        if len(h["symbols"]) > 8:
            syms += f" +{len(h['symbols'])-8}"
        holdings_html += f"""
        <tr>
            <td>{h['entry_date']}</td>
            <td>{h['exit_date']}</td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">{syms}</td>
            <td style="color:{color};font-weight:600">{ret:+.2%}</td>
        </tr>"""

    # 统计卡片
    tr = result["total_return"]
    ar = result["annual_return"]
    tr_color = "#22c55e" if tr > 0 else "#ef4444"
    ar_color = "#22c55e" if ar > 0 else "#ef4444"
    btc_excess = result["vs_btc_excess"]
    btc_color = "#22c55e" if btc_excess > 0 else "#ef4444"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Walk-forward 回测报告</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 24px; }}
  .header {{ text-align: center; margin-bottom: 32px; }}
  .header h1 {{ font-size: 28px; color: #f8fafc; }}
  .header p {{ color: #94a3b8; margin-top: 8px; }}
  .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 16px; margin-bottom: 32px; }}
  .stat {{ background: #1e293b; border-radius: 12px; padding: 16px; text-align: center; }}
  .stat .label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; }}
  .stat .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
  .card {{ background: #1e293b; border-radius: 12px; padding: 24px; margin-bottom: 24px; }}
  .card h2 {{ font-size: 18px; color: #f8fafc; margin-bottom: 16px; }}
  canvas {{ max-height: 300px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th {{ text-align: left; padding: 8px 12px; color: #94a3b8; border-bottom: 1px solid #334155; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #1e293b; }}
  .footer {{ text-align: center; color: #475569; font-size: 12px; margin-top: 32px; }}
</style>
</head>
<body>
  <div class="header">
    <h1>Walk-forward 组合回测</h1>
    <p>Top-{result['top_n']} 等权 · {result['rebalance_days']}天换仓 · {result['date_range']}</p>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="label">总收益</div>
      <div class="value" style="color:{tr_color}">{tr:+.2%}</div>
    </div>
    <div class="stat">
      <div class="label">年化收益</div>
      <div class="value" style="color:{ar_color}">{ar:+.2%}</div>
    </div>
    <div class="stat">
      <div class="label">Sharpe</div>
      <div class="value">{result['sharpe']:.2f}</div>
    </div>
    <div class="stat">
      <div class="label">Sortino</div>
      <div class="value">{result['sortino']:.2f}</div>
    </div>
    <div class="stat">
      <div class="label">Max Drawdown</div>
      <div class="value" style="color:#ef4444">{result['max_drawdown']:.2%}</div>
    </div>
    <div class="stat">
      <div class="label">Calmar</div>
      <div class="value">{result['calmar']:.2f}</div>
    </div>
    <div class="stat">
      <div class="label">胜率</div>
      <div class="value">{result['win_rate']:.0%}</div>
    </div>
    <div class="stat">
      <div class="label">vs BTC</div>
      <div class="value" style="color:{btc_color}">{btc_excess:+.2%}</div>
    </div>
  </div>

  <div class="card">
    <h2>净值曲线</h2>
    <canvas id="equityChart"></canvas>
  </div>

  <div class="card">
    <h2>各期收益</h2>
    <canvas id="returnsChart"></canvas>
  </div>

  <div class="card">
    <h2>最近持仓记录</h2>
    <table>
      <thead><tr><th>入场</th><th>出场</th><th>持仓</th><th>收益</th></tr></thead>
      <tbody>{holdings_html}</tbody>
    </table>
  </div>

  <div class="footer">
    CryptoIntel Walk-forward Backtest · Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>

<script>
const dates = {dates_js};
const equity = {equity_js};
const returns = {returns_js};

// 净值曲线
new Chart(document.getElementById('equityChart'), {{
  type: 'line',
  data: {{
    labels: dates,
    datasets: [{{
      label: '组合净值 ($)',
      data: equity,
      borderColor: '#3b82f6',
      backgroundColor: 'rgba(59,130,246,0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 3,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#94a3b8', callback: v => '$' + v.toLocaleString() }},
            grid: {{ color: '#1e293b' }} }},
    }},
  }}
}});

// 各期收益柱状图
new Chart(document.getElementById('returnsChart'), {{
  type: 'bar',
  data: {{
    labels: dates,
    datasets: [{{
      label: '期间收益 (%)',
      data: returns,
      backgroundColor: returns.map(r => r >= 0 ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)'),
      borderRadius: 4,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ labels: {{ color: '#94a3b8' }} }} }},
    scales: {{
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#1e293b' }} }},
      y: {{ ticks: {{ color: '#94a3b8', callback: v => v.toFixed(1) + '%' }},
            grid: {{ color: '#1e293b' }} }},
    }},
  }}
}});
</script>
</body>
</html>"""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = report_dir / f"backtest_{timestamp}.html"
    report_path.write_text(html, encoding="utf-8")
    log.info(f"回测报告: {report_path}")
    return report_path


# ====================================================================
#  参数扫描 (sensitivity analysis)
# ====================================================================

def run_parameter_sweep() -> dict:
    """扫描不同 Top-N 和 rebalance 周期的组合表现。"""
    results = []
    for top_n in [5, 10, 15, 20, 30]:
        for rb_days in [3, 7, 14]:
            r = run_walkforward_backtest(top_n=top_n, rebalance_days=rb_days)
            if r.get("ok"):
                results.append({
                    "top_n": top_n,
                    "rebalance_days": rb_days,
                    "total_return": r["total_return"],
                    "annual_return": r["annual_return"],
                    "sharpe": r["sharpe"],
                    "max_drawdown": r["max_drawdown"],
                    "win_rate": r["win_rate"],
                    "vs_btc": r["vs_btc_excess"],
                })

    if not results:
        return {"ok": False, "error": "无有效回测结果"}

    # 按 Sharpe 排序
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    return {
        "ok": True,
        "configs_tested": len(results),
        "best_config": results[0],
        "all_results": results,
    }
