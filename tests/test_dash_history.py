"""pages/9 历史复盘的纯函数 (src.research.dashboard) 单测。

回归 (C14):
  * load_snapshots 不过滤 backfill 合成快照 (*_0000, 18-29 币, 3 因子): 242 个日期里 99 个
    是合成的, Top-30 覆盖整个合成宇宙 → 命中率按构造约 50%, 累计曲线被抬高。
  * exit_prices / per_coin 按 symbol 建 dict: 两个 VELO (velodrome-finance / velo) 后者覆盖
    前者, rank 16 的行被 rank 57 顶掉; 名次换位时还会拿另一个币的出场价 (-90%)。
  * 累计命中率按 top_n 加权, 快照币数不足 top_n 时分母偏大。
全部用 tmp_path / 内存 dict, 不读真实 data/。
"""
from __future__ import annotations
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import dashboard as dash


def _row(sym, price, score, coin_id=None, mcap=None, **extra):
    r = {"symbol": sym, "price": price, "composite_score": score}
    if coin_id is not None:
        r["coin_id"] = coin_id
    if mcap is not None:
        r["market_cap"] = mcap
    r.update(extra)
    return r


def _snap(date, coins, **meta):
    return {"date": date, "coins": coins, "_meta": dict(meta)}


# ────────────────────────────────────────────────────────────────────
#  load_real_snapshots
# ────────────────────────────────────────────────────────────────────

def _write(meta_dir: pathlib.Path, name: str, date: str, n: int, **meta) -> None:
    coins = [_row(f"C{i}", 1.0 + i, 1.0 - i * 0.001, f_momentum_7d=0.1) for i in range(n)]
    data = {"timestamp": f"{date}T22:00:00+00:00", "date": date,
            "factor_weights": {}, "_meta": meta, "coins": coins}
    (meta_dir / name).write_text(json.dumps(data), encoding="utf-8")


def test_load_real_snapshots_drops_synthetic(tmp_path):
    _write(tmp_path, "snapshot_20261011_0000.json", "2026-10-11", 29,
           source="backfill", synthetic=True)                      # 合成: _0000
    _write(tmp_path, "snapshot_20261011_2250.json", "2026-10-11", 200)  # 真实 schema 1
    _write(tmp_path, "snapshot_20261012_0000.json", "2026-10-12", 29)   # 合成 (文件名)
    _write(tmp_path, "snapshot_20261013_0117.json", "2026-10-13", 150,
           synthetic=True)                                          # _meta 标合成
    _write(tmp_path, "snapshot_20261014_2215.json", "2026-10-14", 30)   # 币数不足
    _write(tmp_path, "snapshot_20261015_2207.json", "2026-10-15", 400,
           schema=2, truncated=False, regime="sideways")
    (tmp_path / "snapshot_20261016_2205.json").write_text("{not json", encoding="utf-8")

    snaps = dash.load_real_snapshots(tmp_path)
    assert [s["_file"] for s in snaps] == ["snapshot_20261011_2250.json",
                                           "snapshot_20261015_2207.json"]
    assert [dash.is_full_universe(s) for s in snaps] == [False, True]


# ────────────────────────────────────────────────────────────────────
#  forward_returns
# ────────────────────────────────────────────────────────────────────

def test_same_symbol_coins_keyed_by_coin_id_not_overwritten():
    """两个 VELO: 各自按 coin_id 定价, rank 16 那行不被 rank 57 覆盖。"""
    entry = _snap("2026-10-13", [
        _row("BTC", 100.0, 0.9, "bitcoin"),
        _row("VELO", 0.05, 0.5, "velodrome-finance"),
        _row("VELO", 0.01, 0.1, "velo"),
    ], schema=2)
    # 出场快照里两者名次互换: 按 symbol 会互相拿错价 (-90% / +400%)
    exit_ = _snap("2026-10-20", [
        _row("BTC", 110.0, 0.9, "bitcoin"),
        _row("VELO", 0.009, 0.6, "velo"),
        _row("VELO", 0.045, 0.2, "velodrome-finance"),
    ], schema=2)
    per = dash.forward_returns([entry, exit_], lookahead_days=7)["2026-10-13"]
    assert set(per) == {"bitcoin", "velodrome-finance", "velo"}
    assert per["velodrome-finance"]["rank"] == 2
    assert per["velodrome-finance"]["ret_pct"] == pytest.approx(-0.10)
    assert per["velo"]["rank"] == 3
    assert per["velo"]["ret_pct"] == pytest.approx(-0.10)
    assert per["velo"]["symbol"] == "VELO"


def test_schema1_entry_falls_back_to_unique_symbol():
    """旧快照无 coin_id: 退回 symbol, 但只在出场快照里该 symbol 唯一时才定价。"""
    entry = _snap("2026-10-06", [
        _row("ETH", 10.0, 0.9),
        _row("VELO", 0.05, 0.5, mcap=5e7),
    ])
    exit_ = _snap("2026-10-13", [
        _row("ETH", 11.0, 0.9, "ethereum"),
        _row("VELO", 0.009, 0.6, "velo"),
        _row("VELO", 0.045, 0.2, "velodrome-finance"),
    ], schema=2)
    per = dash.forward_returns([entry, exit_], lookahead_days=7)["2026-10-06"]
    # ETH: 出场 symbol 唯一 → 定价; VELO: 出场里有两个 → 不知道是哪个, 不计
    assert set(per) == {"ETH"}
    assert per["ETH"]["ret_pct"] == pytest.approx(0.10)


def test_ambiguous_schema1_duplicates_skipped():
    """旧快照里同名且无 coin_id 的行: 只保留市值最大的一行, 小的那行不计入 (rank 不重排)。"""
    entry = _snap("2026-09-20", [
        _row("ONE", 2.0, 0.8, mcap=1e6),      # rank 1, 小市值同名币 → 不能按 symbol 定价
        _row("BTC", 100.0, 0.7, mcap=1e12),
        _row("ONE", 0.01, 0.1, mcap=1e8),     # rank 3, 组内市值最大 → 保留
    ])
    exit_ = _snap("2026-09-27", [
        _row("BTC", 90.0, 0.7),
        _row("ONE", 0.011, 0.1),
    ])
    per = dash.forward_returns([entry, exit_], lookahead_days=7)["2026-09-20"]
    assert set(per) == {"BTC", "ONE"}
    assert per["BTC"]["rank"] == 2
    assert per["ONE"]["rank"] == 3
    assert per["ONE"]["ret_pct"] == pytest.approx(0.10)


def test_coin_id_mismatch_not_priced_by_symbol():
    """入场有 coin_id 但出场里没有这个币: 不能借同名的另一个币的价格。"""
    entry = _snap("2026-10-13", [_row("VELO", 0.05, 0.5, "velodrome-finance")], schema=2)
    exit_ = _snap("2026-10-20", [_row("VELO", 0.009, 0.6, "velo")], schema=2)
    assert dash.forward_returns([entry, exit_], lookahead_days=7)["2026-10-13"] == {}


def test_duplicate_coin_id_keeps_higher_rank_and_bad_prices_skipped():
    entry = _snap("2026-10-14", [
        _row("DEEP", 0.10, 0.6, "deep"),
        _row("DEEP", 0.10, 0.3, "deep"),          # 同 coin_id 重复行 → 不覆盖
        _row("NAN", float("nan"), 0.5, "nan-coin"),
        _row("ZERO", 0.0, 0.4, "zero-coin"),
        _row("NOSCORE", 1.0, None, "noscore"),    # composite_score=None 不应让排序崩
    ], schema=2)
    exit_ = _snap("2026-10-21", [
        _row("DEEP", 0.12, 0.6, "deep"),
        _row("NAN", 1.0, 0.5, "nan-coin"),
        _row("ZERO", 1.0, 0.4, "zero-coin"),
        _row("NOSCORE", 2.0, 0.1, "noscore"),
    ], schema=2)
    per = dash.forward_returns([entry, exit_], lookahead_days=7)["2026-10-14"]
    assert set(per) == {"deep", "noscore"}
    assert per["deep"]["rank"] == 1
    assert per["deep"]["ret_pct"] == pytest.approx(0.20)
    assert per["noscore"]["rank"] == 5


def test_lookahead_uses_first_snapshot_on_or_after_target():
    a = _snap("2026-10-01", [_row("BTC", 100.0, 0.9, "bitcoin")])
    b = _snap("2026-10-05", [_row("BTC", 105.0, 0.9, "bitcoin")])
    c = _snap("2026-10-09", [_row("BTC", 120.0, 0.9, "bitcoin")])
    fw = dash.forward_returns([a, b, c], lookahead_days=7)
    assert set(fw) == {"2026-10-01"}          # 10-05 + 7 = 10-12 还没到
    assert fw["2026-10-01"]["bitcoin"]["exit_price"] == 120.0


# ────────────────────────────────────────────────────────────────────
#  topn_hit_rate
# ────────────────────────────────────────────────────────────────────

def _per(rets):
    return {f"c{i}": {"symbol": f"C{i}", "rank": i + 1, "ret_pct": r}
            for i, r in enumerate(rets)}


def test_topn_hit_rate_reports_actual_topn_size():
    s = dash.topn_hit_rate(_per([0.3, -0.1, 0.2, 0.0, -0.2]), n=2)
    assert s["n_topn"] == 2 and s["n_compared"] == 5
    assert s["all_median"] == pytest.approx(0.0)
    assert s["hit_rate"] == pytest.approx(0.5)          # 0.3 > 0 命中, -0.1 不中
    assert s["topn_avg_ret"] == pytest.approx(0.1)

    small = dash.topn_hit_rate(_per([0.1, -0.1, 0.0]), n=30)
    assert small["n_topn"] == 3                          # 不足 30 个 → 按实际 3 个加权
    assert small["hit_rate"] == pytest.approx(1 / 3)

    empty = dash.topn_hit_rate({}, n=10)
    assert empty["hit_rate"] is None and empty["n_topn"] == 0


def test_is_full_universe():
    assert dash.is_full_universe({"_meta": {"schema": 2, "truncated": False}})
    assert dash.is_full_universe({"_meta": {"truncated": False}})
    assert not dash.is_full_universe({"coins": [{}] * 200})          # schema 1: 前 200
    assert not dash.is_full_universe({"_meta": {"schema": 1}})
