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


def _hist(ic: float, n: int, start_age: int = 1, step: int = 1,
          schema: int = 2, lookback: int = 7) -> list[dict]:
    """n 条带来源标签的 IC 记录, 日期为今天往前 start_age, start_age+step, ... 天."""
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(n):
        d = (today - timedelta(days=start_age + i * step)).isoformat()
        out.append({"date": d, "ic": ic, "lookback": lookback, "asof": f"seed_{d}",
                    "base": "seed_base", "base_schema": schema, "n": 300})
    return list(reversed(out))   # 旧 → 新, 与真实追加顺序一致


def _seed_config(meta_dir, history_ic: float = 0.0, n_hist: int = MIN_IC_RECORDS,
                 weights: dict | None = None, enabled_override: dict | None = None):
    """写一份带 IC 历史的 factor_config (合格记录: schema 2, 最近几天, 不与今天冲突)."""
    cfg = load_factor_config()
    for fname, fdata in cfg["factors"].items():
        fdata["ic_history"] = _hist(history_ic, n_hist)
        if weights and fname in weights:
            fdata["weight"] = weights[fname]
        if enabled_override and fname in enabled_override:
            fdata["enabled"] = enabled_override[fname]
    save_factor_config(cfg)
    return cfg


def _ic_result(factor_ic: dict, constant: list | None = None, lookback: int = 7,
               degenerate: list | None = None, low_coverage: list | None = None,
               latest_file: str | None = None, latest_date: str | None = None,
               base_schema: int = 2):
    r = {"ok": True, "lookback_days": lookback, "factor_ic": factor_ic,
         "degenerate_factors": degenerate or [],
         "constant_factors": constant or [],
         "low_coverage_factors": low_coverage or [],
         "base_schema": base_schema, "snapshot_file": "base.json", "matched_coins": 300}
    if latest_file:
        r["latest_file"] = latest_file
    if latest_date:
        r["latest_date"] = latest_date
    return r


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
    # 启用因子投影到 prior 之和 (关掉的因子带走自己的 prior, 不把权重挤给别人)
    enabled_sum = sum(v["weight"] for k, v in cfg["factors"].items() if v["enabled"])
    assert abs(enabled_sum - ml._prior_mass(cfg["factors"])) < 1e-5
    assert cfg["factors"]["funding_rate"]["disabled_reason"] == "constant"
    assert generate_factor_report()["factors"]["funding_rate"]["status"] == "disabled"

    # 因子重新有变化 → 以 min 权重复活 (force 绕过当日守卫)
    r2 = update_weights_from_ic(_ic_result(_valid_ic(0.05)), force=True)
    assert r2["ok"] and r2["disabled"] == []
    cfg = load_factor_config()
    fr = cfg["factors"]["funding_rate"]
    assert fr["enabled"] is True
    assert fr["degenerate_runs"] == 0
    assert "disabled_reason" not in fr
    # 以 min 复活, 同一轮再向 prior 平滑靠拢一步 (α = 1/lookback): min ≤ w < prior
    assert fr["min"] - 1e-6 <= fr["weight"] < fr["prior"]
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
                    _make_coins(150, dead={"f_funding_rate": 0.5, "f_dev_activity": 0.0}),
                    meta={"schema": 2})
    _write_snapshot(meta_dir, now, _make_coins(150, seed=5), meta={"schema": 2})
    _seed_config(meta_dir, history_ic=0.0)

    ic = run_ic_backtest(lookback_days=7)
    assert ic["base_schema"] == 2 and ic["latest_file"].startswith("snapshot_")
    r = update_weights_from_ic(ic)
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
    # 新记录带来源标签
    h = cfg["factors"]["momentum_30d"]["ic_history"][-1]
    assert h["asof"] == ic["latest_file"] and h["base_schema"] == 2 and h["n"] == 150


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


def test_update_weights_once_per_snapshot_idempotent(meta_dir):
    """同一价格快照第二次调用: 只刷新 IC 记录 (同快照同 lookback 覆盖), 权重不动."""
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
    assert r2["ok"] and r2["skipped"] == "already_updated_for_snapshot"
    assert r2["factors_updated"] == 0 and r2["disabled"] == []
    cfg2 = load_factor_config()
    assert {k: v["weight"] for k, v in cfg2["factors"].items()} == w1
    hist = cfg2["factors"]["momentum_30d"]["ic_history"]
    todays = [h for h in hist if h["date"] == today and h["lookback"] == 7]
    assert len(todays) == 1 and todays[0]["ic"] == 0.25
    assert len(hist) == MIN_IC_RECORDS + 1

    # 不同 lookback 的 IC 记录仍追加 (但权重依旧不动)
    r3 = update_weights_from_ic(_ic_result(_valid_ic(0.2), lookback=14))
    assert r3["skipped"] == "already_updated_for_snapshot"
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
    # 新因子合并进来并带 enabled; 不带来源标签的旧 IC 记录在 v2 迁移时归档, 不再参与学习
    assert cfg["factors"]["funding_rate"]["ic_history"] == []
    assert cfg["ic_history_archive"]["funding_rate"][0]["ic"] == -0.3
    assert cfg["version"] == ml.CONFIG_VERSION and "regime" not in cfg
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



# ────────────────────────────────────────────────────────────────────
#  2026-10-20 审计: 学习规则重写的回归测试
# ────────────────────────────────────────────────────────────────────

def _v1_config_like_production() -> dict:
    """2026-10-18 线上 factor_config 的缩影: 6 月的截断记录 + 10 月记录, 都没有来源标签,
    动量被压到下限, 外加一个从未写过的 regime 字段."""
    june = [{"date": f"2026-06-{d:02d}", "ic": -0.35, "lookback": 7} for d in range(11, 19)]
    octo = [{"date": f"2026-10-{d:02d}", "ic": -0.04, "lookback": 7} for d in range(13, 19)]
    factors = {}
    for fname, fdef in DEFAULT_FACTORS.items():
        factors[fname] = {**fdef, "ic_history": june + octo, "degenerate_runs": 0}
    factors["momentum_30d"]["weight"] = 0.05
    factors["momentum_7d"]["weight"] = 0.03
    factors["volume_turnover"].update(enabled=False, locked=True, weight=0.0)
    factors["dev_activity"].update(enabled=False, weight=0.0, ic_history=[])
    return {"version": 1, "regime": "unknown", "factors": factors,
            "last_weight_update_date": "2026-10-18"}


def test_v2_migration_archives_untagged_ic_and_resets_to_priors(meta_dir):
    meta_dir.mkdir(parents=True)
    (meta_dir / "factor_config.json").write_text(
        json.dumps(_v1_config_like_production()), encoding="utf-8")

    cfg = load_factor_config()   # 测试进程是写入方 → 迁移落盘
    assert cfg["version"] == ml.CONFIG_VERSION and "regime" not in cfg
    on_disk = json.loads((meta_dir / "factor_config.json").read_text(encoding="utf-8"))
    assert on_disk["version"] == ml.CONFIG_VERSION
    for fname, fdata in cfg["factors"].items():
        assert fdata["ic_history"] == [], fname          # 旧记录不再参与学习
        assert "prior" in fdata
    assert len(cfg["ic_history_archive"]["momentum_30d"]) == 14
    vt, dev = cfg["factors"]["volume_turnover"], cfg["factors"]["dev_activity"]
    assert vt["disabled_reason"] == "locked" and vt["weight"] == 0.0
    assert dev["disabled_reason"] == "constant" and dev["weight"] == 0.0
    # 启用因子重置到 2026-10-12 的 prior (被污染的漂移撤销): 相对比例与 prior 一致
    w = get_current_weights(cfg)
    pri = {k: ml.MIGRATION_PRIORS[k] for k in w if w[k] > 0}
    tot = sum(pri.values())
    for k, p in pri.items():
        assert abs(w[k] - p / tot) < 2e-3, k
    assert w["momentum_30d"] > 0.08                      # 不再钉在下限 0.05

    # 第二次加载不会再迁移 / 再追加归档
    cfg2 = load_factor_config()
    assert len(cfg2["ic_history_archive"]["momentum_30d"]) == 14
    assert len(cfg2.get("migrations", [])) == 1


def test_v2_migration_in_memory_only_for_non_writer(meta_dir, monkeypatch):
    meta_dir.mkdir(parents=True)
    path = meta_dir / "factor_config.json"
    path.write_text(json.dumps(_v1_config_like_production()), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    monkeypatch.delenv(ml.WEIGHT_WRITER_ENV, raising=False)
    cfg = load_factor_config()
    assert cfg["version"] == ml.CONFIG_VERSION          # 读方看到同样的视图
    assert path.read_text(encoding="utf-8") == before   # 但不落盘 (单一写入方)


def test_eligible_ic_filters_schema_age_and_lookback():
    today = datetime.now(timezone.utc).date()
    d = lambda n: (today - timedelta(days=n)).isoformat()
    hist = [
        {"date": d(1), "ic": 0.1, "lookback": 7, "base_schema": 2},
        {"date": d(2), "ic": 0.2, "lookback": 7, "base_schema": 1},    # 截断快照
        {"date": d(3), "ic": 0.3, "lookback": 7},                      # 无标签 (旧)
        {"date": d(4), "ic": 0.4, "lookback": 14, "base_schema": 2},   # 别的周期
        {"date": d(40), "ic": 0.5, "lookback": 7, "base_schema": 2},   # 太老
        {"date": d(5), "ic": None, "lookback": 7, "base_schema": 2},
        {"date": "garbage", "ic": 0.6, "lookback": 7, "base_schema": 2},
        {"date": d(0), "ic": -0.1, "lookback": 7, "base_schema": 2},
    ]
    assert ml._eligible_ic(hist, 7, today) == [(0, -0.1), (1, 0.1)]


def test_ic_signal_needs_records_shrinks_by_overlap_and_floors():
    assert ml._ic_signal([(0, 0.3), (1, 0.3)], 7)["signal"] == 0.0     # 记录不足
    week = [(i, 0.1) for i in range(7)]           # 一周的重叠日记录 ≈ 1.5 个独立观测
    s = ml._ic_signal(week, 7)
    assert s["avg_ic"] == 0.1 and 1.4 < s["n_eff"] < 1.55
    assert 0.03 < s["signal"] < 0.04              # 收缩约 0.38
    month = [(i, 0.1) for i in range(29)]
    assert ml._ic_signal(month, 7)["signal"] > s["signal"]   # 证据越多收缩越少
    tiny = [(i, 0.03) for i in range(7)]
    assert ml._ic_signal(tiny, 7)["signal"] == 0.0           # 收缩后低于地板 → 噪声


def _daily_updates(meta_dir, ic_by_factor: dict, days: int):
    """模拟 nightly: 每天一个新价格快照, 用当天 IC 调一次权重."""
    today = datetime.now(timezone.utc).date()
    r = None
    for i in range(days):
        day = (today - timedelta(days=days - 1 - i)).isoformat()
        ic = {f"f_{k}": (v(i) if callable(v) else v)
              for k in DEFAULT_FACTORS for v in [ic_by_factor.get(k, 0.0)]}
        r = update_weights_from_ic(_ic_result(ic, latest_file=f"snapshot_{day}.json",
                                              latest_date=day))
        assert r["ok"] and "skipped" not in r, r
    return r


def test_persistent_mild_negative_ic_does_not_pin_weight_to_min(meta_dir):
    """旧规则每天复利 (1+0.5×IC): IC 均值 -0.05 时 30 天把权重压到 min。
    新规则收敛到 prior×(1+K×收缩信号), 并在那里停住。
    (IC 带噪声 ±0.15: 不重叠窗口 t≈-1, 达不到自动关闭的 -2)"""
    load_factor_config()
    noisy = lambda i: -0.05 + (0.15 if i % 2 == 0 else -0.15)
    r = _daily_updates(meta_dir, {"momentum_30d": noisy}, days=30)
    cfg = load_factor_config()
    m30 = cfg["factors"]["momentum_30d"]
    assert m30["weight"] > m30["min"] + 0.03
    assert m30["weight"] < m30["prior"]          # 方向对: 低于 prior
    w_before = m30["weight"]
    _daily_updates(meta_dir, {"momentum_30d": noisy}, days=5)
    # 稳态附近只做小幅调整, 不会继续单向漂移
    assert abs(load_factor_config()["factors"]["momentum_30d"]["weight"] - w_before) < 0.02
    assert r["signals"]["momentum_30d"]["signal"] < 0
    assert m30["enabled"] is True


def test_persistent_positive_ic_raises_weight_above_prior(meta_dir):
    load_factor_config()
    _daily_updates(meta_dir, {"tvl_mcap": 0.12}, days=30)
    cfg = load_factor_config()
    tvl = cfg["factors"]["tvl_mcap"]
    w = get_current_weights(cfg)
    pri = {k: v["prior"] for k, v in cfg["factors"].items() if v["enabled"]}
    assert w["tvl_mcap"] > pri["tvl_mcap"] / sum(pri.values()) + 0.02
    assert tvl["min"] - 1e-6 <= tvl["weight"] <= tvl["max"] + 1e-6


def test_guard_is_keyed_on_snapshot_not_utc_date(meta_dir):
    """夜间任务被延迟到 UTC 次日: 同一 UTC 日期的两个不同快照各调一次权重."""
    _seed_config(meta_dir, history_ic=0.0)
    today = datetime.now(timezone.utc).date().isoformat()
    r1 = update_weights_from_ic(_ic_result(_valid_ic(0.1), latest_file="snapshot_a.json",
                                           latest_date=today))
    r2 = update_weights_from_ic(_ic_result(_valid_ic(0.1), latest_file="snapshot_b.json",
                                           latest_date=today))
    r3 = update_weights_from_ic(_ic_result(_valid_ic(0.1), latest_file="snapshot_b.json",
                                           latest_date=today))
    assert "skipped" not in r1 and "skipped" not in r2
    assert r3["skipped"] == "already_updated_for_snapshot"
    hist = load_factor_config()["factors"]["momentum_30d"]["ic_history"]
    assert [h["asof"] for h in hist[-2:]] == ["snapshot_a.json", "snapshot_b.json"]


def test_low_coverage_factor_flagged_disabled_and_revived(meta_dir):
    """onchain_activity 只有极少数币非零 → 低覆盖: 不进 factor_ic, 被关闭; 覆盖恢复后复活."""
    now = _now()
    old = _make_coins(150)
    for i, c in enumerate(old):
        c["f_onchain_activity"] = round(0.1 * (i + 1), 3) if i < 8 else 0.0
    _write_snapshot(meta_dir, now - timedelta(days=7), old, meta={"schema": 2})
    _write_snapshot(meta_dir, now, _make_coins(150, seed=5), meta={"schema": 2})
    ic = run_ic_backtest(lookback_days=7)
    assert ic["low_coverage_factors"] == ["f_onchain_activity"]
    assert "f_onchain_activity" not in ic["factor_ic"]
    assert ic["coverage"]["f_onchain_activity"] == 8

    _seed_config(meta_dir, history_ic=0.0)
    r = update_weights_from_ic(ic)
    oc = load_factor_config()["factors"]["onchain_activity"]
    assert oc["enabled"] is False and oc["disabled_reason"] == "low_coverage"
    assert r["disabled_reasons"]["onchain_activity"] == "low_coverage"

    update_weights_from_ic(_ic_result(_valid_ic(0.05), latest_file="snapshot_next.json"))
    oc = load_factor_config()["factors"]["onchain_activity"]
    assert oc["enabled"] is True and "disabled_reason" not in oc


def test_disabling_factors_keeps_relative_weights_of_the_rest():
    """关掉两个低覆盖因子后, 剩余因子保持 prior 的相对比例, 不把多出的权重挤进动量."""
    factors = {k: {**v, "prior": ml.MIGRATION_PRIORS.get(k, v["weight"])}
               for k, v in DEFAULT_FACTORS.items()}
    for k in ("volume_turnover", "dev_activity", "funding_rate",
              "onchain_activity", "narrative_heat"):
        factors[k]["enabled"] = False
    for k, v in factors.items():
        v["weight"] = v["prior"] if v["enabled"] else 0.0
    assert _normalize_and_project(factors, total=ml._prior_mass(factors))
    w = get_current_weights({"factors": factors})
    assert w["momentum_30d"] < 0.15 and w["momentum_7d"] < 0.09
    assert abs(w["ath_drawdown"] / w["tvl_mcap"] - 0.186187 / 0.170568) < 1e-3
    # 对照: 旧做法归一化到 1 会把 ath/size/tvl 顶到上限, 动量合计 > 35%
    legacy = {k: dict(v) for k, v in factors.items()}
    _normalize_and_project(legacy, total=1.0)
    lw = get_current_weights({"factors": legacy})
    assert lw["momentum_30d"] + lw["momentum_7d"] > 0.33


def test_negative_ic_is_flagged_not_disabled_after_persistent_evidence(meta_dir):
    """持续负 IC (全部相位、连续 7 次调权) → negative_ic_flag 提醒人工; 因子不被自动关闭,
    只是目标权重降向 min (复核: 自动关闭在纯噪声下会让因子反复开关)."""
    _seed_config(meta_dir, history_ic=0.0)
    cfg = load_factor_config()
    hist = []
    for age in range(1, 56):
        hist += _hist(-0.15 + 0.03 * math.sin(age), 1, start_age=age)
    cfg["factors"]["narrative_heat"]["ic_history"] = sorted(hist, key=lambda h: h["date"])
    save_factor_config(cfg)
    today = datetime.now(timezone.utc).date().isoformat()
    ic = _valid_ic(0.0)
    ic["f_narrative_heat"] = -0.15
    flags = []
    for i in range(8):
        r = update_weights_from_ic(_ic_result(ic, latest_file=f"snap_neg_{i}.json",
                                              latest_date=today))
        flags.append("narrative_heat" in r["negative_ic_flags"])
    assert flags == [False] * 6 + [True, True]          # 连续 lookback=7 次才提醒
    nh = load_factor_config()["factors"]["narrative_heat"]
    assert nh["enabled"] is True and nh["negative_ic_flag"] is True
    assert nh["weight"] < nh["prior"]                    # 目标规则把它压向 min
    rep = generate_factor_report()["factors"]["narrative_heat"]
    assert rep["negative_ic_flag"] is True

    # 证据消失 → 提醒撤销
    cfg = load_factor_config()
    cfg["factors"]["narrative_heat"]["ic_history"] = _hist(0.02, 30)
    save_factor_config(cfg)
    ic["f_narrative_heat"] = 0.02
    r = update_weights_from_ic(_ic_result(ic, latest_file="snap_pos.json", latest_date=today))
    assert "narrative_heat" not in r["negative_ic_flags"]


def test_zero_mean_autocorrelated_noise_never_flags_or_disables(meta_dir):
    """60 天零均值、按 7 日重叠相关的 IC 噪声: 不应出现负 IC 提醒或任何自动关闭."""
    import random
    load_factor_config()
    rng = random.Random(20261020)
    shocks = {k: [rng.gauss(0, 0.1) for _ in range(70)] for k in DEFAULT_FACTORS}

    def series(k):
        # 7 日前瞻 IC = 最近 7 个日冲击的均值 × √7 → 相邻两天相关 6/7, sd ≈ 0.1
        return lambda i: sum(shocks[k][i:i + 7]) / math.sqrt(7)

    r = _daily_updates(meta_dir, {k: series(k) for k in DEFAULT_FACTORS}, days=60)
    cfg = load_factor_config()
    assert r["negative_ic_flags"] == []
    assert all(v["enabled"] for k, v in cfg["factors"].items())
    w = get_current_weights(cfg)
    pri = {k: v["prior"] for k, v in cfg["factors"].items()}
    for k in w:   # 无信息时权重在 prior 附近 (±50%)
        assert 0.5 < w[k] / (pri[k] / sum(pri.values())) < 1.5, k


def test_unlocking_a_factor_revives_it(meta_dir):
    """locked 改回 false (lock_reason 里写的操作) → 下一轮以 min 复活."""
    _seed_config(meta_dir, history_ic=0.0)
    cfg = load_factor_config()
    cfg["factors"]["volume_turnover"].update(enabled=False, locked=True, weight=0.0)
    save_factor_config(cfg)
    update_weights_from_ic(_ic_result(_valid_ic(0.05), latest_file="s1.json"))
    vt = load_factor_config()["factors"]["volume_turnover"]
    assert vt["enabled"] is False and vt["disabled_reason"] == "locked"
    cfg = load_factor_config()
    cfg["factors"]["volume_turnover"]["locked"] = False
    save_factor_config(cfg)
    update_weights_from_ic(_ic_result(_valid_ic(0.05), latest_file="s2.json"))
    vt = load_factor_config()["factors"]["volume_turnover"]
    assert vt["enabled"] is True and "disabled_reason" not in vt and vt["weight"] > 0


def test_migration_gives_priors_headroom_on_both_sides(meta_dir):
    meta_dir.mkdir(parents=True)
    (meta_dir / "factor_config.json").write_text(
        json.dumps(_v1_config_like_production()), encoding="utf-8")
    cfg = load_factor_config()
    for k, v in cfg["factors"].items():
        if v["enabled"]:
            assert v["min"] <= 0.5 * v["prior"] + 1e-9 or v["min"] < v["prior"], k
            assert v["max"] >= 1.5 * v["prior"] - 1e-9, k
    assert cfg["factors"]["market_cap_size"]["max"] >= 0.225


def test_factor_report_shows_effective_weights(meta_dir):
    _seed_config(meta_dir, history_ic=0.0)
    cfg = load_factor_config()
    for k in ("onchain_activity", "narrative_heat"):
        cfg["factors"][k].update(enabled=False, weight=0.0, disabled_reason="low_coverage")
    _normalize_and_project(cfg["factors"], total=ml._prior_mass(cfg["factors"]))
    save_factor_config(cfg)
    rep = generate_factor_report()["factors"]
    eff = get_current_weights()
    assert abs(sum(f["weight"] for f in rep.values()) - 1.0) < 1e-5
    for k, f in rep.items():
        assert abs(f["weight"] - eff[k]) < 1e-6
    assert sum(f["weight_raw"] for f in rep.values()) < 0.95   # 存储值是 prior 尺度


def test_ic_backtest_matches_by_coin_id_not_symbol(meta_dir):
    """VELO 同时是 velodrome-finance 和 velo: 不能拿另一个币的价格算收益."""
    now = _now()
    old = _make_coins(150)
    old[0].update(symbol="VELO", coin_id="velodrome-finance", price=0.035)
    old[1].update(symbol="VELO", coin_id="velo", price=0.004)
    new = _make_coins(150)
    new[0].update(symbol="VELO", coin_id="velo", price=0.0041)    # velodrome 掉出了新快照
    new = [new[0]] + new[2:]
    _write_snapshot(meta_dir, now - timedelta(days=7), old, meta={"schema": 2})
    _write_snapshot(meta_dir, now, new, meta={"schema": 2})
    r = run_ic_backtest(lookback_days=7)
    assert r["ok"]
    # velodrome 无价可配 → 脱落 (1/150), 而不是被记成 -88%
    assert r["matched_coins"] == 149

    lookup = ml._price_lookup([{"symbol": "ABC", "price": 1.0},
                               {"symbol": "ABC", "price": 2.0},
                               {"symbol": "XYZ", "price": 3.0}])
    assert lookup({"symbol": "ABC"}) is None          # 无 coin_id 且 symbol 不唯一 → 跳过
    assert lookup({"symbol": "XYZ"}) == 3.0


def test_detect_regime_leftover_is_sideways_and_hysteresis():
    from src.research.meta_learner import detect_regime
    assert detect_regime({"change_30d": 9.0, "change_7d": 1.0}) == "sideways"   # 曾被叫成高波动
    assert detect_regime({"change_30d": 10.0, "change_7d": -5.0}) == "volatile"
    assert detect_regime({"change_30d": 20.0, "change_7d": 5.0}) == "bull"
    assert detect_regime({}) == "unknown"
    # 贴着阈值的小抖动不切换
    assert detect_regime({"change_30d": 2.0, "change_7d": 5.2},
                         prev_regime="sideways") == "sideways"
    assert detect_regime({"change_30d": 2.0, "change_7d": 4.8},
                         prev_regime="volatile") == "volatile"
    # 明确越过阈值才切换
    assert detect_regime({"change_30d": 2.0, "change_7d": 7.0},
                         prev_regime="sideways") == "volatile"
    # 当前读数离上一个 regime 很远 → 直接切换, 即使它靠近别的阈值 (复核发现的 bug)
    assert detect_regime({"change_30d": -14.5, "change_7d": -4.0},
                         prev_regime="bull") != "bull"
    assert detect_regime({"change_30d": -15.6, "change_7d": -10.0},
                         prev_regime="sideways") == "bear"


def test_factor_report_regime_from_latest_real_snapshot(meta_dir):
    now = _now()
    _write_snapshot(meta_dir, now - timedelta(hours=2), _make_coins(150),
                    meta={"schema": 2, "regime": "bear"})
    _write_snapshot(meta_dir, now, _make_coins(150), meta={"synthetic": True, "regime": "bull"},
                    name="snapshot_29990101_0000.json")
    rep = generate_factor_report()
    assert rep["regime"] == "bear"
    assert generate_factor_report(regime="bull")["regime"] == "bull"
    assert rep["factors"]["momentum_30d"]["status"] == "insufficient_data"


# ────────────────────────────────────────────────────────────────────
#  影响力份额 / 影响力上限
# ────────────────────────────────────────────────────────────────────

def _binary_vs_continuous(n: int) -> list[dict]:
    """f_tvl_mcap 一半 1.0 一半 0 (二值); f_momentum_30d 在 [0,1] 均匀铺开."""
    return [{"f_tvl_mcap": 1.0 if i % 2 == 0 else 0.0,
             "f_momentum_30d": ((i * 37) % n) / n} for i in range(n)]


def test_influence_shares_sum_to_one_and_skip_zero_weight():
    coins = _binary_vs_continuous(400)
    s = ml.influence_shares(coins, {"momentum_30d": 0.5, "tvl_mcap": 0.5, "dev_activity": 0.0})
    assert set(s) == {"momentum_30d", "tvl_mcap"}          # 权重 0 的因子不计
    assert abs(sum(s.values()) - 1.0) < 1e-9
    assert s["tvl_mcap"] > 0.7                              # 二值因子名义权重一半却占大头
    assert ml.influence_shares([], {"tvl_mcap": 1.0}) == {"tvl_mcap": 0.0}
    const = [{"f_tvl_mcap": 1.0} for _ in range(10)]
    assert ml.influence_shares(const, {"tvl_mcap": 1.0}) == {"tvl_mcap": 0.0}


def test_cap_influence_binds_and_keeps_total():
    coins = _binary_vs_continuous(400)
    w0 = {"momentum_30d": 0.5, "tvl_mcap": 0.5, "dev_activity": 0.0}
    w, log_ = ml.cap_influence(coins, w0, cap=0.55)
    assert set(log_) == {"tvl_mcap"} and log_["tvl_mcap"][0] == 0.5
    assert w["tvl_mcap"] < 0.5 and w["dev_activity"] == 0.0
    assert abs(sum(w.values()) - 1.0) < 1e-5
    assert max(ml.influence_shares(coins, w).values()) <= 0.55 + 1e-3
    # 原权重总和不是 1 时保持原总和 (只改相对比例, 综合分量级不变)
    w2, _ = ml.cap_influence(coins, {"momentum_30d": 0.3, "tvl_mcap": 0.3}, cap=0.55)
    assert abs(sum(w2.values()) - 0.6) < 1e-5


def test_cap_influence_noop_small_universe_or_balanced():
    coins = _binary_vs_continuous(400)
    w0 = {"momentum_30d": 0.5, "tvl_mcap": 0.5}
    # 小样本 (旧截断宇宙 / 测试) 不适用
    w, log_ = ml.cap_influence(coins[:200], w0, cap=0.55)
    assert w == w0 and log_ == {}
    # 没越线: 原样返回
    balanced = {"momentum_30d": 0.62, "tvl_mcap": 0.38}
    assert max(ml.influence_shares(coins, balanced).values()) < 0.55
    w, log_ = ml.cap_influence(coins, balanced, cap=0.55)
    assert w == balanced and log_ == {}


def test_cap_influence_gives_up_when_no_progress(warn_log):
    """只有一个因子有截面差异时压权重没用: 不空转 60 轮, 原样返回且不记成"已触发"."""
    coins = _binary_vs_continuous(400)
    for c in coins:
        c["f_dev_activity"] = 0.0          # 常数因子, 方差为 0
    for w0 in ({"tvl_mcap": 1.0, "momentum_30d": 0.0},
               {"tvl_mcap": 0.5, "dev_activity": 0.5}):
        w, log_ = ml.cap_influence(coins, w0, cap=0.55)
        assert w == w0 and log_ == {}
    assert any("无法生效" in r.getMessage() for r in warn_log.records)


def test_cap_influence_logs_skip_on_small_universe(warn_log):
    ml.cap_influence(_binary_vs_continuous(250), {"tvl_mcap": 0.5, "momentum_30d": 0.5})
    assert any("影响力上限跳过" in r.getMessage() for r in warn_log.records)
