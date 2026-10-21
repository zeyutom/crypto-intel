"""scripts/sentinel.py: 漏跑/空跑哨兵 + refresh/watchdog 失败告警。

背景 (2026-10 云端盘点): GitHub cron 会延迟/丢弃 (nightly 两次滑过 UTC 午夜, watchdog 约 2/3 被丢),
09-29 nightly job 绿却什么都没提交; 所有告警都在 run 内部, "根本没跑" 没人知道。
"""
import datetime as dt
import importlib.util
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sentinel", ROOT / "scripts" / "sentinel.py")
sentinel = importlib.util.module_from_spec(_spec)
sys.modules["sentinel"] = sentinel   # dataclass 要能从 sys.modules 找到所在模块
_spec.loader.exec_module(sentinel)

UTC = dt.timezone.utc
NIGHTLY = next(w for w in sentinel.WATCHES if w.name == "nightly")
REFRESH = next(w for w in sentinel.WATCHES if w.name == "refresh")
_HOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/f00dbabe-1234-5678-9abc-def012345678"


def _t(s):
    return dt.datetime.fromisoformat(s).replace(tzinfo=UTC)


def _run(created, status="completed", conclusion="success", event="schedule"):
    return {"created_at": created, "status": status, "conclusion": conclusion,
            "event": event, "html_url": "https://github.com/x/y/actions/runs/1"}


# ── 时间窗 ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("now, watch, want", [
    ("2026-10-21T23:25", NIGHTLY, "2026-10-21T16:17"),   # 当晚 cron + 7h 已过
    ("2026-10-21T22:00", NIGHTLY, "2026-10-20T16:17"),   # 宽限未过 → 看前一晚
    ("2026-10-22T02:10", NIGHTLY, "2026-10-21T16:17"),   # 哨兵自己被延迟到次日: 仍看前一晚
    ("2026-10-21T09:45", REFRESH, "2026-10-21T00:30"),
    ("2026-10-21T09:00", REFRESH, "2026-10-20T00:30"),
])
def test_expected_since(now, watch, want):
    assert sentinel.expected_since(_t(now), watch.cron_utc, watch.grace_h) == _t(want)


# ── 判断 ──────────────────────────────────────────────────────────────────────

SINCE = _t("2026-10-21T16:17")


def test_ok_when_artifact_committed():
    action, _ = sentinel.decide(NIGHTLY, SINCE, [], ["🌙 nightly 2026-10-22 — auto-run results (retry)"])
    assert action == "ok"   # 滑过午夜 → 日期是次日, 仍算这一轮的产物


def test_wait_while_run_in_progress():
    runs = [_run("2026-10-21T22:50:00Z", status="in_progress", conclusion=None)]
    assert sentinel.decide(NIGHTLY, SINCE, runs, [])[0] == "wait"


def test_dispatch_when_cron_dropped():
    old = [_run("2026-10-20T21:59:07Z")]   # 前一晚的 run 不算
    action, why = sentinel.decide(NIGHTLY, SINCE, old, ["🔄 Daily data refresh 2026-10-22_05:08"])
    assert action == "dispatch" and "没有触发" in why


def test_dispatch_on_empty_green_run():
    """09-29: job 绿 (nightly_run.sh 总是 exit 0) 却没有任何提交。"""
    action, why = sentinel.decide(NIGHTLY, SINCE, [_run("2026-10-21T19:40:00Z")], [])
    assert action == "dispatch" and "空跑" in why


def test_alert_instead_of_looping_after_redispatch():
    runs = [_run("2026-10-21T19:40:00Z"),
            _run("2026-10-21T23:26:00Z", conclusion="failure", event="workflow_dispatch")]
    action, why = sentinel.decide(NIGHTLY, SINCE, runs, [])
    assert action == "alert" and "failure" in why


def test_other_workflow_commits_do_not_count():
    assert sentinel.decide(REFRESH, _t("2026-10-21T00:30"), [], ["🌙 nightly 2026-10-21"])[0] == "dispatch"


def test_watchdog_gap():
    now = _t("2026-10-21T14:35")
    assert sentinel.watchdog_needs_run([], now)
    assert sentinel.watchdog_needs_run([_run("2026-10-21T09:50:00Z")], now)
    assert not sentinel.watchdog_needs_run([_run("2026-10-21T12:00:00Z")], now)
    assert not sentinel.watchdog_needs_run(
        [_run("2026-10-21T08:00:00Z"), _run("2026-10-21T14:30:00Z", status="queued", conclusion=None)], now)


# ── check 全流程 (假 GitHub API) ───────────────────────────────────────────────

@pytest.fixture
def fake_github(monkeypatch):
    state = {"runs": {}, "commits": [], "dispatched": [], "feishu": []}

    def fake_gh(method, path, body=None):
        if method == "POST":
            state["dispatched"].append(re.search(r"workflows/([^/]+)/dispatches", path).group(1))
            return None
        if "/actions/workflows/" in path:
            wf = re.search(r"workflows/([^/]+)/runs", path).group(1)
            return {"workflow_runs": state["runs"].get(wf, [])}
        if "/commits?" in path:
            return [{"commit": {"message": m}} for m in state["commits"]]
        raise AssertionError(path)

    monkeypatch.setattr(sentinel, "_gh", fake_gh)
    monkeypatch.setattr(sentinel, "feishu_text", lambda title, lines: state["feishu"].append(title) or True)
    return state


def test_check_redispatches_missed_nightly(fake_github):
    fake_github["runs"] = {"watchdog.yml": [_run("2026-10-21T22:00:00Z")]}
    fake_github["commits"] = ["🔄 Daily data refresh 2026-10-21_05:08"]
    rc = sentinel.cmd_check("o/r", _t("2026-10-21T23:25"), dry_run=False)
    assert rc == 0
    assert fake_github["dispatched"] == ["nightly.yml"]
    assert len(fake_github["feishu"]) == 1 and "nightly" in fake_github["feishu"][0]


def test_check_dry_run_touches_nothing(fake_github):
    rc = sentinel.cmd_check("o/r", _t("2026-10-21T23:25"), dry_run=True)
    assert rc == 0 and fake_github["dispatched"] == [] and fake_github["feishu"] == []


def test_check_exit_1_when_redispatch_also_empty(fake_github):
    fake_github["runs"] = {
        "nightly.yml": [_run("2026-10-21T23:26:00Z", conclusion="failure", event="workflow_dispatch")],
        "watchdog.yml": [_run("2026-10-22T09:00:00Z")],
    }
    fake_github["commits"] = ["🔄 Daily data refresh 2026-10-22_05:08"]
    rc = sentinel.cmd_check("o/r", _t("2026-10-22T09:45"), dry_run=False)
    assert rc == 1 and fake_github["dispatched"] == []
    assert any("补跑后仍无产物" in t for t in fake_github["feishu"])


# ── 飞书 ──────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return json.dumps(self.body).encode()


def test_feishu_text_signs_and_checks_code(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout):
        sent.update(json.loads(req.data))
        return _Resp({"code": 0})

    monkeypatch.setenv("FEISHU_GROUP_1_URL", _HOOK)
    monkeypatch.setenv("FEISHU_GROUP_1_SECRET", "s3cret")
    monkeypatch.setattr(sentinel.urllib.request, "urlopen", fake_urlopen)
    assert sentinel.feishu_text("🚨 Crypto Intel 测试", ["a", "", "b"])
    assert sent["content"]["text"] == "🚨 Crypto Intel 测试\na\nb"
    assert sent["timestamp"] and sent["sign"]


def test_feishu_text_never_prints_webhook(monkeypatch, capsys):
    def boom(req, timeout):
        raise OSError(f"connection refused: {_HOOK}")

    monkeypatch.setenv("FEISHU_GROUP_1_URL", _HOOK)
    monkeypatch.setattr(sentinel.urllib.request, "urlopen", boom)
    assert sentinel.feishu_text("t", ["x"]) is False
    assert "f00dbabe" not in capsys.readouterr().out   # Actions 日志是公开的


def test_feishu_text_without_webhook_just_prints(monkeypatch, capsys):
    monkeypatch.delenv("FEISHU_GROUP_1_URL", raising=False)
    assert sentinel.feishu_text("t", ["x"]) is False
    assert "未配置" in capsys.readouterr().out


# ── workflow 配置不漂移 ────────────────────────────────────────────────────────

def _wf(name):
    import yaml
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _on(wf):
    return wf.get("on", wf.get(True))   # PyYAML 把裸 on: 解析成 True


@pytest.mark.parametrize("watch", sentinel.WATCHES, ids=lambda w: w.name)
def test_watch_cron_matches_workflow(watch):
    """改了 nightly/refresh 的 cron 却忘了改哨兵 → 哨兵会在错误的时间窗里找产物。"""
    crons = [c["cron"].split() for c in _on(_wf(watch.workflow))["schedule"]]
    assert [int(crons[0][1]), int(crons[0][0])] == list(watch.cron_utc)


def test_sentinel_slots_come_after_grace():
    slots = [c["cron"].split() for c in _on(_wf("sentinel.yml"))["schedule"]]
    minutes = {int(h) * 60 + int(m) for m, h, *_ in slots}
    for w in sentinel.WATCHES:
        due = (w.cron_utc[0] * 60 + w.cron_utc[1] + int(w.grace_h * 60)) % 1440
        assert any(0 <= (s - due) % 1440 <= 60 for s in minutes), f"{w.name} 宽限期后 1h 内没有哨兵时段"


@pytest.mark.parametrize("name, job", [("refresh.yml", "refresh"), ("watchdog.yml", "watchdog")])
def test_failure_alert_step_present(name, job):
    steps = _wf(name)["jobs"][job]["steps"]
    step = next(s for s in steps if s.get("name") == "Alert on job failure or cancel")
    assert step["if"] == "failure() || cancelled()"
    assert "scripts/sentinel.py alert" in step["run"]
    assert steps[-1] is step   # 放最后: 前面任一步失败都能走到
