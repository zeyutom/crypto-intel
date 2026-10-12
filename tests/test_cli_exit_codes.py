"""流水线诚实性测试: CLI 失败必须非零退出 + nightly 脚本/workflow 的结构约束.

回归 (2026-10-10 回测审计):
- `python -m src.cli screen` 打印 "✗ CoinGecko API 无数据" 却 exit 0, run_step 只看退出码
  → 日志写 "✓ screen 完成", failures.json 为空, 当天 meta 快照静默缺失。
- DOW 在第 8 步才算, screen 跨午夜后周日的 weekly-review/scorecard 被跳过。
- nightly.yml 只回推 snapshot_*.json, runner 上学到的 factor_config.json 每晚被丢弃。
- .gitignore 整目录忽略 data/nightly_logs/, 云端失败记录永远进不了仓库。
"""
import io
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
                              "skipped": "already_updated_today",
                              "disabled": ["dev_activity", "volume_turnover"]})
    assert _run(["update-weights", "7"]) is None
    out = capsys.readouterr().out
    assert "跳过" in out and "今天已调过权重" in out
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
    assert re.search(r'^run_step "screen" run_with_timeout ', text, re.M)
    assert 'record_failure "snapshot-check" 1' in text


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
    assert commit.get("continue-on-error") is True


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
