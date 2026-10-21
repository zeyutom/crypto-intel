"""Unit tests for src.research.watchdog — 阈值 + 去重 + alert 结构.

回归 (2026-10 审计):
- run_check 不管推送成败都记去重 → 飞书一次超时, 该告警 24h 内不再发。
- 清算 / ETF 检测器数月无数据, funding 检测器在云端因缺 ccxt 永远 return None,
  每轮都打印 "✓ 当前无告警触发", 没人知道一半检测器是瞎的。
- nightly 漏跑/取消/push 失败没有任何 dead-man 告警 (pipeline_stale)。
- 旧 test_run_check_returns_list 跑真实 CHECKS 联网 21 次, 断言永远成立。
所有测试离线: CHECKS / 推送 / funding 拉取全部 monkeypatch, 文件写 tmp_path。
"""
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


def test_checks_registry_names():
    """清算 / ETF 留在注册表 (无数据时报 no_data), 新增 pipeline_stale dead-man."""
    names = [n for n, _ in wd.CHECKS]
    assert names == ["peg", "funding", "fear_greed", "dex_tvl",
                     "liquidations", "etf_outflow", "pipeline_stale"]


def _alert(t="peg_deviation"):
    return Alert(type=t, severity="warning", title="X", detail="Y",
                 value=80, threshold=50)


def _boom():
    raise RuntimeError("boom")


@pytest.fixture
def fake_checks(monkeypatch):
    """一个触发 / 一个抛异常 / 一个无数据 / 一个有数据未触发, 不联网不读 DB."""
    monkeypatch.setattr(wd, "CHECKS", [
        ("peg", lambda: _alert("peg_deviation")),
        ("funding", _boom),
        ("liquidations", lambda: wd.NoData("无 26h 内数据")),
        ("fear_greed", lambda: None),
    ])


@pytest.fixture
def push_recorder(monkeypatch):
    """替换飞书推送; .status 控制返回值, .calls 记录推了哪些 type."""
    class Rec:
        status = "sent"
        calls: list = []

    rec = Rec()
    rec.calls = []

    def fake_push(alert):
        rec.calls.append(alert.type)
        return rec.status

    monkeypatch.setattr(wd, "_push_alert_to_feishu", fake_push)
    return rec


def test_run_check_dedup_and_skipped(fresh_state, fake_checks, push_recorder):
    alerts = wd.run_check(push=True, dedup=True)
    assert [a["type"] for a in alerts] == ["peg_deviation"]
    assert push_recorder.calls == ["peg_deviation"]
    # alerts.log 一行, state 记了去重
    assert len(wd.ALERT_LOG.read_text().strip().splitlines()) == 1
    saved = json.loads(fresh_state.read_text())
    assert "peg_deviation" in saved["last_alerts"]
    # 覆盖率: 抛异常按无数据算, 原因带异常信息
    run = wd.LAST_RUN
    assert run["skipped"] == ["funding", "liquidations"]
    assert run["n_checks"] == 4 and run["n_with_data"] == 2
    assert run["statuses"]["funding"]["status"] == "no_data"
    assert "boom" in run["statuses"]["funding"]["reason"]
    assert run["statuses"]["liquidations"]["reason"] == "无 26h 内数据"
    assert run["statuses"]["peg"]["status"] == "alert"
    assert run["statuses"]["fear_greed"]["status"] == "ok"
    assert run["push_failed"] == []

    # 24h 窗口内再跑: 去重, 不再推
    assert wd.run_check(push=True, dedup=True) == []
    assert push_recorder.calls == ["peg_deviation"]
    assert wd.LAST_RUN["statuses"]["peg"]["status"] == "alert"


def test_run_check_failed_push_not_marked_and_retried(fresh_state, fake_checks,
                                                      push_recorder):
    """推送失败不记去重 → 下一轮重试; 成功后才记."""
    push_recorder.status = "failed"
    alerts = wd.run_check(push=True, dedup=True)
    assert [a["type"] for a in alerts] == ["peg_deviation"]
    assert wd.LAST_RUN["push_failed"] == ["peg_deviation"]
    assert "peg_deviation" not in wd._load_state().get("last_alerts", {})

    push_recorder.status = "sent"
    alerts = wd.run_check(push=True, dedup=True)
    assert [a["type"] for a in alerts] == ["peg_deviation"]
    assert push_recorder.calls == ["peg_deviation", "peg_deviation"]
    assert wd.LAST_RUN["push_failed"] == []
    assert "peg_deviation" in wd._load_state()["last_alerts"]

    # 第三轮: 已成功推过, 去重
    assert wd.run_check(push=True, dedup=True) == []
    assert len(push_recorder.calls) == 2


def test_run_check_no_webhook_counts_as_handled(fresh_state, fake_checks,
                                                push_recorder):
    """没配 webhook 的机器: 记去重, 免得每轮重复写 alerts.log."""
    push_recorder.status = "no_webhook"
    wd.run_check(push=True, dedup=True)
    assert wd.LAST_RUN["push_failed"] == []
    assert "peg_deviation" in wd._load_state()["last_alerts"]


def test_run_check_push_false_marks_dedup(fresh_state, fake_checks, push_recorder):
    wd.run_check(push=False, dedup=True)
    assert push_recorder.calls == []
    assert "peg_deviation" in wd._load_state()["last_alerts"]


def test_push_alert_returns_status(monkeypatch):
    """_push_alert_to_feishu 三态: no_webhook / failed / sent."""
    import src.notifier as notifier
    group = {"name": "g", "url": "https://example.invalid", "secret": ""}
    monkeypatch.setattr(notifier, "_load_groups", lambda: [])
    assert wd._push_alert_to_feishu(_alert()) == "no_webhook"

    monkeypatch.setattr(notifier, "_load_groups", lambda: [group])
    monkeypatch.setattr(notifier, "_push_one",
                        lambda payload, g: {"ok": False, "error": "HTTP 502"})
    assert wd._push_alert_to_feishu(_alert()) == "failed"

    monkeypatch.setattr(notifier, "_push_one", lambda payload, g: {"ok": True})
    assert wd._push_alert_to_feishu(_alert()) == "sent"


# ────────────────────────────────────────────────────────────────────
#  funding: 走 fetch_funding_rates (云端 OKX 兜底), 不依赖 ccxt
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def no_ccxt(monkeypatch):
    from src.adapters import ccxt_exchange as cx
    monkeypatch.setattr(cx, "is_available", lambda: False)


def test_funding_extreme_without_ccxt(monkeypatch, no_ccxt):
    from src.research import factors_extended as fe
    monkeypatch.setattr(fe, "fetch_funding_rates",
                        lambda: {"BTC": 0.0002, "ETH": -0.0015, "SOL": 0.0001})
    a = wd.check_funding_extreme()
    assert isinstance(a, Alert)
    assert a.type == "funding_extreme"
    assert "ETH" in a.title and "空头过密" in a.title
    assert a.value == pytest.approx(0.15)


def test_funding_normal_returns_none(monkeypatch, no_ccxt):
    from src.research import factors_extended as fe
    monkeypatch.setattr(fe, "fetch_funding_rates",
                        lambda: {"BTC": 0.0001, "ETH": 0.0001, "SOL": -0.0002})
    assert wd.check_funding_extreme() is None


@pytest.mark.parametrize("rates", [{}, {"DOGE": 0.01}])
def test_funding_no_rates_is_no_data(monkeypatch, rates):
    from src.research import factors_extended as fe
    monkeypatch.setattr(fe, "fetch_funding_rates", lambda: rates)
    assert isinstance(wd.check_funding_extreme(), wd.NoData)


# ────────────────────────────────────────────────────────────────────
#  DB 类检测器 (清算 / ETF): 只认新鲜行, 无数据报 no_data
# ────────────────────────────────────────────────────────────────────

def _ts(hours_ago: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """临时 sqlite, 不碰 data/intel.db."""
    from src import db
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "intel.db")
    db.init_db()
    return db


def _liq_row(ts, value):
    return {"ts": ts, "source": "coinglass", "asset_id": "market",
            "metric": "liquidations_24h_usd", "value": value}


def _etf_row(day, musd):
    return {"ts": day.strftime("%Y-%m-%dT00:00:00Z"), "source": "farside_etf",
            "asset_id": "bitcoin", "metric": "etf_net_flow_musd", "value": musd}


def test_liquidations_empty_db_is_no_data(tmp_db):
    assert isinstance(wd.check_liquidations_extreme(), wd.NoData)


def test_liquidations_ignores_stale_rows(tmp_db):
    tmp_db.upsert_raw([_liq_row(_ts(30), 5e9)])
    res = wd.check_liquidations_extreme()
    assert isinstance(res, wd.NoData)
    assert "26h" in res.reason


def test_liquidations_fresh_row_fires(tmp_db):
    tmp_db.upsert_raw([_liq_row(_ts(30), 1e6), _liq_row(_ts(2), 1.2e9)])
    a = wd.check_liquidations_extreme()
    assert isinstance(a, Alert) and a.type == "liquidations_extreme"
    assert a.severity == "critical"


def test_liquidations_fresh_small_is_ok(tmp_db):
    tmp_db.upsert_raw([_liq_row(_ts(2), 1e8)])
    assert wd.check_liquidations_extreme() is None


def test_etf_outflow_ignores_old_days(tmp_db):
    old_day = datetime.now(timezone.utc) - timedelta(days=4)
    tmp_db.upsert_raw([_etf_row(old_day, -900.0)])
    assert isinstance(wd.check_etf_outflow(), wd.NoData)


def test_etf_outflow_yesterday_fires(tmp_db):
    """日粒度行 (ts=交易日 00:00Z) 按该日结束算新鲜度: 昨天的流量要能触发."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    tmp_db.upsert_raw([_etf_row(yesterday, -350.0)])
    a = wd.check_etf_outflow()
    assert isinstance(a, Alert) and a.type == "etf_outflow"
    assert a.value == pytest.approx(350e6)


def test_etf_outflow_day_ended_over_24h_ago_is_no_data(tmp_db):
    """交易日结束已 25h (ts = now-49h): 不能再触发.

    旧窗口 (DB_FRESH_HOURS+24 = 50h) 下这行仍可见, 而 24h 去重已过期 → 同一天的
    流出被推两次 (例: 周六 00:30 refresh 落周五行, 01:23 推过, 周日 01:23 又推).
    """
    tmp_db.upsert_raw([{**_etf_row(datetime.now(timezone.utc), -900.0),
                        "ts": _ts(49)}])
    res = wd.check_etf_outflow()
    assert isinstance(res, wd.NoData)
    assert "24h" in res.reason


def test_etf_outflow_day_ended_23h_ago_fires(tmp_db):
    """交易日结束 23h (ts = now-47h) 仍在窗口内."""
    tmp_db.upsert_raw([{**_etf_row(datetime.now(timezone.utc), -900.0),
                        "ts": _ts(47)}])
    a = wd.check_etf_outflow()
    assert isinstance(a, Alert) and a.type == "etf_outflow"


# ────────────────────────────────────────────────────────────────────
#  pipeline_stale: dead-man, 只看真实快照
# ────────────────────────────────────────────────────────────────────

def _write_snap(meta_dir, hours_ago, *, name=None, meta=None, with_ts=True):
    dt = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    name = name or f"snapshot_{dt.strftime('%Y%m%d_%H%M')}.json"
    data = {"date": dt.strftime("%Y-%m-%d"), "coins": [], "_meta": meta or {}}
    if with_ts:
        data["timestamp"] = dt.isoformat()
    (meta_dir / name).write_text(json.dumps(data))
    return name


@pytest.fixture
def meta_dir(monkeypatch, tmp_path):
    d = tmp_path / "meta"
    d.mkdir()
    monkeypatch.setattr(wd, "META_DIR", d)
    return d


def test_pipeline_stale_ignores_synthetic_snapshots(meta_dir):
    real = _write_snap(meta_dir, 40)
    # 更新的回填/合成快照都不算
    _write_snap(meta_dir, 20, name="snapshot_29991231_0000.json",
                meta={"source": "backfill", "synthetic": True})
    _write_snap(meta_dir, 10, meta={"synthetic": True})
    _write_snap(meta_dir, 5, meta={"source": "backfill"})
    (meta_dir / "factor_config.json").write_text("{}")
    a = wd.check_pipeline_stale()
    assert isinstance(a, Alert)
    assert a.type == "pipeline_stale" and a.severity == "critical"
    assert "夜间流水线已 40 小时没有产出真实快照" in a.title
    assert real in a.detail
    assert a.threshold == wd.PIPELINE_STALE_HOURS


def test_pipeline_fresh_snapshot_ok(meta_dir):
    _write_snap(meta_dir, 40)
    _write_snap(meta_dir, 23)
    assert wd.check_pipeline_stale() is None


def test_pipeline_stale_falls_back_to_filename_time(meta_dir):
    _write_snap(meta_dir, 50, with_ts=False)
    a = wd.check_pipeline_stale()
    assert isinstance(a, Alert) and a.value >= 49


def test_pipeline_stale_threshold_configurable(meta_dir, monkeypatch):
    _write_snap(meta_dir, 30)
    assert wd.check_pipeline_stale() is None
    monkeypatch.setattr(wd, "PIPELINE_STALE_HOURS", 24.0)
    assert isinstance(wd.check_pipeline_stale(), Alert)


def test_pipeline_no_real_snapshot_is_no_data(meta_dir):
    _write_snap(meta_dir, 5, name="snapshot_20261019_0000.json")
    assert isinstance(wd.check_pipeline_stale(), wd.NoData)


def test_pipeline_stale_dedups_through_run_check(fresh_state, meta_dir,
                                                 monkeypatch, push_recorder):
    """dead-man 走正常 24h 去重: 一次漏跑只推一次."""
    _write_snap(meta_dir, 48)
    monkeypatch.setattr(wd, "CHECKS", [("pipeline_stale", wd.check_pipeline_stale)])
    assert len(wd.run_check(push=True, dedup=True)) == 1
    assert wd.run_check(push=True, dedup=True) == []
    assert push_recorder.calls == ["pipeline_stale"]


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
