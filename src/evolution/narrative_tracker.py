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

输出**严格合法的 JSON** (只有 JSON, 没有任何 markdown / 文字说明 / 代码块围栏):
{
  "narratives": [
    {"name": "AI Agents", "heat_score": 78, "delta_7d": 12,
     "top_tokens": ["VIRTUAL", "AIXBT", "FARTCOIN"],
     "trigger_events": ["Virtual Protocol 推出 V2"]},
    {"name": "RWA", "heat_score": 60, "delta_7d": -3,
     "top_tokens": ["ONDO", "ENA"], "trigger_events": []}
  ]
}

JSON 合法性要求 (重要!):
- 数字不能有 +号 (正确: 12, 错误: +12)
- 所有字符串用双引号
- 数组最后一个元素后不带逗号
- 不要用 markdown 代码块围栏 (```) — 直接输出裸 JSON

规则:
- heat_score: 0-100 整数, 100 = 现象级, 0 = 无人关注
- delta_7d: 整数, 可以为负 (例: -5), 不要带 +号
- 覆盖至少 6 个 narrative (包括新发现的)
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

    # 解析 JSON, 尽量容错 Claude 输出的瑕疵
    raw = result["markdown"]

    # 1. 去掉 markdown 代码块围栏
    import re as _re
    cleaned = _re.sub(r"```(?:json)?\s*", "", raw).replace("```", "")

    # 2. 提取最外层 {...}
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < 0:
        return {"ok": False, "error": "Claude 输出不含 JSON", "raw": raw[:500]}
    json_text = cleaned[start:end+1]

    # 3. 常见 JSON 瑕疵修复
    # 3a. 数字前的 + (JSON 不允许)
    json_text = _re.sub(r":\s*\+(\d)", r": \1", json_text)
    # 3b. 尾随逗号
    json_text = _re.sub(r",(\s*[}\]])", r"\1", json_text)
    # 3c. 单引号包裹字符串 -> 双引号 (粗暴但多数情况有效)
    # 不轻易改引号 (中文里有单引号), 跳过

    try:
        data = json.loads(json_text)
    except Exception as e:
        return {"ok": False, "error": f"JSON 解析失败: {e}",
                "raw": raw[:500], "cleaned": json_text[:500]}

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
