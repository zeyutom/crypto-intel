"""周度自我复盘 Agent (周日 22:00 跑)。

职责:
  1. 读过去 7 天的 briefings / 因子值 / 实际价格
  2. 回答三问:
     - 哪些因子/判断准?哪些不准?
     - 我们漏报了什么重大事件?
     - 有无新叙事形成?
  3. 输出 `weekly_review_YYYY-WW.md` 到 data/reviews/
  4. 追加到 evolution_log + past_calls.yaml
  5. 推到飞书群
"""
from __future__ import annotations
import json
import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..db import query_df, insert_evolution_log
from ..config import ROOT
from ..utils import setup_logger
from ._claude_runner import run_claude

log = setup_logger("weekly_review", "INFO")

REVIEW_DIR = ROOT / "data" / "reviews"
REVIEW_DIR.mkdir(parents=True, exist_ok=True)


SYSTEM = """你是一名资深加密投研 leader。
你在每个周日晚上要做"本周复盘 + 系统自我改进建议"。

原则:
1. 诚实: 如果系统这周表现差,直说
2. 量化: 每个判断配数字 (因子值 / 实际收益)
3. 可执行: 建议必须具体 (加什么因子, 删什么源, 改什么 prompt)
4. 记忆: 识别出的重要模式要明确 "我建议加到 patterns.yaml"
5. 预测: 看到新叙事苗头要说出来 (哪些 token / 哪些社交信号)

输出 Markdown, 章节固定:
## 📊 本周数据
## 🎯 判断准确性复盘
## 🔍 漏报事件
## 🌱 新叙事/热点识别
## 💡 系统改进建议 (具体到代码/配置)
## 📝 新增模式 (YAML 片段, 待追加到 patterns.yaml)"""


def _gather_week_data() -> str:
    """汇总过去 7 天的核心数据。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    # 过去 7 天 briefings
    briefs = query_df(
        """SELECT ts, value_text FROM raw_metrics
           WHERE source='_meta' AND metric='claude_opus_brief'
             AND ts >= ?
           ORDER BY ts""", (cutoff,)
    )

    # 过去 7 天因子值 (聚合)
    factors = query_df(
        """SELECT date, factor, asset_id, raw_value, signal
           FROM factor_snapshots
           WHERE date >= ?
           ORDER BY date, factor""", (cutoff,)
    )

    # PM 反馈
    feedback = query_df(
        """SELECT ts, brief_ts, rating, comment FROM brief_feedback
           WHERE ts >= ? ORDER BY ts""", (cutoff,)
    )

    parts = [f"## 本周数据 (过去 7 天, since {cutoff})\n"]
    parts.append(f"- 生成了 {len(briefs)} 份简报")
    parts.append(f"- 因子观测数: {len(factors)}")
    parts.append(f"- PM 反馈: {len(feedback)} 条 "
                 f"(👍 {len(feedback[feedback['rating']==1]) if len(feedback) else 0}, "
                 f"👎 {len(feedback[feedback['rating']==-1]) if len(feedback) else 0})")

    if len(briefs):
        parts.append("\n### 本周简报要点 (按日)\n")
        for _, r in briefs.iterrows():
            excerpt = (r["value_text"] or "")[:400]
            parts.append(f"**{r['ts'][:10]}**:\n{excerpt}\n---")

    if len(factors):
        # 每个因子本周平均 signal
        agg = (factors.groupby("factor")["signal"]
               .mean().reset_index()
               .sort_values("signal", key=lambda x: x.abs(), ascending=False))
        parts.append("\n### 本周因子平均信号 (|avg signal| 降序, 越偏极端越强)\n")
        for _, r in agg.head(15).iterrows():
            parts.append(f"- {r['factor']}: avg_signal = {r['signal']:+.2f}")

    if len(feedback):
        parts.append("\n### PM 反馈详情\n")
        for _, r in feedback.iterrows():
            tag = "👍" if r["rating"] == 1 else ("👎" if r["rating"] == -1 else "")
            if (r["comment"] or "").strip():
                parts.append(f"- [{r['ts'][:10]}] {tag} {r['comment']}")

    return "\n".join(parts)


def run_weekly_review() -> dict:
    today = datetime.now(timezone.utc)
    week_iso = today.strftime("%Y-W%V")

    data_md = _gather_week_data()
    user_prompt = (
        f"{data_md}\n\n---\n\n"
        f"请基于以上数据做本周复盘。今天是 {today.strftime('%Y-%m-%d')}。"
        f"如果你想查最新事件, 用 web_search 验证。"
    )

    log.info("Running weekly review via Claude CLI...")
    result = run_claude(user_prompt, system=SYSTEM, timeout=900)
    if not result["ok"]:
        log.warning("Weekly review failed: %s", result.get("error"))
        return result

    md = result["markdown"]
    fname = f"weekly_review_{week_iso}.md"
    out_path = REVIEW_DIR / fname
    out_path.write_text(md, encoding="utf-8")
    log.info(f"Weekly review saved: {out_path}")

    # 记到进化日志
    insert_evolution_log(
        kind="weekly_review",
        title=f"周复盘 {week_iso}",
        content=md,
        action="pending",
        meta=json.dumps({"file": str(out_path)}),
    )

    # 推飞书
    try:
        from ..notifier import push_to_feishu, _load_groups
        groups = _load_groups()
        if groups:
            # 简化: 发一条 text 消息 + 标题 + 前 800 字
            import httpx
            for g in groups:
                text_msg = {
                    "msg_type": "text",
                    "content": {"text": f"📝 本周复盘 {week_iso}\n\n{md[:800]}...\n\n[完整版见仪表盘 AI 自我进化页]"}
                }
                try:
                    httpx.post(g["url"], json=text_msg, timeout=15)
                except Exception:
                    pass
    except Exception as e:
        log.warning("Feishu push weekly review failed: %s", e)

    return {"ok": True, "file": str(out_path), "week": week_iso,
            "chars": len(md)}
