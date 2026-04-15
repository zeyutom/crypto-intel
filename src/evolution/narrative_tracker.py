"""Narrative 实时追踪 (每日 Claude 扫描更新)。

数据来源:
  - CoinGecko Trending (已采)
  - Web search: "hottest crypto narrative this week"
  - knowledge/narratives.yaml 既有列表

输出:
  - narratives 表: 当日每个 narrative 的热度分 + top tokens + 触发事件
  - 可选: 更新 knowledge/narratives.yaml 的 status/representative_tokens
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from ..db import query_df, upsert_narratives, insert_evolution_log
from ..knowledge import load_narratives
from ..utils import setup_logger
from ._claude_runner import run_claude

log = setup_logger("narrative_tracker", "INFO")


SYSTEM = """你是 crypto narrative 分析师。每日任务: 给现有 narrative 打热度分 + 识别新 narrative。

输出严格 JSON 格式 (没别的):
{
  "narratives": [
    {"name": "AI Agents", "heat_score": 78, "delta_7d": +12,
     "top_tokens": ["VIRTUAL", "AIXBT", "FARTCOIN"],
     "trigger_events": ["Virtual Protocol 推出 V2", "Warren Buffett 公开讨论 AI 代币"]},
    ...
  ]
}

规则:
- heat_score: 0-100, 100 = 现象级 (Memecoin 2024 Q4), 0 = 无人关注
- delta_7d: 与 7 天前热度的 +/- 百分点变化
- 覆盖至少 6 个 narrative (包括不在我既有列表里的新发现的)
- 用 web_search 查过去 7 天 Twitter/Reddit/Kaito 数据做判断"""


def _gather_input() -> str:
    """把 CG Trending + 既有 narrative 列表打包给 Claude。"""
    nar = load_narratives()
    parts = ["## 已跟踪 narrative (需要你更新热度)\n"]
    for nid, n in nar.items():
        tokens = ", ".join(n.get("representative_tokens", [])[:5])
        parts.append(f"- **{n.get('name')}** ({tokens}): {n.get('driver')} · 当前状态: {n.get('status')}")

    # CG Trending
    tr = query_df(
        """SELECT value_text FROM raw_metrics
           WHERE source='cg_trending' AND metric='trending_coins'
           ORDER BY ts DESC LIMIT 1"""
    )
    if not tr.empty and tr.iloc[0]["value_text"]:
        try:
            coins = json.loads(tr.iloc[0]["value_text"])
            parts.append("\n## 当前 CoinGecko Trending Top 7\n")
            for c in coins:
                parts.append(f"- {c.get('symbol')} ({c.get('name')}) rank #{c.get('rank')}")
        except Exception:
            pass
    return "\n".join(parts)


def run_narrative_tracking() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_prompt = _gather_input() + "\n\n请基于以上 + web_search 2025-2026 最新数据输出 JSON。"

    log.info("Running narrative tracking...")
    result = run_claude(user_prompt, system=SYSTEM, timeout=600)
    if not result["ok"]:
        return result

    # 解析 JSON (Claude 可能带 markdown 代码块)
    raw = result["markdown"]
    # 提取第一个 { ... } 段
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0:
        return {"ok": False, "error": "Claude 输出不含 JSON", "raw": raw[:500]}
    try:
        data = json.loads(raw[start:end+1])
    except Exception as e:
        return {"ok": False, "error": f"JSON 解析失败: {e}", "raw": raw[:500]}

    narratives = data.get("narratives", [])
    if not narratives:
        return {"ok": False, "error": "输出 narratives 列表为空"}

    rows = []
    for n in narratives:
        rows.append({
            "date": today,
            "narrative": n.get("name", "Unknown"),
            "heat_score": float(n.get("heat_score", 0)),
            "top_tokens": json.dumps(n.get("top_tokens", []), ensure_ascii=False),
            "trigger_events": json.dumps(n.get("trigger_events", []), ensure_ascii=False),
            "delta_7d": float(n.get("delta_7d", 0)),
        })
    upsert_narratives(rows)

    insert_evolution_log(
        kind="narrative_tracking",
        title=f"Narrative 追踪 {today}",
        content=json.dumps(narratives, ensure_ascii=False, indent=2),
        action="accepted",  # 自动入库
    )

    log.info(f"Narratives: {len(rows)} tracked")
    return {"ok": True, "narratives_count": len(rows),
            "top": [n.get("name") for n in sorted(
                narratives, key=lambda x: x.get("heat_score", 0), reverse=True)[:3]]}
