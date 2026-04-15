"""PM 反馈收集与加载 (给 LLM briefing 下次调用做参考)。"""
from __future__ import annotations
import pandas as pd
from .db import query_df, insert_feedback


def submit(brief_ts: str, rating: int, comment: str = "",
           source: str = "streamlit") -> None:
    insert_feedback(brief_ts, rating, comment, source)


def recent_feedback(days: int = 14) -> pd.DataFrame:
    """取近 N 天的 PM 反馈, 用于注入下次 LLM context。"""
    return query_df(
        """SELECT ts, brief_ts, rating, comment FROM brief_feedback
           WHERE ts >= datetime('now', ?)
           ORDER BY ts DESC""", (f"-{days} days",)
    )


def render_for_llm(days: int = 14) -> str:
    """把近期反馈变成 Markdown 注入 LLM prompt。"""
    df = recent_feedback(days)
    if df.empty:
        return ""
    parts = [f"## 📮 近 {days} 天 PM 反馈 (根据这些调整你的输出风格/重点)\n"]
    pos = df[df["rating"] == 1]
    neg = df[df["rating"] == -1]
    if len(pos):
        parts.append(f"**👍 被好评 ({len(pos)} 次)**:")
        for _, r in pos.head(5).iterrows():
            if (r["comment"] or "").strip():
                parts.append(f"  - [{r['brief_ts'][:10]}] {r['comment'][:200]}")
    if len(neg):
        parts.append(f"\n**👎 被差评 ({len(neg)} 次) — 这些问题避免重犯:**")
        for _, r in neg.head(5).iterrows():
            if (r["comment"] or "").strip():
                parts.append(f"  - [{r['brief_ts'][:10]}] {r['comment'][:200]}")
    comments_only = df[df["comment"].str.len() > 0]
    if len(comments_only) and not len(pos) and not len(neg):
        parts.append("**PM 评论 (无明确评分)**:")
        for _, r in comments_only.head(5).iterrows():
            parts.append(f"  - [{r['brief_ts'][:10]}] {r['comment'][:200]}")
    return "\n".join(parts)


def stats() -> dict:
    df = recent_feedback(90)
    if df.empty:
        return {"total": 0, "thumbs_up": 0, "thumbs_down": 0, "comments": 0}
    return {
        "total": len(df),
        "thumbs_up": int((df["rating"] == 1).sum()),
        "thumbs_down": int((df["rating"] == -1).sum()),
        "comments": int((df["comment"].str.len() > 0).sum()),
    }
