"""Claude Opus 4.6 + Web Search 智能日报生成。

输入: 全部因子 + 数据源覆盖 + 最近事件
输出: 5-8 段「投资经理早会简报」, 包含市场叙事综述、关键论据、风险、行动建议、24h 重点新闻

调用模式:
    需在 Streamlit Secrets / GitHub Secrets 配置 ANTHROPIC_API_KEY
    模型: claude-opus-4-6 (强推) / claude-sonnet-4-6 (省成本)
    工具: web_search (Anthropic 内置, $10/1000 calls)
"""
from __future__ import annotations
import os
import json
import re
from datetime import datetime, timezone
from typing import Any
import httpx
import pandas as pd
from .db import query_df, latest_factors
from .factors._metadata import factor_meta, asset_cn, regime_cn
from .utils import setup_logger

log = setup_logger("llm_brief", "INFO")

DEFAULT_MODEL = "claude-opus-4-6"
SYSTEM_PROMPT = """你是一名资深加密货币投研分析师, 服务于一位机构投资经理 (PM)。
你的任务是基于今天采集到的多源因子 + 必要时通过 web 搜索补充最新事件,
为 PM 写一份"早会简报", 帮他/她快速决策今日方向。

要求:
1. 中文输出, 简洁有力, 不堆术语 (用通俗解释 + 数字佐证)
2. 必须基于真实数据 (因子值、源覆盖、事件), 不编造
3. 通过 web_search 主动搜索过去 24h 关键事件 (监管/Hack/上市公告/宏观), 至少搜 2-3 次
4. 区分 "数据驱动" (因子) vs "叙事驱动" (web 信息) 两类信号, 避免混淆
5. 给出可执行的 PM 建议, 不只是宏观判断
6. 风险提示要具体 (对哪些仓位有什么影响, 而不是"注意风险")

输出 Markdown 格式, 包含以下章节:
## 🎯 一句话总结
## 📊 因子综合 (今日多空数 / Regime / 关键贡献因子)
## 📰 24h 关键事件 (web 搜来的新闻, 标注来源)
## 📈 看多论据 / 📉 看空论据 (各 2-4 条, 数据 + 事件混合)
## ⚠️ 风险点 (具体, 1-3 条)
## 🎯 PM 行动建议 (具体, 2-4 条)
## 📚 数据置信度 (说明哪些源缺失, 对结论的影响)"""


def _gather_factor_summary() -> str:
    """把所有因子的当前值整理成 Claude 易读的格式。"""
    df = latest_factors()
    if df.empty:
        return "(尚无因子数据)"
    lines = ["| 因子 | 类别 | 资产 | 当前值 | 信号 | 置信度 |",
             "|------|------|------|--------|------|--------|"]
    for _, r in df.iterrows():
        meta = factor_meta(r["factor"])
        cn = meta.cn_name if meta else r["factor"]
        cat = meta.category if meta else "—"
        sig = int(r["signal"]) if r["signal"] is not None else 0
        sig_str = {1: "+1 看多", -1: "-1 看空", 0: "0 中性"}[sig]
        try:
            raw = float(r["raw_value"])
            rv_str = f"{raw:.4f}" if abs(raw) < 100 else f"{raw:,.2f}"
        except Exception:
            rv_str = str(r["raw_value"])
        lines.append(f"| {cn} | {cat} | {asset_cn(r.get('asset_id'))} | {rv_str} | {sig_str} | {(r['confidence'] or 0)*100:.0f}% |")
    return "\n".join(lines)


def _gather_market_snapshot() -> str:
    snap = query_df(
        """SELECT asset_id, value AS price FROM (
              SELECT r.asset_id, r.value, r.source AS src,
                     ROW_NUMBER() OVER (PARTITION BY r.asset_id
                       ORDER BY CASE r.source WHEN 'binance' THEN 1 WHEN 'okx' THEN 2
                                              WHEN 'coingecko' THEN 3 ELSE 9 END,
                                r.ts DESC) AS rn
              FROM raw_metrics r
              WHERE r.metric='price_usd' AND r.source IN ('binance','okx','coingecko')
            ) WHERE rn = 1"""
    )
    if snap.empty:
        return "(无行情数据)"
    chg = query_df(
        """SELECT asset_id, value AS pct FROM (
              SELECT r.asset_id, r.value, ROW_NUMBER() OVER (PARTITION BY r.asset_id
                ORDER BY r.ts DESC) AS rn FROM raw_metrics r
              WHERE r.metric='change_24h_pct' AND r.source IN ('binance','okx','coingecko')
            ) WHERE rn = 1"""
    )
    merged = snap.merge(chg, on="asset_id", how="left")
    lines = []
    for _, r in merged.iterrows():
        lines.append(f"- {asset_cn(r['asset_id'])}: ${r['price']:,.2f} "
                     f"(24h {(r['pct'] or 0):+.2f}%)")
    return "\n".join(lines)


def _gather_etf_recent() -> str:
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='farside_etf' AND metric='etf_net_flow_musd'
           ORDER BY ts DESC LIMIT 5"""
    )
    if df.empty:
        return "(无 ETF 数据)"
    lines = ["BTC 现货 ETF 近 5 日净流入 ($M):"]
    for _, r in df.iterrows():
        date = r["ts"][:10]
        lines.append(f"- {date}: ${r['value']:+.1f}M")
    return "\n".join(lines)


def _gather_trending() -> str:
    df = query_df(
        """SELECT value_text FROM raw_metrics
           WHERE source='cg_trending' AND metric='trending_coins'
           ORDER BY ts DESC LIMIT 1"""
    )
    if df.empty or not df.iloc[0]["value_text"]:
        return "(无 Trending 数据)"
    try:
        coins = json.loads(df.iloc[0]["value_text"])
        lines = ["CoinGecko Trending Top 7:"]
        for c in coins:
            lines.append(f"- {c.get('symbol', '?')} ({c.get('name', '?')}) "
                         f"rank #{c.get('rank') or 'N/A'}, 24h {(c.get('change_24h') or 0):+.1f}%")
        return "\n".join(lines)
    except Exception:
        return "(Trending 解析失败)"


def _build_user_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = [
        f"# 今日数据 ({today} UTC)\n",
        "## 行情快照\n" + _gather_market_snapshot() + "\n",
        "## 全部因子值\n" + _gather_factor_summary() + "\n",
        "## ETF 资金流\n" + _gather_etf_recent() + "\n",
        "## 当前热门叙事\n" + _gather_trending() + "\n",
    ]
    # v0.5: 注入 knowledge graph + 近期 PM 反馈
    try:
        from .knowledge import render_for_llm as kg_render
        kg = kg_render()
        if kg:
            parts.append("\n" + kg + "\n")
    except Exception:
        pass
    try:
        from .feedback import render_for_llm as fb_render
        fb = fb_render(14)
        if fb:
            parts.append("\n" + fb + "\n")
    except Exception:
        pass
    parts.extend([
        "\n---\n",
        "请基于以上数据 + 知识库 + web_search 工具搜索过去 24-48h 的关键加密事件 "
        "(监管动态、CEX 上市公告、协议 hack、宏观会议/数据), "
        "为 PM 写一份完整简报。如有模式匹配 (patterns.yaml), 显式引用。"
        "如 PM 近期给过反馈, 调整你的风格和重点。",
    ])
    return "\n".join(parts)


# 免费 provider 无内置 web 搜索 — 提示模型别编造新闻
_NO_SEARCH_NOTE = (
    "\n\n[重要] 本次无 web 搜索接入。'24h 关键事件'章节请基于因子/数据本身的含义"
    "谨慎概述, 或标注'需人工补充实时新闻'; 严禁编造任何具体新闻、来源或数字。"
)


def _pick_provider() -> str:
    """选 LLM provider: LLM_PROVIDER 显式优先; 否则按 key 存在性 (免费源优先)。"""
    p = os.getenv("LLM_PROVIDER", "").lower().strip()
    if p:
        return p
    if os.getenv("GEMINI_API_KEY"):
        return "gemini"
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return ""


def _call_gemini(system: str, user: str, max_tokens: int) -> tuple[str, dict]:
    """Google Gemini (免费档, REST, 无需额外依赖)。"""
    key = os.getenv("GEMINI_API_KEY", "")
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens, "temperature": 0.4,
            # 2.5-flash 是 thinking 模型: 关掉思考预算, 把全部输出额度留给正文
            # (否则 thinking 吃掉额度, 正文被截断成几十字)
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    r = httpx.post(url, json=body, timeout=120)
    r.raise_for_status()
    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        raise RuntimeError(f"Gemini 无候选返回 (可能被安全过滤): {str(data)[:160]}")
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts)
    um = data.get("usageMetadata", {})
    return text, {"input_tokens": um.get("promptTokenCount"),
                  "output_tokens": um.get("candidatesTokenCount"),
                  "model": model, "provider": "gemini"}


def _call_groq(system: str, user: str, max_tokens: int) -> tuple[str, dict]:
    """Groq (免费档, OpenAI 兼容, 极快, REST)。"""
    key = os.getenv("GROQ_API_KEY", "")
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    r = httpx.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "temperature": 0.4, "max_tokens": max_tokens,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}]},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    u = data.get("usage", {})
    return text, {"input_tokens": u.get("prompt_tokens"),
                  "output_tokens": u.get("completion_tokens"),
                  "model": model, "provider": "groq"}


def generate_brief(model: str = DEFAULT_MODEL,
                   max_tokens: int = 4096,
                   max_searches: int = 5) -> dict:
    """生成 LLM 智能简报 (多 provider)。

    provider 选择: 环境变量 LLM_PROVIDER 显式优先, 否则按 key 自动选
    (GEMINI_API_KEY > GROQ_API_KEY > ANTHROPIC_API_KEY)。gemini/groq 免费档, anthropic 付费。

    Returns:
        {"ok": bool, "markdown": str, "model": str, "usage": dict, "error": str|None}
    """
    provider = _pick_provider()
    if not provider:
        return {"ok": False, "markdown": "", "model": model, "usage": {},
                "error": "未配置任何 LLM key (GEMINI_API_KEY / GROQ_API_KEY / ANTHROPIC_API_KEY)"}

    user_prompt = _build_user_prompt()

    if provider == "anthropic":
        return _generate_anthropic(model, max_tokens, max_searches, user_prompt)

    try:
        if provider == "gemini":
            text, usage = _call_gemini(SYSTEM_PROMPT + _NO_SEARCH_NOTE, user_prompt, max_tokens)
        elif provider == "groq":
            text, usage = _call_groq(SYSTEM_PROMPT + _NO_SEARCH_NOTE, user_prompt, max_tokens)
        else:
            return {"ok": False, "markdown": "", "model": model, "usage": {},
                    "error": f"未知 LLM_PROVIDER: {provider}"}
    except Exception as e:
        return {"ok": False, "markdown": "", "model": model, "usage": {},
                "error": f"{provider} 调用失败: {e}"}

    if not (text or "").strip():
        return {"ok": False, "markdown": "", "model": usage.get("model"), "usage": usage,
                "error": f"{provider} 返回空内容"}
    log.info(f"LLM brief OK ({provider}): in={usage.get('input_tokens')} out={usage.get('output_tokens')}")
    return {"ok": True, "markdown": text.strip(), "model": usage.get("model"),
            "usage": usage, "error": None}


def _generate_anthropic(model: str, max_tokens: int, max_searches: int,
                        user_prompt: str) -> dict:
    """Anthropic Claude + 内置 web_search (付费路径)。"""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    try:
        from anthropic import Anthropic
    except ImportError:
        return {"ok": False, "markdown": "", "model": model, "usage": {},
                "error": "anthropic SDK 未安装 (pip install anthropic)"}

    client = Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_searches,
            }],
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as e:
        return {"ok": False, "markdown": "", "model": model, "usage": {},
                "error": f"Claude API 调用失败: {e}"}

    md_parts = [blk.text for blk in resp.content if blk.type == "text"]
    markdown = "\n\n".join(md_parts).strip()
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "model": resp.model, "provider": "anthropic",
        "stop_reason": resp.stop_reason,
    }
    log.info(f"LLM brief OK (anthropic): in={usage['input_tokens']} out={usage['output_tokens']}")
    return {"ok": True, "markdown": markdown, "model": model, "usage": usage, "error": None}


def save_brief(brief: dict) -> None:
    """把简报存到 raw_metrics (source=_meta), 仪表盘可读取。"""
    from .db import upsert_raw
    if not brief.get("ok"):
        return
    upsert_raw([{
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "_meta",
        "asset_id": "llm_brief",
        "metric": "claude_opus_brief",
        "value": float(brief["usage"].get("output_tokens", 0)),
        "value_text": brief["markdown"],
    }])


def latest_brief() -> dict | None:
    """读取最近一份 LLM 简报。"""
    df = query_df(
        """SELECT ts, value_text FROM raw_metrics
           WHERE source='_meta' AND metric='claude_opus_brief'
           ORDER BY ts DESC LIMIT 1"""
    )
    if df.empty:
        return None
    return {"ts": df.iloc[0]["ts"], "markdown": df.iloc[0]["value_text"]}
