"""Unit tests for src.research.watchdog — 阈值 + 去重 + alert 结构."""
import sys
import pathlib
import json
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import watchdog as wd
from src.research.watchdog import Alert


@pytest.fixture
def fresh_state(monkeypatch, tmp_path):
    """每个 test 一个隔离的 state 文件."""
    state_file = tmp_path / "watchdog_state.json"
    alert_log = tmp_path / "alerts.log"
    monkeypatch.setattr(wd, "STATE_FILE", state_file)
    monkeypatch.setattr(wd, "ALERT_LOG", alert_log)
    return state_file


# ────────────────────────────────────────────────────────────────────
#  Alert dataclass
# ────────────────────────────────────────────────────────────────────

def test_alert_auto_timestamps():
    a = Alert(type="peg", severity="warning", title="X", detail="Y",
              value=50, threshold=40)
    assert a.ts.endswith("Z")
    # 应是 ISO 格式
    assert "T" in a.ts


def test_alert_explicit_ts_preserved():
    a = Alert(type="peg", severity="warning", title="X", detail="Y",
              value=50, threshold=40, ts="2026-01-01T00:00:00Z")
    assert a.ts == "2026-01-01T00:00:00Z"


# ────────────────────────────────────────────────────────────────────
#  去重逻辑
# ────────────────────────────────────────────────────────────────────

def test_dedup_within_window(fresh_state):
    state = wd._load_state()
    # 模拟刚发过 peg 告警
    state["last_alerts"] = {"peg_deviation": datetime.now(timezone.utc).isoformat()}
    assert wd._is_deduped("peg_deviation", state) is True


def test_dedup_expires_after_window(fresh_state, monkeypatch):
    state = wd._load_state()
    # 25 小时前发的, 默认窗口 24h → 不再去重
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    state["last_alerts"] = {"peg_deviation": old}
    assert wd._is_deduped("peg_deviation", state) is False


def test_dedup_new_type_not_deduped(fresh_state):
    state = wd._load_state()
    state["last_alerts"] = {"peg_deviation": datetime.now(timezone.utc).isoformat()}
    # 不同类型不去重
    assert wd._is_deduped("funding_extreme", state) is False


def test_mark_alerted_persists(fresh_state):
    state = wd._load_state()
    a = Alert(type="peg_deviation", severity="warning", title="X", detail="Y",
              value=80, threshold=50)
    wd._mark_alerted(a, state)
    assert "peg_deviation" in state["last_alerts"]
    assert len(state["all_alerts"]) == 1


# ────────────────────────────────────────────────────────────────────
#  阈值配置
# ────────────────────────────────────────────────────────────────────

def test_default_thresholds_loaded():
    """阈值字典应该有所有 5 类."""
    expected_keys = {
        "stable_peg_bps", "funding_pct", "liquidations_usd_24h",
        "etf_outflow_usd", "dex_tvl_drop_pct",
        "fg_extreme_high", "fg_extreme_low",
    }
    assert set(wd.THRESHOLDS.keys()) >= expected_keys


def test_thresholds_have_reasonable_values():
    """阈值不能是 0 / 负数."""
    for k, v in wd.THRESHOLDS.items():
        assert v > 0, f"{k} = {v} not positive"


# ────────────────────────────────────────────────────────────────────
#  Reset state
# ────────────────────────────────────────────────────────────────────

def test_reset_state_clears_history(fresh_state):
    # 先写一些 state
    a = Alert(type="peg_deviation", severity="warning", title="X", detail="Y",
              value=80, threshold=50)
    state = wd._load_state()
    wd._mark_alerted(a, state)
    wd._save_state(state)
    assert fresh_state.exists()

    wd.reset_state()
    new_state = wd._load_state()
    assert new_state.get("last_alerts") == {}
    assert new_state.get("all_alerts") == []


# ────────────────────────────────────────────────────────────────────
#  Checks 注册表
# ────────────────────────────────────────────────────────────────────

def test_checks_list_is_callable():
    assert len(wd.CHECKS) >= 4
    for name, fn in wd.CHECKS:
        assert callable(fn), f"{name} is not callable"


def test_run_check_returns_list(fresh_state):
    """无 push, 无 dedup 模式下 run_check 应返回 list (即便为空)."""
    alerts = wd.run_check(push=False, dedup=False)
    assert isinstance(alerts, list)


# ────────────────────────────────────────────────────────────────────
#  History
# ────────────────────────────────────────────────────────────────────

def test_history_returns_recent(fresh_state):
    state = wd._load_state()
    for i in range(5):
        a = Alert(type=f"test_{i}", severity="info", title=f"T{i}",
                  detail="d", value=1, threshold=0)
        wd._mark_alerted(a, state)
    wd._save_state(state)
    hist = wd.history(3)
    assert len(hist) == 3
    # 应该是最近的 3 条
    assert hist[-1]["type"] == "test_4"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
