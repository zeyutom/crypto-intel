"""Multi-Agent 研究报告 HTML 生成器。"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("research_report", "INFO")

REPORT_DIR = Path(__file__).resolve().parents[2] / "data" / "research"


def _safe_get(d: dict | None, *keys, default="N/A"):
    """安全取嵌套 dict 值。"""
    if d is None:
        return default
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k, default)
        else:
            return default
    return cur


def _fmt_num(val, fmt=",") -> str:
    """安全格式化数字, 非数字直接返回字符串。"""
    if isinstance(val, (int, float)):
        if fmt == ",":
            return f"{val:,}"
        return f"{val:{fmt}}"
    return str(val) if val is not None else "N/A"


def _score_bar(score, max_score=100, color=None) -> str:
    """生成 CSS 评分条。"""
    pct = min(100, max(0, (score / max_score * 100) if max_score else 0))
    if color is None:
        if pct >= 70:
            color = "#4ade80"
        elif pct >= 40:
            color = "#fbbf24"
        else:
            color = "#f87171"
    return (f'<div style="background:#1e293b;border-radius:6px;height:12px;width:100%;overflow:hidden">'
            f'<div style="background:{color};height:100%;width:{pct:.0f}%;border-radius:6px"></div></div>')


def _risk_bar(score) -> str:
    """风险条 (高=红)。"""
    pct = min(100, max(0, score))
    if pct >= 67:
        color = "#f87171"
    elif pct >= 34:
        color = "#fbbf24"
    else:
        color = "#4ade80"
    return _score_bar(score, 100, color)


def _verdict_badge(verdict: str) -> str:
    """投资评级徽章。"""
    colors = {
        "STRONG_BUY": ("#059669", "强力买入"),
        "BUY": ("#10b981", "买入"),
        "CAUTIOUS_BUY": ("#34d399", "谨慎买入"),
        "HOLD": ("#f59e0b", "持有"),
        "CAUTIOUS_SELL": ("#fb923c", "谨慎卖出"),
        "SELL": ("#ef4444", "卖出"),
        "STRONG_SELL": ("#dc2626", "强力卖出"),
    }
    bg, label = colors.get(verdict, ("#6b7280", verdict))
    return (f'<span style="background:{bg};color:white;padding:8px 20px;'
            f'border-radius:20px;font-size:18px;font-weight:bold">{label} {verdict}</span>')


def generate_report(research_data: dict) -> Path:
    """从研究数据生成 HTML 报告。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    project = research_data.get("project", "Unknown")
    token = research_data.get("token", project)
    agents = research_data.get("agents", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    gh = agents.get("github") if isinstance(agents.get("github"), dict) and "error" not in agents.get("github", {}) else {}
    oc = agents.get("onchain") if isinstance(agents.get("onchain"), dict) and "error" not in agents.get("onchain", {}) else {}
    nr = agents.get("narrative") if isinstance(agents.get("narrative"), dict) and "error" not in agents.get("narrative", {}) else {}
    rk = agents.get("risk") if isinstance(agents.get("risk"), dict) and "error" not in agents.get("risk", {}) else {}
    al = agents.get("alpha") if isinstance(agents.get("alpha"), dict) and "error" not in agents.get("alpha", {}) else {}

    # ── 构建 HTML ──
    # GitHub repos table
    repos_html = ""
    for repo in (gh.get("main_repos") or [])[:8]:
        repos_html += f"""<tr>
            <td><a href="https://github.com/{repo.get('repo','')}" target="_blank" style="color:#60a5fa">{repo.get('repo','?')}</a></td>
            <td style="text-align:center">⭐ {_fmt_num(repo.get('stars', '?'))}</td>
            <td style="text-align:center">{_fmt_num(repo.get('forks', '?'))}</td>
            <td style="text-align:center">{repo.get('contributors', '?')}</td>
            <td style="text-align:center">{repo.get('last_commit_days_ago', '?')}d ago</td>
            <td>{repo.get('language', '?')}</td>
        </tr>"""

    # DeFi protocols
    defi_html = ""
    for proto in (oc.get("top_defi_protocols") or [])[:6]:
        defi_html += f"""<tr>
            <td>{proto.get('name','?')}</td>
            <td style="text-align:center">${proto.get('tvl_million', '?')}M</td>
            <td style="text-align:center">{proto.get('category', '?')}</td>
        </tr>"""

    # Risk matrix
    risk_html = ""
    for rname, rdata in (rk.get("risk_matrix") or {}).items():
        if isinstance(rdata, dict):
            label = rname.replace("_", " ").title()
            score = rdata.get("score", 50)
            risk_html += f"""<tr>
                <td style="font-weight:600">{label}</td>
                <td style="width:40%">{_risk_bar(score)}</td>
                <td style="text-align:center;font-weight:bold">{score}</td>
                <td style="color:#94a3b8;font-size:13px">{rdata.get('detail', '')[:80]}</td>
            </tr>"""

    # Alpha signals
    signals_html = ""
    for sig in (al.get("alpha_signals") or [])[:6]:
        direction = sig.get("direction", "neutral")
        arrow = "↑" if direction == "bullish" else ("↓" if direction == "bearish" else "→")
        color = "#4ade80" if direction == "bullish" else ("#f87171" if direction == "bearish" else "#fbbf24")
        signals_html += f"""<div style="background:#1e293b;border-radius:10px;padding:14px;margin-bottom:10px;
            border-left:4px solid {color}">
            <div style="font-weight:bold;color:{color}">{arrow} {sig.get('signal','')}</div>
            <div style="color:#94a3b8;margin-top:4px;font-size:13px">
                强度: {sig.get('strength','?')} · 时间框架: {sig.get('timeframe','?')} · 来源: {sig.get('source','')}
            </div>
        </div>"""

    # Catalysts
    recent_cat_html = ""
    for cat in (nr.get("recent_catalysts") or [])[:5]:
        impact = cat.get("impact", "neutral")
        dot_color = "#4ade80" if impact == "positive" else ("#f87171" if impact == "negative" else "#fbbf24")
        recent_cat_html += f"""<div style="margin-bottom:8px">
            <span style="color:{dot_color};font-size:20px">●</span>
            <strong>{cat.get('event','')}</strong>
            <span style="color:#64748b;font-size:13px"> ({cat.get('date','')})</span>
        </div>"""

    upcoming_cat_html = ""
    for cat in (nr.get("upcoming_catalysts") or [])[:5]:
        upcoming_cat_html += f"""<div style="margin-bottom:8px">
            <span style="color:#818cf8;font-size:20px">◆</span>
            <strong>{cat.get('event','')}</strong>
            <span style="color:#64748b;font-size:13px"> (预计 {cat.get('expected_date','')} · 影响: {cat.get('potential_impact','')})</span>
        </div>"""

    # KOL opinions
    kol_html = ""
    for kol in (nr.get("kol_opinions") or [])[:4]:
        stance = kol.get("stance", "neutral")
        color = "#4ade80" if stance == "bullish" else ("#f87171" if stance == "bearish" else "#fbbf24")
        kol_html += f"""<div style="background:#1e293b;border-radius:8px;padding:12px;margin-bottom:8px">
            <div style="font-weight:bold;color:{color}">{kol.get('who','?')} · {stance}</div>
            <div style="color:#cbd5e1;margin-top:4px;font-size:13px">{kol.get('opinion','')}</div>
        </div>"""

    # Comparable projects
    comp_html = ""
    for comp in (al.get("comparable_projects") or [])[:4]:
        comp_html += f"""<tr>
            <td style="font-weight:bold">{comp.get('name','?')}</td>
            <td>{comp.get('why_comparable','')}</td>
            <td>{comp.get('relative_valuation','')}</td>
        </tr>"""

    # Price levels
    pl = al.get("price_levels") or {}

    # Dimension scores radar-like display
    dims = al.get("dimension_scores") or {}
    dim_labels = {"development": "开发活跃", "onchain": "链上健康",
                  "narrative": "叙事热度", "risk_adjusted": "风险调整"}
    dims_html = ""
    for dk, dl in dim_labels.items():
        sc = dims.get(dk, 0)
        dims_html += f"""<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
            <div style="width:80px;font-weight:600;color:#e2e8f0">{dl}</div>
            <div style="flex:1">{_score_bar(sc)}</div>
            <div style="width:40px;text-align:right;font-weight:bold;color:#f1f5f9">{sc}</div>
        </div>"""

    # Findings & risks
    def _bullet_list(items, color="#94a3b8"):
        return "".join(f'<div style="margin:4px 0;color:{color}">• {i}</div>' for i in (items or []))

    duration = research_data.get("duration_seconds", 0)
    ok_count = research_data.get("agents_ok", 0)
    total_count = research_data.get("agents_total", 5)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{project} ({token}) 深度研究报告</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0f172a; color:#e2e8f0; font-family:'SF Pro Display','PingFang SC',system-ui,sans-serif; }}
.container {{ max-width:1100px; margin:0 auto; padding:30px 20px; }}
.header {{ text-align:center; padding:40px 0 30px; border-bottom:1px solid #1e293b; margin-bottom:30px; }}
.header h1 {{ font-size:36px; font-weight:800; background:linear-gradient(135deg,#60a5fa,#a78bfa,#818cf8);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
.header .subtitle {{ color:#94a3b8; margin-top:8px; }}
.section {{ margin-bottom:32px; }}
.section-title {{ font-size:20px; font-weight:700; color:#f1f5f9; margin-bottom:16px;
    padding-bottom:8px; border-bottom:2px solid #334155; display:flex; align-items:center; gap:10px; }}
.card {{ background:#1e293b; border-radius:14px; padding:20px; margin-bottom:16px; }}
.card-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:16px; }}
.metric {{ background:#1e293b; border-radius:12px; padding:18px; text-align:center; }}
.metric .value {{ font-size:28px; font-weight:800; color:#f1f5f9; }}
.metric .label {{ font-size:13px; color:#64748b; margin-top:4px; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ background:#334155; color:#94a3b8; padding:10px 12px; text-align:left; font-size:13px; font-weight:600; }}
td {{ padding:10px 12px; border-bottom:1px solid #1e293b; color:#cbd5e1; font-size:14px; }}
tr:hover td {{ background:#1e293b; }}
.verdict-box {{ text-align:center; padding:30px; background:linear-gradient(135deg,#1e293b,#0f172a);
    border-radius:16px; border:1px solid #334155; margin-bottom:24px; }}
.price-grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }}
.price-card {{ background:#0f172a; border:1px solid #334155; border-radius:10px; padding:14px; text-align:center; }}
.price-card .label {{ font-size:12px; color:#64748b; }}
.price-card .value {{ font-size:20px; font-weight:700; margin-top:4px; }}
.footer {{ text-align:center; color:#475569; font-size:12px; margin-top:40px; padding-top:20px; border-top:1px solid #1e293b; }}
a {{ color:#60a5fa; text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
<div class="container">

<!-- Header -->
<div class="header">
    <h1>{project} ({token}) 深度研究报告</h1>
    <div class="subtitle">Multi-Agent Alpha 研究系统 · {now} · {ok_count}/{total_count} Agents 成功 · 耗时 {duration:.0f}s</div>
</div>

<!-- 1. 投资结论 (Alpha Agent) -->
<div class="section">
    <div class="section-title">🎯 投资结论</div>
    <div class="verdict-box">
        {_verdict_badge(al.get('verdict', 'N/A'))}
        <div style="margin-top:16px;font-size:15px;color:#94a3b8">
            置信度: <strong style="color:#f1f5f9">{al.get('confidence', 'N/A')}%</strong> ·
            综合评分: <strong style="color:#f1f5f9">{al.get('overall_score', 'N/A')}/100</strong>
        </div>
        <div style="margin-top:16px;font-size:16px;color:#e2e8f0;max-width:700px;margin-left:auto;margin-right:auto">
            {al.get('executive_summary', '暂无摘要')}
        </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
            <div style="font-weight:700;color:#4ade80;margin-bottom:10px">📈 牛市情景</div>
            <div style="color:#cbd5e1;font-size:14px">{al.get('bull_case', 'N/A')}</div>
        </div>
        <div class="card">
            <div style="font-weight:700;color:#f87171;margin-bottom:10px">📉 熊市情景</div>
            <div style="color:#cbd5e1;font-size:14px">{al.get('bear_case', 'N/A')}</div>
        </div>
    </div>
</div>

<!-- 2. 价格水平 & 建仓策略 -->
<div class="section">
    <div class="section-title">💰 价格水平 & 策略</div>
    <div class="price-grid">
        <div class="price-card">
            <div class="label">当前价格</div>
            <div class="value" style="color:#f1f5f9">${pl.get('current', 'N/A')}</div>
        </div>
        <div class="price-card">
            <div class="label">支撑位 1</div>
            <div class="value" style="color:#4ade80">${pl.get('support_1', 'N/A')}</div>
        </div>
        <div class="price-card">
            <div class="label">支撑位 2</div>
            <div class="value" style="color:#22c55e">${pl.get('support_2', 'N/A')}</div>
        </div>
        <div class="price-card">
            <div class="label">阻力位 1</div>
            <div class="value" style="color:#fbbf24">${pl.get('resistance_1', 'N/A')}</div>
        </div>
        <div class="price-card">
            <div class="label">阻力位 2</div>
            <div class="value" style="color:#f59e0b">${pl.get('resistance_2', 'N/A')}</div>
        </div>
        <div class="price-card">
            <div class="label">6 个月目标价</div>
            <div class="value" style="color:#818cf8">${pl.get('target_6m', 'N/A')}</div>
        </div>
    </div>
    <div class="card" style="margin-top:16px">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
            <div>
                <div style="font-weight:700;color:#a78bfa;margin-bottom:8px">🎯 建仓策略</div>
                <div style="color:#cbd5e1;font-size:14px">{al.get('entry_strategy', 'N/A')}</div>
                <div style="margin-top:8px;color:#94a3b8;font-size:13px">仓位建议: {al.get('position_sizing', 'N/A')}</div>
            </div>
            <div>
                <div style="font-weight:700;color:#f87171;margin-bottom:8px">🛑 止损: ${pl.get('stop_loss', 'N/A')}</div>
                <div style="font-weight:600;color:#fbbf24;margin-bottom:8px">退出触发条件:</div>
                {_bullet_list(al.get('exit_triggers'), '#fbbf24')}
            </div>
        </div>
    </div>
</div>

<!-- 3. 多维评分 -->
<div class="section">
    <div class="section-title">📊 多维评分</div>
    <div class="card">{dims_html}</div>
</div>

<!-- 4. Alpha 信号 -->
<div class="section">
    <div class="section-title">⚡ Alpha 信号</div>
    {signals_html or '<div class="card" style="color:#64748b">暂无信号数据</div>'}
</div>

<!-- 5. GitHub 开发活跃度 -->
<div class="section">
    <div class="section-title">🛠️ GitHub 开发活跃度</div>
    <div class="card-grid" style="margin-bottom:16px">
        <div class="metric">
            <div class="value">{gh.get('dev_activity_score', 'N/A')}</div>
            <div class="label">开发活跃度评分</div>
        </div>
        <div class="metric">
            <div class="value" style="color:{'#4ade80' if gh.get('dev_trend')=='rising' else '#fbbf24' if gh.get('dev_trend')=='stable' else '#f87171'}">{gh.get('dev_trend', 'N/A')}</div>
            <div class="label">开发趋势</div>
        </div>
        <div class="metric">
            <div class="value">{len(gh.get('main_repos', []))}</div>
            <div class="label">主要仓库数</div>
        </div>
    </div>
    <div class="card">
        <table>
            <tr><th>仓库</th><th>Stars</th><th>Forks</th><th>贡献者</th><th>最近 Commit</th><th>语言</th></tr>
            {repos_html or '<tr><td colspan="6" style="color:#64748b">暂无数据</td></tr>'}
        </table>
    </div>
    <div class="card">
        <div style="font-weight:600;margin-bottom:8px">关键发现</div>
        {_bullet_list(gh.get('key_findings'), '#60a5fa')}
        <div style="font-weight:600;margin-top:12px;margin-bottom:8px;color:#f87171">风险点</div>
        {_bullet_list(gh.get('risks'), '#f87171')}
    </div>
</div>

<!-- 6. 链上数据 -->
<div class="section">
    <div class="section-title">⛓️ 链上数据 & DeFi 生态</div>
    <div class="card-grid">
        <div class="metric">
            <div class="value" style="color:#60a5fa">${oc.get('price_usd', 'N/A')}</div>
            <div class="label">价格</div>
        </div>
        <div class="metric">
            <div class="value">${oc.get('market_cap_billion', 'N/A')}B</div>
            <div class="label">市值</div>
        </div>
        <div class="metric">
            <div class="value" style="color:{'#4ade80' if str(oc.get('price_change_30d_pct',0)).replace('-','').replace('.','').isdigit() and float(str(oc.get('price_change_30d_pct',0)))>0 else '#f87171'}">{oc.get('price_change_30d_pct', 'N/A')}%</div>
            <div class="label">30 日涨跌</div>
        </div>
        <div class="metric">
            <div class="value">{_fmt_num(oc.get('daily_active_addresses', 'N/A'))}</div>
            <div class="label">日活地址</div>
        </div>
        <div class="metric">
            <div class="value">{_fmt_num(oc.get('daily_transactions', 'N/A'))}</div>
            <div class="label">日交易数</div>
        </div>
        <div class="metric">
            <div class="value">${oc.get('tvl_million', 'N/A')}M</div>
            <div class="label">DeFi TVL</div>
        </div>
    </div>
    <div class="card" style="margin-top:16px">
        <div style="font-weight:600;margin-bottom:10px">Top DeFi 协议</div>
        <table>
            <tr><th>协议</th><th>TVL</th><th>分类</th></tr>
            {defi_html or '<tr><td colspan="3" style="color:#64748b">暂无数据</td></tr>'}
        </table>
    </div>
    <div class="card">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
            <div>
                <div style="font-weight:600;margin-bottom:6px">供应量</div>
                <div style="color:#94a3b8;font-size:14px">流通: {oc.get('supply_circulating_billion', 'N/A')}B / 总量: {oc.get('supply_total_billion', 'N/A')}B</div>
                <div style="color:#94a3b8;font-size:14px;margin-top:4px">通胀率: {oc.get('inflation_rate_pct', 'N/A')}% · 质押收益: {oc.get('staking_yield_pct', 'N/A')}%</div>
            </div>
            <div>
                <div style="font-weight:600;margin-bottom:6px">链上健康评分</div>
                {_score_bar(oc.get('onchain_health_score', 0))}
                <div style="text-align:right;font-weight:bold;margin-top:4px">{oc.get('onchain_health_score', 'N/A')}/100</div>
            </div>
        </div>
    </div>
</div>

<!-- 7. 社区 & 叙事 -->
<div class="section">
    <div class="section-title">🌐 社区 & 叙事分析</div>
    <div class="card-grid">
        <div class="metric">
            <div class="value">{nr.get('narrative_heat', 'N/A')}</div>
            <div class="label">叙事热度</div>
        </div>
        <div class="metric">
            <div class="value">{nr.get('community_score', 'N/A')}</div>
            <div class="label">社区评分</div>
        </div>
        <div class="metric">
            <div class="value" style="color:#818cf8">{nr.get('sentiment', 'N/A').replace('_', ' ')}</div>
            <div class="label">市场情绪</div>
        </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
        <div class="card">
            <div style="font-weight:600;margin-bottom:10px;color:#4ade80">近期催化剂</div>
            {recent_cat_html or '<div style="color:#64748b">暂无数据</div>'}
        </div>
        <div class="card">
            <div style="font-weight:600;margin-bottom:10px;color:#818cf8">即将到来的催化剂</div>
            {upcoming_cat_html or '<div style="color:#64748b">暂无数据</div>'}
        </div>
    </div>
    <div class="card" style="margin-top:16px">
        <div style="font-weight:600;margin-bottom:10px">KOL 观点</div>
        {kol_html or '<div style="color:#64748b">暂无数据</div>'}
    </div>
    <div class="card">
        <div style="font-weight:600;margin-bottom:8px">叙事定位</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
            {"".join(f'<span style="background:#334155;padding:6px 14px;border-radius:20px;font-size:13px">{n}</span>' for n in (nr.get("narratives") or ["N/A"]))}
        </div>
        <div style="margin-top:12px;font-weight:600;margin-bottom:6px">竞争定位</div>
        <div style="color:#94a3b8;font-size:14px">{nr.get('competitive_position', 'N/A')}</div>
    </div>
</div>

<!-- 8. 风险矩阵 -->
<div class="section">
    <div class="section-title">⚠️ 风险矩阵</div>
    <div class="card-grid" style="margin-bottom:16px">
        <div class="metric">
            <div class="value" style="color:{'#f87171' if (rk.get('overall_risk_score',50) or 50) > 66 else '#fbbf24' if (rk.get('overall_risk_score',50) or 50) > 33 else '#4ade80'}">{rk.get('overall_risk_score', 'N/A')}</div>
            <div class="label">综合风险评分</div>
        </div>
        <div class="metric">
            <div class="value">{(rk.get('risk_level', 'N/A') or 'N/A').upper()}</div>
            <div class="label">风险等级</div>
        </div>
    </div>
    <div class="card">
        <table>
            <tr><th>风险维度</th><th style="width:35%">风险水平</th><th>评分</th><th>说明</th></tr>
            {risk_html or '<tr><td colspan="4" style="color:#64748b">暂无数据</td></tr>'}
        </table>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
            <div style="font-weight:600;color:#f87171;margin-bottom:8px">核心风险</div>
            {_bullet_list(rk.get('key_risks'), '#f87171')}
        </div>
        <div class="card">
            <div style="font-weight:600;color:#4ade80;margin-bottom:8px">缓解因素</div>
            {_bullet_list(rk.get('mitigants'), '#4ade80')}
        </div>
    </div>
    <div class="card">
        <div style="font-weight:600;color:#fbbf24;margin-bottom:8px">最坏情景</div>
        <div style="color:#94a3b8">{rk.get('worst_case_scenario', 'N/A')}</div>
        <div style="font-weight:600;color:#a78bfa;margin-top:12px;margin-bottom:8px">黑天鹅风险</div>
        {_bullet_list(rk.get('black_swan_risks'), '#a78bfa')}
    </div>
</div>

<!-- 9. 可比项目 -->
<div class="section">
    <div class="section-title">🔄 可比项目分析</div>
    <div class="card">
        <table>
            <tr><th>项目</th><th>可比原因</th><th>相对估值</th></tr>
            {comp_html or '<tr><td colspan="3" style="color:#64748b">暂无数据</td></tr>'}
        </table>
    </div>
</div>

<!-- 10. 关注催化剂 -->
<div class="section">
    <div class="section-title">👀 需关注的催化剂</div>
    <div class="card">
        {_bullet_list(al.get('catalysts_to_watch'), '#818cf8')}
    </div>
</div>

<div class="footer">
    Crypto Intel Multi-Agent Research System · {ok_count}/{total_count} Agents · {duration:.0f}s · {now}<br>
    ⚠️ 本报告由 AI 生成, 仅供研究参考, 不构成投资建议
</div>

</div>
</body>
</html>"""

    # 保存
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    filename = f"research_{project.lower()}_{date_str}.html"
    path = REPORT_DIR / filename
    path.write_text(html, encoding="utf-8")

    # 同时保存原始 JSON
    json_path = REPORT_DIR / f"research_{project.lower()}_{date_str}.json"
    json_path.write_text(json.dumps(research_data, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")

    log.info(f"报告已保存: {path}")
    return path
