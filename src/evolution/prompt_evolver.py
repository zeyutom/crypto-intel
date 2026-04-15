"""月度 Prompt 自动迭代。

让 Claude 读过去一个月的 briefing + PM feedback, 评估 SYSTEM_PROMPT 的问题,
输出改进版 prompt (人工 review 后替换到 src/llm_brief.py)。
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..db import query_df, insert_evolution_log
from ..config import ROOT
from ..llm_brief import SYSTEM_PROMPT
from ..utils import setup_logger
from ._claude_runner import run_claude

log = setup_logger("prompt_evolver", "INFO")

PROPOSAL_DIR = ROOT / "data" / "proposals"
PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)


SYSTEM = """你是 prompt engineering 专家。

任务: 根据过去 30 天的 briefing 样本 + PM 反馈 (👍/👎 + 评论),
评估当前 SYSTEM_PROMPT 的不足, 给出改进版。

原则:
1. 从 PM 反馈里提炼模式: 他反复吐槽什么? 点赞什么?
2. 对比不同 briefing 的风格与质量, 找出"好 briefing"共性
3. 改进必须具体 (例如: 明确"第一段永远是一句话 headline" / "不要用 XX 词")
4. 保持系统原有目标 (为 PM 决策服务) 不变
5. 输出改进版完整 prompt + 改了什么的 diff 说明"""


def _gather_context() -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")

    briefs = query_df(
        """SELECT ts, value_text FROM raw_metrics
           WHERE source='_meta' AND metric='claude_opus_brief' AND ts >= ?
           ORDER BY ts DESC LIMIT 20""", (cutoff,)
    )

    fb = query_df(
        """SELECT ts, brief_ts, rating, comment FROM brief_feedback
           WHERE ts >= ? ORDER BY ts DESC""", (cutoff,)
    )

    parts = [
        "## 当前 SYSTEM_PROMPT\n",
        "```\n" + SYSTEM_PROMPT + "\n```\n",
        f"\n## 过去 30 天 briefing 样本 ({len(briefs)} 份)\n",
    ]
    for _, r in briefs.head(5).iterrows():
        parts.append(f"### {r['ts'][:10]}\n{(r['value_text'] or '')[:1500]}\n---")

    parts.append(f"\n## PM 反馈 ({len(fb)} 条)\n")
    for _, r in fb.iterrows():
        tag = "👍" if r["rating"] == 1 else ("👎" if r["rating"] == -1 else "中性")
        parts.append(f"- [{r['ts'][:10]}] {tag} {r['comment'] or '(无评论)'}")

    return "\n".join(parts)


def run_prompt_evolution() -> dict:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    user_prompt = (_gather_context() + "\n\n---\n\n"
                   "请分析并输出改进版 SYSTEM_PROMPT。")

    log.info("Running prompt evolution...")
    result = run_claude(user_prompt, system=SYSTEM, timeout=600)
    if not result["ok"]:
        return result

    md = result["markdown"]
    out_path = PROPOSAL_DIR / f"prompt_evolution_{today}.md"
    out_path.write_text(md, encoding="utf-8")
    log.info(f"Prompt evolution saved: {out_path}")

    insert_evolution_log(
        kind="prompt_evolve",
        title=f"Prompt 迭代建议 {today}",
        content=md,
        action="pending",
        meta=json.dumps({"file": str(out_path)}),
    )
    return {"ok": True, "file": str(out_path), "chars": len(md)}
