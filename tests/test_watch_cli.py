"""`python -m src.cli watchdog check` 的输出与退出码 (离线, 不碰 data/).

回归 (2026-10 审计): 清算 / ETF / funding 检测器长期拿不到数据, CLI 却每轮打印
"✓ 当前无告警触发" + 写死的阈值行 (还列着云端根本没跑的 funding);
飞书推送失败也 exit 0, Actions 一片绿。
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.cli as cli  # noqa: E402
from src.research import watchdog as wd  # noqa: E402
from src.research.watchdog import Alert  # noqa: E402


@pytest.fixture
def wd_env(monkeypatch, tmp_path):
    """main() 开头的 init_db 换成 no-op; 去重 state / alerts.log 写 tmp."""
    monkeypatch.setattr(cli, "init_db", lambda: None)
    monkeypatch.setattr(wd, "STATE_FILE", tmp_path / "watchdog_state.json")
    monkeypatch.setattr(wd, "ALERT_LOG", tmp_path / "alerts.log")
    monkeypatch.setattr(wd, "_push_alert_to_feishu", lambda a: "sent")
    return monkeypatch


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["src.cli", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return None


def _ok_checks(monkeypatch):
    monkeypatch.setattr(wd, "CHECKS", [
        ("peg", lambda: None),
        ("funding", lambda: None),
        ("fear_greed", lambda: None),
        ("dex_tvl", lambda: None),
        ("liquidations", lambda: wd.NoData("无 26h 内 coinglass 数据")),
        ("etf_outflow", lambda: wd.NoData("无近期 farside 数据")),
        ("pipeline_stale", lambda: None),
    ])


def test_check_prints_coverage_and_skipped(wd_env, capsys):
    _ok_checks(wd_env)
    assert _run(wd_env, ["watchdog", "check"]) is None
    out = capsys.readouterr().out
    assert "当前无告警触发" in out
    assert "5/7 个检测器有数据; 跳过(无数据): liquidations, etf_outflow" in out
    # 阈值行只列有数据的检测器
    assert "funding>0.1%" in out
    assert "清算" not in out and "ETF" not in out


def test_check_all_data_no_skipped_line(wd_env, capsys):
    wd_env.setattr(wd, "CHECKS", [("peg", lambda: None), ("dex_tvl", lambda: None)])
    assert _run(wd_env, ["watchdog", "check"]) is None
    out = capsys.readouterr().out
    assert "2/2 个检测器有数据" in out
    assert "跳过" not in out


def test_check_push_failure_exits_1(wd_env, capsys):
    wd_env.setattr(wd, "CHECKS", [("peg", lambda: Alert(
        type="peg_deviation", severity="critical", title="稳定币脱锚 120bps",
        detail="d", value=120, threshold=50))])
    wd_env.setattr(wd, "_push_alert_to_feishu", lambda a: "failed")
    assert _run(wd_env, ["watchdog", "check"]) == 1
    out = capsys.readouterr().out
    assert "推送失败" in out and "peg_deviation" in out
    # 失败的告警没记去重
    assert "peg_deviation" not in wd._load_state().get("last_alerts", {})


def test_check_no_push_alert_exits_0(wd_env, capsys):
    wd_env.setattr(wd, "CHECKS", [("peg", lambda: Alert(
        type="peg_deviation", severity="warning", title="稳定币脱锚 60bps",
        detail="d", value=60, threshold=50))])
    assert _run(wd_env, ["watchdog", "check", "--no-push"]) is None
    assert "稳定币脱锚 60bps" in capsys.readouterr().out
