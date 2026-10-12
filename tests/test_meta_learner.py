"""Unit tests for src.research.meta_learner — IC 回测的鲁棒性 + 元学习闭环.

回归 (2026-10-10 4.5 个月实盘回测审计):
  - 快照只存 top-200 → 掉出榜单的币无后续价格 → IC 评估截断式幸存者偏差
  - 回填合成快照 (*_0000.json / _meta.synthetic) 混入 IC 回测
  - 常数因子 IC 返回 0.0 → 死因子在归一化时被动吃掉 ~30% 权重
  - 先 clamp 后归一化 → 最终权重越界 (117 次)
  - 一天多次调权 → 权重震荡 (周 L1 turnover 0.22 vs 0.09)

更早的回归: 回填的合成快照里缺失因子值是 null, 曾导致 update-weights
连续多日崩溃 (TypeError: '<' not supported between NoneType).
"""
import sys
import json
import math
import pathlib
import logging
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import src.research.meta_learner as ml
from src.research.meta_learner import (
    _spearman_rank_corr, save_snapshot, run_ic_backtest,
    update_weights_from_ic, load_factor_config, save_factor_config,
    get_current_weights, generate_factor_report, _normalize_and_project,
    DEFAULT_FACTORS, MIN_IC_RECORDS,
)


# ────────────────────────────────────────────────────────────────────
#  fixtures / helpers
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def meta_dir(tmp_path, monkeypatch):
    """把 META_DIR 指到 tmp_path, 绝不写真实 data/meta."""
    d = tmp_path / "meta"
    monkeypatch.setattr(ml, "META_DIR", d)
    return d


@pytest.fixture(autouse=True)
def weight_writer(monkeypatch):
    """默认把测试进程当作权重写入方; 测试只读行为时再 delenv."""
    monkeypatch.setenv(ml.WEIGHT_WRITER_ENV, "1")


@pytest.fixture
def warn_log(caplog):
    """meta_learner 的 logger propagate=False, 手动挂 caplog handler."""
    ml.log.addHandler(caplog.handler)
    caplog.set_level(logging.INFO)
    yield caplog
    ml.log.removeHandler(caplog.handler)


FACTOR_NAMES = [f"f_{k}" for k in DEFAULT_FACTORS]


def _make_coins(n: int, dead: dict | None = None, seed: int = 1) -> list[dict]:
    """n 个币, 因子值各不相同; dead={f_name: const} 指定常数因子."""
    dead = dead or {}
    coins = []
    for i in range(n):
        # 价格随 seed 扰动, 使不同快照间的收益率有截面差异 (否则收益率常数)
        price = (1.0 + i * 0.01) * (1.0 + ((i * 7 + seed) % 13) / 50.0)
        c = {
            "symbol": f"C{i}", "coin_id": f"coin-{i}",
            "price": round(price, 6), "market_cap": 1e6 * (n - i),
            "composite_score": round(1.0 - i / n, 4),
            "change_24h": 0.1 * i, "change_7d": 0.2 * i, "change_30d": 0.3 * i,
        }
        for j, f in enumerate(FACTOR_NAMES):
            # 不同因子用不同排列, 避免互相完全共线
            c[f] = dead[f] if f in dead else round(((i * (j + 3) + seed) % n) / n, 4)
        coins.append(c)
    return coins


def _write_snapshot(meta_dir, ts: datetime, coins: list[dict],
                    meta: dict | None = None, name: str | None = None):
    meta_dir.mkdir(parents=True, exist_ok=True)
    snap = {"timestamp": ts.isoformat(), "date": ts.strftime("%Y-%m-%d"),
            "factor_weights": {}, "coins": coins}
    if meta is not None:
        snap["_meta"] = meta
    name = name or f"snapshot_{ts.strftime('%Y%m%d_%H%M')}.json"
    p = meta_dir / name
    p.write_text(json.dumps(snap), encoding="utf-8")
    return p


def _seed_config(meta_dir, history_ic: float = 0.0, n_hist: int = MIN_IC_RECORDS,
                 weights: dict | None = None, enabled_override: dict | None = None):
    """写一份带 IC 历史的 factor_config (历史日期都在过去, 不与今天冲突)."""
    cfg = load_factor_config()
    for fname, fdata in cfg["factors"].items():
        fdata["ic_history"] = [
            {"date": f"2026-01-{d + 1:02d}", "ic": history_ic, "lookback": 7}
            for d in range(n_hist)
        ]
        if weights and fname in weights:
            fdata["weight"] = weights[fname]
        if enabled_override and fname in enabled_override:
            fdata["enabled"] = enabled_override[fname]
    save_factor_config(cfg)
    return cfg


def _ic_result(factor_ic: dict, constant: list | None = None, lookback: int = 7,
               degenerate: list | None = None):
    return {"ok": True, "lookback_days": lookback, "factor_ic": factor_ic,
            "degenerate_factors": degenerate or [],
            "constant_factors": constant or []}


# ────────────────────────────────────────────────────────────────────
#  _spearman_rank_corr 基础正确性
# ────────────────────────────────────────────────────────────────────

def test_spearman_perfect_monotone():
    """完全单调 → ±1."""
    x = [1, 2, 3, 4, 5, 6]
    assert abs(_spearman_rank_corr(x, [10, 20, 30, 40, 50, 60]) - 1.0) < 1e-9
    assert abs(_spearman_rank_corr(x, [60, 50, 40, 30, 20, 10]) + 1.0) < 1e-9


def test_spearman_constant_column_is_nan():
    """常数列 Spearman 未定义 → NaN (不再伪装成 0.0, 否则死因子被动吃权重)."""
    assert math.isnan(_spearman_rank_corr([1, 1, 1, 1, 1], [1, 2, 3, 4, 5]))
    # 收益率侧常数同样未定义
    assert math.isnan(_spearman_rank_corr([1, 2, 3, 4, 5], [7, 7, 7, 7, 7]))


# ────────────────────────────────────────────────────────────────────
#  回归: None 因子值 (回填快照) 不再崩
# ────────────────────────────────────────────────────────────────────

def test_spearman_none_pairs_dropped_not_crash():
    """含 None 的配对成对剔除后照常计算 (曾抛 TypeError)."""
    x = [1, None, 2, 3, None, 4, 5, 6]
    y = [2, 9.0, 4, 6, 1.0, 8, 10, 12]
    ic = _spearman_rank_corr(x, y)
    assert abs(ic - 1.0) < 1e-9  # 剔除 None 后剩完全单调的 6 对


def test_spearman_all_none_returns_zero():
    """全 None / 剔除后不足 5 对 → 0.0, 不抛异常."""
    assert _spearman_rank_corr([None] * 8, list(range(8))) == 0.0
    assert _spearman_rank_corr([1, None, None, None, None, 2],
                               [1, 2, 3, 4, 5, 6]) == 0.0


def test_spearman_none_in_returns_side():
    """y 侧含 None 同样剔除."""
    x = [1, 2, 3, 4, 5, 6, 7]
    y = [1, None, 3, 4, 5, 6, 7]
    ic = _spearman_rank_corr(x, y)
    assert abs(ic - 1.0) < 1e-9


# ────────────────────────────────────────────────────────────────────
#  save_snapshot: 全宇宙 + _meta + factor_stats
# ────────────────────────────────────────────────────────────────────

def test_save_snapshot_full_universe_with_meta_and_factor_stats(meta_dir):
    """480 个币全部落盘 (不再截断 200), 带 coin_id / _meta / factor_stats."""
    coins = _make_coins(480, dead={"f_funding_rate": 0.5, "f_dev_activity": 0.0})
    # NaN 必须落盘为 null. 注意: 前 50 个币里出现 null 因子会让这份快照被
    # _load_valid_snapshot 判为不合格 (IC 回测跳过), 这里只测落盘格式
    coins[3]["f_narrative_heat"] = float("nan")
    path = save_snapshot(coins, {"momentum_30d": 0.5}, meta={"source": "live", "run": "t"})

    raw = path.read_text(encoding="utf-8")
    assert "NaN" not in raw
    snap = json.loads(raw)
    assert len(snap["coins"]) == 480
    assert snap["coins"][0]["coin_id"] == "coin-0"
    assert snap["coins"][3]["f_narrative_heat"] is None
    # 原有字段保留
    for k in ("symbol", "price", "market_cap", "composite_score",
              "change_24h", "change_7d", "change_30d", "f_momentum_30d"):
        assert k in snap["coins"][0]

    m = snap["_meta"]
    assert m["universe_size"] == 480
    assert m["truncated"] is False
    assert m["schema"] == 2
    assert m["source"] == "live" and m["run"] == "t"   # 调用方 meta 合并
    fs = m["factor_stats"]
    assert fs["funding_rate"] == {"nunique": 1, "nonzero_share": 1.0}
    assert fs["dev_activity"] == {"nunique": 1, "nonzero_share": 0.0}
    assert fs["momentum_30d"]["nunique"] > 100
    assert "f_momentum_30d" not in fs   # key 去掉 f_ 前缀


def test_save_snapshot_meta_default_none(meta_dir):
    """meta 省略时签名兼容旧调用 save_snapshot(scored, weights)."""
    path = save_snapshot(_make_coins(120), {})
    snap = json.loads(path.read_text())
    assert snap["_meta"]["universe_size"] == 120
    assert snap["_meta"]["schema"] == 2


def test_save_snapshot_nan_in_weights_and_meta_written_as_null(meta_dir):
    """factor_weights / 调用方 meta 里的 NaN/inf 也不能写成字面 NaN (曾只清洗币字段)."""
    path = save_snapshot(_make_coins(120),
                         {"momentum_30d": float("nan"), "tvl_mcap": 0.3},
                         meta={"base_weights": {"x": float("inf")},
                               "sources": [1.0, float("nan")]})
    raw = path.read_text(encoding="utf-8")
    assert "NaN" not in raw and "Infinity" not in raw
    snap = json.loads(raw)
    assert snap["factor_weights"] == {"momentum_30d": None, "tvl_mcap": 0.3}
    assert snap["_meta"]["base_weights"] == {"x": None}
    assert snap["_meta"]["sources"] == [1.0, None]


# ────────────────────────────────────────────────────────────────────
#  run_ic_backtest: 过滤合成/残缺快照, None IC, dropout
# ────────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def test_ic_backtest_skips_synthetic_and_broken_snapshots(meta_dir):
    """旧快照与 latest 快照都要跳过: *_0000.json / _meta.synthetic /
    _meta.source=backfill / <100 币 / 前 50 币含 null 因子."""
    now = _now()
    old_ts = now - timedelta(days=7)
    good_old = _write_snapshot(meta_dir, old_ts, _make_coins(150))
    # 干扰: 更接近 target 的合成快照 (理应被跳过)
    _write_snapshot(meta_dir, old_ts + timedelta(hours=1), _make_coins(150),
                    name=f"snapshot_{(old_ts + timedelta(hours=1)).strftime('%Y%m%d')}_0000.json")
    _write_snapshot(meta_dir, old_ts + timedelta(hours=2), _make_coins(150),
                    meta={"synthetic": True})
    _write_snapshot(meta_dir, old_ts + timedelta(hours=3), _make_coins(150),
                    meta={"source": "backfill"})
    _write_snapshot(meta_dir, old_ts + timedelta(hours=4), _make_coins(80))
    nulled = _make_coins(150)
    nulled[10]["f_tvl_mcap"] = None
    _write_snapshot(meta_dir, old_ts + timedelta(hours=5), nulled)

    # latest: 一份合格的, 之后再来一份更新的 backfill (必须不被当作价格源)
    good_latest = _write_snapshot(meta_dir, now - timedelta(hours=1), _make_coins(150, seed=9))
    _write_snapshot(meta_dir, now, _make_coins(150, seed=9), meta={"source": "backfill"})

    r = run_ic_backtest(lookback_days=7)
    assert r["ok"], r
    assert r["snapshot_file"] == good_old.name
    assert r["latest_file"] == good_latest.name
    assert r["skipped_snapshots"] == 6
    assert r["matched_coins"] == 150
    assert r["dropout_rate"] == 0.0
    assert set(r["factor_ic"]) == set(FACTOR_NAMES)


def test_ic_backtest_all_synthetic_fails_cleanly(meta_dir):
    now = _now()
    _write_snapshot(meta_dir, now - timedelta(days=7), _make_coins(150), meta={"synthetic": True})
    _write_snapshot(meta_dir, now, _make_coins(150), meta={"synthetic": True})
    r = run_ic_backtest(lookback_days=7)
    assert r["ok"] is False


def test_ic_backtest_constant_factor_excluded_from_factor_ic(meta_dir):
    """常数因子 IC 未定义 → 不进 factor_ic (既不写 NaN 也不放 None), 而是
    列入 degenerate_factors + constant_factors.

    回归: 曾把 None 放进 factor_ic, `python -m src.cli ic-backtest` 对每个值做
    abs(ic) / ic > 0.05 → 实盘数据 (dev_activity/funding_rate 恒常数) 必崩."""
    now = _now()
    _write_snapshot(meta_dir, now - timedelta(days=7),
                    _make_coins(150, dead={"f_funding_rate": 0.5, "f_dev_activity": 0.0}))
    _write_snapshot(meta_dir, now, _make_coins(150, seed=5))

    r = run_ic_backtest(lookback_days=7)
    assert r["ok"], r
    assert "f_funding_rate" not in r["factor_ic"]
    assert "f_dev_activity" not in r["factor_ic"]
    assert set(r["degenerate_factors"]) == {"f_funding_rate", "f_dev_activity"}
    assert set(r["constant_factors"]) == {"f_funding_rate", "f_dev_activity"}
    assert set(r["factor_ic"]) == set(FACTOR_NAMES) - {"f_funding_rate", "f_dev_activity"}
    assert all(isinstance(v, float) and math.isfinite(v) for v in r["factor_ic"].values())
    # 模拟 cli.py ic-backtest 分支的消费方式: 排序 + 比较, 不能抛 TypeError
    ranked = sorted(r["factor_ic"].items(), key=lambda x: abs(x[1]), reverse=True)
    for _, ic in ranked:
        _ = "green" if ic > 0.05 else "red" if ic < -0.05 else "yellow"
    # 整个结果可严格序列化 (allow_nan=False 遇 NaN 会抛)
    json.dumps(r, allow_nan=False)


def test_ic_backtest_constant_returns_is_degenerate_not_constant(meta_dir):
    """收益率侧常数 (所有币同涨幅) → 全因子 IC 未定义 → degenerate 但不是
    constant_factors (因子本身有变化, 不应被禁用)."""
    now = _now()
    # 价格统一为 1.0 → 1.1, 收益率在浮点上严格相等 (否则 x*1.1/x 的尾数差会制造伪截面)
    old = [{**c, "price": 1.0} for c in _make_coins(150)]
    _write_snapshot(meta_dir, now - timedelta(days=7), old)
    latest = [{**c, "price": 1.1} for c in old]   # 全部 +10%
    _write_snapshot(meta_dir, now, latest)

    r = run_ic_backtest(lookback_days=7)
    assert r["ok"], r
    assert r["factor_ic"] == {}
    assert set(r["degenerate_factors"]) == set(FACTOR_NAMES)
    assert r["constant_factors"] == []


def test_ic_backtest_naive_timestamp_treated_as_utc(meta_dir):
    """tz-naive 的 timestamp 按 UTC 处理, 不再在 snap_ts - target_date 处 TypeError."""
    now = _now()
    old_ts = now - timedelta(days=7)
    meta_dir.mkdir(parents=True, exist_ok=True)
    snap = {"timestamp": old_ts.replace(tzinfo=None).isoformat(),
            "date": old_ts.strftime("%Y-%m-%d"), "factor_weights": {},
            "coins": _make_coins(150)}
    (meta_dir / f"snapshot_{old_ts.strftime('%Y%m%d_%H%M')}.json").write_text(
        json.dumps(snap), encoding="utf-8")
    _write_snapshot(meta_dir, now, _make_coins(150, seed=5))

    r = run_ic_backtest(lookback_days=7)
    assert r["ok"], r
    assert r["matched_coins"] == 150


def test_ic_backtest_dropout_rate_and_warning(meta_dir, warn_log):
    """旧快照里 30% 的币在 latest 没价格 → dropout_rate≈0.3 且告警."""
    now = _now()
    _write_snapshot(meta_dir, now - timedelta(days=7), _make_coins(200))
    latest = _make_coins(200, seed=3)[:140]   # 丢掉 60 个币
    _write_snapshot(meta_dir, now, latest)

    r = run_ic_backtest(lookback_days=7)
    assert r["ok"], r
    assert abs(r["dropout_rate"] - 0.3) < 1e-6
    assert r["matched_coins"] == 140
    assert any("脱落率" in rec.getMessage() and rec.levelno == logging.WARNING
               for rec in warn_log.records)


# ────────────────────────────────────────────────────────────────────
#  update_weights_from_ic: 死因子禁用/复活, 边界, 当日幂等
# ────────────────────────────────────────────────────────────────────

def _valid_ic(val: float = 0.1) -> dict:
    return {f: val for f in FACTOR_NAMES}


def test_degenerate_constant_factor_auto_disabled_then_reenabled(meta_dir, warn_log):
    _seed_config(meta_dir, history_ic=0.0)
    ic = _valid_ic(0.05)
    ic["f_funding_rate"] = None
    ic["f_dev_activity"] = None   # degenerate 但旧快照非常数 → 不禁用
    r = update_weights_from_ic(_ic_result(ic, constant=["f_funding_rate"]))
    assert r["ok"] and "skipped" not in r
    assert r["disabled"] == ["funding_rate"]

    cfg = load_factor_config()
    fr = cfg["factors"]["funding_rate"]
    assert fr["enabled"] is False and fr["weight"] == 0.0
    assert fr["degenerate_runs"] == 1
    assert cfg["factors"]["dev_activity"]["enabled"] is True
    assert cfg["factors"]["dev_activity"]["degenerate_runs"] == 1
    # degenerate 轮不写 IC 历史 (None 进历史会让报告/均值崩)
    assert all(h["ic"] is not None for h in fr["ic_history"])
    assert len(fr["ic_history"]) == MIN_IC_RECORDS
    assert any("禁用" in rec.getMessage() and rec.levelno == logging.WARNING
               for rec in warn_log.records)

    # 禁用因子不参与归一化: get_current_weights 给 0, 其余 sum=1
    w = get_current_weights(cfg)
    assert w["funding_rate"] == 0.0
    assert abs(sum(w.values()) - 1.0) < 1e-6
    enabled_sum = sum(v["weight"] for k, v in cfg["factors"].items() if v["enabled"])
    assert abs(enabled_sum - 1.0) < 1e-5
    assert generate_factor_report()["factors"]["funding_rate"]["status"] == "disabled"

    # 因子重新有变化 → 以 min 权重复活 (force 绕过当日守卫)
    r2 = update_weights_from_ic(_ic_result(_valid_ic(0.05)), force=True)
    assert r2["ok"] and r2["disabled"] == []
    cfg = load_factor_config()
    fr = cfg["factors"]["funding_rate"]
    assert fr["enabled"] is True
    assert fr["degenerate_runs"] == 0
    assert abs(fr["weight"] - fr["min"]) < 1e-6
    assert get_current_weights(cfg)["funding_rate"] > 0


def test_update_weights_disables_from_backtest_shape_without_none(meta_dir, warn_log):
    """run_ic_backtest 的真实返回形状: 常数因子不在 factor_ic 里, 只出现在
    degenerate_factors / constant_factors → 照样判定死因子并禁用."""
    _seed_config(meta_dir, history_ic=0.0)
    ic = _valid_ic(0.05)
    ic.pop("f_funding_rate")
    ic.pop("f_dev_activity")
    r = update_weights_from_ic(_ic_result(
        ic, degenerate=["f_funding_rate", "f_dev_activity"],
        constant=["f_funding_rate", "f_dev_activity"]))
    assert r["ok"] and "skipped" not in r
    assert set(r["disabled"]) == {"funding_rate", "dev_activity"}
    # 被清零的因子计入 factors_updated (old_weights 在禁用前取)
    assert r["factors_updated"] >= 2
    cfg = load_factor_config()
    for f in ("funding_rate", "dev_activity"):
        assert cfg["factors"][f]["enabled"] is False
        assert cfg["factors"][f]["weight"] == 0.0
        assert cfg["factors"][f]["degenerate_runs"] == 1
    assert r["new_weights"]["funding_rate"] == 0.0
    assert abs(sum(r["new_weights"].values()) - 1.0) < 1e-6


def test_ic_backtest_to_update_weights_end_to_end_disables_dead_factors(meta_dir):
    """快照 → run_ic_backtest → update_weights_from_ic 全链路: 截面常数的
    funding_rate/dev_activity 被禁用, 其余因子权重仍在 [min,max] 且 sum=1."""
    now = _now()
    _write_snapshot(meta_dir, now - timedelta(days=7),
                    _make_coins(150, dead={"f_funding_rate": 0.5, "f_dev_activity": 0.0}))
    _write_snapshot(meta_dir, now, _make_coins(150, seed=5))
    _seed_config(meta_dir, history_ic=0.0)

    r = update_weights_from_ic(run_ic_backtest(lookback_days=7))
    assert r["ok"], r
    assert set(r["disabled"]) == {"funding_rate", "dev_activity"}
    cfg = load_factor_config()
    for fname, fdata in cfg["factors"].items():
        if fdata["enabled"]:
            assert fdata["min"] - 1e-6 <= fdata["weight"] <= fdata["max"] + 1e-6, fname
            assert len(fdata["ic_history"]) == MIN_IC_RECORDS + 1
        else:
            assert fdata["weight"] == 0.0
            assert len(fdata["ic_history"]) == MIN_IC_RECORDS   # degenerate 轮不写历史
    assert abs(sum(r["new_weights"].values()) - 1.0) < 1e-6


def test_degenerate_runs_counts_weight_rounds_not_calls(meta_dir):
    """同日多次调用 (daily-screen / adaptive_trainer / nightly) 只算一轮 degenerate."""
    _seed_config(meta_dir, history_ic=0.0)
    ic = _valid_ic(0.05)
    ic["f_dev_activity"] = None   # degenerate 但非常数 → 不禁用, 只计数
    for _ in range(3):
        update_weights_from_ic(_ic_result(ic))
    cfg = load_factor_config()
    assert cfg["factors"]["dev_activity"]["degenerate_runs"] == 1
    assert cfg["factors"]["dev_activity"]["enabled"] is True
    update_weights_from_ic(_ic_result(ic), force=True)
    assert load_factor_config()["factors"]["dev_activity"]["degenerate_runs"] == 2


def test_weights_within_bounds_after_normalization(meta_dir):
    """乘数 → 归一化 → 投影: 最终权重必须落在 [min,max] 且 sum=1.

    旧实现先 clamp 再归一化, 归一化会把权重推出边界."""
    # 起点故意越界 + 强 IC 把 momentum_30d 往上推
    weights = {k: v["min"] for k, v in DEFAULT_FACTORS.items()}
    weights["momentum_30d"] = 0.9
    _seed_config(meta_dir, history_ic=0.0, weights=weights)
    cfg = load_factor_config()
    cfg["factors"]["momentum_30d"]["ic_history"] = [
        {"date": f"2026-01-{d + 1:02d}", "ic": 0.4, "lookback": 7} for d in range(5)]
    cfg["factors"]["narrative_heat"]["ic_history"] = [
        {"date": f"2026-01-{d + 1:02d}", "ic": -0.4, "lookback": 7} for d in range(5)]
    save_factor_config(cfg)

    r = update_weights_from_ic(_ic_result(_valid_ic(0.1)))
    assert r["ok"]
    cfg = load_factor_config()
    total = 0.0
    for fname, fdata in cfg["factors"].items():
        assert fdata["min"] - 1e-6 <= fdata["weight"] <= fdata["max"] + 1e-6, fname
        total += fdata["weight"]
    assert abs(total - 1.0) < 1e-5
    assert abs(cfg["factors"]["momentum_30d"]["weight"] - 0.30) < 1e-6   # 顶到 max
    nw = r["new_weights"]
    assert abs(sum(nw.values()) - 1.0) < 1e-6


def test_normalize_and_project_excludes_disabled():
    factors = {k: {**v, "enabled": True} for k, v in DEFAULT_FACTORS.items()}
    factors["funding_rate"]["enabled"] = False
    factors["funding_rate"]["weight"] = 0.5   # 应被清零而非参与分配
    factors["tvl_mcap"]["weight"] = 5.0
    assert _normalize_and_project(factors) is True
    assert factors["funding_rate"]["weight"] == 0.0
    enabled = {k: v for k, v in factors.items() if v["enabled"]}
    assert abs(sum(v["weight"] for v in enabled.values()) - 1.0) < 1e-6
    for k, v in enabled.items():
        assert v["min"] - 1e-6 <= v["weight"] <= v["max"] + 1e-6, k
    assert abs(factors["tvl_mcap"]["weight"] - 0.30) < 1e-6


def test_normalize_and_project_infeasible_warns_not_crash(warn_log):
    factors = {"a": {"weight": 0.5, "min": 0.8, "max": 0.9, "enabled": True},
               "b": {"weight": 0.5, "min": 0.8, "max": 0.9, "enabled": True}}
    assert _normalize_and_project(factors) is False
    assert any(rec.levelno == logging.WARNING for rec in warn_log.records)


def test_update_weights_once_per_day_idempotent(meta_dir):
    """同日第二次调用: 只刷新 IC 记录 (同日同 lookback 覆盖), 权重不动."""
    _seed_config(meta_dir, history_ic=0.3)
    # 让 momentum_30d 的历史 IC 与众不同, 否则均匀乘数归一化后权重不变
    cfg = load_factor_config()
    for h in cfg["factors"]["momentum_30d"]["ic_history"]:
        h["ic"] = -0.3
    save_factor_config(cfg)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    r1 = update_weights_from_ic(_ic_result(_valid_ic(0.3)))
    assert r1["ok"] and "skipped" not in r1
    cfg1 = load_factor_config()
    assert cfg1["last_weight_update_date"] == today
    w1 = {k: v["weight"] for k, v in cfg1["factors"].items()}

    # 第二次: 同日同 lookback → 覆盖而非追加; 权重纹丝不动 (不复利)
    r2 = update_weights_from_ic(_ic_result(_valid_ic(0.25)))
    assert r2["ok"] and r2["skipped"] == "already_updated_today"
    assert r2["factors_updated"] == 0 and r2["disabled"] == []
    cfg2 = load_factor_config()
    assert {k: v["weight"] for k, v in cfg2["factors"].items()} == w1
    hist = cfg2["factors"]["momentum_30d"]["ic_history"]
    todays = [h for h in hist if h["date"] == today and h["lookback"] == 7]
    assert len(todays) == 1 and todays[0]["ic"] == 0.25
    assert len(hist) == MIN_IC_RECORDS + 1

    # 不同 lookback 的 IC 记录仍追加 (但权重依旧不动)
    r3 = update_weights_from_ic(_ic_result(_valid_ic(0.2), lookback=14))
    assert r3["skipped"] == "already_updated_today"
    cfg3 = load_factor_config()
    hist = cfg3["factors"]["momentum_30d"]["ic_history"]
    assert len(hist) == MIN_IC_RECORDS + 2
    assert {k: v["weight"] for k, v in cfg3["factors"].items()} == w1

    # force=True 绕过守卫, 权重重新计算
    r4 = update_weights_from_ic(_ic_result(_valid_ic(0.3)), force=True)
    assert r4["ok"] and "skipped" not in r4
    w4 = {k: v["weight"] for k, v in load_factor_config()["factors"].items()}
    assert w4 != w1


def test_update_weights_rejects_failed_ic_result(meta_dir):
    r = update_weights_from_ic({"ok": False, "error": "x"})
    assert r == {"ok": False, "error": "x"}


# ────────────────────────────────────────────────────────────────────
#  load_factor_config: 旧 config 没有 enabled 字段
# ────────────────────────────────────────────────────────────────────

def test_load_old_config_without_enabled_backfills(meta_dir):
    meta_dir.mkdir(parents=True)
    old = {"version": 1, "regime": "unknown", "factors": {
        "momentum_30d": {"weight": 0.4, "min": 0.05, "max": 0.30, "ic_history": []},
        "funding_rate": {"weight": 0.6, "min": 0.02, "max": 0.15,
                         "ic_history": [{"date": "2026-06-18", "ic": -0.3, "lookback": 7}]},
    }}
    (meta_dir / "factor_config.json").write_text(json.dumps(old), encoding="utf-8")

    cfg = load_factor_config()
    assert all(f.get("enabled") is True for f in cfg["factors"].values())
    # 旧字段保留, 新因子合并进来并带 enabled
    assert cfg["factors"]["funding_rate"]["ic_history"][0]["ic"] == -0.3
    assert cfg["factors"]["tvl_mcap"]["enabled"] is True
    assert "last_weight_update_date" not in cfg   # 首次调权前不存在 → 不触发守卫
    w = get_current_weights(cfg)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert generate_factor_report()["factors"]["momentum_30d"]["enabled"] is True


# ────────────────────────────────────────────────────────────────────
#  单一权重写入方 + 人工锁定
# ────────────────────────────────────────────────────────────────────

def test_non_writer_does_not_touch_weights(meta_dir, monkeypatch):
    """未设置 WEIGHT_WRITER_ENV 的环境 (本地 launchd) 不改写 factor_config,
    force 也不能绕过, 避免本地与云端两套权重分叉."""
    _seed_config(meta_dir, history_ic=0.3)
    cfg_path = meta_dir / "factor_config.json"
    before = cfg_path.read_text(encoding="utf-8")

    monkeypatch.delenv(ml.WEIGHT_WRITER_ENV, raising=False)
    assert ml.is_weight_writer() is False
    for force in (False, True):
        r = update_weights_from_ic(_ic_result(_valid_ic(0.3)), force=force)
        assert r["ok"] and r["skipped"] == "not_weight_writer"
        assert r["factors_updated"] == 0
        assert abs(sum(r["new_weights"].values()) - 1.0) < 1e-6
    assert cfg_path.read_text(encoding="utf-8") == before

    for val in ("0", "", "no"):
        monkeypatch.setenv(ml.WEIGHT_WRITER_ENV, val)
        assert ml.is_weight_writer() is False
    for val in ("1", "true", "YES"):
        monkeypatch.setenv(ml.WEIGHT_WRITER_ENV, val)
        assert ml.is_weight_writer() is True


def test_locked_factor_stays_off_even_with_valid_ic(meta_dir):
    """locked=True 的因子 (人工关闭: 方向为负) 即使拿到有效 IC 也不复活,
    权重恒为 0 且不参与归一化; 其余因子仍满足 [min,max] 与 sum=1."""
    _seed_config(meta_dir, history_ic=0.05)
    cfg = load_factor_config()
    vt = cfg["factors"]["volume_turnover"]
    vt.update({"enabled": False, "locked": True, "weight": 0.0})
    save_factor_config(cfg)

    r = update_weights_from_ic(_ic_result(_valid_ic(0.2)))
    assert r["ok"] and "skipped" not in r
    assert "volume_turnover" in r["disabled"]

    cfg = load_factor_config()
    vt = cfg["factors"]["volume_turnover"]
    assert vt["enabled"] is False and vt["locked"] is True and vt["weight"] == 0.0
    w = get_current_weights(cfg)
    assert w["volume_turnover"] == 0.0
    assert abs(sum(w.values()) - 1.0) < 1e-6
    for k, v in cfg["factors"].items():
        if v.get("enabled", True):
            assert v["min"] - 1e-6 <= v["weight"] <= v["max"] + 1e-6, k
    assert generate_factor_report()["factors"]["volume_turnover"]["status"] == "locked"

    # 第二天再调一次 (force) 仍然关闭
    r2 = update_weights_from_ic(_ic_result(_valid_ic(0.2)), force=True)
    assert load_factor_config()["factors"]["volume_turnover"]["weight"] == 0.0
    assert "volume_turnover" in r2["disabled"]
