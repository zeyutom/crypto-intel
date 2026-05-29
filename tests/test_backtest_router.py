"""Unit tests for src.research.backtest_router — facade 路由层.

不真跑回测 (要快照 + 时间), 只测路由 + normalize 行为.
"""
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import backtest_router as br


def test_detect_engine_returns_valid():
    eng = br.detect_engine()
    assert eng in ("vbt", "legacy")


def test_health_structure():
    h = br.health()
    assert "available_engines" in h
    assert "auto_chosen" in h
    assert h["auto_chosen"] in ("vbt", "legacy")
    assert h["available_engines"]["legacy"] is True   # legacy 总在


def test_is_available_always_true():
    assert br.is_available() is True


# ────────────────────────────────────────────────────────────────────
#  _normalize 字段映射
# ────────────────────────────────────────────────────────────────────

def test_normalize_legacy_to_unified():
    legacy_result = {
        "ok": True,
        "total_return": 0.05,
        "annual_return": 0.20,
        "sharpe": 1.5,
        "max_drawdown": 0.10,
        "calmar": 2.0,
        "win_rate": 0.55,
        "vs_btc_excess": 0.03,
        "n_snapshots": 99,
        "n_rebalances": 13,            # legacy 用 n_rebalances
        "final_capital": 10500,
        "date_range": "2026-02-18 ~ 2026-05-18",
        "top_n": 10,
        "rebalance_days": 7,
    }
    out = br._normalize(legacy_result, "legacy")
    assert out["engine"] == "legacy"
    assert out["sharpe"] == 1.5
    assert out["n_periods"] == 13      # mapped from n_rebalances
    assert out["start_date"] == "2026-02-18"
    assert out["end_date"] == "2026-05-18"
    assert out["final_capital"] == 10500


def test_normalize_vbt_to_unified():
    vbt_result = {
        "ok": True,
        "engine": "vectorbt",
        "total_return": 0.03,
        "annual_return": 0.12,
        "sharpe": 0.9,
        "max_drawdown": 0.08,
        "calmar": 1.5,
        "win_rate": 0.5,
        "vs_btc_excess": 0.01,
        "n_snapshots": 99,
        "n_periods": 13,
        "final_equity": 10300,         # vbt 用 final_equity
        "start_date": "2026-02-18",
        "end_date": "2026-05-18",
        "top_n": 10,
        "rebalance_days": 7,
    }
    out = br._normalize(vbt_result, "vectorbt")
    assert out["engine"] == "vectorbt"   # router 选的引擎名优先, 不被 result 覆盖
    assert out["final_capital"] == 10300  # mapped from final_equity


def test_normalize_engine_not_overwritten_by_result():
    """关键: router 传入的 engine 不应被 result 字典的 engine 字段覆盖."""
    result = {"engine": "WRONG", "ok": True, "total_return": 0.1}
    out = br._normalize(result, "expected_engine")
    assert out["engine"] == "expected_engine"


def test_normalize_missing_fields_become_none():
    out = br._normalize({"ok": True}, "legacy")
    assert out["engine"] == "legacy"
    assert out["sharpe"] is None
    assert out["max_drawdown"] is None


def test_normalize_passes_through_extras():
    result = {"ok": True, "equity_curve": [{"date": "x", "equity": 1}],
              "overfit": {"verdict": "robust"}}
    out = br._normalize(result, "vbt")
    assert "equity_curve" in out
    assert "overfit" in out


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
