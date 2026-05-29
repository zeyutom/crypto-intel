"""统一仪表盘 — 一页看全局。

生成一个自包含 HTML, 包含:
  1. 市场概览: Regime / 恐贪指数 / BTC 价格 / 总市值
  2. 筛选 Top-10 + Swarm 共识
  3. 回测净值曲线 (Chart.js)
  4. 因子 IC 健康热力图
  5. 风控状态 (回撤 / 仓位乘数 / 告警)
  6. Alpha 因子池状态
  7. 训练日志 (最近 7 天)

灵感:
  - Vibe-Trading: Observable decision trace
  - OctoBot Dashboard: 实时面板
  - 51bitquant AI-Hedge-Fund: Portfolio overview
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("dashboard", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"
REPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "reports"


def _safe_load(path: Path) -> dict | list | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def generate_dashboard() -> Path:
    """生成统一仪表盘 HTML。"""
    META_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 收集数据 ──

    # 1. 最新快照
    snap_files = sorted(META_DIR.glob("snapshot_*.json"))
    latest_snap = None
    if snap_files:
        latest_snap = _safe_load(snap_files[-1])

    top_coins = latest_snap.get("coins", [])[:10] if latest_snap else []
    regime = latest_snap.get("regime", "unknown") if latest_snap else "unknown"
    snap_date = latest_snap.get("date", "N/A") if latest_snap else "N/A"

    # 2. Swarm trace (最新)
    swarm_files = sorted(META_DIR.glob("swarm_trace_*.json"))
    swarm_data = _safe_load(swarm_files[-1]) if swarm_files else None

    # 3. 因子配置
    factor_cfg = _safe_load(META_DIR / "factor_config.json") or {}

    # 4. 回测权益曲线 (最新报告)
    backtest_files = sorted(REPORT_DIR.glob("backtest_*.html"))

    # 5. Alpha 候选池
    alpha_pool = _safe_load(
        Path(__file__).resolve().parents[2] / "data" / "alpha_candidates" / "candidates.json"
    ) or {}

    # 6. 训练日志
    train_log = _safe_load(META_DIR / "training_log.json") or []
    recent_training = train_log[-7:] if train_log else []

    # 7. Watchdog 告警
    alerts_path = Path(__file__).resolve().parents[2] / "data" / "alerts.log"
    recent_alerts = []
    if alerts_path.exists():
        lines = alerts_path.read_text().strip().split("\n")[-10:]
        recent_alerts = [l for l in lines if l.strip()]

    # ── 构建 HTML ──
    regime_map = {
        "bull": ("🐂 牛市", "#22c55e"),
        "bear": ("🐻 熊市", "#ef4444"),
        "sideways": ("➡️ 震荡", "#eab308"),
        "volatile": ("🌊 高波动", "#f97316"),
    }
    regime_label, regime_color = regime_map.get(regime, ("❓ 未知", "#94a3b8"))

    # Top coins HTML
    coins_html = ""
    for i, c in enumerate(top_coins, 1):
        chg = c.get("change_30d", 0) or 0
        chg_color = "#22c55e" if chg > 0 else "#ef4444"
        score = c.get("composite_score", 0)
        coins_html += f"""
        <tr>
          <td style="font-weight:600">{i}</td>
          <td style="font-weight:700">{c.get('symbol','')}</td>
          <td>${c.get('price',0):,.4f}</td>
          <td>${c.get('market_cap',0)/1e9:.2f}B</td>
          <td style="color:{chg_color}">{chg:+.1f}%</td>
          <td style="color:#3b82f6;font-weight:600">{score:.4f}</td>
        </tr>"""

    # Swarm consensus cards
    swarm_html = ""
    if swarm_data and swarm_data.get("top"):
        for s in swarm_data["top"][:5]:
            cons = s.get("consensus", "weak")
            cons_color = {"strong": "#22c55e", "moderate": "#eab308", "weak": "#ef4444"}.get(cons, "#94a3b8")
            signals = "; ".join(s.get("top_signals", [])[:3])
            swarm_html += f"""
            <div style="background:#1e293b;border-radius:8px;padding:12px;margin-bottom:8px;
                        border-left:4px solid {cons_color}">
              <span style="font-weight:700;font-size:16px">{s['symbol']}</span>
              <span style="float:right;color:{cons_color}">{cons.upper()}</span>
              <div style="font-size:12px;color:#94a3b8;margin-top:4px">{signals}</div>
            </div>"""
    else:
        swarm_html = '<p style="color:#64748b">尚未运行 Swarm Decision</p>'

    # Factor health
    factors_html = ""
    for fname, fdata in factor_cfg.get("factors", {}).items():
        weight = fdata.get("weight", 0)
        ic_hist = fdata.get("ic_history", [])
        last_ic = ic_hist[-1].get("ic", 0) if ic_hist else 0
        ic_color = "#22c55e" if last_ic > 0.05 else "#ef4444" if last_ic < -0.05 else "#eab308"
        bar_w = int(weight * 300)
        factors_html += f"""
        <div style="display:flex;align-items:center;margin-bottom:6px;font-size:13px">
          <span style="width:140px;color:#cbd5e1">{fname}</span>
          <div style="width:{bar_w}px;height:14px;background:#3b82f6;border-radius:3px;margin-right:8px"></div>
          <span style="width:50px">{weight:.1%}</span>
          <span style="color:{ic_color};width:70px">IC {last_ic:+.3f}</span>
        </div>"""

    # Alpha pool
    n_candidates = len(alpha_pool.get("candidates", {}))
    n_graduated = len(alpha_pool.get("graduated", []))
    n_retired = len(alpha_pool.get("retired", []))

    # Training log
    train_html = ""
    for te in reversed(recent_training):
        steps = te.get("steps", [])
        step_names = ", ".join(s.get("name", "?") for s in steps) or "无操作"
        train_html += f"""
        <div style="display:flex;justify-content:space-between;padding:4px 0;
                    font-size:13px;border-bottom:1px solid #1e293b">
          <span>{te.get('date', 'N/A')}</span>
          <span style="color:#94a3b8">{step_names}</span>
          <span>{te.get('steps_run', 0)} 步</span>
        </div>"""

    # Alerts
    alerts_html = ""
    for alert in recent_alerts[-5:]:
        alerts_html += f'<div style="font-size:12px;color:#f97316;padding:2px 0">⚠️ {alert[:100]}</div>'
    if not alerts_html:
        alerts_html = '<div style="color:#22c55e;font-size:13px">无告警</div>'

    n_snaps = len(snap_files)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>CryptoIntel Dashboard</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         background:#0f172a; color:#e2e8f0; padding:20px; }}
  .header {{ text-align:center; margin-bottom:24px; }}
  .header h1 {{ font-size:24px; color:#f8fafc; }}
  .header .sub {{ color:#64748b; font-size:13px; margin-top:4px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr)); gap:16px; }}
  .card {{ background:#1a2332; border-radius:12px; padding:20px; }}
  .card h2 {{ font-size:15px; color:#94a3b8; margin-bottom:12px; text-transform:uppercase;
              letter-spacing:1px; }}
  .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }}
  .stat {{ background:#1e293b; border-radius:10px; padding:14px; text-align:center; }}
  .stat .label {{ font-size:11px; color:#64748b; text-transform:uppercase; }}
  .stat .value {{ font-size:22px; font-weight:700; margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; padding:6px 8px; color:#64748b; border-bottom:1px solid #334155;
       font-size:11px; text-transform:uppercase; }}
  td {{ padding:6px 8px; border-bottom:1px solid #1e293b; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:12px;
            font-weight:600; }}
</style>
</head>
<body>
  <div class="header">
    <h1>CryptoIntel 量化仪表盘</h1>
    <div class="sub">数据: {snap_date} · {n_snaps} 天快照 · 自动刷新: daily-screen</div>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="label">Regime</div>
      <div class="value" style="color:{regime_color};font-size:18px">{regime_label}</div>
    </div>
    <div class="stat">
      <div class="label">快照天数</div>
      <div class="value">{n_snaps}</div>
    </div>
    <div class="stat">
      <div class="label">Alpha 候选</div>
      <div class="value">{n_candidates}</div>
    </div>
    <div class="stat">
      <div class="label">已毕业因子</div>
      <div class="value" style="color:#22c55e">{n_graduated}</div>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>📊 Top-10 筛选结果</h2>
      <table>
        <thead><tr><th>#</th><th>Symbol</th><th>Price</th><th>MCap</th><th>30d</th><th>Score</th></tr></thead>
        <tbody>{coins_html}</tbody>
      </table>
    </div>

    <div class="card">
      <h2>🐝 Swarm 共识 (Top-5)</h2>
      {swarm_html}
    </div>

    <div class="card">
      <h2>📈 因子 IC 健康</h2>
      {factors_html if factors_html else '<p style="color:#64748b">尚无因子数据</p>'}
    </div>

    <div class="card">
      <h2>🧬 Alpha 因子池</h2>
      <div style="display:flex;gap:20px;margin-bottom:12px">
        <div><span style="font-size:28px;font-weight:700">{n_candidates}</span>
             <span style="color:#64748b;font-size:12px"> 候选中</span></div>
        <div><span style="font-size:28px;font-weight:700;color:#22c55e">{n_graduated}</span>
             <span style="color:#64748b;font-size:12px"> 已毕业</span></div>
        <div><span style="font-size:28px;font-weight:700;color:#ef4444">{n_retired}</span>
             <span style="color:#64748b;font-size:12px"> 已淘汰</span></div>
      </div>
    </div>

    <div class="card">
      <h2>🛡️ 风险告警</h2>
      {alerts_html}
    </div>

    <div class="card">
      <h2>🔄 训练日志 (最近 7 天)</h2>
      {train_html if train_html else '<p style="color:#64748b">尚无训练记录</p>'}
    </div>
  </div>

  <div style="text-align:center;color:#334155;font-size:11px;margin-top:24px">
    CryptoIntel v3 Dashboard · Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}
  </div>
</body>
</html>"""

    ts = datetime.now().strftime("%Y%m%d_%H%M")
    out = REPORT_DIR / f"dashboard_{ts}.html"
    out.write_text(html, encoding="utf-8")
    log.info(f"仪表盘: {out}")
    return out
