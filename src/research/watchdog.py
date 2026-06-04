"""实时风险预警 watchdog (W4-A).

监听一组关键阈值, 触发即推飞书:
  1. 稳定币加权脱锚 > 50bps          → 流动性紧张/危机
  2. 主流币 funding rate 极端值       → 多空过度拥挤
  3. 24h 全市场清算量 > $500M        → 大行情
  4. BTC ETF 单日净流出 > $200M      → 机构撤资
  5. Top-10 DEX TVL 24h 跌 > 10%    → DeFi 风险
  6. Fear & Greed > 80 或 < 20      → 极端情绪

设计:
  - 每次跑 = 一轮 sample, 不维持长连接
  - 可单次跑 (check) 或循环跑 (loop, 默认 5 分钟一次)
  - 触发去重: 24h 内同一类型告警只发一次 (state 持久化)
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
#  Check 函数 (每个 = 一个独立检测器, 返回 Alert 或 None)
# ────────────────────────────────────────────────────────────────────

def check_stable_peg() -> Optional[Alert]:
    """稳定币加权脱锚."""
    try:
        from ..factors.defillama_factors import compute_stable_peg_deviation
        res = compute_stable_peg_deviation()
    except Exception as e:
        log.warning(f"  peg check failed: {e}")
        return None
    if res.get("_status") != "ok":
        return None
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


def check_funding_extreme() -> Optional[Alert]:
    """主流币 funding rate 极端."""
    try:
        from ..adapters import defillama_full as dlf
        # 用 perp OI 数据集间接拿 funding (DefiLlama 免费版不直接有 funding)
        # 退而求其次: 通过 ccxt fetch_funding_rate (如装了)
        from ..adapters import ccxt_exchange as cx
        if not cx.is_available():
            return None
        worst_sym, worst_rate = None, 0.0
        for sym in ["BTC", "ETH", "SOL"]:
            fr = cx.fetch_funding_rate(sym)
            if fr and abs(fr.get("rate", 0)) > abs(worst_rate):
                worst_rate = fr["rate"]
                worst_sym = sym
        if worst_sym and abs(worst_rate) * 100 > THRESHOLDS["funding_pct"]:
            # funding rate 通常是 8h 一次, 0.1% 算极端
            sign = "多头过热" if worst_rate > 0 else "空头过密"
            return Alert(
                type="funding_extreme",
                severity="warning",
                title=f"💸 {worst_sym} funding {worst_rate*100:+.3f}% ({sign})",
                detail=f"{worst_sym} 永续 funding rate {worst_rate*100:+.3f}%, "
                       f"阈值 ±{THRESHOLDS['funding_pct']}%",
                value=abs(worst_rate * 100),
                threshold=THRESHOLDS["funding_pct"],
            )
    except Exception as e:
        log.warning(f"  funding check failed: {e}")
    return None


def check_fear_greed_extreme() -> Optional[Alert]:
    """Fear & Greed 极端."""
    try:
        from ..http_client import http
        data = http.get_json("https://api.alternative.me/fng/?limit=1", ttl="hot")
        if not data or "data" not in data:
            return None
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
    return None


def check_dex_tvl_drop() -> Optional[Alert]:
    """主流 DEX 24h TVL 跌幅."""
    try:
        from ..adapters import defillama_full as dlf
        top = dlf.get_top_protocols_by_tvl(50) or []
        # v0.9 bug fix: "Dex" 子串过松会匹配到 CEX/derivatives Dex aggregator 等,
        # 改成精确匹配业界标准 DEX 类目
        DEX_CATEGORIES = {"Dexs", "Dexes", "DEX Aggregator", "Spot DEX"}
        dex_protocols = [
            p for p in top
            if (p.get("category") or "") in DEX_CATEGORIES
        ]
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
    return None


def check_liquidations_extreme() -> Optional[Alert]:
    """全网衍生品 24h 清算总额极端 (coinglass, 数据缺失时优雅跳过)."""
    try:
        from ..db import query_df
        df = query_df(
            """SELECT value FROM raw_metrics
               WHERE source='coinglass' AND metric='liquidations_24h_usd'
               ORDER BY ts DESC LIMIT 1"""
        )
        if df.empty or df.iloc[0]["value"] is None:
            return None
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
    return None


def check_etf_outflow() -> Optional[Alert]:
    """BTC 现货 ETF 单日大额净流出 (farside, 数据缺失时优雅跳过)."""
    try:
        from ..db import query_df
        df = query_df(
            """SELECT value FROM raw_metrics
               WHERE source='farside_etf' AND asset_id='bitcoin'
                 AND metric='etf_net_flow_musd'
               ORDER BY ts DESC LIMIT 1"""
        )
        if df.empty or df.iloc[0]["value"] is None:
            return None
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
    return None


# 检测器注册表
CHECKS = [
    ("peg", check_stable_peg),
    ("funding", check_funding_extreme),
    ("fear_greed", check_fear_greed_extreme),
    ("dex_tvl", check_dex_tvl_drop),
    ("liquidations", check_liquidations_extreme),
    ("etf_outflow", check_etf_outflow),
]


# ────────────────────────────────────────────────────────────────────
#  推送 (飞书 + 日志)
# ────────────────────────────────────────────────────────────────────

def _push_alert_to_feishu(alert: Alert) -> bool:
    """推单条告警到飞书 (用 simple text 而不是 interactive card)."""
    try:
        from ..notifier import _load_groups, _push_one
    except Exception:
        return False
    groups = _load_groups()
    if not groups:
        log.warning("  无 FEISHU_*_URL 配置, 跳过飞书推送")
        return False

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
    return ok_count > 0


def _log_alert(alert: Alert):
    """追加到 data/alerts.log."""
    ALERT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(alert), ensure_ascii=False) + "\n")


# ────────────────────────────────────────────────────────────────────
#  主入口
# ────────────────────────────────────────────────────────────────────

def run_check(push: bool = True, dedup: bool = True) -> list[dict]:
    """跑一轮检查, 返回触发的告警列表."""
    state = _load_state()
    triggered: list[Alert] = []

    for name, fn in CHECKS:
        try:
            alert = fn()
        except Exception as e:
            log.warning(f"  检测器 {name} 异常: {e}")
            continue
        if alert is None:
            continue
        if dedup and _is_deduped(alert.type, state):
            log.info(f"  跳过 {alert.type} (24h 内已发过)")
            continue
        triggered.append(alert)
        _log_alert(alert)
        if push:
            ok = _push_alert_to_feishu(alert)
            log.info(f"  推送 {alert.type}: {'✓' if ok else '✗ (无 webhook)'}")
        if dedup:
            _mark_alerted(alert, state)

    _save_state(state)
    log.info(f"watchdog 完成, 触发 {len(triggered)} 条告警")
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
