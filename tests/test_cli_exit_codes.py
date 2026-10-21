"""流水线诚实性测试: CLI 失败必须非零退出 + nightly 脚本/workflow 的结构约束.

回归 (2026-10-10 回测审计):
- `python -m src.cli screen` 打印 "✗ CoinGecko API 无数据" 却 exit 0, run_step 只看退出码
  → 日志写 "✓ screen 完成", failures.json 为空, 当天 meta 快照静默缺失。
- DOW 在第 8 步才算, screen 跨午夜后周日的 weekly-review/scorecard 被跳过。
- nightly.yml 只回推 snapshot_*.json, runner 上学到的 factor_config.json 每晚被丢弃。
- .gitignore 整目录忽略 data/nightly_logs/, 云端失败记录永远进不了仓库。

回归 (2026-10-20 运维审计):
- 本地 daily-screen 停用后云端只跑 screen 不推送, Top-N 静默断推 8 天 → screen --push。
- push-feishu / llm-local / 月度 evolution 命令失败也 exit 0, failures.json 与 launchd 都看不见。
- 云端没有 Claude CLI, 每个周日 weekly-review 必挂并推 🚨 告警; nightly 的 watchdog 无去重状态重复推送。
- nightly.yml: 30min job 超时 < 2h SCREEN_TIMEOUT; 推送失败/冲突回退静默丢产物; 跨 UTC 午夜。
"""
import io
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.cli as cli  # noqa: E402


# ────────────────────────────────────────────────────────────────────
#  公共 fixture: 不碰 DB, 不开浏览器, 不联网
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def quiet_cli(monkeypatch):
    """main() 开头会 init_db(); 测试里换成 no-op。"""
    monkeypatch.setattr(cli, "init_db", lambda: None)
    # screen 成功分支在 Darwin 上会 Popen("open", 报告) → 假装是 Linux
    import platform
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    return cli


def _run(argv):
    """跑 main(); 正常返回 None, 失败抛 SystemExit (返回其 code)。"""
    sys.argv = ["src.cli", *argv]
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return None


# ────────────────────────────────────────────────────────────────────
#  screen
# ────────────────────────────────────────────────────────────────────

def test_screen_ok_false_exits_1(quiet_cli, monkeypatch, capsys):
    import src.research.screener as screener
    monkeypatch.setattr(screener, "run_screen", lambda top_n=30: {"ok": False, "error": "CoinGecko API 无数据"})
    assert _run(["screen"]) == 1
    assert "CoinGecko API 无数据" in capsys.readouterr().out


def test_screen_ok_true_exits_0(quiet_cli, monkeypatch, tmp_path):
    import src.research.screener as screener
    good = {
        "ok": True, "total_screened": 2, "regime": "bull", "anomalies": [],
        "top": [{"symbol": "BTC", "price": 1.0, "market_cap": 1e9, "change_30d": 1.0, "composite_score": 0.5}],
    }
    monkeypatch.setattr(screener, "run_screen", lambda top_n=30: good)
    monkeypatch.setattr(screener, "generate_screen_report", lambda r: tmp_path / "screen.html")
    assert _run(["screen"]) is None


_GOOD_SCREEN = {
    "ok": True, "total_screened": 2, "regime": "bull", "anomalies": [],
    "top": [{"symbol": "BTC", "price": 1.0, "market_cap": 1e9, "change_30d": 1.0, "composite_score": 0.5}],
}


@pytest.fixture
def screen_ok(quiet_cli, monkeypatch, tmp_path):
    """screen 成功 + 记录 run_screen 的 top_n 参数; push_screen_to_feishu 默认会被调用就记下来。"""
    import src.research.screener as screener
    import src.research.returns_tracker as rt
    seen = {"top_n": None, "pushed": []}

    def fake_run_screen(top_n=30):
        seen["top_n"] = top_n
        return dict(_GOOD_SCREEN)

    monkeypatch.setattr(screener, "run_screen", fake_run_screen)
    monkeypatch.setattr(screener, "generate_screen_report", lambda r: tmp_path / "screen.html")
    monkeypatch.setattr(rt, "_load_feishu_groups", lambda: [{"name": "g", "url": "u", "secret": ""}])

    def fake_push(result):
        seen["pushed"].append(result)
        return {"ok": True, "pushed": 1, "total": 1}

    monkeypatch.setattr(rt, "push_screen_to_feishu", fake_push)
    return seen


def test_screen_without_push_flag_does_not_push(screen_ok):
    assert _run(["screen"]) is None
    assert screen_ok["pushed"] == []


def test_screen_push_sends_in_memory_result(screen_ok):
    """--push 推的是内存里的完整结果 (含 anomalies/top), 不是从快照文件重建的。"""
    assert _run(["screen", "--push"]) is None
    assert len(screen_ok["pushed"]) == 1
    assert screen_ok["pushed"][0]["top"][0]["symbol"] == "BTC"


def test_screen_flags_any_order(screen_ok):
    assert _run(["screen", "--push", "--top", "15"]) is None
    assert screen_ok["top_n"] == 15 and len(screen_ok["pushed"]) == 1
    assert _run(["screen", "--top", "12"]) is None
    assert screen_ok["top_n"] == 12 and len(screen_ok["pushed"]) == 1


def test_screen_push_failure_exits_3(screen_ok, monkeypatch, capsys):
    import src.research.returns_tracker as rt
    monkeypatch.setattr(rt, "push_screen_to_feishu", lambda r: {"ok": False, "pushed": 0, "total": 1})
    assert _run(["screen", "--push"]) == 3
    assert "推送失败" in capsys.readouterr().out


def test_screen_push_partial_failure_exits_3(screen_ok, monkeypatch):
    import src.research.returns_tracker as rt
    monkeypatch.setattr(rt, "push_screen_to_feishu", lambda r: {"ok": True, "pushed": 1, "total": 2})
    assert _run(["screen", "--push"]) == 3


def test_screen_push_exception_exits_3(screen_ok, monkeypatch):
    import src.research.returns_tracker as rt

    def boom(r):
        raise KeyError("price")

    monkeypatch.setattr(rt, "push_screen_to_feishu", boom)
    assert _run(["screen", "--push"]) == 3


def test_screen_push_without_groups_exits_0(screen_ok, monkeypatch, capsys):
    """没配飞书群: 打印提示, 正常退出, 不调推送。"""
    import src.research.returns_tracker as rt
    monkeypatch.setattr(rt, "_load_feishu_groups", lambda: [])
    assert _run(["screen", "--push"]) is None
    assert screen_ok["pushed"] == []
    assert "未配置飞书群" in capsys.readouterr().out


def test_screen_failure_with_push_still_exits_1(quiet_cli, monkeypatch):
    import src.research.screener as screener
    import src.research.returns_tracker as rt
    called = []
    monkeypatch.setattr(screener, "run_screen", lambda top_n=30: {"ok": False, "error": "无数据"})
    monkeypatch.setattr(rt, "push_screen_to_feishu", lambda r: called.append(r))
    assert _run(["screen", "--push"]) == 1
    assert called == []


# ────────────────────────────────────────────────────────────────────
#  push-feishu
# ────────────────────────────────────────────────────────────────────

def _patch_push(monkeypatch, result, groups):
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "push_to_feishu", lambda: result)
    monkeypatch.setattr(notifier, "_load_groups", lambda: groups)


def test_push_feishu_failed_push_exits_1(quiet_cli, monkeypatch):
    _patch_push(monkeypatch, {"ok": False, "pushed": 0, "failed": 1,
                              "groups": [{"name": "g", "ok": False, "error": "HTTP 错误"}]},
                [{"name": "g", "url": "u", "secret": ""}])
    assert _run(["push-feishu"]) == 1


def test_push_feishu_ok_exits_0(quiet_cli, monkeypatch):
    _patch_push(monkeypatch, {"ok": True, "pushed": 1, "failed": 0, "groups": []},
                [{"name": "g", "url": "u", "secret": ""}])
    assert _run(["push-feishu"]) is None


def test_push_feishu_no_groups_exits_0(quiet_cli, monkeypatch):
    """没配任何飞书群 (本地/测试) 不算失败。"""
    _patch_push(monkeypatch, {"ok": False, "error": "未配置飞书群", "pushed": 0}, [])
    assert _run(["push-feishu"]) is None


# ────────────────────────────────────────────────────────────────────
#  llm-local / 月度 evolution 命令: 失败非零退出
# ────────────────────────────────────────────────────────────────────

def test_llm_local_ok_false_exits_1(quiet_cli, monkeypatch):
    import src.llm_brief_local as lbl
    monkeypatch.setattr(lbl, "run_local_brief",
                        lambda: {"ok": False, "markdown": "", "error": "Claude CLI rc=1: 401 OAuth expired"})
    assert _run(["llm-local"]) == 1


def test_llm_local_ok_true_exits_0(quiet_cli, monkeypatch):
    import src.llm_brief_local as lbl
    monkeypatch.setattr(lbl, "run_local_brief",
                        lambda: {"ok": True, "markdown": "x" * 200, "usage": {"output_chars": 200}})
    assert _run(["llm-local"]) is None


@pytest.mark.parametrize("cmd,module,func", [
    ("track-narratives", "src.evolution.narrative_tracker", "run_narrative_tracking"),
    ("propose-factors", "src.evolution.factor_proposer", "run_factor_proposal"),
    ("discover-sources", "src.evolution.source_discoverer", "run_source_discovery"),
    ("evolve-prompt", "src.evolution.prompt_evolver", "run_prompt_evolution"),
])
def test_evolution_commands_exit_code(quiet_cli, monkeypatch, cmd, module, func):
    import importlib
    mod = importlib.import_module(module)
    monkeypatch.setattr(mod, func, lambda: {"ok": False, "error": "401 OAuth access token has expired"})
    assert _run([cmd]) == 1
    monkeypatch.setattr(mod, func, lambda: {"ok": True, "file": "x.md", "chars": 10})
    assert _run([cmd]) is None


# ────────────────────────────────────────────────────────────────────
#  weekly-review
# ────────────────────────────────────────────────────────────────────

def test_weekly_review_ok_false_exits_1(quiet_cli, monkeypatch):
    import src.evolution.weekly_review as wr
    monkeypatch.setattr(wr, "run_weekly_review", lambda: {"ok": False, "error": "claude cli missing"})
    assert _run(["weekly-review"]) == 1


def test_weekly_review_ok_true_exits_0(quiet_cli, monkeypatch):
    import src.evolution.weekly_review as wr
    monkeypatch.setattr(wr, "run_weekly_review",
                        lambda: {"ok": True, "file": "x.md", "week": "2026-W41", "chars": 10})
    assert _run(["weekly-review"]) is None


# ────────────────────────────────────────────────────────────────────
#  verify-returns / scorecard
# ────────────────────────────────────────────────────────────────────

def test_verify_returns_ok_false_exits_1(quiet_cli, monkeypatch):
    import src.research.returns_tracker as rt
    monkeypatch.setattr(rt, "verify_returns", lambda lookback_days=7: {"ok": False, "error": "无历史快照"})
    assert _run(["verify-returns", "7"]) == 1


def test_verify_returns_ok_true_exits_0(quiet_cli, monkeypatch):
    import src.research.returns_tracker as rt
    good = {"ok": True, "snapshot_date": "2026-10-03", "matched_coins": 1, "vs_btc": 0.01,
            "stats": {"avg_return": 0.02, "median_return": 0.01, "win_rate": 0.6,
                      "best_coin": "BTC", "best_return": 0.1, "worst_coin": "ETH", "worst_return": -0.1},
            "coins": [{"symbol": "BTC", "old_price": 1.0, "new_price": 1.1, "return_pct": 0.1}]}
    monkeypatch.setattr(rt, "verify_returns", lambda lookback_days=7: good)
    assert _run(["verify-returns"]) is None


def test_scorecard_verify_failed_exits_1(quiet_cli, monkeypatch):
    """底层 verify_returns 失败 (没有 stats) → 非零退出."""
    import src.research.returns_tracker as rt
    monkeypatch.setattr(rt, "push_returns_scorecard",
                        lambda lookback_days=7: {"ok": False, "error": "无历史快照", "pushed": 0})
    assert _run(["scorecard", "7"]) == 1


def test_scorecard_stats_but_not_pushed_exits_0(quiet_cli, monkeypatch):
    """复盘算出来了只是飞书没配 → 不算失败."""
    import src.research.returns_tracker as rt
    monkeypatch.setattr(rt, "push_returns_scorecard",
                        lambda lookback_days=7: {"ok": False, "error": "未配置飞书群", "pushed": 0,
                                                 "stats": {"avg_return": 0.01}})
    assert _run(["scorecard"]) is None


def test_scorecard_ok_exits_0(quiet_cli, monkeypatch):
    import src.research.returns_tracker as rt
    monkeypatch.setattr(rt, "push_returns_scorecard",
                        lambda lookback_days=7: {"ok": True, "pushed": 1, "stats": {"avg_return": 0.01}})
    assert _run(["scorecard"]) is None


# ────────────────────────────────────────────────────────────────────
#  update-weights: 打印 skipped / disabled, 键缺失时照常工作
# ────────────────────────────────────────────────────────────────────

def _patch_meta(monkeypatch, update_result):
    import src.research.meta_learner as ml
    monkeypatch.setattr(ml, "run_ic_backtest",
                        lambda lookback_days=7: {"ok": True, "matched_coins": 50, "factor_ic": {}})
    monkeypatch.setattr(ml, "update_weights_from_ic", lambda ic: update_result)


def test_update_weights_prints_skipped_and_disabled(quiet_cli, monkeypatch, capsys):
    """meta_learner 实际返回: skipped 是原因字符串, disabled 是因子名列表."""
    _patch_meta(monkeypatch, {"ok": True, "factors_updated": 0,
                              "new_weights": {"momentum_30d": 0.6, "tvl_mcap": 0.4},
                              "skipped": "already_updated_for_snapshot",
                              "disabled": ["dev_activity", "volume_turnover"]})
    assert _run(["update-weights", "7"]) is None
    out = capsys.readouterr().out
    assert "跳过" in out and "这个价格快照已调过权重" in out
    assert "已关闭因子" in out and "dev_activity" in out and "volume_turnover" in out


def test_update_weights_explains_not_weight_writer(quiet_cli, monkeypatch, capsys):
    """本地非写入方: 提示由云端调权以及如何覆盖, 且正常退出."""
    _patch_meta(monkeypatch, {"ok": True, "factors_updated": 0,
                              "new_weights": {"momentum_30d": 1.0},
                              "skipped": "not_weight_writer", "disabled": []})
    assert _run(["update-weights"]) is None
    out = capsys.readouterr().out
    assert "不是权重写入方" in out and "CRYPTO_INTEL_WEIGHT_WRITER" in out


def test_update_weights_tolerates_missing_keys(quiet_cli, monkeypatch, capsys):
    """老版 meta_learner 不返回 skipped/disabled 也不能崩."""
    _patch_meta(monkeypatch, {"ok": True, "factors_updated": 0, "new_weights": {"momentum_30d": 1.0}})
    assert _run(["update-weights"]) is None
    out = capsys.readouterr().out
    assert "跳过" not in out and "已关闭" not in out


def test_update_weights_ic_failed_does_not_crash(quiet_cli, monkeypatch):
    import src.research.meta_learner as ml
    monkeypatch.setattr(ml, "run_ic_backtest", lambda lookback_days=7: {"ok": False, "error": "无历史快照"})
    assert _run(["update-weights"]) is None


# ────────────────────────────────────────────────────────────────────
#  nightly_run.sh 结构约束
# ────────────────────────────────────────────────────────────────────

SCRIPT = ROOT / "scripts" / "nightly_run.sh"


def test_nightly_script_syntax():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_nightly_dow_computed_before_first_step():
    """DOW/DATE 必须在第一个 run_step 之前算好 (跨午夜不再漏掉周日复盘)."""
    text = SCRIPT.read_text(encoding="utf-8")
    first_step = text.index("\nrun_step ")
    assert re.search(r"^DOW=\$\(date \+%u\)", text[:first_step], re.M)
    assert re.search(r"^DATE=\$\(date \+%Y-%m-%d\)", text[:first_step], re.M)
    # 第 8 步不再重新算 DOW
    assert "DOW=" not in text[first_step:]


def test_nightly_screen_has_timeout_and_snapshot_check():
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^run_step "screen" run_with_timeout "\$SCREEN_TIMEOUT" python3 -m src.cli screen \$SCREEN_ARGS',
                     text, re.M)
    assert 'record_failure "snapshot-check" 1' in text


def _bash_block(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i) + len(end)]


def test_nightly_screen_push_only_when_feishu_configured(tmp_path):
    """配了飞书 (GROUP_1 或老的 WEBHOOK_URL) 才 screen --push, 否则普通 screen。"""
    text = SCRIPT.read_text(encoding="utf-8")
    block = _bash_block(text, 'SCREEN_ARGS=""', "\nfi\n")
    helper = tmp_path / "s.sh"
    helper.write_text("set -u\n" + block + 'echo "args=[$SCREEN_ARGS]"\n', encoding="utf-8")

    def args(env):
        r = subprocess.run(["bash", str(helper)], capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", **env})
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    assert args({}) == "args=[]"
    assert args({"FEISHU_GROUP_1_URL": "https://x"}) == "args=[--push]"
    assert args({"FEISHU_WEBHOOK_URL": "https://x"}) == "args=[--push]"


def test_nightly_discover_alpha_runs_offline():
    """runner 没有 Claude CLI: 显式 --no-llm, 别每晚先试 LLM 再降级。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^run_step "discover-alpha" python3 -m src.cli discover-alpha --no-llm', text, re.M)


def test_nightly_no_watchdog_step():
    """watchdog.yml 每 2h 跑且有去重状态; nightly 再跑一次只会重复推送。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"^\s*run_step \"watchdog\"", text, re.M)
    assert "src.cli watchdog" not in text


def _run_sunday_block(tmp_path, env):
    text = SCRIPT.read_text(encoding="utf-8")
    block = _bash_block(text, 'if [ "$DOW" = "7" ]; then', "\nfi\n")
    helper = tmp_path / "sun.sh"
    helper.write_text(
        "set -u\nDOW=7\n"
        'log() { echo "LOG $1"; }\n'
        'run_step() { echo "STEP $1"; }\n' + block, encoding="utf-8")
    r = subprocess.run(["bash", str(helper)], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", **env})   # PATH 里没有 claude
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_nightly_weekly_review_skipped_without_cli_or_key(tmp_path):
    out = _run_sunday_block(tmp_path, {})
    assert "STEP weekly-review" not in out and "跳过 weekly-review" in out
    assert "STEP scorecard" in out   # 记分卡照跑


def test_nightly_weekly_review_runs_with_api_key(tmp_path):
    out = _run_sunday_block(tmp_path, {"GEMINI_API_KEY": "k"})
    assert "STEP weekly-review" in out and "STEP scorecard" in out


def test_nightly_failures_file_not_truncated_on_rerun(tmp_path):
    """同一 DATE 第二次 run 不能清空第一次的失败记录; 损坏/不存在才初始化。"""
    text = SCRIPT.read_text(encoding="utf-8")
    block = _bash_block(text, 'FAILS="$LOGDIR/$DATE.failures.json"', "\nfi\n")
    helper = tmp_path / "f.sh"
    helper.write_text("set -u\nLOGDIR=" + str(tmp_path) + "\nDATE=2026-10-13\n" + block
                      + 'echo "start=$FAILS_START"\n', encoding="utf-8")
    fails = tmp_path / "2026-10-13.failures.json"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

    r = subprocess.run(["bash", str(helper)], capture_output=True, text=True, env=env)
    assert "start=0" in r.stdout, r.stdout + r.stderr
    assert json.loads(fails.read_text()) == {"failures": []}

    fails.write_text(json.dumps({"failures": [{"step": "screen", "rc": 1, "ts": "x"}]}))
    r = subprocess.run(["bash", str(helper)], capture_output=True, text=True, env=env)
    assert "start=1" in r.stdout, r.stdout + r.stderr
    assert json.loads(fails.read_text())["failures"][0]["step"] == "screen"

    fails.write_text("{broken")
    r = subprocess.run(["bash", str(helper)], capture_output=True, text=True, env=env)
    assert "start=0" in r.stdout and json.loads(fails.read_text()) == {"failures": []}


def test_nightly_alert_counts_only_this_runs_failures():
    """结尾的失败数/告警只算本次 run 新增的 (同 DATE 前一次 run 的失败已经告警过)。"""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "['failures']) - $FAILS_START)" in text
    assert ".get('failures', [])[$FAILS_START:]" in text


def test_run_with_timeout_returns_124(tmp_path):
    """可移植超时包装: 超时 → 124, 正常 → 透传子进程退出码."""
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^run_with_timeout\(\) \{.*?^\}", text, re.M | re.S)
    assert m, "run_with_timeout 函数不存在"
    helper = tmp_path / "w.sh"
    helper.write_text(m.group(0), encoding="utf-8")
    cmd = (f"source {helper}; run_with_timeout 1 sleep 5; echo rc=$?; "
           f"run_with_timeout 5 sh -c 'exit 3'; echo rc=$?")
    r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=30)
    assert "rc=124" in r.stdout and "rc=3" in r.stdout, r.stdout + r.stderr


# ────────────────────────────────────────────────────────────────────
#  nightly.yml / .gitignore
# ────────────────────────────────────────────────────────────────────

def test_nightly_yml_commits_learned_weights_with_rebase_retry():
    import yaml
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["nightly"]["steps"]
    commit = next(s for s in steps if "Commit data" in s.get("name", ""))
    run = commit["run"]
    for must in ("data/meta/factor_config.json", "data/meta/training_log.json",
                 "data/meta/returns_*_7d.json", "data/meta/factor_report*.json",
                 "data/nightly_logs/*.failures.json",
                 "git pull --rebase --strategy-option=theirs origin main"):
        assert must in run, must
    assert "for i in 1 2 3" in run
    # push 失败要让 job 变红 (触发失败告警), 不能再 continue-on-error
    assert not commit.get("continue-on-error")


def _nightly_wf():
    import yaml
    return yaml.safe_load((ROOT / ".github" / "workflows" / "nightly.yml").read_text(encoding="utf-8"))


def test_nightly_yml_commit_step_restores_outputs_on_conflict():
    steps = _nightly_wf()["jobs"]["nightly"]["steps"]
    commit = next(s for s in steps if "Commit data" in s.get("name", ""))
    run = commit["run"]
    assert commit["if"].replace(" ", "") == "always()&&github.ref=='refs/heads/main'"
    assert "data/meta/ai_brief_latest.json" in run          # 云端简报回推仓库
    assert "data/rd_agent" not in run                        # gitignored, 死代码
    # 产物 commit 之后: 记 SHA + 丢掉 runner 独有的 intel.db/reports 改动, 再 pull --rebase
    i_commit = run.index('git commit -m "🌙 nightly')
    i_sha = run.index('NIGHTLY_SHA=$(git rev-parse HEAD)')
    i_db = run.index("git checkout -- data/intel.db")
    i_pull = run.index("git pull --rebase")
    assert i_commit < i_sha < i_pull and i_commit < i_db < i_pull
    # 冲突回退: reset --hard 之后先从产物 commit 恢复, 再 add_outputs
    lines = [ln.strip() for ln in run.splitlines()]
    i_reset = lines.index("git reset --hard origin/main")
    i_restore = lines.index('git checkout "$NIGHTLY_SHA" -- data/meta data/nightly_logs')
    i_readd = lines.index("add_outputs", i_reset)
    assert i_reset < i_restore < i_readd


def test_nightly_yml_failure_alert_step_is_last():
    steps = _nightly_wf()["jobs"]["nightly"]["steps"]
    last = steps[-1]
    assert last["if"].replace(" ", "") == "failure()||cancelled()"
    assert "push_alert" in last["run"] and "github.run_id" in last["env"]["RUN_URL"]


def test_nightly_yml_timeouts_and_schedule():
    wf = _nightly_wf()
    job = wf["jobs"]["nightly"]
    screen_timeout_s = int(job["env"]["SCREEN_TIMEOUT"])
    # 内层 screen 超时必须明显小于 job 超时, 给调权/推送/告警/提交留 ≥10min
    assert screen_timeout_s + 600 <= job["timeout-minutes"] * 60
    on = wf.get("on", wf.get(True))   # PyYAML 把 on: 解析成 True
    assert on["schedule"][0]["cron"] == "17 16 * * *"


def test_gitignore_keeps_dated_nightly_logs(tmp_path):
    """用临时 git 仓库验证: 日期日志/failures 入库, watchdog/launchd 仍忽略."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text(encoding="utf-8"), encoding="utf-8")

    def ignored(p):
        return subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", p]).returncode == 0

    assert not ignored("data/nightly_logs/2026-10-12.log")
    assert not ignored("data/nightly_logs/2026-10-12.failures.json")
    assert ignored("data/nightly_logs/watchdog.log")
    assert ignored("data/nightly_logs/launchd.log")
