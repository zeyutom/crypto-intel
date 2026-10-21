"""Unit tests for screener 数据质量修复 (2026-10 回测审计).

覆盖:
  - is_excluded: 稳定币名单 / 锚定启发式 / wrapped+LST / 代币化股票+黄金 /
    陈旧 last_updated / 冻结报价 (最近 3 个真实快照价格+市值相同且 24h 涨跌 0;
    LPT 式 2 位小数价格撞车但市值在动的真币不被误杀)
  - load_recent_snapshot_prices: 跳过合成快照, 容忍目录缺失, 合并 _meta.excluded_rows,
    coin_id 维度历史 (同名 VELO 互不干扰)
  - run_screen 多次运行: 冻结币攒满历史后每次都排除 (修复前 4 次只排除 1 次)
  - fetch_defillama_protocols: category=CEX (交易所托管储备) 不计入 TVL
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
                         exclusion_stats=stats, now=NOW)
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


# ────────────────────────────────────────────────────────────────────
#  C07: DeFiLlama CEX 储备不算协议 TVL
# ────────────────────────────────────────────────────────────────────

def test_defillama_protocols_skip_cex_category(monkeypatch):
    """category=CEX (交易所托管储备) 不进 tvl_map; Chain 等其他类别照常累加."""
    rows = [
        {"name": "Binance CEX", "symbol": "BNB", "category": "CEX", "tvl": 171.9e9},
        {"name": "OKX", "symbol": "OKB", "category": "CEX", "tvl": 30.9e9},
        {"name": "Backpack", "symbol": "BP", "category": "CEX", "tvl": 5.8e8},
        {"name": "Aave V3", "symbol": "AAVE", "category": "Lending", "tvl": 20e9},
        {"name": "Aave V2", "symbol": "aave", "category": "Lending", "tvl": 1e9},
        {"name": "Polygon Bridge", "symbol": "POL", "category": "Chain", "tvl": 2.68e9},
        {"name": "Venus", "symbol": "XVS", "category": "Lending", "tvl": 0},
    ]
    monkeypatch.setattr(screener, "_get", lambda url, **kw: rows)
    tvl = screener.fetch_defillama_protocols()
    assert tvl == {"AAVE": 21e9, "POL": 2.68e9}


def test_cex_reserves_no_longer_max_tvl_factor(monkeypatch):
    """BP 的 5.8 亿 CEX 储备不再把 f_tvl_mcap 顶到 1.0; BNB 回退到 BSC 链 TVL."""
    monkeypatch.setattr(screener, "_get", lambda url, **kw: [
        {"symbol": "BP", "category": "CEX", "tvl": 5.8e8},
        {"symbol": "BNB", "category": "CEX", "tvl": 171.9e9},
    ])
    tvl_proto = screener.fetch_defillama_protocols()
    coins = [_coin("BP", price=0.5, mcap=1e8), _coin("BNB", price=600.0, mcap=9e10)]
    scored = {c["symbol"]: c for c in score_coins(
        coins, tvl_proto, {"BSC": 5.4e9}, {}, now=NOW)}
    assert scored["BP"]["f_tvl_mcap"] == 0.0
    assert scored["BNB"]["tvl"] == 5.4e9
    assert scored["BNB"]["f_tvl_mcap"] == pytest.approx(5.4e9 / 9e10 / 0.5, abs=1e-3)


# ────────────────────────────────────────────────────────────────────
#  C53: 被排除的冻结币仍延续历史 → 每次都排除 (不再 4 天 1 次)
# ────────────────────────────────────────────────────────────────────

def _write_snapshot_with_meta(meta_dir: pathlib.Path, name: str, rows: list[dict],
                              excluded_rows: list[dict] | None = None,
                              n_pad: int = 120):
    coins = list(rows) + [_row(f"PAD{i}", float(i + 1), 1e8) for i in range(n_pad)]
    snap = {"timestamp": f"{name}T22:00:00+00:00", "date": name,
            "factor_weights": {}, "coins": coins,
            "_meta": {"excluded_rows": excluded_rows or []}}
    (meta_dir / f"snapshot_{name.replace('-', '')}_2200.json").write_text(
        json.dumps(snap), encoding="utf-8")


def test_load_recent_snapshot_prices_merges_excluded_rows(meta_dir):
    """_meta.excluded_rows 里的报价和 coins 一样计入历史, 但同一快照只计一次."""
    frz = {"symbol": "ALFW", "coin_id": "alpha-bulgaria-warrants", "price": 3.2,
           "market_cap": 114330001, "reason": "frozen"}
    _write_snapshot_with_meta(meta_dir, "2026-10-13", [dict(frz)])
    _write_snapshot_with_meta(meta_dir, "2026-10-14", [], excluded_rows=[frz])
    _write_snapshot_with_meta(meta_dir, "2026-10-15", [dict(frz)], excluded_rows=[frz])
    prior = load_recent_snapshot_prices()
    assert prior["ALFW"] == [(3.2, 114330001)] * 3
    assert prior[screener._id_key("alpha-bulgaria-warrants")] == [(3.2, 114330001)] * 3
    c = _coin("ALFW", price=3.2, mcap=114330001, chg_24h=0.0,
              cid="alpha-bulgaria-warrants")
    assert is_excluded(c, prior, now=NOW) == "frozen"


def test_frozen_history_keyed_by_coin_id_not_symbol(meta_dir):
    """两个同名 VELO: 按 symbol 只能记第一条, 按 coin_id 各有各的历史."""
    velo = {"symbol": "VELO", "coin_id": "velo", "price": 0.00407,
            "market_cap": 71474964, "change_24h": 0.0}
    velodrome = {"symbol": "VELO", "coin_id": "velodrome-finance", "price": 0.03473,
                 "market_cap": 46874331, "change_24h": 0.0}
    for day in ("2026-10-13", "2026-10-14", "2026-10-15"):
        _write_snapshot_with_meta(meta_dir, day, [dict(velo), dict(velodrome)])
    prior = load_recent_snapshot_prices()
    assert prior["VELO"] == [(0.00407, 71474964)] * 3          # symbol: 每快照第一条
    assert prior[screener._id_key("velodrome-finance")] == [(0.03473, 46874331)] * 3
    frozen_velodrome = _coin("VELO", price=0.03473, mcap=46874331, chg_24h=0.0,
                             cid="velodrome-finance")
    # 修复前按 symbol 只能看到 velo 的历史, velodrome 的冻结报价永远漏判
    assert is_excluded(frozen_velodrome, prior, now=NOW) == "frozen"
    # 旧快照无 coin_id 时仍按 symbol 回退
    assert screener._prior_history(prior, "", "VELO") == [(0.00407, 71474964)] * 3


def test_score_coins_reports_excluded_rows_for_frozen_and_stale(meta_dir):
    for day in ("2026-10-08", "2026-10-09", "2026-10-10"):
        _write_snapshot(meta_dir, day, [_row("FRZ", 0.5, 7e6, 0.0)])
    prior = load_recent_snapshot_prices()
    stale = (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    coins = [
        _coin("SOL", price=150.0, cid="solana"),
        _coin("USDG", price=1.0),                                   # stable: 不记
        _coin("FRZ", price=0.5, mcap=7e6, chg_24h=0.0, cid="frz-coin"),
        _coin("OLD", price=3.0, mcap=2e7, last_updated=stale, cid="old-coin"),
    ]
    rows: list[dict] = []
    scored = score_coins(coins, {}, {}, {}, prior_prices=prior,
                         excluded_rows=rows, now=NOW)
    assert [c["symbol"] for c in scored] == ["SOL"]
    assert rows == [
        {"symbol": "FRZ", "coin_id": "frz-coin", "price": 0.5,
         "market_cap": 7e6, "reason": "frozen"},
        {"symbol": "OLD", "coin_id": "old-coin", "price": 3.0,
         "market_cap": 2e7, "reason": "stale"},
    ]


def _fake_universe(run: int, frozen: dict) -> list[dict]:
    """一次 CoinGecko 拉取: 120 个每天都在动的币 + 一个报价冻结的币."""
    lu = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    coins = [_coin(f"MOV{i}", price=2.0 + i + run * 0.37, mcap=5e8 + i * 1e6 + run * 7e5,
                   cid=f"mover-{i}", last_updated=lu) for i in range(120)]
    coins.append(_coin(frozen["symbol"], price=frozen["price"], mcap=frozen["mcap"],
                       chg_24h=0.0, chg_7d=0.0, chg_30d=0.0, cid=frozen["id"],
                       last_updated=lu))
    return coins


def _patch_run_screen_deps(monkeypatch, tmp_path, meta_dir):
    """run_screen 的网络/元学习依赖全部打桩; 快照写到 meta_dir (每次运行一个文件)."""
    from src.research import meta_learner as ml, factor_bridge as fb, onchain_real as oc
    monkeypatch.setattr(screener, "DATA_DIR", tmp_path / "research")
    monkeypatch.setattr(ml, "META_DIR", meta_dir)
    monkeypatch.setattr(screener, "fetch_defillama_protocols", lambda: {})
    monkeypatch.setattr(screener, "fetch_defillama_chains", lambda: {})
    monkeypatch.setattr(screener, "fetch_binance_volumes", lambda: {})
    monkeypatch.setattr(fx, "fetch_onchain_activity", lambda ids: {})
    monkeypatch.setattr(fx, "fetch_funding_rates", lambda: {})
    monkeypatch.setattr(oc, "fetch_real_onchain_data", lambda: {})
    monkeypatch.setattr(ml, "load_factor_config", lambda: {})
    monkeypatch.setattr(ml, "get_current_weights", lambda cfg=None: {"momentum_30d": 1.0})
    # **kw: 兼容 detect_regime(btc, prev_regime=...) / generate_factor_report(regime=...)
    monkeypatch.setattr(ml, "detect_regime", lambda btc=None, **kw: "sideways")
    monkeypatch.setattr(ml, "apply_regime_adjustment", lambda w, r: dict(w))
    monkeypatch.setattr(ml, "generate_factor_report", lambda **kw: {})
    monkeypatch.setattr(fb, "load_pipeline_signals", lambda: {})
    monkeypatch.setattr(fb, "calc_market_overlay", lambda s: 1.0)
    monkeypatch.setattr(fb, "get_pipeline_summary", lambda s: [])

    counter = {"n": 0}

    def _save_snapshot(scored, weights, meta=None):
        counter["n"] += 1
        snap = {"timestamp": f"2026-10-{10 + counter['n']:02d}T22:00:00+00:00",
                "factor_weights": weights, "_meta": dict(meta or {}),
                "coins": [{"symbol": c["symbol"], "coin_id": c["coin_id"],
                           "price": c["price"], "market_cap": c["market_cap"]}
                          for c in scored]}
        path = meta_dir / f"snapshot_202610{10 + counter['n']:02d}_2200.json"
        path.write_text(json.dumps(snap), encoding="utf-8")
        return path
    monkeypatch.setattr(ml, "save_snapshot", _save_snapshot)


def test_frozen_coin_excluded_on_every_run_after_detection(monkeypatch, tmp_path, meta_dir):
    """ALFW 式冻结报价: 攒满 3 个快照后, 之后每一次运行都排除 (修复前 4 次只排除 1 次).

    排除的币写进快照 _meta.excluded_rows, 但不进 coins (IC / 记分卡输入不变)。
    """
    _patch_run_screen_deps(monkeypatch, tmp_path, meta_dir)
    frozen = {"symbol": "ALFW", "id": "alpha-bulgaria-warrants",
              "price": 3.2, "mcap": 114330001}
    in_coins, excluded_frozen = [], []
    for run in range(7):
        monkeypatch.setattr(screener, "fetch_coingecko_top500",
                            lambda run=run: _fake_universe(run, frozen))
        res = screener.run_screen(top_n=10)
        assert res["ok"]
        snap = json.loads(pathlib.Path(res["snapshot_path"]).read_text())
        in_coins.append(any(c["symbol"] == "ALFW" for c in snap["coins"]))
        excluded_frozen.append(res["data_sources"]["excluded"]["frozen"])
        if excluded_frozen[-1]:
            assert snap["_meta"]["excluded_rows"] == [{
                "symbol": "ALFW", "coin_id": "alpha-bulgaria-warrants",
                "price": 3.2, "market_cap": 114330001, "reason": "frozen"}]
        else:
            assert snap["_meta"]["excluded_rows"] == []
        # 动来动去的 120 个币从不被误判
        assert sum(c["symbol"].startswith("MOV") for c in snap["coins"]) == 120
    # 前 3 次历史不足 → 打分; 第 4 次起每次都排除 (修复前第 5-7 次会漏回来)
    assert in_coins == [True, True, True, False, False, False, False]
    assert excluded_frozen == [0, 0, 0, 1, 1, 1, 1]


# ────────────────────────────────────────────────────────────────────
#  影响力上限: 单个因子不能独占排名 (2026-10-20 审计: tvl_mcap 影响力 0.87)
# ────────────────────────────────────────────────────────────────────

def _tvl_dominated_universe(n: int = 400) -> tuple[list[dict], dict]:
    """n 个币: 动量连续分布; 一半的币 TVL = 市值 (f_tvl 封顶 1.0), 另一半没有 TVL."""
    lu = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    coins, tvl = [], {}
    for i in range(n):
        sym = f"C{i:03d}"
        coins.append(_coin(sym, price=1.0 + i * 0.01, chg_7d=(i % 37) - 18,
                           chg_30d=(i * 7 % 61) - 30, cid=f"c-{i}",
                           mcap=2e8 + i * 1e6, last_updated=lu))
        if i % 2 == 0:
            tvl[sym] = 2e8 + i * 1e6
    return coins, tvl


def test_run_screen_caps_dominant_factor_influence(monkeypatch, tmp_path, meta_dir):
    """tvl_mcap 名义权重只有一半, 但二值分布让它独占综合分方差 → 上限触发,
    按新权重重新打分; 快照/结果记录上限前后的权重和影响力。"""
    from src.research import meta_learner as ml
    _patch_run_screen_deps(monkeypatch, tmp_path, meta_dir)
    coins, tvl = _tvl_dominated_universe()
    coins.append(_coin("USDT", price=1.0, chg_7d=0.0, chg_30d=0.0, cid="tether", mcap=1e11))
    w0 = {"momentum_30d": 0.5, "tvl_mcap": 0.5}
    monkeypatch.setattr(ml, "get_current_weights", lambda cfg=None: dict(w0))
    monkeypatch.setattr(screener, "fetch_coingecko_top500", lambda: coins)
    monkeypatch.setattr(screener, "fetch_defillama_protocols", lambda: dict(tvl))

    # 上限前: tvl 确实独占
    pre = screener.score_coins(coins, tvl, {}, {}, weights=w0)
    assert ml.influence_shares(pre, w0)["tvl_mcap"] > 0.7

    res = screener.run_screen(top_n=10)
    assert res["ok"]
    assert res["influence_cap_applied"]
    assert set(res["influence_cap_applied"]) == {"tvl_mcap"}
    w = res["weights_used"]
    assert w["tvl_mcap"] < 0.5 < w["momentum_30d"]
    assert abs(sum(w.values()) - 1.0) < 1e-5          # 权重总和不变 → 综合分量级不变
    assert max(res["influence"].values()) <= ml.INFLUENCE_CAP + 1e-3

    snap = json.loads(pathlib.Path(res["snapshot_path"]).read_text())
    m = snap["_meta"]
    assert m["weights_pre_cap"] == w0
    assert m["weights_in_force"] == w and snap["factor_weights"] == w
    assert m["influence_cap_applied"] == res["influence_cap_applied"]
    assert m["influence"] == res["influence"]
    # 上限触发后重新打分了两次: 排除计数和排除明细不能被累加
    assert res["data_sources"]["excluded"]["stable"] == 1
    assert sum(res["data_sources"]["excluded"].values()) == 1
    assert not any(c["symbol"] == "USDT" for c in snap["coins"])


def test_run_screen_cap_not_applied_when_balanced(monkeypatch, tmp_path, meta_dir):
    """没有因子越过上限时权重原样使用, 不重新打分, 记录空的 cap 日志."""
    from src.research import meta_learner as ml
    _patch_run_screen_deps(monkeypatch, tmp_path, meta_dir)
    coins, _ = _tvl_dominated_universe()
    w0 = {"momentum_30d": 0.53, "momentum_7d": 0.47}
    # 前提: 这组权重下没有因子越线
    pre = screener.score_coins(coins, {}, {}, {}, weights=w0)
    assert max(ml.influence_shares(pre, w0).values()) < ml.INFLUENCE_CAP
    monkeypatch.setattr(ml, "get_current_weights", lambda cfg=None: dict(w0))
    monkeypatch.setattr(screener, "fetch_coingecko_top500", lambda: coins)
    calls = {"n": 0}
    real = screener.score_coins

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)
    monkeypatch.setattr(screener, "score_coins", counting)

    res = screener.run_screen(top_n=10)
    assert res["ok"] and res["influence_cap_applied"] == {}
    assert res["weights_used"] == w0 and calls["n"] == 1
    assert max(res["influence"].values()) <= ml.INFLUENCE_CAP
