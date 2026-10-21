"""run_claude 的 opt-in API 兜底 + weekly_review 接线.

回归 (2026-10-20 运维审计): 云端 runner 没有 Claude CLI, 本地 OAuth 也过期了, weekly-review
每个周日必挂并推 🚨 告警, 19 周没产出一份复盘; 而能用的 Gemini 路径 (llm_brief) 早就有了。
兜底必须是 opt-in: 依赖 WebSearch 的 narrative_tracker 等不能悄悄拿无搜索模型充数。
"""
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.evolution._claude_runner as cr  # noqa: E402
import src.llm_brief as lb  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """无 CLI / 无 key / 不读 .env; 预算账本隔离到 tmp。"""
    import src.llm_budget as m
    for k in ("LLM_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY",
              "CRYPTO_INTEL_DISABLE_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cr, "_load_env", lambda: None)
    monkeypatch.setattr(cr, "find_claude_cli", lambda: None)
    monkeypatch.setattr(m, "LEDGER_FILE", tmp_path / "ledger.json")
    b = m.LLMBudget()
    monkeypatch.setattr(m, "budget", b)
    return b


def _fake_gemini(calls, text="## 📊 本周数据\n复盘正文"):
    def f(system, user, max_tokens):
        calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        return text, {"input_tokens": 1200, "output_tokens": 800,
                      "model": "gemini-2.5-flash", "provider": "gemini"}
    return f


def test_no_cli_without_fallback_still_errors(env, monkeypatch):
    """默认 (api_fallback=False) 行为不变: 其他调用方不会悄悄开始烧 API 额度。"""
    monkeypatch.setenv("GEMINI_API_KEY", "k" * 12)
    calls = []
    monkeypatch.setattr(lb, "_call_gemini", _fake_gemini(calls))
    r = cr.run_claude("prompt", system="sys")
    assert r["ok"] is False and "Claude CLI not found" in r["error"]
    assert calls == []


def test_no_cli_api_fallback_uses_gemini_and_records_budget(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k" * 12)
    calls = []
    monkeypatch.setattr(lb, "_call_gemini", _fake_gemini(calls))
    r = cr.run_claude("用 web_search 验证", system="SYS", api_fallback=True, fallback_prompt="无搜索版 prompt")
    assert r["ok"] is True and r["provider"] == "gemini" and r["fallback"] is True
    assert calls[0]["user"] == "无搜索版 prompt"
    assert calls[0]["system"].startswith("SYS") and "没有 web 搜索" in calls[0]["system"]
    assert "复盘正文" in r["markdown"] and "没有 web 搜索" in r["markdown"]   # 输出里也注明
    rep = env.report()["today"]
    assert rep["calls"] == 1 and rep["tokens_in"] == 1200 and rep["tokens_out"] == 800


def test_api_fallback_without_any_key_errors(env):
    r = cr.run_claude("p", api_fallback=True)
    assert r["ok"] is False and "GEMINI_API_KEY" in r["error"] and "Claude CLI 未安装" in r["error"]


def test_api_fallback_provider_error_is_redacted_and_recorded(env, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRETKEY987654321")

    def boom(system, user, max_tokens):
        raise RuntimeError("429 for url https://x/v1beta:generateContent?key=SECRETKEY987654321")

    monkeypatch.setattr(lb, "_call_gemini", boom)
    r = cr.run_claude("p", api_fallback=True)
    assert r["ok"] is False and "SECRETKEY987654321" not in r["error"]
    assert env.report()["today"]["failures"] == 1


def test_auth_failure_falls_back_when_enabled(env, monkeypatch):
    """CLI 在但 OAuth 过期 (401 打在 stdout): api_fallback=True 时改走 API, 否则照旧报认证失败。"""
    monkeypatch.setattr(cr, "find_claude_cli", lambda: "/fake/claude")

    class R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(cmd, **kw):
        if "--version" in cmd:
            return R(0, "2.0.0")
        return R(1, "API Error: 401 OAuth access token has expired · Please run /login")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = cr.run_claude("p")
    assert r["ok"] is False and "认证失败" in r["error"]

    monkeypatch.setenv("GROQ_API_KEY", "g" * 12)
    calls = []
    monkeypatch.setattr(lb, "_call_groq", _fake_gemini(calls, text="groq 复盘"))
    r = cr.run_claude("p", api_fallback=True)
    assert r["ok"] is True and r["markdown"].startswith("groq 复盘") and len(calls) == 1
    assert "Claude CLI 认证失败" in r["markdown"]


def test_timeout_is_recorded_as_failure(env, monkeypatch):
    monkeypatch.setattr(cr, "find_claude_cli", lambda: "/fake/claude")

    def fake_run(cmd, **kw):
        if "--version" in cmd:
            class V:
                returncode, stdout, stderr = 0, "2.0.0", ""
            return V()
        raise subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = cr.run_claude("p", timeout=5)
    assert r["ok"] is False and "Timeout" in r["error"]
    assert env.report()["today"]["failures"] == 1


# ────────────────────────────────────────────────────────────────────
#  weekly_review 接线
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def wr(monkeypatch, tmp_path):
    import src.evolution.weekly_review as wr
    import src.notifier as notifier
    monkeypatch.setattr(wr, "REVIEW_DIR", tmp_path)
    monkeypatch.setattr(wr, "_gather_week_data", lambda: "## 本周数据\n- 生成了 1 份简报")
    monkeypatch.setattr(wr, "insert_evolution_log", lambda **kw: None)
    monkeypatch.setattr(notifier, "_load_groups", lambda: [])
    return wr


def test_weekly_review_enables_fallback_without_web_search_prompt(wr, monkeypatch):
    seen = {}

    def fake_run_claude(prompt, system="", timeout=900, **kw):
        seen.update(prompt=prompt, **kw)
        return {"ok": True, "markdown": "## 📊 本周数据\n...", "provider": "gemini", "fallback": True}

    monkeypatch.setattr(wr, "run_claude", fake_run_claude)
    r = wr.run_weekly_review()
    assert r["ok"] is True and r["provider"] == "gemini"
    assert seen["api_fallback"] is True
    assert "web_search" in seen["prompt"]                       # CLI 路径照旧可以搜
    assert "web_search" not in seen["fallback_prompt"]          # API 兜底没有搜索, 不能叫它搜
    assert pathlib.Path(r["file"]).read_text(encoding="utf-8").startswith("## 📊 本周数据")


def test_weekly_review_failure_propagates(wr, monkeypatch):
    monkeypatch.setattr(wr, "run_claude", lambda *a, **k: {"ok": False, "error": "no provider"})
    assert wr.run_weekly_review()["ok"] is False


def test_week_data_includes_committed_brief_file(monkeypatch, tmp_path):
    """云端简报只在 data/meta/ai_brief_latest.json 里 (intel.db 没有), 复盘也要看得到。"""
    import json
    from datetime import datetime, timezone
    import pandas as pd
    import src.evolution.weekly_review as wr
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(lb, "BRIEF_FILE", tmp_path / "ai_brief_latest.json")
    lb.BRIEF_FILE.write_text(json.dumps({"ts": ts, "provider": "gemini", "markdown": "云端 Gemini 简报"}),
                             encoding="utf-8")

    def fake_query_df(sql, params=()):
        if "claude_opus_brief" in sql:
            return pd.DataFrame(columns=["ts", "value_text"])
        if "factor_snapshots" in sql:
            return pd.DataFrame(columns=["date", "factor", "asset_id", "raw_value", "signal"])
        return pd.DataFrame(columns=["ts", "brief_ts", "rating", "comment"])

    monkeypatch.setattr(wr, "query_df", fake_query_df)
    md = wr._gather_week_data()
    assert "生成了 1 份简报" in md and "云端 Gemini 简报" in md
