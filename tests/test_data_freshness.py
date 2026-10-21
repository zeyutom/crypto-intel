"""行情报价 / 数据源状态的新鲜度 (C23 / C54 / C20 回归)。

- db.latest_quotes: 先新鲜度、再源优先级 (binance > okx > coingecko); 价格与
  24h 涨跌来自同一条报价。之前 ORDER BY 源优先级在前, 云端 binance 常年 451,
  2026-06-05 的 binance 旧价 (BTC $62,810) 在飞书卡片 / 仪表盘 / 日报 / Gemini
  输入 / factor_snapshots 里冻了 4.5 个月。
- db.source_status: "ok" = 该源最新 ts 距全库最新采集在容忍时长内, 过期显示
  "⚠ 过期 (last_ts)" 并进覆盖度警告; 之前只要出现过就 ✔, 表头 "数据源 10/6"。

全部用 tmp_path 下的临时 sqlite (monkeypatch db.DB_PATH), 不联网、不碰 data/。
"""
from __future__ import annotations
import pathlib
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "intel.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def _quote(ts: str, source: str, asset: str, price: float, chg: float | None) -> list[dict]:
    rows = [{"ts": ts, "source": source, "asset_id": asset, "metric": "price_usd",
             "value": price}]
    if chg is not None:
        rows.append({"ts": ts, "source": source, "asset_id": asset,
                     "metric": "change_24h_pct", "value": chg})
    return rows


def _by_asset(df: pd.DataFrame) -> dict:
    return {r["asset_id"]: r for _, r in df.iterrows()}


# 2026-10-18 的云端库: binance 只有 06-05 的旧行, okx / coingecko 每天新鲜
STALE_BINANCE_DB = (
    _quote("2026-06-05T05:45:44Z", "binance", "bitcoin", 62810.01, -2.232)
    + _quote("2026-10-18T04:56:11Z", "okx", "bitcoin", 81789.5, 0.41)
    + _quote("2026-10-18T04:56:11Z", "coingecko", "bitcoin", 81716.0, 0.52)
    + _quote("2026-06-05T05:45:44Z", "binance", "ethereum", 1694.26, -3.1)
    + _quote("2026-10-18T04:56:12Z", "coingecko", "ethereum", 2409.73, 1.25)
)


# ────────────────────────────────────────────────────────────────────
#  latest_quotes
# ────────────────────────────────────────────────────────────────────

def test_fresh_quote_beats_stale_binance(tmp_db):
    db.upsert_raw(STALE_BINANCE_DB)
    q = _by_asset(db.latest_quotes())
    assert q["bitcoin"]["source"] == "okx"
    assert q["bitcoin"]["price"] == 81789.5
    assert q["bitcoin"]["change_24h"] == pytest.approx(0.41)
    # 只有 coingecko 新鲜时用 coingecko, 价格与涨跌配套
    assert q["ethereum"]["source"] == "coingecko"
    assert (q["ethereum"]["price"], q["ethereum"]["change_24h"]) == (2409.73, 1.25)


def test_source_priority_breaks_ties_within_same_run(tmp_db):
    """同一批采集 (相差几秒) 内仍按 binance > okx > coingecko."""
    db.upsert_raw(
        _quote("2026-10-19T05:05:24Z", "binance", "bitcoin", 82557.87, 0.948)
        + _quote("2026-10-19T05:05:26Z", "okx", "bitcoin", 82555.7, 0.54)
        + _quote("2026-10-19T05:05:24Z", "coingecko", "bitcoin", 82544.0, 1.03)
    )
    q = _by_asset(db.latest_quotes())["bitcoin"]
    assert (q["source"], q["price"], q["change_24h"]) == ("binance", 82557.87, 0.948)


def test_day_old_binance_loses_despite_same_calendar_day_cutoff(tmp_db):
    """10-20 的库: binance 停在 10-19 05:05 (24h 前). 截止时间若用 datetime()
    ('2026-10-19 23:07:40'), 'T' > ' ' 会让 10-19 的整天都算新鲜, binance 照样胜出."""
    db.upsert_raw(
        _quote("2026-10-19T05:05:24Z", "binance", "bitcoin", 82557.87, 0.948)
        + _quote("2026-10-20T05:07:40Z", "okx", "bitcoin", 82019.2, 0.094)
        + _quote("2026-10-20T05:07:39Z", "coingecko", "bitcoin", 82015.0, -0.617)
    )
    q = _by_asset(db.latest_quotes())["bitcoin"]
    assert (q["source"], q["price"]) == ("okx", 82019.2)
    assert q["ts"] == "2026-10-20T05:07:40Z"


def test_change_comes_from_the_chosen_quote_only(tmp_db):
    """被选中的报价没有 change_24h_pct → NaN, 不拿别的源/更新时刻的涨跌拼凑."""
    db.upsert_raw(
        _quote("2026-10-20T05:07:40Z", "okx", "solana", 105.11, None)
        + _quote("2026-10-20T05:07:39Z", "coingecko", "solana", 105.13, 0.77)
    )
    q = _by_asset(db.latest_quotes())["solana"]
    assert (q["source"], q["price"]) == ("okx", 105.11)
    assert pd.isna(q["change_24h"])


def test_ignores_other_sources_and_metrics(tmp_db):
    db.upsert_raw(
        _quote("2026-10-20T05:07:41Z", "coinbase", "bitcoin", 81909.44, None)
        + _quote("2026-10-20T05:07:39Z", "coingecko", "bitcoin", 82015.0, -0.6)
        + [{"ts": "2026-10-20T05:07:42Z", "source": "okx", "asset_id": "bitcoin",
            "metric": "funding_rate", "value": 0.0001}]
    )
    q = _by_asset(db.latest_quotes())
    assert list(q) == ["bitcoin"]
    assert q["bitcoin"]["source"] == "coingecko"


def test_empty_db_returns_empty_frame(tmp_db):
    assert db.latest_quotes().empty


# ────────────────────────────────────────────────────────────────────
#  latest_quotes 的使用方
# ────────────────────────────────────────────────────────────────────

def test_factor_snapshot_current_price_not_frozen(tmp_db):
    from src import snapshot
    db.upsert_raw(STALE_BINANCE_DB)
    prices = snapshot._get_current_prices()
    assert prices == {"bitcoin": 81789.5, "ethereum": 2409.73}


def test_llm_brief_market_snapshot_uses_fresh_consistent_quote(tmp_db):
    from src import llm_brief
    db.upsert_raw(STALE_BINANCE_DB)
    txt = llm_brief._gather_market_snapshot()
    assert "$81,789.50 (24h +0.41%)" in txt
    assert "62,810" not in txt and "1,694" not in txt


def test_notifier_summary_prices_fresh(tmp_db, monkeypatch):
    from src import notifier
    db.upsert_raw(STALE_BINANCE_DB)
    summary = notifier._gather_summary()
    assert summary["prices"]["bitcoin"] == {"price": 81789.5, "chg": pytest.approx(0.41)}
    assert summary["prices"]["ethereum"]["price"] == 2409.73
    assert "solana" not in summary["prices"]


def test_dashboard_snapshot_and_source_status(tmp_db):
    du = pytest.importorskip("src.dashboard_utils")
    db.upsert_raw(STALE_BINANCE_DB)
    du.load_snapshot.clear()
    du.load_source_status.clear()
    snap = du.load_snapshot()
    btc = snap[snap["asset_id"] == "bitcoin"].iloc[0]
    assert (btc["price"], btc["change_24h"]) == (81789.5, pytest.approx(0.41))
    assert btc["symbol"] == "BTC"
    status = {s["source"]: s for s in du.load_source_status()}
    assert status["binance"]["ok"] is False
    assert status["binance"]["last_ts"] == "⚠ 过期 (2026-06-05 05:45)"
    assert status["coingecko"]["ok"] is True


# ────────────────────────────────────────────────────────────────────
#  source_status
# ────────────────────────────────────────────────────────────────────

def _src_row(ts: str, source: str, metric: str = "m", asset: str | None = "x") -> dict:
    return {"ts": ts, "source": source, "asset_id": asset, "metric": metric, "value": 1.0}


def test_source_status_marks_stale_and_missing(tmp_db):
    db.upsert_raw([
        _src_row("2026-10-20T05:07:39Z", "coingecko"),
        _src_row("2026-10-19T05:07:39Z", "coingecko", metric="old"),
        _src_row("2026-06-05T05:45:44Z", "binance"),
        _src_row("2026-10-19T04:00:00Z", "coinbase"),        # 25h07m: 26h 内仍新鲜
        _src_row("2026-10-19T03:00:00Z", "defillama"),       # 26h07m: 过期
        _src_row("2026-10-19T00:00:00Z", "feargreed"),       # 29h: feargreed 容忍 48h
        _src_row("2026-10-20T05:07:42Z", "okx"),             # 不在期望列表, 只做参照
        _src_row("2026-06-18T03:02:34Z", "_meta"),
    ])
    st = {s["source"]: s for s in db.source_status()}
    assert list(st) == list(db.EXPECTED_SOURCES)
    assert st["coingecko"] == {"source": "coingecko", "ok": True, "stale": False,
                               "n": 2, "last_ts": "2026-10-20 05:07"}
    assert st["binance"] == {"source": "binance", "ok": False, "stale": True,
                             "n": 1, "last_ts": "⚠ 过期 (2026-06-05 05:45)"}
    assert st["coinbase"]["ok"] is True
    assert st["defillama"]["ok"] is False and st["defillama"]["stale"] is True
    assert st["feargreed"]["ok"] is True
    assert st["farside_etf"] == {"source": "farside_etf", "ok": False, "stale": False,
                                 "n": 0, "last_ts": "—"}


def test_source_status_weekend_tolerance(tmp_db):
    """周一早上: yfinance_macro / farside_etf 最新一条是上周五, 不算过期; 超过 4 天才算."""
    db.upsert_raw([
        _src_row("2026-10-19T05:07:39Z", "coingecko"),       # 周一
        _src_row("2026-10-16T20:00:00Z", "yfinance_macro"),  # 周五收盘
        _src_row("2026-10-16T00:00:00Z", "farside_etf"),
        _src_row("2026-10-17T04:00:00Z", "feargreed"),       # 49h: 过期
    ])
    st = {s["source"]: s for s in db.source_status(
        ["coingecko", "yfinance_macro", "farside_etf", "feargreed"])}
    assert st["yfinance_macro"]["ok"] is True
    assert st["farside_etf"]["ok"] is True
    assert st["feargreed"]["ok"] is False
    db.upsert_raw([_src_row("2026-10-21T05:07:39Z", "coingecko")])   # 再过两天
    st = {s["source"]: s for s in db.source_status(["yfinance_macro"])}
    assert st["yfinance_macro"]["ok"] is False


def test_daily_report_shows_fresh_price_and_stale_source(tmp_db, tmp_path):
    from src.report import daily
    db.upsert_raw(STALE_BINANCE_DB + [
        _src_row("2026-10-18T04:56:13Z", "coinbase"),
        _src_row("2026-10-18T04:56:14Z", "defillama"),
        _src_row("2026-10-18T00:00:00Z", "feargreed"),
    ])
    out = daily.generate(output_path=tmp_path / "reports")
    html = out.read_text(encoding="utf-8")
    assert "$81,789.50" in html and "62,810" not in html
    assert "⚠ 过期 (2026-06-05 05:45)" in html
    # 新鲜的期望源: coingecko / coinbase / defillama / feargreed = 4 (binance 过期, farside 缺)
    assert "数据源 4/6" in html
    assert "本次有 2 个数据源未采集成功: binance, farside_etf" in html
    assert str(out).startswith(str(tmp_path))
