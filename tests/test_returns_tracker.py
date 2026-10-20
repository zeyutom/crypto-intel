"""Unit tests for src.research.returns_tracker (记分卡) + scripts/backfill_snapshots.py.

回归 (2026-10-10 实盘回测审计):
  - verify_returns 曾在所有 snapshot_*.json 里按 now-7d 就近挑选, 把 backfill 合成快照
    (snapshot_YYYYMMDD_0000.json, 22-29 个大市值币, 3 个动量因子) 当实盘 Top-30 打分,
    8 张推到飞书的记分卡里有 4 张评的是合成快照.
  - vs_btc 只有 BTC 恰好在 Top-30 里才算; 后面页同名 symbol 会覆盖大市值币的价格.
  - 中位数用 sorted[n//2] (偶数 n 有偏); 匹配不到价格的币被静默丢弃 (08-22 只评了 16/30).
  - backfill 在已有真实快照的日子也写合成快照 (131 个), 污染元学习和记分卡.
"""
from __future__ import annotations
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import returns_tracker as rt


FACTOR_KEYS = [
    "f_momentum_30d", "f_momentum_7d", "f_ath_drawdown", "f_volume_turnover",
    "f_tvl_mcap", "f_market_cap_size", "f_onchain_activity", "f_dev_activity",
    "f_funding_rate", "f_narrative_heat",
]


# ────────────────────────────────────────────────────────────────────
#  fixtures / helpers
# ────────────────────────────────────────────────────────────────────

def _coin(i: int, price: float = 1.0, coin_id: str | None = None, symbol: str | None = None,
          factors_none: bool = False) -> dict:
    row = {
        "symbol": symbol or f"C{i}",
        "price": price,
        "composite_score": round(1.0 - i * 0.001, 4),
    }
    if coin_id is not None:
        row["coin_id"] = coin_id
    for k in FACTOR_KEYS:
        row[k] = None if factors_none else 0.5
    return row


def _write_snapshot(meta_dir: pathlib.Path, ts: datetime, coins: list[dict],
                    synthetic: bool = False, name: str | None = None) -> pathlib.Path:
    """按生产 schema 写一个快照文件. synthetic=True → _0000 文件名 + _meta 标记."""
    if name is None:
        name = f"snapshot_{ts.strftime('%Y%m%d')}_{'0000' if synthetic else ts.strftime('%H%M')}.json"
    data = {
        "timestamp": ts.isoformat(),
        "date": ts.strftime("%Y-%m-%d"),
        "factor_weights": {},
        "coins": coins,
    }
    if synthetic:
        data["_meta"] = {"source": "backfill", "synthetic": True}
    p = meta_dir / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def _real_coins(n: int = 120, price: float = 1.0, with_btc_at: int | None = 0) -> list[dict]:
    """n 个真实风格的币 (含 coin_id, 全部 f_* 非 null). with_btc_at=k → 第 k 行是 BTC."""
    coins = []
    for i in range(n):
        if with_btc_at is not None and i == with_btc_at:
            coins.append(_coin(i, price=100.0, coin_id="bitcoin", symbol="BTC"))
        else:
            coins.append(_coin(i, price=price, coin_id=f"coin-{i}"))
    return coins


@pytest.fixture
def meta_dir(tmp_path, monkeypatch):
    d = tmp_path / "meta"
    d.mkdir()
    monkeypatch.setattr(rt, "META_DIR", d)
    return d


@pytest.fixture
def now_utc():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def no_btc_fallback_network(monkeypatch):
    """默认把 DefiLlama BTC 历史价回退打桩成 None, 任何用例都不许真的出网."""
    monkeypatch.setattr(rt, "_fetch_btc_price_at", lambda entry_ts: None)


def _patch_prices(monkeypatch, prices: dict, missing_pages: list[int] | None = None):
    def _fake(symbols, pages_missing=None):
        if pages_missing is not None:
            pages_missing.extend(missing_pages or [])
        return dict(prices)
    monkeypatch.setattr(rt, "_fetch_current_prices", _fake)


# ────────────────────────────────────────────────────────────────────
#  1. 合成快照被忽略, 只选真实快照
# ────────────────────────────────────────────────────────────────────

def test_synthetic_snapshot_ignored_in_favor_of_real(meta_dir, monkeypatch, now_utc):
    """now-7d 正好落在合成快照上, 但旁边 20h 内有真实快照 → 选真实的."""
    target = now_utc - timedelta(days=7)
    # 合成快照: 时间完美匹配, 但 _0000 + _meta.synthetic
    syn_coins = [_coin(i, price=1.0, symbol=s) for i, s in enumerate(
        ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "DOT", "LINK",
         "MATIC", "TRX", "LTC", "UNI", "ATOM", "ETC", "XLM", "BCH", "NEAR", "APT",
         "FIL", "ARB"])]
    _write_snapshot(meta_dir, target, syn_coins, synthetic=True)
    # 真实快照: 差 20h
    real_ts = target + timedelta(hours=20)
    real_path = _write_snapshot(meta_dir, real_ts, _real_coins())

    prices = {f"coin-{i}": 1.1 for i in range(1, 120)}
    prices["bitcoin"] = 110.0
    _patch_prices(monkeypatch, prices)

    r = rt.verify_returns(lookback_days=7)
    assert r["ok"], r
    assert r["snapshot_file"] == real_path.name
    assert r["entry_ts"] == real_ts.isoformat()
    assert r["synthetic"] is False


def test_synthetic_only_meta_marker_is_rejected(meta_dir, monkeypatch, now_utc):
    """即使文件名不是 _0000, 只要 _meta.synthetic / source=backfill 也排除."""
    target = now_utc - timedelta(days=7)
    coins = _real_coins()
    p = _write_snapshot(meta_dir, target, coins, name="snapshot_x_1200.json")
    d = json.loads(p.read_text())
    d["_meta"] = {"source": "backfill"}
    p.write_text(json.dumps(d))
    _patch_prices(monkeypatch, {"bitcoin": 100.0})
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"] is False
    assert "真实快照" in r["error"]


def test_small_or_null_factor_snapshot_is_not_real(meta_dir, now_utc):
    """< 100 币 或 前 50 行 f_* 含 null → 不算真实快照."""
    small = _write_snapshot(meta_dir, now_utc, _real_coins(n=50), name="snapshot_a_1200.json")
    nulls = _write_snapshot(meta_dir, now_utc,
                            [_coin(i, coin_id=f"c{i}", factors_none=(i == 3)) for i in range(120)],
                            name="snapshot_b_1200.json")
    good = _write_snapshot(meta_dir, now_utc, _real_coins(), name="snapshot_c_1200.json")
    assert rt._is_real_snapshot(small, json.loads(small.read_text())) is False
    assert rt._is_real_snapshot(nulls, json.loads(nulls.read_text())) is False
    assert rt._is_real_snapshot(good, json.loads(good.read_text())) is True


# ────────────────────────────────────────────────────────────────────
#  2. 24h 容差
# ────────────────────────────────────────────────────────────────────

def test_tolerance_24h_error(meta_dir, monkeypatch, now_utc):
    """最近的真实快照差 30h → ok False, 错误信息说明容差."""
    target = now_utc - timedelta(days=7)
    _write_snapshot(meta_dir, target + timedelta(hours=30), _real_coins())
    _patch_prices(monkeypatch, {"bitcoin": 100.0})
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"] is False
    assert "24h" in r["error"]


# ────────────────────────────────────────────────────────────────────
#  3. vs_btc 独立定价
# ────────────────────────────────────────────────────────────────────

def test_vs_btc_computed_when_btc_not_in_top30(meta_dir, monkeypatch, now_utc):
    """BTC 在快照第 80 行 (不在 Top-30) → 仍通过 coin_id=bitcoin 算出 vs_btc."""
    target = now_utc - timedelta(days=7)
    coins = _real_coins(with_btc_at=80)
    _write_snapshot(meta_dir, target, coins)
    prices = {f"coin-{i}": 1.2 for i in range(120)}   # Top-30 全部 +20%
    prices["bitcoin"] = 110.0                           # BTC +10%
    _patch_prices(monkeypatch, prices)

    r = rt.verify_returns(lookback_days=7)
    assert r["ok"], r
    assert all(c["symbol"] != "BTC" for c in r["coins"])
    assert r["btc_entry_missing"] is False
    assert r["vs_btc"] == pytest.approx(0.10, abs=1e-4)


def test_vs_btc_none_with_flag_when_btc_absent_and_fallback_fails(meta_dir, monkeypatch, now_utc):
    """快照无 BTC 行 + DefiLlama 回退也拿不到 → vs_btc None, btc_entry_missing True."""
    target = now_utc - timedelta(days=7)
    _write_snapshot(meta_dir, target, _real_coins(with_btc_at=None))
    prices = {f"coin-{i}": 1.2 for i in range(120)}
    prices["bitcoin"] = 110.0
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"]
    assert r["vs_btc"] is None
    assert r["btc_entry_missing"] is True
    assert r["btc_entry_source"] is None


def test_vs_btc_fallback_to_defillama_when_btc_absent_from_snapshot(meta_dir, monkeypatch, now_utc):
    """回归 (审计 blocking): 2026-09-28 之后的真实快照 (旧 save_snapshot 只存前 200 名) 没有 BTC 行,
    入场价必须能从 DefiLlama 历史价回退拿到, 而不是直接 btc_entry_missing."""
    target = now_utc - timedelta(days=7)
    _write_snapshot(meta_dir, target, _real_coins(with_btc_at=None))
    prices = {f"coin-{i}": 1.2 for i in range(120)}   # Top-30 +20%
    prices["bitcoin"] = 110.0                           # BTC 当前价
    _patch_prices(monkeypatch, prices)

    calls = []

    def _fake_fallback(entry_ts):
        calls.append(entry_ts)
        return 100.0                                    # 入场价 → BTC +10%

    monkeypatch.setattr(rt, "_fetch_btc_price_at", _fake_fallback)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"], r
    assert calls and calls[0] == target                 # 用快照的 timestamp 去查历史价
    assert r["btc_entry_missing"] is False
    assert r["btc_entry_source"] == "defillama"
    assert r["vs_btc"] == pytest.approx(0.10, abs=1e-4)


def test_btc_entry_prefers_snapshot_row_over_fallback(meta_dir, monkeypatch, now_utc):
    """快照里有 BTC 行时不调回退 (source=snapshot)."""
    target = now_utc - timedelta(days=7)
    _write_snapshot(meta_dir, target, _real_coins(with_btc_at=50))
    prices = {f"coin-{i}": 1.0 for i in range(120)}
    prices["bitcoin"] = 110.0
    _patch_prices(monkeypatch, prices)
    monkeypatch.setattr(rt, "_fetch_btc_price_at",
                        lambda entry_ts: pytest.fail("快照已有 BTC 行, 不应回退"))
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"] and r["btc_entry_source"] == "snapshot"
    assert r["vs_btc"] == pytest.approx(-0.10, abs=1e-4)


def test_fetch_btc_price_at_parses_defillama_and_rejects_drift(monkeypatch):
    """_fetch_btc_price_at: 解析 DefiLlama 返回; 时间偏差 >24h 或异常 → None."""
    import src.adapters.defillama_full as dlf
    entry = datetime(2026, 10, 4, 22, 23, tzinfo=timezone.utc)
    monkeypatch.undo()   # 去掉 autouse 打桩, 测真函数
    seen = {}

    def _hist(ts, coins):
        seen["ts"], seen["coins"] = ts, coins
        return {"coins": {"coingecko:bitcoin": {"price": 123456.0, "symbol": "BTC",
                                                "timestamp": ts - 1800}}}

    monkeypatch.setattr(dlf, "historical_prices", _hist)
    assert rt._fetch_btc_price_at(entry) == 123456.0
    assert seen == {"ts": int(entry.timestamp()), "coins": ["coingecko:bitcoin"]}

    # 数据点时间偏差 2 天 → 弃用
    monkeypatch.setattr(dlf, "historical_prices", lambda ts, coins: {
        "coins": {"coingecko:bitcoin": {"price": 1.0, "timestamp": ts - 2 * 86400}}})
    assert rt._fetch_btc_price_at(entry) is None
    # 空结果 / 异常 → None, 不抛
    monkeypatch.setattr(dlf, "historical_prices", lambda ts, coins: None)
    assert rt._fetch_btc_price_at(entry) is None
    monkeypatch.setattr(dlf, "historical_prices",
                        lambda ts, coins: (_ for _ in ()).throw(RuntimeError("offline")))
    assert rt._fetch_btc_price_at(entry) is None


# ────────────────────────────────────────────────────────────────────
#  4. 价格表: coin_id + symbol 双 key, 同名 symbol 先到先得
# ────────────────────────────────────────────────────────────────────

def test_fetch_prices_symbol_collision_first_occurrence_wins(monkeypatch):
    """第 2 页出现同名 symbol (小市值仿盘) 不能覆盖第 1 页的大市值币."""
    pages = {
        1: [{"id": "bitcoin", "symbol": "btc", "current_price": 100.0},
            {"id": "ethereum", "symbol": "eth", "current_price": 10.0}],
        2: [{"id": "fake-eth", "symbol": "eth", "current_price": 0.01},
            {"id": "zero-price", "symbol": "zzz", "current_price": 0}],
    }
    monkeypatch.setattr(rt, "_get_json",
                        lambda url, params=None: pages.get(params["page"], []))
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)

    missing: list[int] = []
    prices = rt._fetch_current_prices(["BTC", "ETH"], pages_missing=missing)
    assert prices["ETH"] == 10.0            # 先到先得
    assert prices["ethereum"] == 10.0       # 按 coin_id 也能查
    assert prices["fake-eth"] == 0.01       # coin_id 不冲突, 照常保留
    assert prices["bitcoin"] == 100.0 and prices["BTC"] == 100.0
    assert "zero-price" not in prices and "ZZZ" not in prices
    assert missing == []                    # 空页也算拉到了


def test_fetch_prices_goes_through_shared_http_client_with_retries(monkeypatch):
    """价格请求走 http_client (限速 + 429 退避), 不缓存, 带重试."""
    from src import http_client
    calls = []

    def _fake_get_json(url, **kw):
        calls.append(kw)
        return [{"id": f"c{kw['params']['page']}", "symbol": "x", "current_price": 1.0}]
    monkeypatch.setattr(http_client.http, "get_json", _fake_get_json)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    prices = rt._fetch_current_prices([])
    assert len(calls) == 5
    assert all(c["retries"] >= 1 and c["ttl"] == 0 for c in calls)
    assert [c["params"]["page"] for c in calls] == [1, 2, 3, 4, 5]
    assert prices["c5"] == 1.0


def test_fetch_prices_records_failed_pages(monkeypatch, caplog):
    """第 5 页重试后仍失败 (get_json 返回 None): 记入 pages_missing 并告警, 不静默丢弃."""
    def _fake(url, params=None):
        page = params["page"]
        if page == 5:
            return None
        return [{"id": f"coin-{page}", "symbol": f"c{page}", "current_price": float(page)}]
    monkeypatch.setattr(rt, "_get_json", _fake)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    monkeypatch.setattr(rt.log, "propagate", True)
    missing: list[int] = []
    with caplog.at_level("WARNING", logger="returns_tracker"):
        prices = rt._fetch_current_prices([], pages_missing=missing)
    assert missing == [5]
    assert prices["coin-4"] == 4.0 and "coin-5" not in prices
    assert any("page 5/5" in r.getMessage() for r in caplog.records)


def test_fetch_prices_stops_requesting_after_time_budget(monkeypatch, caplog):
    """429 + Retry-After 让每页拖很久: 超出 FETCH_BUDGET_S 后剩余页不再请求, 记入 pages_missing."""
    clock = {"t": 0.0}
    calls = []

    def _fake(url, params=None):
        calls.append(params["page"])
        clock["t"] += 100.0          # 每页 (含退避重试) 耗时 100s
        return [{"id": f"coin-{params['page']}", "symbol": "x", "current_price": 1.0}]
    monkeypatch.setattr(rt, "_get_json", _fake)
    monkeypatch.setattr(rt, "FETCH_BUDGET_S", 300)
    monkeypatch.setattr(rt.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    monkeypatch.setattr(rt.log, "propagate", True)
    missing: list[int] = []
    with caplog.at_level("WARNING", logger="returns_tracker"):
        prices = rt._fetch_current_prices([], pages_missing=missing)
    assert calls == [1, 2, 3]                 # 第 4 页开始时已用 300s, 不再请求
    assert missing == [4, 5]
    assert prices["coin-3"] == 1.0 and "coin-4" not in prices
    assert any("预算" in r.getMessage() for r in caplog.records)


def test_build_scorecard_page_missing_with_unmatched_lists_them():
    """缺页且确有 Top-N 币未定价: 缺页行 + 未定价清单都要出现."""
    r = {
        "ok": True, "snapshot_date": "2026-10-13", "lookback_days": 7,
        "graded": 30, "matched_coins": 28, "coverage": 28 / 30, "low_coverage": True,
        "pages_missing": [5], "unmatched": ["AAA", "BBB"],
        "stats": {"avg_return": 0.01, "median_return": 0.01, "win_rate": 0.6},
        "vs_btc": 0.0, "vs_universe_ew": None, "coins": [],
    }
    _, lines, _ = rt._build_scorecard(r)
    body = "\n".join(lines)
    assert "CoinGecko 第 5 页" in body
    assert "覆盖率不足, 未定价: AAA, BBB" in body


def test_verify_returns_missing_page_sets_low_coverage(meta_dir, monkeypatch, now_utc):
    """覆盖率 100% 但有页没拉到 → low_coverage=True, pages_missing 落盘, 卡片有说明."""
    prices = _snapshot_with_returns(meta_dir, now_utc, [0.1] * 30, [0.0] * 70)
    _patch_prices(monkeypatch, prices, missing_pages=[5])
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"] and r["coverage"] == 1.0
    assert r["pages_missing"] == [5]
    assert r["low_coverage"] is True
    saved = json.loads(next(meta_dir.glob("returns_*_7d.json")).read_text())
    assert saved["pages_missing"] == [5]
    _, lines, _ = rt._build_scorecard(r)
    assert any("CoinGecko 第 5 页" in ln and "偏向大市值" in ln for ln in lines)
    # Top-N 全部定价 (unmatched 为空): 不能再出"覆盖率不足, 未定价: <空>", 与"覆盖 100%"矛盾
    assert r["unmatched"] == []
    assert not any("覆盖率不足" in ln for ln in lines)


def test_verify_returns_no_missing_pages(meta_dir, monkeypatch, now_utc):
    prices = _snapshot_with_returns(meta_dir, now_utc, [0.1] * 30, [0.0] * 70)
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["pages_missing"] == [] and r["low_coverage"] is False
    _, lines, _ = rt._build_scorecard(r)
    assert not any("CoinGecko 第" in ln for ln in lines)


def test_lookup_coin_id_only_when_present():
    prices = {"ethereum": 10.0, "ETH": 0.01, "SOL": 5.0}
    assert rt._lookup_price({"coin_id": "ethereum", "symbol": "ETH"}, prices) == 10.0
    assert rt._lookup_price({"symbol": "SOL"}, prices) == 5.0            # 无 coin_id 回退 symbol
    assert rt._lookup_price({"coin_id": "", "symbol": "SOL"}, prices) == 5.0
    # 有 coin_id 但查不到 → unmatched, 不能借同名 symbol 的价格
    assert rt._lookup_price({"coin_id": "missing", "symbol": "SOL"}, prices) is None
    assert rt._lookup_price({"coin_id": "nope", "symbol": "NOPE"}, prices) is None


def test_coin_id_row_not_graded_with_same_symbol_coin(meta_dir, monkeypatch, now_utc):
    """VELO 案例: velodrome-finance 掉出当前 Top 500, 同名 velo 还在 →
    velodrome 记 unmatched, 不能用 velo 的价格算出 -88% 的假收益."""
    target = now_utc - timedelta(days=7)
    coins = _real_coins()
    coins[3].update({"symbol": "VELO", "coin_id": "velodrome-finance", "price": 0.03473})
    coins[60].update({"symbol": "VELO", "coin_id": "velo", "price": 0.00407})
    _write_snapshot(meta_dir, target, coins)
    prices = {f"coin-{i}": 1.1 for i in range(120)}
    prices.update({"bitcoin": 100.0, "velo": 0.0041, "VELO": 0.0041})
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"]
    assert "VELO" in r["unmatched"]
    assert all(c["coin_id"] != "velodrome-finance" for c in r["coins"])
    assert r["stats"]["worst_return"] > -0.5
    # universe 里 velo 自己照常定价, velodrome 不算
    assert r["universe_n"] == 119


def test_duplicate_symbol_without_coin_id_only_largest_cap_priced(meta_dir, monkeypatch, now_utc):
    """旧快照 (无 coin_id) 里两个 VELO: 只有市值大的那行按 symbol 定价, 小的记 unmatched."""
    target = now_utc - timedelta(days=7)
    coins = [_coin(i) for i in range(120)]
    coins[0]["symbol"], coins[0]["market_cap"], coins[0]["price"] = "VELO", 54e6, 0.04
    coins[1]["symbol"], coins[1]["market_cap"], coins[1]["price"] = "VELO", 86e6, 0.005
    coins[2]["symbol"], coins[2]["market_cap"] = "SOL", 1e9     # 不重复, 正常定价
    _write_snapshot(meta_dir, target, coins)
    prices = {f"C{i}": 1.1 for i in range(120)}
    prices.update({"VELO": 0.0055, "SOL": 1.1, "bitcoin": 100.0})
    _patch_prices(monkeypatch, prices)

    r = rt.verify_returns(lookback_days=7)
    assert r["ok"]
    assert r["unmatched"] == ["VELO"]
    priced = {(c["symbol"], c["old_price"]) for c in r["coins"]}
    assert ("VELO", 0.005) in priced and ("VELO", 0.04) not in priced
    assert ("SOL", 1.0) in priced
    # universe 同样排除小 VELO: 119 个币能定价
    assert r["universe_n"] == 119


def test_ambiguous_rows_helper():
    coins = [{"symbol": "A", "market_cap": 1}, {"symbol": "A", "market_cap": 5},
             {"symbol": "B"}, {"symbol": "B"},                              # 都无市值 → 全排除
             {"symbol": "C", "coin_id": "c-real"}, {"symbol": "C", "market_cap": 9}]  # 有 coin_id 的不排除
    assert rt._ambiguous_rows(coins) == {0, 2, 3}


# ────────────────────────────────────────────────────────────────────
#  5. 统计: 偶数 n 中位数, unmatched / coverage, vs_universe
# ────────────────────────────────────────────────────────────────────

def _snapshot_with_returns(meta_dir, now_utc, top_rets: list[float], rest_rets: list[float],
                           unpriced_top: int = 0):
    """构造快照 + 价格表: 前 len(top_rets) 行为 Top, 其余为池子. 返回价格表.

    Top-N 固定 30 行: top_rets 之后用 unpriced_top 个无价格行补足, 再用 +0% 的行填满.
    """
    target = now_utc - timedelta(days=7)
    coins, prices = [], {}
    i = 0
    for ret in top_rets:
        coins.append(_coin(i, price=1.0, coin_id=f"coin-{i}"))
        prices[f"coin-{i}"] = 1.0 + ret
        i += 1
    for _ in range(unpriced_top):
        coins.append(_coin(i, price=1.0, coin_id=f"coin-{i}"))  # 没有价格
        i += 1
    while i < rt.TOP_N:
        coins.append(_coin(i, price=1.0, coin_id=f"coin-{i}"))
        prices[f"coin-{i}"] = 1.0
        i += 1
    for ret in rest_rets:
        coins.append(_coin(i, price=1.0, coin_id=f"coin-{i}"))
        prices[f"coin-{i}"] = 1.0 + ret
        i += 1
    while len(coins) < rt.MIN_REAL_SNAPSHOT_COINS:
        coins.append(_coin(i, price=1.0, coin_id=f"coin-{i}"))  # 池子里无价格的 → 不计入 universe
        i += 1
    _write_snapshot(meta_dir, target, coins)
    return prices


def test_median_uses_statistics_median_for_even_n(meta_dir, monkeypatch, now_utc):
    """Top-30 里 4 个有价格 (+0.1 +0.2 +0.3 +0.4) 其余无价格 → 中位数 0.25, 不是 0.3."""
    prices = _snapshot_with_returns(meta_dir, now_utc, [0.1, 0.2, 0.3, 0.4], [],
                                    unpriced_top=26)
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"]
    assert r["matched_coins"] == 4
    assert r["stats"]["median_return"] == pytest.approx(0.25)
    assert r["stats"]["avg_return"] == pytest.approx(0.25)


def test_unmatched_and_coverage(meta_dir, monkeypatch, now_utc):
    """Top-30 里 14 个无价格 → unmatched 列出, coverage=16/30, low_coverage=True."""
    prices = _snapshot_with_returns(meta_dir, now_utc, [0.1] * 16, [], unpriced_top=14)
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"]
    assert r["graded"] == 30 and r["matched_coins"] == 16
    assert r["coverage"] == pytest.approx(16 / 30, abs=1e-4)
    assert r["low_coverage"] is True
    assert sorted(r["unmatched"]) == sorted(f"C{i}" for i in range(16, 30))


def test_coverage_ok_not_flagged(meta_dir, monkeypatch, now_utc):
    prices = _snapshot_with_returns(meta_dir, now_utc, [0.1] * 25, [], unpriced_top=5)
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"] and r["low_coverage"] is False and len(r["unmatched"]) == 5


def test_vs_universe_ew_and_median(meta_dir, monkeypatch, now_utc):
    """Top-30 全部 +10%; 池子另有 70 个币 -10% → 全池等权 = (30*0.1 + 70*-0.1)/100 = -0.04."""
    prices = _snapshot_with_returns(meta_dir, now_utc, [0.1] * 30, [-0.1] * 70)
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"]
    assert r["universe_n"] == 100
    assert r["vs_universe_ew"] == pytest.approx(0.1 - (-0.04), abs=1e-4)
    # 全池中位: 100 个值, 30 个 +0.1 / 70 个 -0.1 → 中位 -0.1; Top 中位 0.1 → 差 0.2
    assert r["vs_universe_median"] == pytest.approx(0.2, abs=1e-4)


def test_result_written_without_nan(meta_dir, monkeypatch, now_utc):
    """结果 JSON 落盘且不含 NaN/Infinity."""
    prices = _snapshot_with_returns(meta_dir, now_utc, [0.1] * 30, [0.0] * 70)
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    files = list(meta_dir.glob("returns_*_7d.json"))
    assert len(files) == 1
    txt = files[0].read_text()
    assert "NaN" not in txt and "Infinity" not in txt
    assert json.loads(txt)["snapshot_file"] == r["snapshot_file"]


def test_nan_composite_score_in_snapshot_written_as_null(meta_dir, monkeypatch, now_utc):
    """快照行 composite_score 是 NaN (旧 json 的 NaN token) → 结果里写 null, 不透传 NaN."""
    target = now_utc - timedelta(days=7)
    coins = _real_coins()
    coins[1]["composite_score"] = float("nan")
    _write_snapshot(meta_dir, target, coins)       # json.dumps 默认 allow_nan → 文件里是 NaN token
    prices = {f"coin-{i}": 1.1 for i in range(120)}
    prices["bitcoin"] = 100.0
    _patch_prices(monkeypatch, prices)
    r = rt.verify_returns(lookback_days=7)
    assert r["ok"]
    row = next(c for c in r["coins"] if c["symbol"] == "C1")
    assert row["composite_score"] is None
    txt = next(meta_dir.glob("returns_*_7d.json")).read_text()
    assert "NaN" not in txt


# ────────────────────────────────────────────────────────────────────
#  6. 记分卡文案
# ────────────────────────────────────────────────────────────────────

def test_build_scorecard_shows_universe_and_coverage():
    r = {
        "ok": True, "snapshot_date": "2026-10-03", "lookback_days": 7,
        "graded": 30, "matched_coins": 28, "coverage": 28 / 30, "low_coverage": False,
        "unmatched": ["AAA", "BBB"],
        "stats": {"avg_return": 0.05, "median_return": 0.04, "win_rate": 0.6,
                  "best_coin": "X", "best_return": 0.2, "worst_coin": "Y", "worst_return": -0.1},
        "vs_btc": 0.02, "btc_entry_missing": False,
        "vs_universe_ew": -0.01, "vs_universe_median": 0.005, "universe_n": 180,
        "coins": [{"symbol": "X", "return_pct": 0.2}, {"symbol": "Y", "return_pct": -0.1}],
    }
    title, lines, color = rt._build_scorecard(r)
    body = "\n".join(lines)
    assert "vs BTC 超额: **+2.00%**" in body
    assert "vs 全池等权 (n=180): **-1.00%**" in body
    assert "vs 全池中位: +0.50%" in body
    assert "匹配 28/30 币 (覆盖 93%)" in body
    assert color == "green"


def test_build_scorecard_low_coverage_warning_and_btc_missing():
    r = {
        "ok": True, "snapshot_date": "2026-08-22", "lookback_days": 7,
        "graded": 30, "matched_coins": 16, "coverage": 16 / 30, "low_coverage": True,
        "unmatched": ["AAA", "BBB"],
        "stats": {"avg_return": -0.02, "median_return": -0.01, "win_rate": 0.4},
        "vs_btc": None, "btc_entry_missing": True,
        "vs_universe_ew": None, "coins": [],
    }
    _, lines, color = rt._build_scorecard(r)
    body = "\n".join(lines)
    assert "覆盖率不足" in body and "AAA, BBB" in body
    assert "缺 BTC 入场价" in body
    assert color == "red"


def test_build_scorecard_btc_current_price_missing_is_visible():
    """BTC 入场价有、当前价拉不到 (vs_btc None, btc_entry_missing False) → 也要有一行说明."""
    r = {"ok": True, "snapshot_date": "2026-10-04", "lookback_days": 7, "graded": 30,
         "matched_coins": 30, "coverage": 1.0, "stats": {"avg_return": 0.01},
         "vs_btc": None, "btc_entry_missing": False, "btc_entry_source": "snapshot", "coins": []}
    _, lines, _ = rt._build_scorecard(r)
    assert any("无法获取当前 BTC 价" in ln for ln in lines)


def test_build_scorecard_marks_defillama_entry_source():
    r = {"ok": True, "snapshot_date": "2026-10-04", "lookback_days": 7, "graded": 30,
         "matched_coins": 30, "coverage": 1.0, "stats": {"avg_return": 0.01},
         "vs_btc": 0.03, "btc_entry_missing": False, "btc_entry_source": "defillama", "coins": []}
    _, lines, _ = rt._build_scorecard(r)
    assert any("vs BTC 超额: **+3.00%** (入场价: DefiLlama)" in ln for ln in lines)


def test_build_scorecard_refuses_synthetic():
    with pytest.raises(ValueError):
        rt._build_scorecard({"ok": True, "synthetic": True, "snapshot_file": "snapshot_20260802_0000.json",
                             "stats": {"avg_return": 0.1}})


def test_push_scorecard_refuses_synthetic_without_pushing(monkeypatch):
    """守卫: verify_returns 若返回 synthetic=True, push 不调 push_alert."""
    monkeypatch.setattr(rt, "verify_returns",
                        lambda lookback_days=7: {"ok": True, "synthetic": True,
                                                 "snapshot_file": "snapshot_20260802_0000.json",
                                                 "stats": {"avg_return": 0.1}})
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "push_alert",
                        lambda *a, **k: pytest.fail("不应推送合成快照记分卡"))
    r = rt.push_returns_scorecard(7)
    assert r["ok"] is False and r["pushed"] == 0 and "合成" in r["error"]


# ────────────────────────────────────────────────────────────────────
#  7. backfill: 已有真实快照的日期跳过
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def backfill(tmp_path, monkeypatch):
    p = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_snapshots.py"
    spec = importlib.util.spec_from_file_location("backfill_snapshots_under_test", p)
    mod = importlib.util.module_from_spec(spec)
    # 脚本 import 时会 META_DIR.mkdir (以及 defillama_full 的 CACHE_DIR.mkdir) — 打桩掉, 不碰项目 data/
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pathlib.Path, "mkdir", lambda self, *a, **k: None)
        spec.loader.exec_module(mod)
    d = tmp_path / "meta"
    d.mkdir()
    monkeypatch.setattr(mod, "META_DIR", d)
    monkeypatch.setattr(mod, "PRICE_CACHE", tmp_path / "cache" / "backfill_prices.json")
    return mod


def test_has_real_snapshot_for_date(backfill):
    d = backfill.META_DIR
    day = "20260805"
    assert backfill.has_real_snapshot_for_date(day) is False
    # _0000 合成文件不算
    (d / f"snapshot_{day}_0000.json").write_text(json.dumps(
        {"timestamp": "2026-08-05T00:00:00+00:00", "coins": [],
         "_meta": {"synthetic": True, "source": "backfill"}}))
    assert backfill.has_real_snapshot_for_date(day) is False
    # 非 _0000 但带 backfill 标记也不算
    (d / f"snapshot_{day}_0100.json").write_text(json.dumps(
        {"timestamp": "2026-08-05T01:00:00+00:00", "coins": [], "_meta": {"source": "backfill"}}))
    assert backfill.has_real_snapshot_for_date(day) is False
    # 真实快照
    (d / f"snapshot_{day}_2221.json").write_text(json.dumps(
        {"timestamp": "2026-08-05T22:21:00+00:00", "coins": []}))
    assert backfill.has_real_snapshot_for_date(day) is True
    assert backfill.has_real_snapshot_for_date("20260806") is False


def _utc_midnight() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _fake_history(symbols: list[str], days: int = 140) -> dict:
    """每个币 days 天升序日线, 价格线性上涨 (保证 compute_coin_factors 有值)."""
    today = _utc_midnight()
    out = {}
    for j, s in enumerate(symbols):
        out[s] = [{"timestamp": int((today - timedelta(days=days - k)).timestamp()),
                   "price": 10.0 + j + k * 0.05} for k in range(days + 1)]
    return out


def test_backfill_main_skips_dates_with_real_snapshot(backfill, monkeypatch):
    """窗口 3 天: 其中 1 天已有真实快照 (非 _0000 文件名) → 不写该天的 _0000."""
    d = backfill.META_DIR
    symbols = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA"]
    universe = [(s, f"coingecko:{s.lower()}") for s in symbols]
    monkeypatch.setattr(backfill, "load_universe", lambda n=30: universe)
    monkeypatch.setattr(backfill, "fetch_history",
                        lambda universe, days=60, use_cache=True: _fake_history(symbols))

    today = _utc_midnight()
    real_day = (today - timedelta(days=2)).strftime("%Y%m%d")
    (d / f"snapshot_{real_day}_2221.json").write_text(json.dumps(
        {"timestamp": f"{real_day[:4]}-{real_day[4:6]}-{real_day[6:]}T22:21:00+00:00",
         "coins": [{"symbol": "BTC", "price": 1.0}]}))

    monkeypatch.setattr(sys, "argv", ["backfill_snapshots.py", "--days", "3", "--top", "6"])
    backfill.main()

    written = sorted(p.name for p in d.glob("snapshot_*_0000.json"))
    assert f"snapshot_{real_day}_0000.json" not in written
    assert len(written) == 2
    for p in d.glob("snapshot_*_0000.json"):
        snap = json.loads(p.read_text())
        assert snap["_meta"]["synthetic"] is True
        assert snap["_meta"]["source"] == "backfill"
    # 真实快照原样保留
    assert (d / f"snapshot_{real_day}_2221.json").exists()


def test_backfill_overwrite_only_touches_synthetic(backfill, monkeypatch):
    """已有合成 _0000: 默认跳过; --overwrite 重写; 真实快照日期 --overwrite 也不写."""
    d = backfill.META_DIR
    symbols = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA"]
    universe = [(s, f"coingecko:{s.lower()}") for s in symbols]
    monkeypatch.setattr(backfill, "load_universe", lambda n=30: universe)
    monkeypatch.setattr(backfill, "fetch_history",
                        lambda universe, days=60, use_cache=True: _fake_history(symbols))
    today = _utc_midnight()
    syn_day = (today - timedelta(days=1)).strftime("%Y%m%d")
    real_day = (today - timedelta(days=2)).strftime("%Y%m%d")
    syn_path = d / f"snapshot_{syn_day}_0000.json"
    syn_path.write_text(json.dumps({"timestamp": "x", "coins": [],
                                    "_meta": {"synthetic": True, "source": "backfill"}, "marker": 1}))
    (d / f"snapshot_{real_day}_1000.json").write_text(json.dumps({"timestamp": "x", "coins": []}))

    monkeypatch.setattr(sys, "argv", ["backfill_snapshots.py", "--days", "2", "--top", "6"])
    backfill.main()
    assert json.loads(syn_path.read_text()).get("marker") == 1   # 未覆盖
    assert not (d / f"snapshot_{real_day}_0000.json").exists()

    monkeypatch.setattr(sys, "argv", ["backfill_snapshots.py", "--days", "2", "--top", "6", "--overwrite"])
    backfill.main()
    new = json.loads(syn_path.read_text())
    assert "marker" not in new and new["_meta"]["synthetic"] is True   # 合成的被重写
    assert not (d / f"snapshot_{real_day}_0000.json").exists()         # 真实快照日期仍不写
