"""飞书/Lark 群机器人 Webhook 推送。

支持:
  - Interactive Card (推荐, 美观, 支持 Markdown + 按钮 + 字段)
  - 带签名校验 (可选, 设了 secret 就会自动签)
  - 自动从 DB 抓最新 AI 简报 + 行情 + 信号

环境变量:
  FEISHU_WEBHOOK_URL  # 机器人 webhook, 必需
  FEISHU_SECRET       # 签名密钥, 可选 (建机器人时勾"签名校验"才需要)
"""
from __future__ import annotations
import os
import json
import hmac
import base64
import hashlib
import time
from datetime import datetime, timezone, timedelta
import httpx
import pandas as pd
from .db import query_df
from .factors._metadata import asset_cn, regime_cn
from .report.insights import build_briefing
from .llm_brief import latest_brief
from .utils import setup_logger

log = setup_logger("feishu_notifier", "INFO")


def _sign(timestamp: str, secret: str) -> str:
    """飞书机器人签名校验。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _beijing_now() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def _regime_color(regime: str) -> str:
    """根据 regime 返回卡片主题色。"""
    colors = {
        "BULL": "green", "BEAR": "red", "CHOP": "grey",
        "CRISIS": "red", "BEAR_BOUNCE": "orange", "UNKNOWN": "grey",
    }
    return colors.get(regime, "blue")


def _gather_summary() -> dict:
    """从 DB 抓今日摘要数据。"""
    # 信号
    sig_df = query_df(
        """SELECT s.* FROM signals s
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM signals GROUP BY asset_id) m
           ON (s.asset_id IS m.a_ OR (s.asset_id IS NULL AND m.a_ IS NULL))
           AND s.ts = m.mts"""
    )
    regime = sig_df.iloc[0]["regime"] if not sig_df.empty else "UNKNOWN"

    # 行情快照 (BTC / ETH)
    snap = query_df(
        """SELECT asset_id, price FROM (
              SELECT r.asset_id, r.value AS price,
                     ROW_NUMBER() OVER (PARTITION BY r.asset_id
                       ORDER BY CASE r.source WHEN 'binance' THEN 1 WHEN 'okx' THEN 2
                                              WHEN 'coingecko' THEN 3 ELSE 9 END,
                                r.ts DESC) AS rn
              FROM raw_metrics r
              WHERE r.metric='price_usd' AND r.source IN ('binance','okx','coingecko')
            ) WHERE rn = 1"""
    )
    chg = query_df(
        """SELECT asset_id, pct FROM (
              SELECT r.asset_id, r.value AS pct,
                     ROW_NUMBER() OVER (PARTITION BY r.asset_id ORDER BY r.ts DESC) AS rn
              FROM raw_metrics r
              WHERE r.metric='change_24h_pct' AND r.source IN ('binance','okx','coingecko')
            ) WHERE rn = 1"""
    )
    prices = {}
    if not snap.empty:
        m1 = dict(zip(snap["asset_id"], snap["price"]))
        m2 = dict(zip(chg["asset_id"], chg["pct"])) if not chg.empty else {}
        for a in ["bitcoin", "ethereum", "solana"]:
            if a in m1:
                prices[a] = {"price": float(m1[a]), "chg": float(m2.get(a, 0))}

    # F&G
    fg_df = query_df(
        """SELECT value, value_text FROM raw_metrics
           WHERE source='feargreed' AND metric='fear_greed_index'
           ORDER BY ts DESC LIMIT 1"""
    )
    fg = None
    if not fg_df.empty:
        fg = {"value": int(fg_df.iloc[0]["value"]), "label": fg_df.iloc[0]["value_text"]}

    # ETF 5d
    etf_df = query_df(
        """SELECT value FROM raw_metrics
           WHERE source='farside_etf' AND metric='etf_net_flow_musd'
           ORDER BY ts DESC LIMIT 5"""
    )
    etf_5d_musd = float(etf_df["value"].sum()) if not etf_df.empty else None

    # 规则版 briefing
    from .db import latest_factors
    fact_df = latest_factors()
    src_df = query_df("SELECT DISTINCT source FROM raw_metrics WHERE source != '_meta'")
    expected = {"coingecko", "binance", "okx", "coinbase", "defillama", "feargreed",
                "farside_etf", "cg_global", "cg_trending", "coinglass", "yfinance_macro"}
    missing = sorted(expected - set(src_df["source"].tolist()))
    briefing = build_briefing(sig_df, fact_df, regime, missing)

    # AI 简报
    ai = latest_brief()

    return {
        "regime": regime,
        "regime_cn": regime_cn(regime)[0],
        "prices": prices,
        "fg": fg,
        "etf_5d_musd": etf_5d_musd,
        "bull_points": briefing.bull_points[:3],
        "bear_points": briefing.bear_points[:2],
        "pm_actions": briefing.pm_actions[:2],
        "risks": briefing.risk_warnings[:2],
        "headline": briefing.headline,
        "ai_markdown": ai["markdown"] if ai else None,
        "ai_ts": ai["ts"] if ai else None,
    }


def _build_card(dashboard_url: str = "https://crypto-intel-zeyutom.streamlit.app/") -> dict:
    """构建飞书交互式卡片。"""
    d = _gather_summary()

    # 行情行 (BTC / ETH / SOL)
    price_lines = []
    for aid in ("bitcoin", "ethereum", "solana"):
        if aid in d["prices"]:
            p = d["prices"][aid]
            chg = p["chg"]
            arrow = "🟢" if chg > 0 else ("🔴" if chg < 0 else "⚪")
            sym = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL"}[aid]
            price_lines.append(f"{arrow} **{sym}** ${p['price']:,.0f} ({chg:+.2f}%)")
    price_md = " · ".join(price_lines) if price_lines else "(无行情数据)"

    # 关键指标
    metric_lines = []
    if d["fg"]:
        metric_lines.append(f"😨 F&G: **{d['fg']['value']}/100** ({d['fg']['label']})")
    if d["etf_5d_musd"] is not None:
        emoji = "📈" if d["etf_5d_musd"] > 0 else "📉"
        metric_lines.append(f"{emoji} ETF 5d 累计: **${d['etf_5d_musd']:+.0f}M**")
    metrics_md = " · ".join(metric_lines) if metric_lines else ""

    # 看多
    bull_md = "\n".join(f"🟢 {p}" for p in d["bull_points"]) or "*(无明显看多信号)*"
    # 看空
    bear_md = "\n".join(f"🔴 {p}" for p in d["bear_points"]) or "*(无明显看空信号)*"
    # PM 行动
    action_md = "\n".join(f"🎯 {p}" for p in d["pm_actions"]) or "*(维持现有配置)*"

    elements = [
        # Headline
        {"tag": "div", "text": {"tag": "lark_md",
                                 "content": f"**📋 {d['headline']}**"}},
        {"tag": "div", "text": {"tag": "lark_md",
                                 "content": f"市场状态: **{d['regime_cn']}** ({d['regime']})"}},
        {"tag": "hr"},

        # 行情 + 关键指标
        {"tag": "div", "text": {"tag": "lark_md", "content": price_md}},
    ]
    if metrics_md:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": metrics_md}})

    elements.append({"tag": "hr"})

    # 多空
    elements.extend([
        {"tag": "div", "fields": [
            {"is_short": True, "text": {"tag": "lark_md",
                                         "content": f"**📈 看多 ({len(d['bull_points'])}条)**\n{bull_md}"}},
            {"is_short": True, "text": {"tag": "lark_md",
                                         "content": f"**📉 看空 ({len(d['bear_points'])}条)**\n{bear_md}"}},
        ]},
    ])

    # 风险
    if d["risks"]:
        risk_md = "\n".join(f"⚠️ {r}" for r in d["risks"])
        elements.extend([
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                                     "content": f"**⚠️ 风险提示**\n{risk_md}"}},
        ])

    # PM 行动
    elements.extend([
        {"tag": "hr"},
        {"tag": "div", "text": {"tag": "lark_md",
                                 "content": f"**🎯 PM 行动建议**\n{action_md}"}},
    ])

    # AI 简报节选
    if d["ai_markdown"]:
        # 取前 500 字符, 避开代码块等
        excerpt = d["ai_markdown"][:500].rsplit("\n", 1)[0]
        elements.extend([
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
                                     "content": f"**🤖 Claude 深度简报 (节选)**\n\n{excerpt}...\n\n*[完整版见仪表盘]*"}},
        ])

    # 操作按钮
    elements.extend([
        {"tag": "hr"},
        {"tag": "action", "actions": [
            {"tag": "button",
             "text": {"tag": "plain_text", "content": "🌐 查看完整仪表盘"},
             "type": "primary",
             "url": dashboard_url},
            {"tag": "button",
             "text": {"tag": "plain_text", "content": "📊 因子详解"},
             "type": "default",
             "url": f"{dashboard_url}因子详解"},
        ]},
        # 页脚
        {"tag": "note", "elements": [
            {"tag": "plain_text",
             "content": f"生成时间: {_beijing_now()} 北京时间 · "
                        f"12 数据源 · 15 因子 · Claude Max 订阅驱动"},
        ]},
    ])

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                           "content": f"📋 Crypto Intel 早会简报 · {datetime.now().strftime('%m-%d')}"},
                "template": _regime_color(d["regime"]),
            },
            "elements": elements,
            "config": {"wide_screen_mode": True},
        },
    }


def _load_groups() -> list[dict]:
    """扫 env, 返回所有配置的飞书群 [{name, url, secret}, ...]。

    支持两种写法:
      1. 多群索引式:
         FEISHU_GROUP_1_URL=...
         FEISHU_GROUP_1_SECRET=...   (可选)
         FEISHU_GROUP_1_NAME=投研群   (可选, 默认 Group-1)
         FEISHU_GROUP_2_URL=...
         ...
      2. 单群兼容式 (老配置):
         FEISHU_WEBHOOK_URL=...
         FEISHU_SECRET=...
    """
    groups = []
    # 扫索引式
    idx = 1
    while True:
        url = os.getenv(f"FEISHU_GROUP_{idx}_URL", "").strip()
        if not url:
            break
        groups.append({
            "name": os.getenv(f"FEISHU_GROUP_{idx}_NAME", f"Group-{idx}"),
            "url": url,
            "secret": os.getenv(f"FEISHU_GROUP_{idx}_SECRET", "").strip(),
        })
        idx += 1
    # 单群兼容 (若未写索引式时)
    if not groups:
        url = os.getenv("FEISHU_WEBHOOK_URL", "").strip()
        if url:
            groups.append({
                "name": os.getenv("FEISHU_GROUP_NAME", "Default"),
                "url": url,
                "secret": os.getenv("FEISHU_SECRET", "").strip(),
            })
    return groups


def _push_one(payload: dict, group: dict) -> dict:
    """推单个群, 返回 {ok, error, group_name}。"""
    url, secret, name = group["url"], group["secret"], group["name"]
    msg = dict(payload)  # shallow copy, don't mutate caller
    if secret:
        ts = str(int(time.time()))
        msg["timestamp"] = ts
        msg["sign"] = _sign(ts, secret)
    try:
        r = httpx.post(url, json=msg, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"HTTP 错误: {e}", "group_name": name}
    if data.get("code") not in (0, None):
        return {"ok": False, "error": f"飞书返回: {data}", "group_name": name}
    return {"ok": True, "group_name": name, "response": data}


def push_to_feishu(webhook_url: str | None = None,
                    secret: str | None = None,
                    dashboard_url: str = "https://crypto-intel-zeyutom.streamlit.app/") -> dict:
    """推送今日简报到所有配置的飞书群。

    如果显式传 webhook_url, 只推那一个 (兼容老调用)。
    否则从 env 扫所有群, 逐个推送。
    """
    # 显式单群 (测试用)
    if webhook_url:
        groups = [{"name": "Explicit", "url": webhook_url, "secret": secret or ""}]
    else:
        groups = _load_groups()

    if not groups:
        return {"ok": False, "error": "未配置飞书群 (FEISHU_GROUP_N_URL 或 FEISHU_WEBHOOK_URL)",
                "pushed": 0}

    payload = _build_card(dashboard_url)
    results = [_push_one(payload, g) for g in groups]
    ok_count = sum(1 for r in results if r["ok"])
    err_count = len(results) - ok_count

    overall_ok = err_count == 0
    log.info(f"Feishu push: {ok_count}/{len(results)} OK")
    return {
        "ok": overall_ok,
        "pushed": ok_count,
        "failed": err_count,
        "groups": [{"name": r["group_name"], "ok": r["ok"],
                    "error": r.get("error")} for r in results],
    }


def push_test_message(webhook_url: str, secret: str = "") -> dict:
    """发一条测试消息 (简单 text, 用于 webhook 验证)。"""
    payload = {
        "msg_type": "text",
        "content": {"text": "✅ Crypto Intel 测试消息 · 飞书推送配置成功\n"
                            "每日 08:30 会自动推送早会简报到本群"},
    }
    if secret:
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = _sign(ts, secret)
    try:
        r = httpx.post(webhook_url, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": data.get("code") in (0, None), "response": data}
