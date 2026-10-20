"""实时风险预警 watchdog (W4-A).

监听一组关键阈值, 触发即推飞书:
  1. 稳定币加权脱锚 > 50bps          → 流动性紧张/危机
  2. 主流币 funding rate 极端值       → 多空过度拥挤 (Binance→OKX→Bybit 纯 HTTP)
  3. 24h 全市场清算量 > $500M        → 大行情 (读 DB; coinglass 清算端点已死, 目前无数据)
  4. BTC ETF 单日净流出 > $200M      → 机构撤资 (读 DB; farside 云端 403, 目前无数据)
  5. Top-10 DEX TVL 24h 跌 > 10%    → DeFi 风险
  6. Fear & Greed > 80 或 < 20      → 极端情绪
  7. 夜间流水线停摆 (最新真实快照 > 36h) → dead-man, 漏跑/取消/push 失败

设计:
  - 每次跑 = 一轮 sample, 不维持长连接
  - 可单次跑 (check) 或循环跑 (loop, 默认 5 分钟一次)
  - 检测器三态: 返回 Alert (触发) / None (有数据, 未触发) / NoData(reason) (无数据).
    run_check 把因无数据被跳过的检测器记进 LAST_RUN 并打一行汇总日志,
    "无告警" 不再掩盖拿不到数据的盲区 (清算/ETF 曾数月静默无数据)
  - DB 类检测器只认新鲜行 (清算: 26h 内; ETF: 交易日结束 ≤24h),
    旧行不会在去重窗口过期后反复触发
  - 触发去重: 24h 内同一类型告警只发一次 (state 持久化);
    飞书推送失败的告警不记去重, 下一轮 (2h 后) 重试
  - 复用 notifier 推飞书 + 同时写到 data/alerts.log

API:
    from src.research.watchdog import run_check, run_loop
    alerts = run_check()  # 返回触发的告警列表
    run_loop(interval_sec=300)  # 循环, Ctrl+C 退出

CLI:
    python -m src.cli watchdog check   # 跑一次
    python -m src.cli watchdog loop    # 后台循环
    python -m src.cli watchdog history # 查看历史告警
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from ..utils import setup_logger

log = setup_logger("watchdog", "INFO")

STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "watchdog_state.json"
ALERT_LOG = Path(__file__).resolve().parents[2] / "data" / "alerts.log"
# 夜间流水线产出的快照目录 (pipeline_stale 检测器读; 测试里 monkeypatch)
META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


# ────────────────────────────────────────────────────────────────────
#  阈值 (env 可覆盖)
# ────────────────────────────────────────────────────────────────────

import os

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


THRESHOLDS = {
    "stable_peg_bps": _env_float("WATCHDOG_PEG_BPS", 50.0),       # 加权脱锚 bps
    "funding_pct": _env_float("WATCHDOG_FUNDING_PCT", 0.10),      # 单次 funding %
    "liquidations_usd_24h": _env_float("WATCHDOG_LIQ_USD", 500e6),
    "etf_outflow_usd": _env_float("WATCHDOG_ETF_OUT_USD", 200e6),
    "dex_tvl_drop_pct": _env_float("WATCHDOG_TVL_DROP_PCT", 0.10),
    "fg_extreme_high": _env_float("WATCHDOG_FG_HIGH", 80.0),
    "fg_extreme_low": _env_float("WATCHDOG_FG_LOW", 20.0),
}

# 告警去重窗口 (秒)
DEDUP_WINDOW = int(os.environ.get("WATCHDOG_DEDUP_SEC", "86400"))  # 24h

# 清算检测器只认这么新的 raw_metrics 行; refresh 每天一次, 留 2h 余量
DB_FRESH_HOURS = _env_float("WATCHDOG_DB_FRESH_H", 26.0)

# ETF 是日粒度行 (ts=交易日 00:00Z), 新鲜度按交易日结束 (ts+24h) 算, 只认结束 ≤ 这么多小时的交易日.
# 交易日 D 的行最早 D+1 00:00 落库, D+2 00:00 起就看不到 → 最多可见 24h, 不超过 24h 去重窗口,
# 同一天的流出不会推两次; refresh 平常 ~05:00 UTC 落库, 当天仍有 ~19h 可触发.
# 不走 env: 调大会让一行可见超过去重窗口
ETF_DAY_END_FRESH_HOURS = 24.0

# dead-man: 最新真实快照超过这么多小时 → 夜间流水线停摆.
# 历史上 nightly 跨午夜延迟跑, 相邻真实快照最长隔 28.9h; 真漏跑 (09-29) 隔 47.1h
PIPELINE_STALE_HOURS = _env_float("WATCHDOG_PIPELINE_STALE_H", 36.0)


# ────────────────────────────────────────────────────────────────────
#  Alert dataclass
# ────────────────────────────────────────────────────────────────────

@dataclass
class Alert:
    type: str           # "peg_deviation" / "funding_extreme" / ...
    severity: str       # "info" | "warning" | "critical"
    title: str          # 短标题 (飞书卡片用)
    detail: str         # 详细描述
    value: float        # 实际触发的值
    threshold: float    # 阈值
    ts: str = ""

    def __post_init__(self):
        if not self.ts:
            self.ts = datetime.utcnow().isoformat() + "Z"


@dataclass
class NoData:
    """检测器本轮拿不到输入数据 (≠ 有数据但未触发), reason 进日志和 LAST_RUN."""
    reason: str


# ────────────────────────────────────────────────────────────────────
#  State (去重)
# ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_alerts": {}, "all_alerts": []}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_alerts": {}, "all_alerts": []}


def _save_state(state: dict):
    # 滚动保留最近 200 条告警
    state["all_alerts"] = state.get("all_alerts", [])[-200:]
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2,
                                     default=str))


def _is_deduped(alert_type: str, state: dict) -> bool:
    last_ts = state.get("last_alerts", {}).get(alert_type)
    if not last_ts:
        return False
    try:
        last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - last_dt).total_seconds()
        return age < DEDUP_WINDOW
    except Exception:
        return False


def _mark_alerted(alert: Alert, state: dict):
    state.setdefault("last_alerts", {})[alert.type] = alert.ts
    state.setdefault("all_alerts", []).append(asdict(alert))


# ────────────────────────────────────────────────────────────────────
#  Check 函数 (每个 = 一个独立检测器)
#  返回 Alert (触发) / None (有数据, 未触发) / NoData(reason) (无数据, 跳过)
# ────────────────────────────────────────────────────────────────────

def check_stable_peg() -> Alert | NoData | None:
    """稳定币加权脱锚."""
    try:
        from ..factors.defillama_factors import compute_stable_peg_deviation
        res = compute_stable_peg_deviation()
    except Exception as e:
        log.warning(f"  peg check failed: {e}")
        return NoData(f"peg 拉取失败: {e}")
    if res.get("_status") != "ok":
        return NoData(f"DefiLlama 稳定币 {res.get('_status')}")
    bps = abs(res.get("weighted_deviation_bps", 0))
    if bps > THRESHOLDS["stable_peg_bps"]:
        return Alert(
            type="peg_deviation",
            severity="critical" if bps > 100 else "warning",
            title=f"⚠️ 稳定币脱锚 {bps:.1f}bps",
            detail=res.get("interpretation",
                          f"主流稳定币加权偏离 {bps:.1f}bps, 阈值 "
                          f"{THRESHOLDS['stable_peg_bps']}bps"),
            value=bps,
            threshold=THRESHOLDS["stable_peg_bps"],
        )
    return None


def check_funding_extreme() -> Alert | NoData | None:
    """主流币 funding rate 极端.

    走 factors_extended.fetch_funding_rates (Binance → OKX → Bybit 纯 HTTP):
    云端 Binance 451 时 OKX 兜底可用。以前走 ccxt, 但 requirements.txt 里没有
    ccxt, 云端每 2h 静默 return None, 这个检测器从上线起就没在云端跑过。
    """
    try:
        from . import factors_extended as fe
        rates = fe.fetch_funding_rates()
    except Exception as e:
        log.warning(f"  funding check failed: {e}")
        return NoData(f"funding 拉取失败: {e}")
    if not rates:
        log.warning("  funding check: Binance/OKX/Bybit 均无资金费率, 跳过")
        return NoData("Binance/OKX/Bybit 资金费率均不可用")
    worst_sym, worst_rate = None, 0.0
    for sym in ("BTC", "ETH", "SOL"):
        rate = rates.get(sym)
        if rate is None:
            continue
        if worst_sym is None or abs(rate) > abs(worst_rate):
            worst_sym, worst_rate = sym, rate
    if worst_sym is None:
        log.warning("  funding check: 费率表里没有 BTC/ETH/SOL, 跳过")
        return NoData("资金费率里没有 BTC/ETH/SOL")
    if abs(worst_rate) * 100 > THRESHOLDS["funding_pct"]:
        # funding rate 通常是 8h 一次, 0.1% 算极端
        sign = "多头过热" if worst_rate > 0 else "空头过密"
        return Alert(
            type="funding_extreme",
            severity="warning",
            title=f"💸 {worst_sym} funding {worst_rate*100:+.3f}% ({sign})",
            detail=f"{worst_sym} 永续 funding rate {worst_rate*100:+.3f}% "
                   f"({fe.FUNDING_SOURCE or '?'}), 阈值 ±{THRESHOLDS['funding_pct']}%",
            value=abs(worst_rate * 100),
            threshold=THRESHOLDS["funding_pct"],
        )
    return None


def check_fear_greed_extreme() -> Alert | NoData | None:
    """Fear & Greed 极端."""
    try:
        from ..http_client import http
        data = http.get_json("https://api.alternative.me/fng/?limit=1", ttl="hot")
        if not data or not data.get("data"):
            return NoData("alternative.me F&G 无数据")
        d0 = data["data"][0]
        value = float(d0.get("value", 50))
        classification = d0.get("value_classification", "")
        if value >= THRESHOLDS["fg_extreme_high"]:
            return Alert(
                type="fear_greed_high",
                severity="warning",
                title=f"😱 极度贪婪 (FG={value:.0f})",
                detail=f"Fear & Greed Index = {value:.0f} "
                       f"({classification}). 历史上 ≥{THRESHOLDS['fg_extreme_high']:.0f} "
                       f"常预示短期顶部",
                value=value,
                threshold=THRESHOLDS["fg_extreme_high"],
            )
        if value <= THRESHOLDS["fg_extreme_low"]:
            return Alert(
                type="fear_greed_low",
                severity="info",
                title=f"😨 极度恐慌 (FG={value:.0f})",
                detail=f"Fear & Greed Index = {value:.0f} "
                       f"({classification}). 历史上 ≤{THRESHOLDS['fg_extreme_low']:.0f} "
                       f"是抄底窗口",
                value=value,
                threshold=THRESHOLDS["fg_extreme_low"],
            )
    except Exception as e:
        log.warning(f"  F&G check failed: {e}")
        return NoData(f"F&G 拉取失败: {e}")
    return None


def check_dex_tvl_drop() -> Alert | NoData | None:
    """主流 DEX 24h TVL 跌幅."""
    try:
        from ..adapters import defillama_full as dlf
        top = dlf.get_top_protocols_by_tvl(50) or []
        if not top:
            return NoData("DefiLlama protocols 无数据")
        # v0.9 bug fix: "Dex" 子串过松会匹配到 CEX/derivatives Dex aggregator 等,
        # 改成精确匹配业界标准 DEX 类目
        DEX_CATEGORIES = {"Dexs", "Dexes", "DEX Aggregator", "Spot DEX"}
        dex_protocols = [
            p for p in top
            if (p.get("category") or "") in DEX_CATEGORIES
        ]
        if not dex_protocols:
            return NoData("Top-50 协议里没有 DEX 类目")
        worst, worst_change = None, 0.0
        for p in dex_protocols[:10]:
            change_1d = p.get("change_1d") or 0
            if change_1d < worst_change:
                worst_change = change_1d
                worst = p
        if worst and abs(worst_change) > THRESHOLDS["dex_tvl_drop_pct"] * 100:
            return Alert(
                type="dex_tvl_drop",
                severity="warning",
                title=f"📉 {worst.get('name')} TVL 24h {worst_change:+.1f}%",
                detail=f"{worst.get('name')} TVL 24h 变化 {worst_change:+.1f}%, "
                       f"阈值 ±{THRESHOLDS['dex_tvl_drop_pct']*100:.0f}%",
                value=abs(worst_change),
                threshold=THRESHOLDS["dex_tvl_drop_pct"] * 100,
            )
    except Exception as e:
        log.warning(f"  DEX TVL check failed: {e}")
        return NoData(f"DEX TVL 拉取失败: {e}")
    return None


def _missing(v) -> bool:
    """query_df 把 SQL NULL 读成 None 或 NaN, 两种都算缺值."""
    return v is None or v != v


def _db_cutoff(hours: float) -> str:
    """raw_metrics.ts 是 '%Y-%m-%dT%H:%M:%SZ' 字符串, 同格式可直接按字典序比较."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def check_liquidations_extreme() -> Alert | NoData | None:
    """全网衍生品 24h 清算总额极端 (coinglass, 只认 DB_FRESH_HOURS 内的行).

    coinglass 清算端点 2026-05 起改 API-key, 目前没有数据 → 每轮报 no_data。
    """
    try:
        from ..db import query_df
        df = query_df(
            """SELECT value FROM raw_metrics
               WHERE source='coinglass' AND metric='liquidations_24h_usd'
                 AND ts >= ?
               ORDER BY ts DESC LIMIT 1""",
            (_db_cutoff(DB_FRESH_HOURS),),
        )
        if df.empty or _missing(df.iloc[0]["value"]):
            return NoData(f"无 {DB_FRESH_HOURS:.0f}h 内的 coinglass "
                          f"liquidations_24h_usd 数据")
        total = float(df.iloc[0]["value"])
        if total > THRESHOLDS["liquidations_usd_24h"]:
            return Alert(
                type="liquidations_extreme",
                severity="critical" if total > 2 * THRESHOLDS["liquidations_usd_24h"] else "warning",
                title=f"💥 全网清算 24h ${total/1e6:.0f}M",
                detail=f"过去 24h 全网衍生品清算 ${total/1e6:.0f}M, 阈值 "
                       f"${THRESHOLDS['liquidations_usd_24h']/1e6:.0f}M. 大额清算常对应剧烈行情或短期顶/底",
                value=total,
                threshold=THRESHOLDS["liquidations_usd_24h"],
            )
    except Exception as e:
        log.warning(f"  liquidations check failed: {e}")
        return NoData(f"清算查询失败: {e}")
    return None


def check_etf_outflow() -> Alert | NoData | None:
    """BTC 现货 ETF 单日大额净流出 (farside, 只认新鲜的行).

    farside 行的 ts 是交易日 00:00Z (日粒度), 当天流量要到该日结束才完整,
    所以新鲜度按该日结束 (ts + 24h) 算: 只认结束不超过 ETF_DAY_END_FRESH_HOURS 的交易日,
    保证一行可见时长不超过 24h 去重窗口 (见常量注释)。
    farside 云端 403 且 CoinGlass 兜底已死, 目前没有数据 → 每轮报 no_data。
    """
    try:
        from ..db import query_df
        df = query_df(
            """SELECT value FROM raw_metrics
               WHERE source='farside_etf' AND asset_id='bitcoin'
                 AND metric='etf_net_flow_musd' AND ts >= ?
               ORDER BY ts DESC LIMIT 1""",
            (_db_cutoff(24 + ETF_DAY_END_FRESH_HOURS),),
        )
        if df.empty or _missing(df.iloc[0]["value"]):
            return NoData(f"无近期 farside_etf etf_net_flow_musd 数据 "
                          f"(交易日结束 ≤{ETF_DAY_END_FRESH_HOURS:.0f}h)")
        flow_musd = float(df.iloc[0]["value"])      # 单位: 百万美元; 负=净流出
        outflow_usd = -flow_musd * 1e6
        if outflow_usd > THRESHOLDS["etf_outflow_usd"]:
            return Alert(
                type="etf_outflow",
                severity="warning",
                title=f"🏦 BTC ETF 净流出 ${outflow_usd/1e6:.0f}M",
                detail=f"BTC 现货 ETF 单日净流出 ${outflow_usd/1e6:.0f}M, 阈值 "
                       f"${THRESHOLDS['etf_outflow_usd']/1e6:.0f}M. 机构资金撤离信号",
                value=outflow_usd,
                threshold=THRESHOLDS["etf_outflow_usd"],
            )
    except Exception as e:
        log.warning(f"  ETF outflow check failed: {e}")
        return NoData(f"ETF 查询失败: {e}")
    return None


def _snapshot_time(path: Path, data: dict) -> Optional[datetime]:
    """快照时间: 优先 timestamp 字段, 退回文件名 snapshot_YYYYMMDD_HHMM (UTC)."""
    ts = data.get("timestamp")
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.strptime(path.stem[len("snapshot_"):],
                                 "%Y%m%d_%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _latest_real_snapshot() -> Optional[tuple]:
    """META_DIR 里最新的真实快照 (path, datetime); 没有返回 None.

    跳过回填合成快照: 文件名 *_0000.json, 或 _meta.synthetic / _meta.source ==
    "backfill" (同 meta_learner._load_valid_snapshot 的口径)。
    """
    # 文件名 snapshot_YYYYMMDD_HHMM 字典序 = 时间序, 从新往旧找第一个真实快照
    for p in sorted(META_DIR.glob("snapshot_*.json"), reverse=True):
        if p.name.endswith("_0000.json"):
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        meta = data.get("_meta") or {}
        if meta.get("synthetic") or meta.get("source") == "backfill":
            continue
        dt = _snapshot_time(p, data)
        if dt is not None:
            return p, dt
    return None


def check_pipeline_stale() -> Alert | NoData | None:
    """dead-man: 夜间流水线还在不在产出真实快照.

    nightly_run.sh 自己的失败告警在任务被 GitHub 丢弃/取消/超时或 push 失败时
    根本不会执行, 次日 data_quality 又用合成 *_0000 快照把缺口补平 → 没人知道
    (09-29 整晚漏跑就是这样)。watchdog 每 2h 看一次 data/meta 最新真实快照,
    超过 PIPELINE_STALE_HOURS 即 critical 告警, 走正常 24h 去重。
    """
    try:
        found = _latest_real_snapshot()
    except Exception as e:
        log.warning(f"  pipeline_stale check failed: {e}")
        return NoData(f"读取 data/meta 失败: {e}")
    if found is None:
        return NoData("data/meta 里没有真实快照")
    path, dt = found
    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    if hours > PIPELINE_STALE_HOURS:
        return Alert(
            type="pipeline_stale",
            severity="critical",
            title=f"🛑 夜间流水线已 {hours:.0f} 小时没有产出真实快照",
            detail=f"最新真实快照 {path.name} ({dt.strftime('%Y-%m-%d %H:%M')} UTC), "
                   f"距今 {hours:.1f}h, 阈值 {PIPELINE_STALE_HOURS:.0f}h。"
                   f"nightly 可能被 GitHub 丢弃/取消/超时, 或结果没 push 上去; "
                   f"合成回填快照 (*_0000) 不算。请查 Actions 的 Nightly Auto-Run",
            value=round(hours, 1),
            threshold=PIPELINE_STALE_HOURS,
        )
    return None


# 检测器注册表 (清算 / ETF 目前无数据源, 保留在表里: 每轮报 no_data, 盲区可见)
CHECKS = [
    ("peg", check_stable_peg),
    ("funding", check_funding_extreme),
    ("fear_greed", check_fear_greed_extreme),
    ("dex_tvl", check_dex_tvl_drop),
    ("liquidations", check_liquidations_extreme),
    ("etf_outflow", check_etf_outflow),
    ("pipeline_stale", check_pipeline_stale),
]


def threshold_label(name: str) -> str:
    """检测器名 → 阈值简述 (CLI 只列本轮真正有数据的检测器)."""
    t = THRESHOLDS
    return {
        "peg": f"peg>{t['stable_peg_bps']:.0f}bps",
        "funding": f"funding>{t['funding_pct']}%",
        "fear_greed": f"F&G ≥{t['fg_extreme_high']:.0f}/≤{t['fg_extreme_low']:.0f}",
        "dex_tvl": f"DEX TVL ±{t['dex_tvl_drop_pct'] * 100:.0f}%",
        "liquidations": f"清算>${t['liquidations_usd_24h'] / 1e6:.0f}M",
        "etf_outflow": f"ETF 净流出>${t['etf_outflow_usd'] / 1e6:.0f}M",
        "pipeline_stale": f"真实快照≤{PIPELINE_STALE_HOURS:.0f}h",
    }.get(name, name)


# ────────────────────────────────────────────────────────────────────
#  推送 (飞书 + 日志)
# ────────────────────────────────────────────────────────────────────

PUSH_SENT, PUSH_NO_WEBHOOK, PUSH_FAILED = "sent", "no_webhook", "failed"


def _push_alert_to_feishu(alert: Alert) -> str:
    """推单条告警到飞书 (用 simple text 而不是 interactive card).

    返回 "sent" (至少一个群成功) | "no_webhook" (没配群) | "failed" (全部失败)。
    "failed" 时 run_check 不记去重, 下一轮重试。
    """
    try:
        from ..notifier import _load_groups, _push_one
    except Exception as e:
        log.warning(f"  notifier 导入失败: {e}")
        return PUSH_FAILED
    groups = _load_groups()
    if not groups:
        log.warning("  无 FEISHU_*_URL 配置, 跳过飞书推送")
        return PUSH_NO_WEBHOOK

    color_map = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
    emoji = color_map.get(alert.severity, "⚪")
    text = (
        f"{emoji} CRYPTO INTEL WATCHDOG\n"
        f"{alert.title}\n"
        f"{alert.detail}\n"
        f"实际值: {alert.value:.3f}  阈值: {alert.threshold:.3f}\n"
        f"时间: {alert.ts}"
    )
    payload = {"msg_type": "text", "content": {"text": text}}
    ok_count = 0
    for g in groups:
        res = _push_one(payload, g)
        if res.get("ok"):
            ok_count += 1
        else:
            log.warning(f"  飞书 {g['name']} 失败: {res.get('error')}")
    return PUSH_SENT if ok_count > 0 else PUSH_FAILED


def _log_alert(alert: Alert):
    """追加到 data/alerts.log."""
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(alert), ensure_ascii=False) + "\n")


# ────────────────────────────────────────────────────────────────────
#  主入口
# ────────────────────────────────────────────────────────────────────

# 最近一次 run_check 的检测器覆盖情况 (同 factors_extended.FUNDING_SOURCE 的模块变量写法):
#   statuses: {name: {"status": "alert"|"ok"|"no_data", "reason": str}}
#   skipped: 因无数据跳过的检测器名; push_failed: 推送失败 (未记去重) 的告警类型
LAST_RUN: dict = {}


def run_check(push: bool = True, dedup: bool = True) -> list[dict]:
    """跑一轮检查, 返回触发的告警列表.

    每个检测器的状态记进 LAST_RUN, 末尾一行汇总 "N/M 个检测器有数据; 跳过(无数据): ...";
    检测器抛异常按 no_data 记 (原因写异常)。
    去重只在推送成功 / 没配 webhook / push=False 时记; 推送失败留给下一轮重试。
    """
    state = _load_state()
    triggered: list[Alert] = []
    statuses: dict[str, dict] = {}
    push_failed: list[str] = []

    for name, fn in CHECKS:
        try:
            res = fn()
        except Exception as e:
            log.warning(f"  检测器 {name} 异常: {e}")
            res = NoData(f"异常: {e}")
        if isinstance(res, NoData):
            statuses[name] = {"status": "no_data", "reason": res.reason}
            log.info(f"  {name}: 无数据 ({res.reason})")
            continue
        if res is None:
            statuses[name] = {"status": "ok", "reason": ""}
            continue
        alert = res
        statuses[name] = {"status": "alert", "reason": alert.type}
        if dedup and _is_deduped(alert.type, state):
            log.info(f"  跳过 {alert.type} (24h 内已发过)")
            continue
        triggered.append(alert)
        _log_alert(alert)
        push_status = None
        if push:
            push_status = _push_alert_to_feishu(alert)
            if push_status == PUSH_FAILED:
                push_failed.append(alert.type)
                log.warning(f"  推送 {alert.type}: ✗ 推送失败 (不记去重, 下一轮重试)")
            else:
                log.info(f"  推送 {alert.type}: "
                         f"{'✓' if push_status == PUSH_SENT else '✗ (无 webhook)'}")
        if dedup and (not push or push_status in (PUSH_SENT, PUSH_NO_WEBHOOK)):
            _mark_alerted(alert, state)

    _save_state(state)
    skipped = [n for n, s in statuses.items() if s["status"] == "no_data"]
    n_with_data = len(statuses) - len(skipped)
    LAST_RUN.clear()
    LAST_RUN.update({
        "n_checks": len(statuses),
        "n_with_data": n_with_data,
        "skipped": skipped,
        "statuses": statuses,
        "push_failed": push_failed,
    })
    summary = (f"watchdog 完成, 触发 {len(triggered)} 条告警 · "
               f"{n_with_data}/{len(statuses)} 个检测器有数据")
    if skipped:
        summary += f"; 跳过(无数据): {', '.join(skipped)}"
    if push_failed:
        summary += f"; ✗ 推送失败: {', '.join(push_failed)}"
    log.info(summary)
    return [asdict(a) for a in triggered]


def run_loop(interval_sec: int = 300, push: bool = True):
    """循环运行 (默认 5 分钟一次)."""
    log.info(f"watchdog loop 启动, 间隔 {interval_sec}s, Ctrl+C 退出")
    while True:
        try:
            run_check(push=push)
            time.sleep(interval_sec)
        except KeyboardInterrupt:
            log.info("watchdog stopped by user")
            break
        except Exception as e:
            log.warning(f"watchdog iteration failed: {e}")
            time.sleep(interval_sec)


def history(n: int = 50) -> list[dict]:
    """读最近 N 条告警."""
    state = _load_state()
    return state.get("all_alerts", [])[-n:]


def reset_state():
    """清空告警去重状态 (debug 用)."""
    try:
        STATE_FILE.write_text(json.dumps(
            {"last_alerts": {}, "all_alerts": []}, ensure_ascii=False
        ))
        log.info("watchdog state cleared")
    except Exception as e:
        log.warning(f"reset_state failed: {e}")


def is_available() -> bool:
    return True


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== watchdog 一次性检查 (no push) ===")
    alerts = run_check(push=False, dedup=False)
    print(f"触发 {len(alerts)} 条告警:")
    for a in alerts:
        print(f"  [{a['severity']}] {a['title']}")
        print(f"    {a['detail']}")
