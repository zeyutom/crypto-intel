"""AI 简报运维回归: key 不进 URL/日志、预算记账、简报持久化 + 时效门槛.

回归 (2026-10-20 运维审计):
- Gemini key 拼在 ?key= 查询参数里, httpx 报错文案带完整 URL → 进 nightly 日志 → 提交到公开仓库。
- generate_brief 从不记 LLM 预算: 云端每晚 Gemini 调用成功, llm-budget 却报 0 tokens。
- 简报只存 runner 的 intel.db, nightly 不回推 → 卡片/仪表盘 4 个月都在用 2026-06-18 的旧简报,
  latest_brief() 也没有任何时效检查。
"""
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.llm_brief as lb  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime.now(timezone.utc)


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("LLM_PROVIDER", "GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(lb, "_build_user_prompt", lambda: "today's data")


@pytest.fixture
def ledger(monkeypatch, tmp_path):
    """隔离的预算账本 (不写真实 data/llm_ledger.json)。"""
    import src.llm_budget as m
    monkeypatch.setattr(m, "LEDGER_FILE", tmp_path / "ledger.json")
    monkeypatch.delenv("CRYPTO_INTEL_DISABLE_BUDGET", raising=False)
    b = m.LLMBudget()
    monkeypatch.setattr(m, "budget", b)
    return b


@pytest.fixture
def brief_store(monkeypatch, tmp_path):
    """BRIEF_FILE → tmp; DB 行由测试自己给 (query_df 打桩), upsert_raw 记下写入的行。"""
    import src.db as db
    monkeypatch.setattr(lb, "BRIEF_FILE", tmp_path / "meta" / "ai_brief_latest.json")
    state = {"db_rows": [], "upserted": []}

    def fake_query_df(sql, params=()):
        rows = state["db_rows"]
        return pd.DataFrame(rows, columns=["ts", "asset_id", "value_text"])

    monkeypatch.setattr(lb, "query_df", fake_query_df)
    monkeypatch.setattr(db, "upsert_raw", lambda rows: state["upserted"].extend(rows) or len(rows))
    return state


# ────────────────────────────────────────────────────────────────────
#  C04: Gemini key 不进 URL, 错误文案脱敏
# ────────────────────────────────────────────────────────────────────

def test_gemini_key_sent_in_header_not_url(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "FAKEKEY1234567890")
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "简报正文"}]}}],
                    "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 22}}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, headers=headers or {})
        return Resp()

    monkeypatch.setattr(lb.httpx, "post", fake_post)
    text, usage = lb._call_gemini("sys", "user", 100)
    assert text == "简报正文" and usage["input_tokens"] == 11
    assert "key=" not in seen["url"] and "FAKEKEY" not in seen["url"]
    assert seen["headers"].get("x-goog-api-key") == "FAKEKEY1234567890"


def test_generate_brief_error_redacts_key(no_keys, ledger, monkeypatch):
    """即便底层异常文案带了 ?key=..., generate_brief 返回的 error 也不能含 key。"""
    import httpx
    monkeypatch.setenv("GEMINI_API_KEY", "SECRETKEY987654321")
    req = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/m:generateContent"
                                "?key=SECRETKEY987654321")
    resp = httpx.Response(429, request=req)

    def boom(system, user, max_tokens):
        raise httpx.HTTPStatusError(f"Client error '429' for url '{req.url}' (SECRETKEY987654321)",
                                    request=req, response=resp)

    monkeypatch.setattr(lb, "_call_gemini", boom)
    r = lb.generate_brief()
    assert r["ok"] is False
    assert "SECRETKEY987654321" not in r["error"]
    assert "key=REDACTED" in r["error"] and "429" in r["error"]


def test_redact_secrets_handles_plain_text(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert lb._redact_secrets("ok?a=1&key=abc123&b=2") == "ok?a=1&key=REDACTED&b=2"
    assert lb._redact_secrets("") == ""


# ────────────────────────────────────────────────────────────────────
#  C44: 每次 provider 调用都记账, 用 provider 返回的真实 token 数
# ────────────────────────────────────────────────────────────────────

def test_generate_brief_records_provider_tokens(no_keys, ledger, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k" * 12)
    monkeypatch.setattr(lb, "_call_gemini", lambda s, u, m: (
        "## 🎯 一句话总结\n正文", {"input_tokens": 2287, "output_tokens": 1451,
                                "model": "gemini-2.5-flash", "provider": "gemini"}))
    r = lb.generate_brief()
    assert r["ok"] is True
    rep = ledger.report()["today"]
    assert rep["calls"] == 1 and rep["tokens_in"] == 2287 and rep["tokens_out"] == 1451
    assert rep["failures"] == 0


def test_generate_brief_records_failures(no_keys, ledger, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "g" * 12)

    def boom(s, u, m):
        raise RuntimeError("503 overloaded")

    monkeypatch.setattr(lb, "_call_groq", boom)
    assert lb.generate_brief()["ok"] is False
    monkeypatch.setattr(lb, "_call_groq", lambda s, u, m: ("   ", {"input_tokens": 50, "output_tokens": 0}))
    assert lb.generate_brief()["ok"] is False
    rep = ledger.report()
    assert rep["today"]["calls"] == 2 and rep["today"]["failures"] == 2
    assert rep["today"]["tokens_in"] == 50          # 第一次失败没有 usage, 不算 token
    assert rep["circuit"]["consecutive_failures"] == 2


def test_generate_brief_budget_disabled_env(no_keys, ledger, monkeypatch):
    monkeypatch.setenv("CRYPTO_INTEL_DISABLE_BUDGET", "1")
    monkeypatch.setenv("GEMINI_API_KEY", "k" * 12)
    monkeypatch.setattr(lb, "_call_gemini", lambda s, u, m: ("正文", {"input_tokens": 5, "output_tokens": 5}))
    assert lb.generate_brief()["ok"] is True
    assert ledger.report()["today"]["calls"] == 0


# ────────────────────────────────────────────────────────────────────
#  C01/C22/C45: 简报持久化到 data/meta/ai_brief_latest.json + 时效门槛 + provider
# ────────────────────────────────────────────────────────────────────

def test_save_brief_writes_tracked_file_and_db_row(brief_store):
    lb.save_brief({"ok": True, "markdown": "# 今日简报",
                   "usage": {"provider": "gemini", "model": "gemini-2.5-flash", "output_tokens": None}})
    rec = json.loads(lb.BRIEF_FILE.read_text(encoding="utf-8"))
    assert rec["provider"] == "gemini" and rec["model"] == "gemini-2.5-flash"
    assert rec["markdown"] == "# 今日简报" and lb._parse_brief_ts(rec["ts"]) is not None
    assert not list(lb.BRIEF_FILE.parent.glob("*.tmp"))          # 原子写, 不留临时文件
    row = brief_store["upserted"][0]
    assert row["metric"] == "claude_opus_brief" and row["asset_id"] == "llm_brief:gemini"
    assert row["ts"] == rec["ts"] and row["value"] == 0.0


def test_save_brief_ignores_failed_brief(brief_store):
    lb.save_brief({"ok": False, "markdown": "", "error": "x"})
    assert not lb.BRIEF_FILE.exists() and brief_store["upserted"] == []


def test_latest_brief_rejects_stale_db_row(brief_store):
    """唯一的简报是 4 个月前的 DB 行 → 默认 (36h 门槛) 返回 None, 关掉门槛才返回它。"""
    brief_store["db_rows"] = [("2026-06-18T03:02:34Z", "llm_brief", "极度恐慌 + 机构悄悄接货")]
    assert lb.latest_brief() is None
    old = lb.latest_brief(max_age_hours=None)
    assert old["markdown"].startswith("极度恐慌") and old["provider"] is None


def test_latest_brief_prefers_newer_file_over_old_db_row(brief_store):
    brief_store["db_rows"] = [("2026-06-18T03:02:34Z", "llm_brief", "六月的旧简报")]
    lb.BRIEF_FILE.parent.mkdir(parents=True)
    fresh_ts = _iso(NOW - timedelta(hours=10))
    lb.BRIEF_FILE.write_text(json.dumps({"ts": fresh_ts, "provider": "gemini", "model": "gemini-2.5-flash",
                                         "markdown": "昨晚的 Gemini 简报"}), encoding="utf-8")
    b = lb.latest_brief()
    assert b["markdown"] == "昨晚的 Gemini 简报" and b["provider"] == "gemini" and b["ts"] == fresh_ts


def test_latest_brief_prefers_newer_db_row(brief_store):
    lb.BRIEF_FILE.parent.mkdir(parents=True)
    lb.BRIEF_FILE.write_text(json.dumps({"ts": _iso(NOW - timedelta(hours=30)), "provider": "gemini",
                                         "markdown": "较旧的文件版"}), encoding="utf-8")
    brief_store["db_rows"] = [(_iso(NOW - timedelta(hours=2)), "llm_brief:claude-cli", "今早本地 Claude 版")]
    b = lb.latest_brief()
    assert b["markdown"] == "今早本地 Claude 版" and b["provider"] == "claude-cli"


def test_latest_brief_file_only_and_corrupt_file(brief_store):
    lb.BRIEF_FILE.parent.mkdir(parents=True)
    lb.BRIEF_FILE.write_text("{not json", encoding="utf-8")
    assert lb.latest_brief(max_age_hours=None) is None
    lb.BRIEF_FILE.write_text(json.dumps({"ts": _iso(NOW - timedelta(hours=40)), "provider": "groq",
                                         "markdown": "40h 前"}), encoding="utf-8")
    assert lb.latest_brief() is None                                  # 超过 36h
    assert lb.latest_brief(max_age_hours=48)["provider"] == "groq"


def test_latest_brief_survives_db_error(brief_store, monkeypatch):
    def boom(sql, params=()):
        raise RuntimeError("no such table: raw_metrics")

    monkeypatch.setattr(lb, "query_df", boom)
    lb.BRIEF_FILE.parent.mkdir(parents=True)
    lb.BRIEF_FILE.write_text(json.dumps({"ts": _iso(NOW), "provider": "gemini", "markdown": "x"}),
                             encoding="utf-8")
    assert lb.latest_brief()["markdown"] == "x"


def test_local_brief_saves_claude_cli_provider(brief_store, monkeypatch):
    import src.llm_brief_local as lbl
    monkeypatch.setattr(lbl, "save_brief", lb.save_brief)
    monkeypatch.setattr(lbl, "generate_brief_via_cli", lambda: {
        "ok": True, "markdown": "m" * 400, "model": "claude-cli-subscription", "error": None,
        "usage": {"output_chars": 400}})
    assert lbl.run_local_brief()["ok"] is True
    rec = json.loads(lb.BRIEF_FILE.read_text(encoding="utf-8"))
    assert rec["provider"] == "claude-cli"


# ────────────────────────────────────────────────────────────────────
#  C11: Claude CLI 认证错误打在 stdout, 错误信息要带上
# ────────────────────────────────────────────────────────────────────

def test_local_cli_error_includes_stdout_when_stderr_empty(monkeypatch):
    import subprocess
    import src.llm_brief_local as lbl
    monkeypatch.setattr(lbl, "_build_user_prompt", lambda: "data")

    class R:
        returncode = 1
        stderr = ""
        stdout = 'API Error: 401 {"error":{"message":"OAuth access token has expired"}}'

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    r = lbl.generate_brief_via_cli(claude_path="/fake/claude")
    assert r["ok"] is False
    assert "OAuth access token has expired" in r["error"] and "rc=1" in r["error"]
