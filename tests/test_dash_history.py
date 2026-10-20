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
    """旧快照里同名、无 coin_id 且价格差很大的两行 = 两个不同的币: 整组都不定价 (rank 不重排)。

    回归: 之前沿用 returns_tracker 的 "留市值最大那行"。出场快照只是 composite 前 200 的子集,
    剩下那个唯一的同名行可能是另一个币 — SAFE 07-26 rank 69 (0.0953, 市值大) 被 08-02 唯一的
    SAFE (0.1535, 延续 rank 177 那个 0.14 的币) 定价, 记成 +61%。
    """
    entry = _snap("2026-09-20", [
        _row("ONE", 2.0, 0.8, mcap=1e6),      # rank 1
        _row("BTC", 100.0, 0.7, mcap=1e12),
        _row("ONE", 0.01, 0.1, mcap=1e8),     # rank 3, 组内市值最大也不能按 symbol 定价
    ])
    exit_ = _snap("2026-09-27", [
        _row("BTC", 90.0, 0.7),
        _row("ONE", 2.2, 0.1),                # 出场唯一的 ONE 其实延续的是 rank 1 那个币
    ])
    per = dash.forward_returns([entry, exit_], lookahead_days=7)["2026-09-20"]
    assert set(per) == {"BTC"}
    assert per["BTC"]["rank"] == 2


def test_same_coin_listed_twice_kept_once_at_higher_rank():
    """同一个币在旧快照里列了两次 (HNT 08-30 rank 9/10: 0.8099 / 0.8066): 价格 ±2% 内 → 留一行。"""
    entry = _snap("2026-08-30", [
        _row("BTC", 100.0, 0.9),
        _row("HNT", 0.8099, 0.8, mcap=1.4e8),   # rank 2, 市值略小也照样保留 (按名次, 不按市值)
        _row("HNT", 0.8066, 0.7, mcap=1.5e8),   # rank 3, 同一个币 → 不重复计
        _row("ETH", 10.0, 0.6),
    ])
    exit_ = _snap("2026-09-06", [
        _row("BTC", 100.0, 0.9),
        _row("HNT", 0.5559, 0.5),
        _row("ETH", 11.0, 0.6),
    ])
    per = dash.forward_returns([entry, exit_], lookahead_days=7)["2026-08-30"]
    assert set(per) == {"BTC", "HNT", "ETH"}
    assert per["HNT"]["rank"] == 2
    assert per["HNT"]["entry_price"] == pytest.approx(0.8099)
    assert per["HNT"]["ret_pct"] == pytest.approx(0.5559 / 0.8099 - 1)
    assert per["ETH"]["rank"] == 4     # 被丢掉的重复行仍占名次


def test_duplicate_group_with_bad_price_not_treated_as_same_coin():
    """组内有一行价格无效 → 无法确认是同一个币, 无 coin_id 的行整组不定价。"""
    entry = _snap("2026-08-30", [
        _row("HNT", 0.81, 0.8),
        _row("HNT", None, 0.7),
    ])
    exit_ = _snap("2026-09-06", [_row("HNT", 0.56, 0.5)])
    assert dash.forward_returns([entry, exit_], lookahead_days=7)["2026-08-30"] == {}


def test_duplicate_symbol_with_distinct_coin_ids_untouched():
    """schema 2 两个 VELO 都带 coin_id: 不算歧义, 各按 coin_id 定价。"""
    entry = _snap("2026-10-13", [
        _row("VELO", 0.05, 0.6, "velodrome-finance"),
        _row("VELO", 0.009, 0.5, "velo"),
    ], schema=2)
    exit_ = _snap("2026-10-20", [
        _row("VELO", 0.055, 0.6, "velodrome-finance"),
        _row("VELO", 0.0081, 0.5, "velo"),
    ], schema=2)
    per = dash.forward_returns([entry, exit_], lookahead_days=7)["2026-10-13"]
    assert per["velodrome-finance"]["ret_pct"] == pytest.approx(0.10)
    assert per["velo"]["ret_pct"] == pytest.approx(-0.10)


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


def test_exit_snapshot_dates_first_on_or_after_target():
    snaps = [_snap(d, [_row("BTC", 1.0, 0.9)]) for d in
             ["2026-10-01", "2026-10-05", "2026-10-09", "bad-date", "2026-10-12"]]
    ex = dash.exit_snapshot_dates(snaps, lookahead_days=7)
    assert ex == {"2026-10-01": "2026-10-09", "2026-10-05": "2026-10-12"}


def test_truncated_baseline_counts_separates_full_universe_exits():
    """入场截断但出场是 schema 2 全量宇宙 (10-11 → 10-18): 没有 "仍在前 200" 的幸存者偏差。

    回归: 页面说明一概写 "只含 N 天后仍在前 200 的币", 默认选中的 10-11 也被这样描述,
    但它的出场快照 10-18 保存完整评分宇宙。
    """
    snaps = [
        _snap("2026-10-01", [_row("BTC", 1.0, 0.9)]),                         # 截断
        _snap("2026-10-08", [_row("BTC", 1.0, 0.9)]),                         # 截断
        _snap("2026-10-11", [_row("BTC", 1.0, 0.9)]),                         # 截断
        _snap("2026-10-15", [_row("BTC", 1.0, 0.9)], schema=2, truncated=False),
        _snap("2026-10-18", [_row("BTC", 1.0, 0.9)], schema=2, truncated=False),
    ]
    fw = dash.forward_returns(snaps, lookahead_days=7)
    assert sorted(fw) == ["2026-10-01", "2026-10-08", "2026-10-11"]
    # 10-01 → 10-08 两边都截断; 10-08 → 10-15 / 10-11 → 10-18 出场是全量
    assert dash.truncated_baseline_counts(snaps, sorted(fw), lookahead_days=7) == (3, 1)
    assert dash.truncated_baseline_counts(snaps, ["2026-10-15"], lookahead_days=7) == (0, 0)
