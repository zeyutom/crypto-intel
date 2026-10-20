"""数据自愈层 + 链上拉取预算回归.

回归 (2026-10-20 运维审计):
- 漏跑告警只看 backfill 之后的缺口: 合成 _0000 快照一补就清零, 09-29 整晚漏跑无人知晓。
  改成看"最新真实快照"的新鲜度 (36h), 在 backfill 之前算, 跨 UTC 午夜的晚跑 (26-29h) 不误报。
- fetch_onchain_activity 每晚 8-9 次 429 × 60s, 占掉 screen ~14/15 分钟; 加总耗时预算。
"""
import json
import logging
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.research.data_quality as dq  # noqa: E402
import src.research.factors_extended as fx  # noqa: E402


def _write_snap(d: pathlib.Path, ts: datetime, synthetic=False, source=None, name=None):
    if not synthetic and ts.strftime("%H%M") == "0000":
        ts -= timedelta(minutes=1)       # 真实快照别恰好撞上 _0000 文件名
    name = name or f"snapshot_{ts.strftime('%Y%m%d_%H%M')}.json"
    meta = {"regime": "sideways"}
    if synthetic:
        meta["synthetic"] = True
    if source:
        meta["source"] = source
    (d / name).write_text(json.dumps({"timestamp": ts.isoformat(), "date": ts.strftime("%Y-%m-%d"),
                                      "_meta": meta, "coins": []}), encoding="utf-8")
    return name


@pytest.fixture
def meta_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(dq, "META_DIR", tmp_path)
    monkeypatch.setattr(dq, "detect_source_dryness", lambda days=5: [])
    pushed = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "push_alert",
                        lambda title, lines, color="red": pushed.append((title, lines)) or {"ok": True})
    return SimpleNamespace(path=tmp_path, pushed=pushed)


NOW = datetime.now(timezone.utc)


def test_newest_real_snapshot_skips_synthetic(meta_dir):
    real = _write_snap(meta_dir.path, NOW - timedelta(hours=50))
    _write_snap(meta_dir.path, (NOW - timedelta(hours=10)).replace(hour=0, minute=0), synthetic=True,
                source="backfill")                                     # *_0000.json
    _write_snap(meta_dir.path, NOW - timedelta(hours=5), synthetic=True)   # 非 _0000 但 synthetic
    _write_snap(meta_dir.path, NOW - timedelta(hours=4), source="backfill",
                name=f"snapshot_{(NOW - timedelta(hours=4)).strftime('%Y%m%d_%H%M')}.json")
    n = dq.newest_real_snapshot()
    assert n["file"] == real


def test_missed_run_alerts_even_after_backfill(meta_dir, monkeypatch, caplog):
    """上一份真实快照 47h 前 (漏跑一晚); backfill 补了合成快照, 仍必须告警 (且写进 nightly 日志)。"""
    _write_snap(meta_dir.path, NOW - timedelta(hours=47))

    def fake_backfill(days):
        for i in range(1, days + 1):
            day = (NOW - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            _write_snap(meta_dir.path, day, synthetic=True, source="backfill")
        return {"ok": True}

    monkeypatch.setattr(dq, "_run_backfill", fake_backfill)
    monkeypatch.setattr(dq.log, "propagate", True)
    with caplog.at_level(logging.WARNING, logger="data_quality"):
        r = dq.run_data_quality(push=True)
    assert r["real_snapshot"]["stale"] is True and r["real_snapshot"]["age_hours"] >= 46
    # 断更信息落日志 (nightly 日志入库; 推送失败/没配飞书时这是唯一记录)
    logged = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("真实快照断更" in m and r["real_snapshot"]["newest"] in m for m in logged)
    assert r["gaps_after"] == []                       # backfill 把日期缺口全补上了 ...
    assert r["alerted"] is True                        # ... 但断更告警照样推
    assert any("真实快照断更" in ln for ln in meta_dir.pushed[0][1])
    # 返回结构保持兼容 (cli data-quality 分支读这些键)
    for k in ("gaps_before", "gaps_after", "recent_gaps", "backfill", "dry_sources", "alert_lines"):
        assert k in r


def test_late_run_across_midnight_does_not_alert(meta_dir, monkeypatch):
    """29h 前的真实快照 (晚跑跨了 UTC 午夜, 某个日历日没有真实快照) 不算漏跑。"""
    _write_snap(meta_dir.path, NOW - timedelta(hours=29))
    monkeypatch.setattr(dq, "_run_backfill", lambda days: {"ok": True})
    r = dq.run_data_quality(push=True)
    assert r["real_snapshot"]["stale"] is False
    assert not any("真实快照断更" in ln for ln in r["alert_lines"])
    assert meta_dir.pushed == []


def test_no_real_snapshot_at_all_alerts(meta_dir, monkeypatch):
    _write_snap(meta_dir.path, (NOW - timedelta(days=1)).replace(hour=0, minute=0), synthetic=True)
    monkeypatch.setattr(dq, "_run_backfill", lambda days: {"ok": True})
    r = dq.run_data_quality(push=False)
    assert r["real_snapshot"]["newest"] is None and r["real_snapshot"]["stale"] is True
    assert any("找不到任何真实快照" in ln for ln in r["alert_lines"])


def test_snapshot_ts_falls_back_to_filename(tmp_path):
    p = tmp_path / "snapshot_20261019_2256.json"
    ts = dq._parse_snapshot_ts({}, p)
    assert ts == datetime(2026, 10, 19, 22, 56, tzinfo=timezone.utc)


# ────────────────────────────────────────────────────────────────────
#  fetch_onchain_activity: 总耗时预算
# ────────────────────────────────────────────────────────────────────

def _coin(sym):
    return {"symbol": sym, "developer_data": {}, "community_data": {"twitter_followers": 10},
            "sentiment_votes_up_percentage": 55.0}


def test_onchain_fetch_stops_at_budget(monkeypatch, caplog):
    clock = {"t": 0.0}
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        clock["t"] += 100.0          # 每个币 100s (含 429 Retry-After)
        return _coin(url.rsplit("/", 1)[-1])

    sleeps = []
    monkeypatch.setattr(fx, "time", SimpleNamespace(monotonic=lambda: clock["t"],
                                                    sleep=lambda s: sleeps.append(s)))
    monkeypatch.setattr(fx, "_get", fake_get)
    monkeypatch.setenv("ONCHAIN_FETCH_BUDGET_S", "250")
    monkeypatch.setattr(fx.log, "propagate", True)
    ids = [f"c{i}" for i in range(10)]
    with caplog.at_level(logging.WARNING, logger="factors_ext"):
        res = fx.fetch_onchain_activity(ids)
    assert len(calls) == 3                                   # 0/100/200s 时开拉, 300s 超预算停
    assert set(res) == {"C0", "C1", "C2", fx.ONCHAIN_META_KEY}
    assert set(res[fx.ONCHAIN_META_KEY]) == {"coins_with_dev_data", "coins_with_community_data",
                                             "coins_with_any_data"}   # 结构不变
    assert res[fx.ONCHAIN_META_KEY]["coins_with_community_data"] == 3
    assert len(sleeps) == 2                                  # 超预算后不再白睡
    assert any("跳过剩余 7/10" in r.getMessage() for r in caplog.records)


def test_onchain_fetch_budget_disabled(monkeypatch):
    clock = {"t": 0.0}

    def fake_get(url, **kw):
        clock["t"] += 1000.0
        return _coin(url.rsplit("/", 1)[-1])

    monkeypatch.setattr(fx, "time", SimpleNamespace(monotonic=lambda: clock["t"], sleep=lambda s: None))
    monkeypatch.setattr(fx, "_get", fake_get)
    monkeypatch.setenv("ONCHAIN_FETCH_BUDGET_S", "0")      # ≤0 = 关闭预算
    res = fx.fetch_onchain_activity(["a", "b", "c"])
    assert set(res) == {"A", "B", "C", fx.ONCHAIN_META_KEY}


def test_onchain_fetch_default_budget_is_300s(monkeypatch):
    clock = {"t": 0.0}
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        clock["t"] += 60.0
        return _coin("x")

    monkeypatch.setattr(fx, "time", SimpleNamespace(monotonic=lambda: clock["t"], sleep=lambda s: None))
    monkeypatch.setattr(fx, "_get", fake_get)
    monkeypatch.delenv("ONCHAIN_FETCH_BUDGET_S", raising=False)
    fx.fetch_onchain_activity([f"c{i}" for i in range(50)])
    assert len(calls) == 5                                   # 0,60,120,180,240 开拉; 300 停
