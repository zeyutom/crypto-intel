"""Unit tests for src.llm_budget — token 预算 + 熔断器."""
import sys
import pathlib
import os
import json
import tempfile

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


@pytest.fixture
def fresh_budget(monkeypatch, tmp_path):
    """每个 test 一个隔离的 ledger 文件."""
    import src.llm_budget as m
    ledger_path = tmp_path / "test_ledger.json"
    monkeypatch.setattr(m, "LEDGER_FILE", ledger_path)
    # 重新实例化 (绕过 module-level singleton)
    budget = m.LLMBudget()
    return budget


# ────────────────────────────────────────────────────────────────────
#  Chars → tokens
# ────────────────────────────────────────────────────────────────────

def test_chars_to_tokens_basic(fresh_budget):
    assert fresh_budget.chars_to_tokens(4) == 1
    assert fresh_budget.chars_to_tokens(40) == 10
    assert fresh_budget.chars_to_tokens(1) >= 1  # 最少 1


# ────────────────────────────────────────────────────────────────────
#  Allow / record 基本路径
# ────────────────────────────────────────────────────────────────────

def test_fresh_budget_allows_call(fresh_budget):
    allowed, reason = fresh_budget.allow()
    assert allowed is True
    assert reason == "ok"


def test_record_success_increments_counters(fresh_budget):
    fresh_budget.record_call(prompt_chars=400, response_chars=800, success=True)
    today = fresh_budget._today()
    assert today.calls == 1
    assert today.tokens_in == 100   # 400/4
    assert today.tokens_out == 200  # 800/4
    assert today.failures == 0


def test_record_failure_increments_failures(fresh_budget):
    fresh_budget.record_call(prompt_chars=400, response_chars=0, success=False)
    assert fresh_budget._today().failures == 1


# ────────────────────────────────────────────────────────────────────
#  Daily budget cap
# ────────────────────────────────────────────────────────────────────

def test_daily_budget_blocks_at_cap(fresh_budget, monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_DAY", "100")
    # 用掉 80 tokens
    fresh_budget.record_call(prompt_chars=320, response_chars=0, success=True)
    # 再请求 50 tokens 应被拒
    allowed, reason = fresh_budget.allow(est_input_tokens=50)
    assert allowed is False
    assert "daily budget" in reason


def test_daily_budget_allows_under_cap(fresh_budget, monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_DAY", "1000")
    fresh_budget.record_call(prompt_chars=400, response_chars=0, success=True)
    allowed, reason = fresh_budget.allow(est_input_tokens=50)
    assert allowed is True


# ────────────────────────────────────────────────────────────────────
#  Circuit breaker
# ────────────────────────────────────────────────────────────────────

def test_circuit_opens_after_consecutive_failures(fresh_budget, monkeypatch):
    monkeypatch.setenv("MAX_FAILS_BEFORE_BREAK", "3")
    # 3 次失败
    for _ in range(3):
        fresh_budget.record_call(0, 0, success=False)
    # 现在熔断应该打开
    assert fresh_budget._ledger.circuit.is_open()
    allowed, reason = fresh_budget.allow()
    assert allowed is False
    assert "circuit" in reason.lower()


def test_circuit_resets_on_success(fresh_budget, monkeypatch):
    monkeypatch.setenv("MAX_FAILS_BEFORE_BREAK", "3")
    fresh_budget.record_call(0, 0, success=False)
    fresh_budget.record_call(0, 0, success=False)
    # 第 3 次成功 → 失败计数清零
    fresh_budget.record_call(100, 100, success=True)
    assert fresh_budget._ledger.circuit.consecutive_failures == 0
    assert not fresh_budget._ledger.circuit.is_open()


def test_circuit_manual_reset(fresh_budget, monkeypatch):
    monkeypatch.setenv("MAX_FAILS_BEFORE_BREAK", "2")
    fresh_budget.record_call(0, 0, success=False)
    fresh_budget.record_call(0, 0, success=False)
    assert fresh_budget._ledger.circuit.is_open()
    fresh_budget.reset_circuit()
    assert not fresh_budget._ledger.circuit.is_open()


# ────────────────────────────────────────────────────────────────────
#  Report
# ────────────────────────────────────────────────────────────────────

def test_report_structure(fresh_budget):
    fresh_budget.record_call(400, 800, success=True)
    r = fresh_budget.report()
    assert "today" in r
    assert "budgets" in r
    assert "circuit" in r
    assert "recent_7d" in r
    assert r["today"]["total"] == 300  # 100 + 200
    assert r["today"]["calls"] == 1


def test_report_budget_pct(fresh_budget, monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_DAY", "1000")
    # 400 chars in + 0 chars out: chars_to_tokens(0)=max(1,0)=1 → 100+1=101 tokens
    fresh_budget.record_call(400, 0, success=True)
    r = fresh_budget.report()
    assert 9.5 <= r["today"]["budget_pct"] <= 10.5  # 容忍 chars_to_tokens 边界


# ────────────────────────────────────────────────────────────────────
#  Ledger 持久化
# ────────────────────────────────────────────────────────────────────

def test_ledger_persists_across_instances(monkeypatch, tmp_path):
    """新实例应能读上一个实例存的 ledger."""
    import src.llm_budget as m
    ledger_path = tmp_path / "shared_ledger.json"
    monkeypatch.setattr(m, "LEDGER_FILE", ledger_path)

    b1 = m.LLMBudget()
    b1.record_call(400, 800, success=True)
    # 新实例
    b2 = m.LLMBudget()
    today = b2._today()
    assert today.total_tokens == 300


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
