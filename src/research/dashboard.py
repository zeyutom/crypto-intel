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

另外放 Streamlit 页面 (pages/7 因子有效性, pages/9 历史复盘) 用的纯函数:
页面脚本 import 即渲染、没法单测, 口径逻辑放这里由 tests/test_dash_*.py 覆盖。
"""
from __future__ import annotations
import html
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("dashboard", "INFO")

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
META_DIR = DATA_DIR / "meta"
REPORT_DIR = DATA_DIR / "reports"


def _safe_load(path: Path) -> dict | list | None:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
    return None


def _snapshot_regime(snap: dict | None, factor_cfg: dict | None) -> str:
    """快照的 regime: schema 2 存在 _meta.regime; 兼容旧的顶层 regime 键,
    再退回 factor_config 的 regime (该键可能被移除), 都没有 → unknown。"""
    snap = snap or {}
    return ((snap.get("_meta") or {}).get("regime")
            or snap.get("regime")
            or (factor_cfg or {}).get("regime")
            or "unknown")


def _scan_real_snapshots(snap_files: list[Path]) -> tuple[dict | None, int]:
    """扫描快照文件 → (最新一个真实快照, 真实快照的不同日期数)。

    backfill 合成快照 (*_0000 / _meta.synthetic / 币数不足) 不算: 之前直接取最后一个
    文件、按文件数算"天数", 把 99 个合成日期和同日多文件都算进去 (319 "天" vs 实际 143)。
    """
    from .returns_tracker import _is_real_snapshot
    latest = None
    dates: set[str] = set()
    for p in snap_files:
        d = _safe_load(p)
        if not isinstance(d, dict) or not d.get("coins") or not _is_real_snapshot(p, d):
            continue
        latest = d
        dates.add(d.get("date") or p.stem.split("_")[1])
    return latest, len(dates)


def _factor_rows_html(factor_cfg: dict) -> str:
    """因子 IC 健康卡片: 停用 / 锁定的因子要标出来, 没有 IC 历史显示 n/a 而不是 +0.000。"""
    out = ""
    for fname, fdata in (factor_cfg.get("factors") or {}).items():
        weight = fdata.get("weight", 0) or 0
        ic_hist = fdata.get("ic_history") or []
        last_ic = ic_hist[-1].get("ic") if ic_hist else None
        if isinstance(last_ic, (int, float)) and math.isfinite(last_ic):
            ic_color = "#22c55e" if last_ic > 0.05 else "#ef4444" if last_ic < -0.05 else "#eab308"
            ic_text = f"IC {last_ic:+.3f}"
        else:
            ic_color, ic_text = "#64748b", "IC n/a"
        enabled = fdata.get("enabled", True) is not False
        locked = bool(fdata.get("locked"))
        badges = ""
        if locked:
            badges += ('<span class="badge" style="background:#334155;color:#cbd5e1;'
                       'margin-left:6px">🔒 锁定</span>')
        if not enabled:
            badges += ('<span class="badge" style="background:#7f1d1d;color:#fecaca;'
                       'margin-left:6px">停用</span>')
        name_color = "#cbd5e1" if (enabled and not locked) else "#64748b"
        reason = fdata.get("disabled_reason") or fdata.get("lock_reason") or ""
        reason_html = ""
        if reason and (locked or not enabled):
            reason_html = (f'<div style="font-size:11px;color:#64748b;margin:-4px 0 6px 0">'
                           f'{html.escape(str(reason)[:160])}</div>')
        bar_w = int(weight * 300)
        out += f"""
        <div style="display:flex;align-items:center;margin-bottom:6px;font-size:13px">
          <span style="width:140px;color:{name_color}">{fname}</span>
          <div style="width:{bar_w}px;height:14px;background:#3b82f6;border-radius:3px;margin-right:8px"></div>
          <span style="width:50px">{weight:.1%}</span>
          <span style="color:{ic_color};width:70px">{ic_text}</span>{badges}
        </div>{reason_html}"""
    return out


def generate_dashboard() -> Path:
    """生成统一仪表盘 HTML。"""
    META_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 收集数据 ──

    # 1. 最新快照 — 只认真实筛选快照; 一个真实快照都没有时才退回最新文件
    snap_files = sorted(META_DIR.glob("snapshot_*.json"))
    latest_snap, n_snaps = _scan_real_snapshots(snap_files)
    if latest_snap is None and snap_files:
        latest_snap = _safe_load(snap_files[-1])

    top_coins = latest_snap.get("coins", [])[:10] if latest_snap else []
    snap_date = latest_snap.get("date", "N/A") if latest_snap else "N/A"

    # 2. Swarm trace (最新)
    swarm_files = sorted(META_DIR.glob("swarm_trace_*.json"))
    swarm_data = _safe_load(swarm_files[-1]) if swarm_files else None

    # 3. 因子配置
    factor_cfg = _safe_load(META_DIR / "factor_config.json") or {}
    regime = _snapshot_regime(latest_snap, factor_cfg)

    # 4. 回测权益曲线 (最新报告)
    backtest_files = sorted(REPORT_DIR.glob("backtest_*.html"))

    # 5. Alpha 候选池
    alpha_pool = _safe_load(DATA_DIR / "alpha_candidates" / "candidates.json") or {}

    # 6. 训练日志
    train_log = _safe_load(META_DIR / "training_log.json") or []
    recent_training = train_log[-7:] if train_log else []

    # 7. Watchdog 告警
    alerts_path = DATA_DIR / "alerts.log"
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
    factors_html = _factor_rows_html(factor_cfg)

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
    <div class="sub">数据: {snap_date} · {n_snaps} 天真实快照 · 自动刷新: daily-screen</div>
  </div>

  <div class="stats">
    <div class="stat">
      <div class="label">Regime</div>
      <div class="value" style="color:{regime_color};font-size:18px">{regime_label}</div>
    </div>
    <div class="stat">
      <div class="label">真实快照天数</div>
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


# ====================================================================
#  pages/7 因子有效性 — 与 signals.composite 同口径的 IR 展示
# ====================================================================

def factor_mean_ir(ir_w: dict) -> dict[str, float]:
    """get_latest_ir_weights() 的 {(factor, asset_id): ir} → {factor: 跨 asset 均值}。

    与 signals.composite._effective_weights 完全同口径 (调权实际用的就是这个均值);
    页面之前用全部 window/forward 行的 max IR 排行, 与真实调权输入不一致。
    """
    by_factor: dict[str, list[float]] = {}
    for (factor, _aid), ir in ir_w.items():
        by_factor.setdefault(factor, []).append(ir)
    return {f: sum(v) / len(v) for f, v in by_factor.items() if v}


def ir_verdict(mean_ir: float | None) -> str:
    """调权用 IR 均值 → 评价; None = 没有通过 n_eff / 占位值过滤, 权重按默认。"""
    if mean_ir is None:
        return "⚪ 数据不足"
    if mean_ir >= 1:
        return "🟢 优秀"
    if mean_ir >= 0.5:
        return "🟢 可用"
    if mean_ir >= 0:
        return "🟡 边缘"
    return "🔴 失效 (降权)"


def ir_weight_source(default_w: float, eff_w: float, has_ir: bool) -> tuple[float | None, str]:
    """实际权重相对默认权重的倍数 + 来源标签。

    composite 自 221c827 起可升可降; 之前页面只认 eff > default 为 "IR 动态",
    被降权的因子全部显示成 "默认"。
    """
    mult = eff_w / default_w if default_w else None
    if not has_ir or mult is None:
        return mult, "🔧 默认 (无可用 IR)"
    # eff_w 在 composite 里 round 到 4 位, 容差 1e-3 吸收舍入误差
    if mult > 1 + 1e-3:
        return mult, f"🎯 IR ↑ ×{mult:.2f}"
    if mult < 1 - 1e-3:
        return mult, f"🎯 IR ↓ ×{mult:.2f}"
    return mult, "🎯 IR ×1.00"


def ir_weight_rule_text() -> str:
    """权重规则说明 — 常量直接取自 composite, 文案不会再与代码漂移。"""
    from ..signals.composite import IR_SLOPE, IR_MULT_MIN, IR_MULT_MAX
    return (f"实际权重 = 默认 × clamp(1 + {IR_SLOPE} × IR均值, {IR_MULT_MIN}, {IR_MULT_MAX}); "
            f"IR均值 = 各资产最新计算日、过样本门槛并截断后的 IR 的平均 (即 ② 的「调权用 IR」)。"
            f"负 IR 因子会降权, 但最低保留 {IR_MULT_MIN}× 默认权重, 不会归零; "
            f"无可用 IR 的因子按默认权重。")


# ====================================================================
#  pages/9 历史复盘 — 推荐 vs N 天后实际
# ====================================================================

def load_real_snapshots(meta_dir: Path) -> list[dict]:
    """读取 meta_dir 下的真实筛选快照 (按文件名排序), 每个附带 _file。

    backfill 合成快照 (*_0000, 18-29 个币, 3 个动量因子) 不参与复盘: 混进去会让 Top-30
    覆盖整个宇宙、命中率按构造约 50%, 累计曲线在 2026-10-12 出现结构断点。
    """
    from .returns_tracker import _is_real_snapshot
    snaps = []
    for f in sorted(meta_dir.glob("snapshot_*.json")):
        d = _safe_load(f)
        if not isinstance(d, dict) or not d.get("date") or not d.get("coins"):
            continue
        if not _is_real_snapshot(f, d):
            continue
        d["_file"] = f.name
        snaps.append(d)
    return snaps


def is_full_universe(snap: dict) -> bool:
    """schema 2 起快照保存完整评分宇宙; 更早的 (2026-10-12 之前) 只存 composite 前 200 名。"""
    meta = snap.get("_meta") or {}
    return meta.get("truncated") is False or (meta.get("schema") or 0) >= 2


def _pos_price(v) -> float | None:
    """有效正价格 (None / NaN / 非数值 / <=0 → None)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _exit_price_index(coins: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """出场快照 → (按 coin_id 的行, 按唯一 symbol 的行)。

    symbol 只收在本快照里恰好出现一次的 (如两个 VELO 都不收), 避免同名币互相定价。
    """
    sym_count: dict[str, int] = {}
    for c in coins:
        sym = (c.get("symbol") or "").upper()
        if sym:
            sym_count[sym] = sym_count.get(sym, 0) + 1
    by_id: dict[str, dict] = {}
    by_sym: dict[str, dict] = {}
    for c in coins:
        if _pos_price(c.get("price")) is None:
            continue
        cid = c.get("coin_id")
        if cid:
            by_id.setdefault(cid, c)
        sym = (c.get("symbol") or "").upper()
        if sym and sym_count.get(sym) == 1:
            by_sym[sym] = c
    return by_id, by_sym


def _match_exit_price(row: dict, by_id: dict[str, dict], by_sym: dict[str, dict]) -> float | None:
    """入场行 → 出场价: 先 coin_id, 再唯一 symbol; 两边都有 coin_id 但不同 → 不是同一个币。"""
    cid = row.get("coin_id")
    if cid and cid in by_id:
        return _pos_price(by_id[cid].get("price"))
    ex = by_sym.get((row.get("symbol") or "").upper())
    if ex is None:
        return None
    ex_cid = ex.get("coin_id")
    if cid and ex_cid and ex_cid != cid:
        return None
    return _pos_price(ex.get("price"))


# 同一 symbol 的几行价格都在名次最高那行的 ±2% 内 → 视为同一个币被列了两次
SAME_COIN_PRICE_TOL = 0.02


def _ambiguous_entry_rows(coins: list[dict], order: list[int]) -> set[int]:
    """入场快照里不能按 symbol 安全定价的行索引 (旧 schema 1: 同名且无 coin_id)。

    symbol (大写) 出现多次、且组内有行缺 coin_id 时:
      * 组内每行价格都在名次最高那行的 ±2% 内 → 同一个币被列了两次
        (HNT 08-30 rank 9/10: 0.8099 / 0.8066), 只留名次最高那行;
      * 否则是不同的币 (SAFE 07-26 rank 69/177: 0.0953 / 0.1414), 组内无 coin_id 的行全跳过。
    不用 returns_tracker._ambiguous_rows 的 "留市值最大那行": 那条规则依赖 CoinGecko
    /coins/markets 按市值排序; 这里出场快照只是 composite 前 200 的子集, 剩下那个唯一的
    同名行可能是另一个币 (SAFE 07-26 → 08-02 被记成 +61%)。
    order: 按 composite 排好的行索引 (名次从高到低)。
    """
    pos = {k: r for r, k in enumerate(order)}
    groups: dict[str, list[int]] = {}
    for i, c in enumerate(coins):
        sym = (c.get("symbol") or "").upper()
        if sym:
            groups.setdefault(sym, []).append(i)
    out: set[int] = set()
    for idxs in groups.values():
        if len(idxs) < 2 or all(coins[i].get("coin_id") for i in idxs):
            continue
        idxs = sorted(idxs, key=lambda i: pos.get(i, len(pos)))
        prices = [_pos_price(coins[i].get("price")) for i in idxs]
        ref = prices[0]
        same_coin = ref is not None and all(
            p is not None and abs(p - ref) <= SAME_COIN_PRICE_TOL * ref for p in prices)
        if same_coin:
            out.update(idxs[1:])
        else:
            out.update(i for i in idxs if not coins[i].get("coin_id"))
    return out


def exit_snapshot_dates(snapshots: list[dict], lookahead_days: int = 7) -> dict[str, str]:
    """入场日期 → 出场快照日期 (第一个 >= 入场日 + lookahead 天的快照); 还没到的不在结果里。"""
    dates = sorted({s["date"] for s in snapshots if s.get("date")})
    parsed: dict[str, datetime] = {}
    for d in dates:
        try:
            parsed[d] = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
    out: dict[str, str] = {}
    for i, d in enumerate(dates):
        if d not in parsed:
            continue
        target_dt = parsed[d] + timedelta(days=lookahead_days)
        for cand in dates[i + 1:]:
            if cand in parsed and parsed[cand] >= target_dt:
                out[d] = cand
                break
    return out


def truncated_baseline_counts(snapshots: list[dict], entry_dates: list[str],
                              lookahead_days: int = 7) -> tuple[int, int]:
    """(入场快照被截断的天数, 其中出场快照也被截断的天数)。

    入场截断 (2026-10-12 之前只存 composite 前 200) → 「快照内中位」只覆盖入场时的前 200 名;
    出场也截断时基准还只含 N 天后仍在前 200 的币 (幸存者偏差)。出场是 schema 2 全量宇宙时
    (如 10-06 ~ 10-11 + 7 天) 没有后一种偏差, 不能一概说 "只含仍在前 200 的币"。
    """
    by_date = {s["date"]: s for s in snapshots if s.get("date")}
    exit_of = exit_snapshot_dates(snapshots, lookahead_days)
    n_entry = n_both = 0
    for d in entry_dates:
        if is_full_universe(by_date.get(d) or {}):
            continue
        n_entry += 1
        ed = exit_of.get(d)
        if ed is not None and not is_full_universe(by_date.get(ed) or {}):
            n_both += 1
    return n_entry, n_both


def forward_returns(snapshots: list[dict], lookahead_days: int = 7) -> dict[str, dict]:
    """对每个快照日期, 算其全部币在 lookahead 天后 (第一个 >= 目标日的快照) 的收益。

    Returns: {snapshot_date: {coin_key: {symbol, coin_id, rank, entry_price, exit_price,
              ret_pct, composite_score}}}; coin_key = coin_id or symbol。
    rank 是该币在入场快照 composite 排序里的名次 (被跳过的币也占名次)。
    之前按 symbol 建 dict: 同名币 (VELO: velodrome-finance / velo) 后者覆盖前者,
    rank 16 的行被 rank 57 的顶掉, 名次换位时还会拿到另一个币的价格 (-90%)。
    """
    by_date = {s["date"]: s for s in snapshots if s.get("date")}   # 同日多文件 → 排序靠后的
    exit_of = exit_snapshot_dates(snapshots, lookahead_days)

    out: dict[str, dict] = {}
    for d in sorted(by_date):
        if d not in exit_of:
            continue   # 还没到 lookahead 日
        snap = by_date[d]
        next_snap = by_date[exit_of[d]]

        by_id, by_sym = _exit_price_index(next_snap.get("coins") or [])
        coins = snap.get("coins") or []
        order = sorted(range(len(coins)),
                       key=lambda k: coins[k].get("composite_score") or 0,
                       reverse=True)
        # 同名且无 coin_id 的行 (旧 schema 1 的两个 VELO / SAFE): 不能按 symbol 安全定价, 跳过;
        # 同一个币被列两次 (价格 ±2% 内) 只留名次最高那行
        ambiguous = _ambiguous_entry_rows(coins, order)
        per_coin: dict[str, dict] = {}
        for rank_idx, k in enumerate(order):
            if k in ambiguous:
                continue
            c = coins[k]
            sym = c.get("symbol") or ""
            key = c.get("coin_id") or sym
            if not key or key in per_coin:   # 永不覆盖已有 (名次更高) 的行
                continue
            entry = _pos_price(c.get("price"))
            exit_ = _match_exit_price(c, by_id, by_sym)
            if entry is None or exit_ is None:
                continue
            per_coin[key] = {
                "symbol": sym,
                "coin_id": c.get("coin_id") or "",
                "rank": rank_idx + 1,
                "entry_price": entry,
                "exit_price": exit_,
                "ret_pct": (exit_ - entry) / entry,
                "composite_score": c.get("composite_score") or 0,
            }
        out[d] = per_coin
    return out


def topn_hit_rate(per_coin: dict, n: int = 10) -> dict:
    """Top-N 推荐里, ret > 快照内 (能匹配到出场价的币) 中位数的比例。

    n_topn = 实际参与的 Top-N 行数 (快照币数不足 n 时 < n), 累计命中率要按它加权。
    """
    if not per_coin or n <= 0:
        return {"hit_rate": None, "topn_avg_ret": None, "all_median": None,
                "all_mean": None, "n_compared": 0, "n_topn": 0}
    rows = sorted(per_coin.values(), key=lambda r: r["rank"])
    all_rets = [r["ret_pct"] for r in rows]
    median = statistics.median(all_rets)
    topn = rows[:n]
    hits = sum(1 for r in topn if r["ret_pct"] > median)
    return {
        "hit_rate": hits / len(topn),
        "topn_avg_ret": sum(r["ret_pct"] for r in topn) / len(topn),
        "all_median": median,
        "all_mean": sum(all_rets) / len(all_rets),
        "n_compared": len(rows),
        "n_topn": len(topn),
    }
