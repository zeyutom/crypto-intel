"""早会卡片回归: 北京日期标题、AI 简报来源/时间标注、过期简报不当今天的发.

回归 (2026-10-20 运维审计):
- 标题用 runner 的 UTC 日期, 北京早上 06:56 收到的卡片标着昨天 (10-19)。
- AI 小节写死 "🤖 Claude 深度简报", 页脚写死 "Claude Max 订阅驱动", 实际云端是 Gemini (无搜索)。
- latest_brief 无时效: 本地 08:30 卡片把 2026-06-18 的简报当今天的分析推了 4 个月。
"""
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.notifier as notifier  # noqa: E402
import src.llm_brief as lb  # noqa: E402


def _summary(**over):
    d = {
        "regime": "CHOP", "regime_cn": "震荡", "prices": {}, "fg": None, "etf_5d_musd": None,
        "bull_points": [], "bear_points": [], "pm_actions": [], "risks": [], "headline": "今日震荡",
        "ai_markdown": None, "ai_ts": None, "ai_provider": None, "ai_model": None,
    }
    d.update(over)
    return d


def _card_text(card) -> str:
    out = [card["card"]["header"]["title"]["content"]]
    for el in card["card"]["elements"]:
        if "text" in el:
            out.append(el["text"]["content"])
        for f in el.get("fields", []):
            out.append(f["text"]["content"])
        for sub in el.get("elements", []):
            out.append(sub.get("content", ""))
    return "\n".join(out)


def test_card_title_uses_beijing_date(monkeypatch):
    monkeypatch.setattr(notifier, "_gather_summary", lambda: _summary())
    monkeypatch.setattr(notifier, "_beijing_now", lambda: "2026-10-20 06:56")
    card = notifier._build_card()
    assert card["card"]["header"]["title"]["content"].endswith("· 10-20")


def test_card_labels_gemini_brief_with_time_and_no_claude_footer(monkeypatch):
    monkeypatch.setattr(notifier, "_gather_summary", lambda: _summary(
        ai_markdown="## 🎯 一句话总结\n震荡偏多\n" * 5, ai_ts="2026-10-19T22:50:00Z", ai_provider="gemini"))
    text = _card_text(notifier._build_card())
    assert "Claude 深度简报" not in text and "Claude Max" not in text
    assert "AI 简报 · Gemini" in text and "无 web 搜索" in text
    assert "生成于 10-20 06:50 北京时间" in text            # 22:50 UTC = 次日 06:50 北京


def test_card_claude_cli_brief_keeps_subscription_footer(monkeypatch):
    monkeypatch.setattr(notifier, "_gather_summary", lambda: _summary(
        ai_markdown="本地 Claude 简报\n" * 5, ai_ts="2026-10-20T00:35:00Z", ai_provider="claude-cli"))
    text = _card_text(notifier._build_card())
    assert "AI 简报 · Claude CLI" in text and "无 web 搜索" not in text
    assert "Claude Max 订阅驱动" in text


def test_card_without_fresh_brief_says_not_generated(monkeypatch):
    monkeypatch.setattr(notifier, "_gather_summary", lambda: _summary())
    text = _card_text(notifier._build_card())
    assert "今日 AI 简报未生成" in text
    assert "节选" not in text and "Claude Max" not in text


def test_ai_section_title_tolerates_legacy_rows():
    t = notifier._ai_section_title(None, "garbage")
    assert t.startswith("🤖 AI 简报 · AI") and "生成于" not in t


@pytest.fixture
def summary_env(monkeypatch, tmp_path):
    """_gather_summary 的 DB 查询全部返回空表; latest_brief 读打桩的 DB 行 + tmp BRIEF_FILE。"""
    import src.db as db
    empty = pd.DataFrame(columns=["source", "regime", "asset_id", "price", "pct", "value", "value_text"])
    monkeypatch.setattr(notifier, "query_df", lambda sql, params=(): empty.copy())
    monkeypatch.setattr(db, "latest_factors", lambda: pd.DataFrame())
    monkeypatch.setattr(notifier, "build_briefing", lambda *a, **k: SimpleNamespace(
        bull_points=[], bear_points=[], pm_actions=[], risk_warnings=[], headline="h"))
    monkeypatch.setattr(lb, "BRIEF_FILE", tmp_path / "ai_brief_latest.json")
    rows = []
    monkeypatch.setattr(lb, "query_df",
                        lambda sql, params=(): pd.DataFrame(rows, columns=["ts", "asset_id", "value_text"]))
    return rows


def test_gather_summary_drops_june_brief(summary_env):
    summary_env.append(("2026-06-18T03:02:34Z", "llm_brief", "极度恐慌 + 机构悄悄接货 … 小仓位左侧吸 BTC"))
    d = notifier._gather_summary()
    assert d["ai_markdown"] is None and d["ai_ts"] is None


def test_gather_summary_keeps_fresh_brief_with_provider(summary_env):
    ts = (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_env.append((ts, "llm_brief:gemini", "昨晚的简报"))
    d = notifier._gather_summary()
    assert d["ai_markdown"] == "昨晚的简报" and d["ai_ts"] == ts and d["ai_provider"] == "gemini"
