"""日报生成器: 从 DB 组装数据 → 渲染 HTML (v0.2: 加入 PM 简报、因子卡片解读、术语词典)。"""
import json
from pathlib import Path
from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader, select_autoescape
from .. import __version__
from ..config import CFG, ROOT
from ..db import query_df, latest_factors, latest_reviews
from ..factors._metadata import factor_meta, asset_cn, regime_cn, signal_label
from .insights import build_briefing
from .glossary import GLOSSARY


TEMPLATE_DIR = Path(__file__).parent


def _fmt_raw(factor: str, v) -> str:
    if v is None:
        return "N/A"
    if factor == "funding_composite":
        return f"{v*100:.4f}% (8h)"
    if factor == "coinbase_premium":
        return f"{v*100:+.3f}%"
    if factor == "stablecoin_mint_7d":
        return f"${v/1e9:+.2f}B"
    if factor == "fear_greed_reversal":
        return f"{v:.0f}/100"
    if factor == "etf_flow_5d":
        return f"${v:+.1f}M"
    return str(v)


def _humanize_review(check: str, detail: dict) -> str:
    """把复核 detail 翻译成人话。"""
    if check == "price_cross":
        dev = detail.get("max_deviation_pct", 0)
        thr = detail.get("threshold_pct", 1)
        if dev < thr * 0.5:
            return f"3 个数据源价格高度一致 (最大偏差 {dev:.3f}%), 价格可信"
        if dev < thr:
            return f"价格存在 {dev:.3f}% 偏差但未超阈值 {thr}%, 仍可用"
        return f"⚠ 价格偏差 {dev:.3f}% 超过阈值 {thr}%, 建议人工核查"
    if check == "ic_monitor":
        obs = detail.get("observations", 0)
        if detail.get("status") == "insufficient_history" or obs < 7:
            return f"已积累 {obs} 个观测, 数据量太少, 7 天后才能开始评估因子有效性"
        ir = detail.get("ir_proxy", 0)
        floor = detail.get("ir_floor", 0.3)
        if ir >= floor:
            return f"IR代理 = {ir:.2f} (达标 ≥{floor}), 因子稳定"
        return f"⚠ IR代理 = {ir:.2f} 低于阈值 {floor}, 因子近期失效, 建议降权"
    return json.dumps(detail, ensure_ascii=False)[:120]


def generate(output_path: str | Path | None = None) -> Path:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=select_autoescape())
    tmpl = env.get_template("template.html")

    # --- 1. 行情快照 (Binance 主源 + CoinGecko 24h 涨跌) ---
    snap_df = query_df(
        """SELECT r.asset_id AS asset_id, r.value AS price FROM raw_metrics r
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                 WHERE source='binance' AND metric='price_usd'
                 GROUP BY asset_id) m
           ON r.asset_id=m.a_ AND r.ts=m.mts
           WHERE r.source='binance' AND r.metric='price_usd'"""
    )
    chg_df = query_df(
        """SELECT r.asset_id AS asset_id, r.value AS change_24h FROM raw_metrics r
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                 WHERE source='binance' AND metric='change_24h_pct'
                 GROUP BY asset_id) m
           ON r.asset_id=m.a_ AND r.ts=m.mts
           WHERE r.source='binance' AND r.metric='change_24h_pct'"""
    )
    merged = snap_df.merge(chg_df, on="asset_id", how="left")
    snap_rows = []
    for _, r in merged.iterrows():
        sym = next((a["symbol"] for a in CFG["universe"] if a["id"] == r["asset_id"]), r["asset_id"])
        snap_rows.append({
            "symbol": sym,
            "cn_name": asset_cn(r["asset_id"]),
            "price": float(r["price"]),
            "change_24h": float(r["change_24h"]) if r["change_24h"] is not None else None,
        })

    # --- 2. 信号 ---
    sig_df = query_df(
        """SELECT s.* FROM signals s
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM signals GROUP BY asset_id) m
           ON (s.asset_id IS m.a_ OR (s.asset_id IS NULL AND m.a_ IS NULL))
           AND s.ts=m.mts"""
    )
    sig_rows = []
    for _, r in sig_df.iterrows():
        breakdown = json.loads(r["factor_breakdown"]) if r["factor_breakdown"] else {}
        drivers = [f for f, v in breakdown.items() if v.get("signal") != 0]
        sig_rows.append({
            "asset_id": r["asset_id"],
            "asset_cn": asset_cn(r["asset_id"]),
            "direction": r["direction"],
            "composite": float(r["composite"]),
            "confidence": float(r["confidence"]),
            "regime": r["regime"],
            "regime_cn": regime_cn(r["regime"])[0],
            "top_drivers": ", ".join(drivers[:3]) or "—",
        })

    # --- 3. 因子卡片 (核心改造: 每个因子带元数据解读) ---
    fact_df = latest_factors()
    fact_cards = []
    seen_factors = set()
    for _, r in fact_df.iterrows():
        fname = r["factor"]
        # 同一 factor 多个 asset (如 coinbase_premium) → 取 bitcoin 那条;其他展开为子条目
        if fname in seen_factors and (r.get("asset_id") or "") != "bitcoin":
            continue
        seen_factors.add(fname)
        meta_obj = factor_meta(fname)
        try:
            raw_meta = json.loads(r["meta"]) if r["meta"] else {}
        except Exception:
            raw_meta = {}
        sig = int(r["signal"]) if r["signal"] is not None else 0
        card = {
            "factor": fname,
            "cn_name": meta_obj.cn_name if meta_obj else fname,
            "category": meta_obj.category if meta_obj else "—",
            "asset_cn": asset_cn(r.get("asset_id")),
            "raw_value_fmt": _fmt_raw(fname, r["raw_value"]),
            "fmt_unit": meta_obj.fmt_unit if meta_obj else "",
            "signal": sig,
            "signal_label": signal_label(sig),
            "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
            "one_line": meta_obj.one_line if meta_obj else "",
            "why_matters": meta_obj.why_matters if meta_obj else "",
            "how_to_read": meta_obj.how_to_read.get(sig, "") if meta_obj else "",
            "pm_action": meta_obj.pm_action.get(sig, "") if meta_obj else "",
            "raw_meta": raw_meta,
        }
        fact_cards.append(card)

    # 同 factor 多 asset 的额外条目 (展示每个 asset 的值)
    multi_asset_factors = {}
    for _, r in fact_df.iterrows():
        fname = r["factor"]
        ass = r.get("asset_id") or "market"
        multi_asset_factors.setdefault(fname, []).append({
            "asset": asset_cn(ass),
            "raw": _fmt_raw(fname, r["raw_value"]),
            "signal": int(r["signal"]) if r["signal"] is not None else 0,
        })
    for card in fact_cards:
        items = multi_asset_factors.get(card["factor"], [])
        card["multi_assets"] = items if len(items) > 1 else []

    # --- 4. ETF 历史 (最近 10 日柱状图) ---
    etf_df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='farside_etf' AND metric='etf_net_flow_musd'
           ORDER BY ts DESC LIMIT 10"""
    )
    etf_rows = []
    if not etf_df.empty:
        import pandas as pd
        etf_df["ts"] = pd.to_datetime(etf_df["ts"])
        etf_df = etf_df.sort_values("ts")
        vmax = max(abs(etf_df["value"]).max(), 1)
        for _, r in etf_df.iterrows():
            etf_rows.append({
                "date": r["ts"].strftime("%m/%d"),
                "value": float(r["value"]),
                "height": int(abs(r["value"]) / vmax * 60),
            })

    # --- 5. 复核 ---
    rev_df = latest_reviews()
    rev_rows = []
    for _, r in rev_df.iterrows():
        try:
            d = json.loads(r["detail"])
        except Exception:
            d = {}
        rev_rows.append({
            "check_name": r["check_name"],
            "check_cn": "价格交叉验证" if r["check_name"] == "price_cross" else "因子有效性监控",
            "subject": r["subject"] or "—",
            "subject_cn": asset_cn(r["subject"]) if r["check_name"] == "price_cross" else (factor_meta(r["subject"]).cn_name if factor_meta(r["subject"]) else r["subject"]),
            "severity": r["severity"],
            "explanation": _humanize_review(r["check_name"], d),
        })

    # --- 6. 数据源覆盖度 ---
    sources_df = query_df(
        """SELECT source, COUNT(*) AS n, MAX(ts) AS last_ts FROM raw_metrics
           WHERE source != '_meta' GROUP BY source"""
    )
    expected_sources = {"coingecko", "binance", "coinbase", "defillama", "feargreed", "farside_etf"}
    actual_sources = set(sources_df["source"].tolist())
    missing = sorted(expected_sources - actual_sources)
    src_status = []
    for s in sorted(expected_sources):
        if s in actual_sources:
            row = sources_df[sources_df["source"] == s].iloc[0]
            src_status.append({"source": s, "ok": True, "n": int(row["n"]), "last_ts": row["last_ts"][:16].replace("T", " ")})
        else:
            src_status.append({"source": s, "ok": False, "n": 0, "last_ts": "—"})

    # --- 7. PM 简报 ---
    regime = sig_rows[0]["regime"] if sig_rows else "UNKNOWN"
    briefing = build_briefing(sig_df, fact_df, regime, missing)

    # --- 8. DEMO 检测 (基于 sentinel marker, 不再用启发式) ---
    demo_check = query_df("SELECT COUNT(*) AS c FROM raw_metrics WHERE source='_meta' AND metric='is_demo'")
    is_demo = bool(int(demo_check.iloc[0]["c"]) > 0)

    src_count = len(actual_sources)
    factor_count = int(query_df("SELECT COUNT(DISTINCT factor) AS c FROM factors").iloc[0]["c"])

    ctx = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "regime": regime,
        "regime_cn": regime_cn(regime)[0],
        "regime_explain": regime_cn(regime)[1],
        "source_count": src_count,
        "factor_count": factor_count,
        "snapshot": snap_rows,
        "signals": sig_rows,
        "factor_cards": fact_cards,
        "etf_history": etf_rows,
        "reviews": rev_rows,
        "src_status": src_status,
        "briefing": briefing,
        "glossary": GLOSSARY,
        "version": __version__,
        "is_demo": is_demo,
    }

    html = tmpl.render(**ctx)

    out_dir = Path(output_path) if output_path else (ROOT / CFG["output"]["report_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"daily_{datetime.now(timezone.utc).strftime('%Y%m%d')}.html"
    path = out_dir / fname
    path.write_text(html, encoding="utf-8")
    return path
