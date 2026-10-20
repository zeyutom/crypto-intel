"""数据自愈层: 快照缺口检测 + 自动 backfill + 真实快照断更告警 + 核心数据源掉线告警。

- 缺口检测: 最近 N 天里完全没有快照的日期 = 缺口 (云端漏跑/采集失败)。
- 自动 backfill: 调 scripts/backfill_snapshots.py 用历史价格合成缺失日的快照 (增量, 不覆盖真实快照)。
- 断更告警: 最新一份"真实"快照 (排除 _0000 / synthetic / backfill) 超过 36h → 飞书。
  在 backfill 之前算, 而且不看日历日期: 曾经告警只看 backfill 之后的缺口, 合成快照一补就清零,
  09-29 整晚漏跑也没人知道; 按日期算又会把跨 UTC 午夜的晚跑 (26-29h 间隔) 误报成漏跑。
- 源掉线告警: 核心源(本应一直可用) 历史有数据、但最近 N 天没新数据 = 静默回归 → 飞书。

与 nightly 的 watchdog(实时风险) 不同: 这里盯的是"数据管道本身的健康度", 多日维度。
"""
from __future__ import annotations
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..utils import setup_logger
from ..db import query_df

log = setup_logger("data_quality", "INFO")
ROOT = Path(__file__).resolve().parents[2]
META_DIR = ROOT / "data" / "meta"

# 核心源: 不受地域封锁/付费墙影响, 本应一直有数据。只对这些做"掉线"告警, 避免
# binance(451)/farside(403)/coinglass(paywall) 这种已知优雅降级源的噪声。
CORE_SOURCES = {
    "coingecko", "defillama", "defillama_extra", "feargreed",
    "cg_global", "cg_trending", "yfinance_macro",
}


def _snapshot_dates() -> set[str]:
    dates = set()
    for p in META_DIR.glob("snapshot_*.json"):
        try:
            d = json.loads(p.read_text()).get("date")
            if d:
                dates.add(d)
        except Exception:
            pass
    return dates


def detect_snapshot_gaps(window_days: int = 14) -> dict:
    """最近 window_days 天 (不含今天, 今天的快照在 run 后段才生成) 里缺失的快照日期。"""
    have = _snapshot_dates()
    today = datetime.now(timezone.utc).date()
    expected = [(today - timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range(1, window_days + 1)]
    gaps = [d for d in expected if d not in have]
    two_days_ago = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    recent = [d for d in gaps if d >= two_days_ago]
    return {"window_days": window_days, "gaps": sorted(gaps),
            "recent_gaps": sorted(recent),
            "have_in_window": len(expected) - len(gaps)}


# 真实快照断更阈值: 正常 nightly 间隔 22-25h, 跨午夜的晚跑最长 28.9h, 真正漏跑一晚是 ~47h
REAL_SNAPSHOT_STALE_HOURS = 36


def _parse_snapshot_ts(data: dict, path: Path) -> datetime | None:
    """快照时间: 优先 timestamp 字段, 退回文件名里的 UTC 时间 snapshot_YYYYMMDD_HHMM.json。"""
    ts = data.get("timestamp")
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.strptime(path.stem[len("snapshot_"):], "%Y%m%d_%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def newest_real_snapshot() -> dict | None:
    """最新一份真实快照 (跳过 *_0000.json 及 _meta.synthetic / _meta.source=='backfill' 的合成快照)。"""
    # 文件名带 UTC 时间, 按名字倒序扫, 找到第一份真实的就停 (不必读完几百个大 JSON)
    for p in sorted(META_DIR.glob("snapshot_*.json"), key=lambda x: x.name, reverse=True):
        if p.name.endswith("_0000.json"):
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        meta = data.get("_meta") or {}
        if meta.get("synthetic") or meta.get("source") == "backfill":
            continue
        ts = _parse_snapshot_ts(data, p)
        if ts is None:
            continue
        return {"file": p.name, "ts": ts}
    return None


def check_real_snapshot_staleness(max_age_hours: float = REAL_SNAPSHOT_STALE_HOURS) -> dict:
    """最新真实快照距今多久; 超过 max_age_hours (或一份都没有) = 断更 (nightly 漏跑/被取消/没推上去)。"""
    newest = newest_real_snapshot()
    if newest is None:
        return {"stale": True, "newest": None, "age_hours": None, "max_age_hours": max_age_hours}
    age_h = (datetime.now(timezone.utc) - newest["ts"]).total_seconds() / 3600.0
    return {"stale": age_h > max_age_hours, "newest": newest["file"],
            "age_hours": round(age_h, 1), "max_age_hours": max_age_hours}


def _run_backfill(days: int) -> dict:
    script = ROOT / "scripts" / "backfill_snapshots.py"
    try:
        r = subprocess.run(
            [sys.executable, str(script), "--days", str(days), "--top", "30"],
            capture_output=True, text=True, timeout=600,
        )
        return {"ok": r.returncode == 0, "rc": r.returncode,
                "tail": (r.stdout or "")[-200:]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def detect_source_dryness(lookback_days: int = 5) -> list[dict]:
    """核心源历史有数据、但最近 lookback_days 没新数据 = 可能掉线 (静默回归)。"""
    try:
        df = query_df(
            "SELECT source, COUNT(*) AS n, MAX(ts) AS last_ts "
            "FROM raw_metrics WHERE source != '_meta' GROUP BY source"
        )
    except Exception as e:
        log.warning(f"source dryness 查询失败: {e}")
        return []
    if df.empty:
        return []
    now = datetime.now(timezone.utc)
    out = []
    for _, r in df.iterrows():
        src = r["source"]
        if src not in CORE_SOURCES:
            continue
        n = int(r["n"] or 0)
        last_ts = str(r["last_ts"] or "")
        try:
            last = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_days = (now - last).total_seconds() / 86400.0
        except Exception:
            continue
        if n >= 20 and age_days > lookback_days:
            out.append({"source": src, "rows": n, "last_ts": last_ts[:19],
                        "age_days": round(age_days, 1)})
    return sorted(out, key=lambda x: -x["age_days"])


def run_data_quality(push: bool = True, window_days: int = 14,
                     dryness_days: int = 5, backfill: bool = True) -> dict:
    """编排: 真实快照断更检查 → 检测缺口 → backfill → 检测源掉线 → 有问题就推飞书。"""
    # 在 backfill 之前看真实快照 (backfill 只会补合成 _0000 快照, 不能让它掩盖漏跑)
    staleness = check_real_snapshot_staleness()
    gaps = detect_snapshot_gaps(window_days)
    bf = None
    if backfill and gaps["gaps"]:
        log.info(f"检测到 {len(gaps['gaps'])} 个快照缺口 → 自动 backfill {window_days}d")
        bf = _run_backfill(window_days)
        gaps_after = detect_snapshot_gaps(window_days)
    else:
        gaps_after = gaps

    dry = detect_source_dryness(dryness_days)

    # 只对"可操作"信号告警: 真实快照断更 (云端漏跑) 或 核心源掉线。
    # 不再看 backfill 之后的 recent_gaps: 合成快照一补它就是空的, 永远不会响。
    # 老的、补不上的缺口只作为上下文附带, 不单独触发推送 (否则窗口边缘几天会每晚 nag)。
    alert_lines = []
    if staleness["stale"]:
        if staleness["newest"]:
            alert_lines.append(
                f"⚠️ **真实快照断更**: 最新真实快照 {staleness['newest']} 已是 "
                f"{staleness['age_hours']:.0f}h 前 (> {staleness['max_age_hours']:.0f}h), "
                f"云端 nightly 可能漏跑/被取消/没推上去 (合成 _0000 快照不算)")
        else:
            alert_lines.append("⚠️ **找不到任何真实快照** (data/meta 里只有合成 _0000 快照或为空)")
    if dry:
        alert_lines.append("📉 **可能掉线的核心数据源**:")
        for d in dry:
            alert_lines.append(
                f"  • {d['source']}: 最近数据 {d['last_ts']} ({d['age_days']}天前)")
    still = [g for g in gaps_after["gaps"] if g not in gaps_after["recent_gaps"]]
    if still and alert_lines:   # 仅在已经要告警时附带, 不单独触发
        alert_lines.append(f"🩹 (另: backfill 后仍缺 {len(still)} 天, 多为窗口边缘/较久以前, 影响小)")

    pushed = None
    if push and alert_lines:
        from ..notifier import push_alert
        pushed = push_alert("🩺 数据质量告警 · Crypto Intel", alert_lines, color="orange")

    return {
        "gaps_before": gaps["gaps"], "gaps_after": gaps_after["gaps"],
        "recent_gaps": gaps_after["recent_gaps"], "backfill": bf,
        "real_snapshot": staleness,
        "dry_sources": dry, "alert_lines": alert_lines,
        "alerted": bool(pushed and pushed.get("ok")),
    }
