"""Knowledge Graph 加载器: 把 YAML 知识库读成 dict, 注入 LLM context。"""
from __future__ import annotations
import yaml
from pathlib import Path
from .config import ROOT

KNOW_DIR = ROOT / "knowledge"


def _load_yaml(name: str) -> dict:
    p = KNOW_DIR / name
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_assets() -> dict:
    return _load_yaml("assets.yaml")


def load_narratives() -> dict:
    return _load_yaml("narratives.yaml")


def load_patterns() -> dict:
    return _load_yaml("patterns.yaml")


def load_past_calls() -> dict:
    return _load_yaml("past_calls.yaml")


def render_for_llm() -> str:
    """把所有知识打包成 Markdown, 供 LLM briefing prompt 注入。"""
    parts = ["## 🧠 持久知识库 (Knowledge Graph)\n"]

    # Assets
    assets = load_assets()
    if assets:
        parts.append("### 资产画像\n")
        for aid, a in assets.items():
            parts.append(f"**{a.get('symbol', aid)}** ({a.get('category', '—')}): {a.get('current_thesis', '').strip()}")
            parts.append("")

    # Narratives
    nar = load_narratives()
    if nar:
        parts.append("### 活跃叙事\n")
        for nid, n in nar.items():
            status = n.get("status", "—")
            tokens = ", ".join(n.get("representative_tokens", [])[:5])
            parts.append(f"- **{n.get('name', nid)}** [{status}]: {tokens} · {n.get('driver', '')}")
        parts.append("")

    # Patterns
    pat = load_patterns()
    if pat.get("patterns"):
        parts.append("### 已验证模式 (可引用)\n")
        for p in pat.get("patterns", [])[:8]:
            parts.append(f"- **{p.get('id')}**: {p.get('trigger')} → {p.get('expected')} (置信: {p.get('confidence')})")
        parts.append("")
    if pat.get("antipatterns"):
        parts.append("### 反模式 (警惕)\n")
        for p in pat.get("antipatterns", [])[:5]:
            parts.append(f"- **{p.get('id')}**: {p.get('trigger')} → {p.get('expected')}")
        parts.append("")

    # Past calls summary
    calls = load_past_calls().get("calls", [])
    if calls:
        parts.append(f"### 过往判断复盘 (近期 {min(5, len(calls))} 条)\n")
        for c in calls[-5:]:
            correct_icon = {"true": "✓", "false": "✗", "partial": "◐"}.get(
                str(c.get("correct", "")).lower(), "?")
        parts.append("")
        parts.append(f"(共有 {len(calls)} 条历史判断, 用于防止重复错误。)")

    return "\n".join(parts)
