"""Unit tests for screener 数据质量修复 (2026-10 回测审计).

覆盖:
  - is_excluded: 稳定币名单 / 锚定启发式 / wrapped+LST / 代币化股票+黄金 /
    陈旧 last_updated / 冻结报价 (最近 3 个真实快照价格+市值相同且 24h 涨跌 0;
    LPT 式 2 位小数价格撞车但市值在动的真币不被误杀)
  - load_recent_snapshot_prices: 跳过合成快照, 容忍目录缺失
  - score_coins: exclusion_stats 计数, 输出含 coin_id + last_updated
  - fetch_funding_rates: Binance → OKX → Bybit 兜底顺序 + FUNDING_SOURCE
  - fetch_onchain_activity: developer_data/community_data 为空的告警 + __meta__

全部不联网: monkeypatch _get / META_DIR / time.sleep。
"""
import sys
import json
import logging
import pathlib
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import screener
from src.research import factors_extended as fx
from src.research.screener import (
    is_excluded, load_recent_snapshot_prices, score_coins,
)


NOW = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)


def _coin(symbol: str, price: float = 5.0, chg_7d: float = 4.0,
          chg_30d: float = 12.0, cid: str | None = None,
          last_updated: str | None = None, mcap: float = 5e8,
          chg_24h: float | None = 1.0) -> dict:
    """构造一个最小 CoinGecko /coins/markets 条目。"""
    return {
        "id": cid or symbol.lower(),
        "symbol": symbol.lower(),
        "name": symbol,
        "current_price": price,
        "market_cap": mcap,
        "market_cap_rank": 50,
        "total_volume": mcap * 0.1,
        "price_change_percentage_24h": chg_24h,
        "price_change_percentage_7d_in_currency": chg_7d,
        "price_change_percentage_30d_in_currency": chg_30d,
        "ath": price * 2,
        "ath_change_percentage": -50.0,
        "last_updated": last_updated or (NOW - timedelta(minutes=5)).isoformat(),
    }


# ────────────────────────────────────────────────────────────────────
#  is_excluded — 各类别
# ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sym", ["USDG", "JUPUSD", "USD1", "RLUSD", "BUIDL",
                                 "SYRUPUSDC", "CUSDO", "USDT"])
def test_excluded_stablecoin_list(sym):
    assert is_excluded(_coin(sym, price=1.0), now=NOW) == "stable"


def test_pegged_heuristic_catches_unlisted_stable():
    """未登记的 $1 锚定币: 价格 0.98-1.02 且 7d/30d 几乎不动 → stable."""
    c = _coin("NEWUSD", price=1.001, chg_7d=0.1, chg_30d=-0.3)
    assert is_excluded(c, now=NOW) == "stable"


def test_pegged_heuristic_spares_volatile_dollar_coin():
    """价格碰巧在 $1 附近但有波动的真币不被误杀."""
    c = _coin("ALPHA", price=1.01, chg_7d=8.0, chg_30d=25.0)
    assert is_excluded(c, now=NOW) is None


@pytest.mark.parametrize("sym", ["SPCXB", "SNDKB", "CRCLX", "TSLAX", "PAXG",
                                 "XAUT", "KAU"])
def test_excluded_non_crypto_list(sym):
    assert is_excluded(_coin(sym, price=100.0), now=NOW) == "non_crypto"


def test_excluded_tokenized_by_coingecko_id_hint():
    """名单外的代币化股票: symbol 以 X/B 结尾 + id 含 xstock/backed/tokenized."""
    assert is_excluded(_coin("NFLXX", cid="netflix-xstock"), now=NOW) == "non_crypto"
    assert is_excluded(_coin("IBMB", cid="backed-ibm"), now=NOW) == "non_crypto"
    assert is_excluded(_coin("ABCX", cid="abc-tokenized-etf"), now=NOW) == "non_crypto"
    # 不以 X/B 结尾则 id 提示不生效 (避免误杀 "backed" 字样的普通项目)
    assert is_excluded(_coin("ABC", cid="abc-backed-token"), now=NOW) is None


@pytest.mark.parametrize("sym", ["WBTC", "WSTETH", "CBBTC", "JITOSOL", "WHYPE",
                                 "SAVAX", "WETH", "STETH"])
def test_excluded_wrapped_lst_list(sym):
    assert is_excluded(_coin(sym, price=3000.0), now=NOW) == "wrapped"


def test_wrapped_prefix_heuristic_kept():
    """原有 W/S 前缀启发式仍然生效 (如 SMATIC)."""
    assert is_excluded(_coin("SMATIC"), now=NOW) == "wrapped"


def test_stale_last_updated_excluded():
    old = (NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert is_excluded(_coin("OLD", last_updated=old), now=NOW) == "stale"


def test_fresh_or_missing_last_updated_not_excluded():
    fresh = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert is_excluded(_coin("OK", last_updated=fresh), now=NOW) is None
    c = _coin("NOLU")
    c["last_updated"] = None
    assert is_excluded(c, now=NOW) is None
    c["last_updated"] = "garbage"
    assert is_excluded(c, now=NOW) is None


def test_normal_coin_passes():
    assert is_excluded(_coin("SOL", price=150.0), now=NOW) is None


# ────────────────────────────────────────────────────────────────────
#  冻结报价: 最近 3 个真实快照
# ────────────────────────────────────────────────────────────────────

def _row(sym: str, price: float, mcap: float | None = 1e9,
         chg_24h: float = 0.0) -> dict:
    """快照里的一行 (save_snapshot 写出的字段子集)."""
    return {"symbol": sym, "price": price, "market_cap": mcap,
            "change_24h": chg_24h, "composite_score": 0.5}


def _write_snapshot(meta_dir: pathlib.Path, name: str, rows: list[dict],
                    n_pad: int = 120, synthetic: bool = False):
    """写一个快照: rows 指定的币 + n_pad 个填充币 (保证 >=100 真实快照门槛)."""
    coins = list(rows)
    if not synthetic:
        coins += [_row(f"PAD{i}", float(i + 1), 1e8) for i in range(n_pad)]
    snap = {"timestamp": f"{name}T00:00:00+00:00", "date": name,
            "factor_weights": {}, "coins": coins}
    if synthetic:
        snap["_meta"] = {"source": "backfill", "synthetic": True}
    (meta_dir / f"snapshot_{name.replace('-', '')}_0000.json").write_text(
        json.dumps(snap), encoding="utf-8")


@pytest.fixture
def meta_dir(tmp_path, monkeypatch):
    d = tmp_path / "meta"
    d.mkdir()
    monkeypatch.setattr(screener, "META_DIR", d)
    return d


# 审计原始数据 (data/meta/snapshot_20261008..10): REUR 价格 4.13、市值
# 62,008,637、change_24h 0 在 6 个快照里纹丝不动; LPT 价格同样连续 1.79,
# 但市值 89,153,219 / 88,971,863 / 89,167,086、change_24h 6.35 / 0.75 / 0.82
# 都在动 —— CoinGecko 对 >= $1 的价格只给 2 位小数, 单看价格会误杀 LPT。
REUR_ROWS = [_row("REUR", 4.13, 62008637, 0.0)] * 3
LPT_ROWS = [_row("LPT", 1.79, 89167086, 0.82), _row("LPT", 1.79, 88971863, 0.75),
            _row("LPT", 1.79, 89153219, 6.35)]


def _write_three_days(meta_dir, per_day: list[list[dict]]):
    for day, rows in zip(("2026-10-08", "2026-10-09", "2026-10-10"), per_day):
        _write_snapshot(meta_dir, day, rows)


def test_frozen_quote_reur_price_and_mcap_identical(meta_dir):
    """REUR 案例: 价格+市值 3 个快照完全一致, 当前 24h 涨跌 0 → frozen."""
    _write_three_days(meta_dir, [[REUR_ROWS[i], LPT_ROWS[i], _row("BTC", 100.0 + i)]
                                 for i in range(3)])
    prior = load_recent_snapshot_prices()
    assert prior["REUR"] == [(4.13, 62008637)] * 3

    reur = _coin("REUR", price=4.13, mcap=62008637, chg_24h=0.0,
                 chg_7d=260.8, chg_30d=260.8)
    assert is_excluded(reur, prior, now=NOW) == "frozen"
    # 价格有变化的币不受影响
    assert is_excluded(_coin("BTC", price=100.0), prior, now=NOW) is None
    # 当前价与历史不同 → 不算冻结
    assert is_excluded(_coin("REUR", price=4.30, mcap=64e6, chg_7d=5, chg_30d=5),
                       prior, now=NOW) is None


def test_frozen_quote_spares_moving_coin_with_rounded_price(meta_dir):
    """LPT 案例: 2 位小数价格连续 3 天 1.79, 但市值和 24h 涨跌都在动 → 不排除."""
    _write_three_days(meta_dir, [[LPT_ROWS[i]] for i in range(3)])
    prior = load_recent_snapshot_prices()
    assert [p for p, _ in prior["LPT"]] == [1.79, 1.79, 1.79]

    lpt = _coin("LPT", price=1.79, mcap=89e6, chg_24h=0.82,
                chg_7d=2.23, chg_30d=30.65)
    assert is_excluded(lpt, prior, now=NOW) is None
    # 市值全部相同但 24h 涨跌非 0 (价格刚好在四舍五入边界) → 仍不排除
    _write_three_days(meta_dir, [[_row("LPT", 1.79, 89e6, 0.5)]] * 3)
    prior = load_recent_snapshot_prices()
    assert is_excluded(lpt, prior, now=NOW) is None
    # 24h 涨跌为 0 但市值在动 → 不排除
    _write_three_days(meta_dir, [[LPT_ROWS[i]] for i in range(3)])
    prior = load_recent_snapshot_prices()
    lpt_flat = _coin("LPT", price=1.79, mcap=89e6, chg_24h=0.0)
    assert is_excluded(lpt_flat, prior, now=NOW) is None


def test_frozen_quote_missing_change_24h_treated_as_zero(meta_dir):
    """change_24h 为 None 不阻止冻结判定 (REUR 式全一致仍排除)."""
    _write_three_days(meta_dir, [[REUR_ROWS[i]] for i in range(3)])
    prior = load_recent_snapshot_prices()
    reur = _coin("REUR", price=4.13, mcap=62008637, chg_24h=None)
    assert is_excluded(reur, prior, now=NOW) == "frozen"


def test_frozen_quote_missing_mcap_in_history_is_spared(meta_dir):
    """历史快照缺市值无法确认 → 宁可放过."""
    _write_three_days(meta_dir, [[_row("X", 2.0, None, 0.0)]] * 3)
    prior = load_recent_snapshot_prices()
    assert prior["X"] == [(2.0, None)] * 3
    assert is_excluded(_coin("X", price=2.0, mcap=1e9, chg_24h=0.0),
                       prior, now=NOW) is None


def test_frozen_quote_requires_three_real_snapshots(meta_dir):
    """只有 2 个真实快照 + 1 个合成快照 → 合成的被跳过, 不足 3 个不判冻结."""
    _write_snapshot(meta_dir, "2026-10-09", [REUR_ROWS[0]])
    _write_snapshot(meta_dir, "2026-10-10", [REUR_ROWS[0]])
    _write_snapshot(meta_dir, "2026-10-11", [REUR_ROWS[0]], synthetic=True)
    prior = load_recent_snapshot_prices()
    assert prior.get("REUR") == [(4.13, 62008637)] * 2
    assert is_excluded(_coin("REUR", price=4.13, mcap=62008637, chg_24h=0.0),
                       prior, now=NOW) is None


def test_frozen_quote_duplicate_symbols_count_once_per_snapshot(meta_dir):
    """同一快照内重复 symbol (如 VELO) 只取第一条: 2 个快照各 2 条不凑成 3."""
    _write_snapshot(meta_dir, "2026-10-09", [REUR_ROWS[0], REUR_ROWS[0]])
    _write_snapshot(meta_dir, "2026-10-10", [REUR_ROWS[0], REUR_ROWS[0]])
    prior = load_recent_snapshot_prices()
    assert len(prior["REUR"]) == 2
    assert is_excluded(_coin("REUR", price=4.13, mcap=62008637, chg_24h=0.0),
                       prior, now=NOW) is None
    # 补第 3 个快照 (也带重复) → 恰好 3 条, 判冻结
    _write_snapshot(meta_dir, "2026-10-11", [REUR_ROWS[0], REUR_ROWS[0]])
    prior = load_recent_snapshot_prices()
    assert len(prior["REUR"]) == 3
    assert is_excluded(_coin("REUR", price=4.13, mcap=62008637, chg_24h=0.0),
                       prior, now=NOW) == "frozen"


def test_load_recent_snapshot_prices_uses_latest_three(meta_dir):
    """多于 3 个快照时只取最新 3 个; 损坏文件被跳过."""
    for i, day in enumerate(["2026-10-05", "2026-10-06", "2026-10-07",
                             "2026-10-08", "2026-10-09"]):
        _write_snapshot(meta_dir, day, [_row("ABC", float(i), 1e9)])
    (meta_dir / "snapshot_20261010_0000.json").write_text("{not json", encoding="utf-8")
    prior = load_recent_snapshot_prices()
    assert prior["ABC"] == [(4.0, 1e9), (3.0, 1e9), (2.0, 1e9)]


def test_load_recent_snapshot_prices_missing_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(screener, "META_DIR", tmp_path / "does-not-exist")
    assert load_recent_snapshot_prices() == {}


# ────────────────────────────────────────────────────────────────────
#  score_coins: 排除计数 + 输出字段
# ────────────────────────────────────────────────────────────────────

def test_score_coins_exclusion_stats_and_fields(meta_dir):
    for day in ("2026-10-08", "2026-10-09", "2026-10-10"):
        _write_snapshot(meta_dir, day, [_row("FRZ", 0.5, 7e6, 0.0)])
    prior = load_recent_snapshot_prices()
    stale = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    coins = [
        _coin("SOL", price=150.0, cid="solana"),
        _coin("USDG", price=1.0),                       # stable (名单)
        _coin("PEGX", price=0.999, chg_7d=0.2, chg_30d=0.5),  # stable (启发式)
        _coin("WBTC", price=60000.0),                   # wrapped
        _coin("PAXG", price=2500.0),                    # non_crypto
        _coin("FRZ", price=0.5, mcap=7e6, chg_24h=0.0),  # frozen
        _coin("OLD", price=3.0, last_updated=stale),    # stale
        _coin("TINY", price=1.0, mcap=10_000),          # 基础市值过滤, 不计入
    ]
    stats: dict = {}
    scored = score_coins(coins, {}, {}, {}, prior_prices=prior,
                         exclusion_stats=stats)
    assert [c["symbol"] for c in scored] == ["SOL"]
    assert stats == {"stable": 2, "wrapped": 1, "non_crypto": 1,
                     "frozen": 1, "stale": 1}
    sol = scored[0]
    assert sol["coin_id"] == "solana"
    assert sol["last_updated"] == coins[0]["last_updated"]


# ────────────────────────────────────────────────────────────────────
#  fetch_funding_rates: Binance → OKX → Bybit
# ────────────────────────────────────────────────────────────────────

BINANCE_RESP = [
    {"symbol": "BTCUSDT", "lastFundingRate": "0.00010000"},
    {"symbol": "ETHUSDT", "lastFundingRate": "-0.00020000"},
    {"symbol": "BTCBUSD", "lastFundingRate": "0.00050000"},   # 非 USDT, 跳过
]
# 2026-10-12 实测 OKX 返回形状 (含 USD 本位和交割合约, 只取 -USDT-SWAP)
OKX_RESP = {"code": "0", "msg": "", "data": [
    {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "fundingRate": "0.0000248936",
     "nextFundingRate": "", "fundingTime": "1791748800000"},
    {"instId": "ETH-USDT-SWAP", "instType": "SWAP", "fundingRate": "-0.0001199447"},
    {"instId": "BTC-USD-SWAP", "instType": "SWAP", "fundingRate": "0.0009"},
    {"instId": "XAU-USD_UM_XPERP-310502", "instType": "SWAP", "fundingRate": "0.0"},
]}
BYBIT_RESP = {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": [
    {"symbol": "BTCUSDT", "fundingRate": "0.0001", "lastPrice": "100000"},
    {"symbol": "SOLUSDT", "fundingRate": "-0.0003"},
    {"symbol": "BTCPERP", "fundingRate": "0.0002"},               # USDC perp, 跳过
    {"symbol": "BTC-27DEC26", "fundingRate": ""},                  # 交割合约, 跳过
]}}


def _fake_get(responses: dict[str, object], calls: list[str]):
    """按 host 返回预设响应, 同时记录调用顺序."""
    def _get(url, params=None, **kw):
        for host, resp in responses.items():
            if host in url:
                calls.append(host)
                return resp
        calls.append(url)
        return None
    return _get


def test_funding_binance_first(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fx, "_get", _fake_get(
        {"binance": BINANCE_RESP, "okx": OKX_RESP, "bybit": BYBIT_RESP}, calls))
    rates = fx.fetch_funding_rates()
    assert calls == ["binance"]
    assert rates == {"BTC": 0.0001, "ETH": -0.0002}
    assert fx.FUNDING_SOURCE == "binance"


def test_funding_falls_back_to_okx_when_binance_451(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fx, "_get", _fake_get(
        {"binance": None, "okx": OKX_RESP, "bybit": BYBIT_RESP}, calls))
    rates = fx.fetch_funding_rates()
    assert calls == ["binance", "okx"]
    assert rates == pytest.approx({"BTC": 0.0000248936, "ETH": -0.0001199447})
    assert "XAU" not in rates
    assert fx.FUNDING_SOURCE == "okx"


def test_funding_falls_back_to_bybit_when_okx_errors(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fx, "_get", _fake_get(
        {"binance": None, "okx": {"code": "50011", "msg": "rate limit", "data": []},
         "bybit": BYBIT_RESP}, calls))
    rates = fx.fetch_funding_rates()
    assert calls == ["binance", "okx", "bybit"]
    assert rates == pytest.approx({"BTC": 0.0001, "SOL": -0.0003})
    assert fx.FUNDING_SOURCE == "bybit"


def test_funding_all_sources_fail_returns_empty(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fx, "_get", _fake_get(
        {"binance": None, "okx": None, "bybit": {"retCode": 10001, "result": {}}}, calls))
    assert fx.fetch_funding_rates() == {}
    assert calls == ["binance", "okx", "bybit"]
    assert fx.FUNDING_SOURCE is None


# ────────────────────────────────────────────────────────────────────
#  fetch_onchain_activity: dev/community 字段为空的告警
# ────────────────────────────────────────────────────────────────────

def _coingecko_coin(sym: str, dev: dict | None, comm: dict | None) -> dict:
    return {"id": sym.lower(), "symbol": sym.lower(),
            "developer_data": dev, "community_data": comm,
            "sentiment_votes_up_percentage": 60.0}


def test_onchain_empty_dev_data_flags_and_warns(monkeypatch, caplog):
    monkeypatch.setattr(fx.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fx, "_get", lambda url, **kw: _coingecko_coin(
        url.rsplit("/", 1)[-1], {}, None))
    monkeypatch.setattr(fx.log, "propagate", True)   # setup_logger 默认不冒泡
    with caplog.at_level(logging.WARNING, logger="factors_ext"):
        res = fx.fetch_onchain_activity(["btc", "eth"])
    assert res[fx.ONCHAIN_META_KEY] == {"coins_with_dev_data": 0,
                                         "coins_with_community_data": 0,
                                         "coins_with_any_data": 0}
    assert set(res) == {"BTC", "ETH", fx.ONCHAIN_META_KEY}
    assert any("developer_data/community_data 字段为空" in r.getMessage()
               for r in caplog.records)
    # 因子函数拿到空数据 → 0 (退化为常数), 行为与修复前一致
    assert fx.calc_dev_activity_score(res["BTC"]) == 0.0


def test_onchain_counts_coins_with_data(monkeypatch, caplog):
    monkeypatch.setattr(fx.time, "sleep", lambda *_: None)
    payloads = {
        "btc": _coingecko_coin("btc", {"commit_count_4_weeks": 120, "stars": 70000}, {}),
        "eth": _coingecko_coin("eth", {"forks": 0, "stars": 0}, {"twitter_followers": 0}),
        "sol": _coingecko_coin("sol", None, {"twitter_followers": 2_500_000}),
    }
    monkeypatch.setattr(fx, "_get", lambda url, **kw: payloads[url.rsplit("/", 1)[-1]])
    monkeypatch.setattr(fx.log, "propagate", True)
    with caplog.at_level(logging.WARNING, logger="factors_ext"):
        res = fx.fetch_onchain_activity(["btc", "eth", "sol"])
    # BTC (dev) + SOL (community) 有数据, ETH 全 0 不算
    assert res[fx.ONCHAIN_META_KEY] == {"coins_with_dev_data": 1,
                                         "coins_with_community_data": 1,
                                         "coins_with_any_data": 2}
    assert not any("字段为空" in r.getMessage() for r in caplog.records)
    assert fx.calc_dev_activity_score(res["BTC"]) > 0.5


def test_onchain_dev_only_degradation_still_warns(monkeypatch, caplog):
    """只有 community_data 有值、developer_data 全空 → dev 因子退化也要告警."""
    monkeypatch.setattr(fx.time, "sleep", lambda *_: None)
    monkeypatch.setattr(fx, "_get", lambda url, **kw: _coingecko_coin(
        url.rsplit("/", 1)[-1], {}, {"twitter_followers": 1000}))
    monkeypatch.setattr(fx.log, "propagate", True)
    with caplog.at_level(logging.WARNING, logger="factors_ext"):
        res = fx.fetch_onchain_activity(["btc", "eth"])
    assert res[fx.ONCHAIN_META_KEY] == {"coins_with_dev_data": 0,
                                         "coins_with_community_data": 2,
                                         "coins_with_any_data": 2}
    assert any("developer_data 字段为空" in r.getMessage() for r in caplog.records)
