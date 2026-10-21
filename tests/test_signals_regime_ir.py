"""Unit tests for 信号层 regime 识别 + IR 动态权重.

回归 (2026-10 4.5 个月实盘回测):
  * etf_flow_5d 从未入库 (farside 被封) 却被默认成 0 → 1770/1770 行 regime=CHOP;
  * get_latest_ir_weights 对全历史平均、丢弃负 IR、不封顶 (funding_composite IR 13.9),
    _effective_weights 取 max(default, IR) → 只会加权, 负 IR 因子仍满权重。
不碰真实 DB: 全部 monkeypatch query_df / 辅助函数。
"""
import sys
import json
import pathlib

import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.signals import composite as comp
from src.review import backtest as bt


def _factors_df(fg=None, etf=None) -> pd.DataFrame:
    rows = []
    if fg is not None:
        rows.append({"factor": "fear_greed_reversal", "raw_value": fg})
    if etf is not None:
        rows.append({"factor": "etf_flow_5d", "raw_value": etf})
    return pd.DataFrame(rows, columns=["factor", "raw_value"])


# ────────────────────────────────────────────────────────────────────
#  _detect_regime
# ────────────────────────────────────────────────────────────────────

def test_regime_from_etf_when_present(monkeypatch):
    """ETF + F&G 都在 → 走原 ETF 规则, source=etf+fg (不动 BTC 动量)."""
    monkeypatch.setattr(comp, "query_df", lambda *a, **k: _factors_df(fg=50, etf=800))
    monkeypatch.setattr(comp, "_btc_price_changes",
                        lambda: pytest.fail("ETF 在场时不应查 BTC 动量"))
    assert comp._detect_regime() == ("BULL", "etf+fg")

    monkeypatch.setattr(comp, "query_df", lambda *a, **k: _factors_df(fg=80, etf=-100))
    assert comp._detect_regime() == ("CRISIS", "etf+fg")

    monkeypatch.setattr(comp, "query_df", lambda *a, **k: _factors_df(fg=50, etf=100))
    assert comp._detect_regime() == ("CHOP", "etf+fg")


@pytest.mark.parametrize("chg_30d, chg_7d, expected", [
    (20.0, 5.0, "BULL"),        # bull
    (-20.0, -5.0, "BEAR"),      # bear
    (2.0, 1.0, "CHOP"),         # sideways
    (10.0, -5.0, "VOLATILE"),   # volatile
])
def test_regime_btc_momentum_when_etf_absent(monkeypatch, chg_30d, chg_7d, expected):
    """etf_flow_5d 缺失 → 不再当 0, 用 BTC 动量 (meta_learner 阈值) 映射."""
    monkeypatch.setattr(comp, "query_df", lambda *a, **k: _factors_df(fg=50, etf=None))
    monkeypatch.setattr(comp, "_btc_price_changes",
                        lambda: {"change_30d": chg_30d, "change_7d": chg_7d})
    assert comp._detect_regime() == (expected, "btc_momentum")


def test_regime_btc_momentum_even_when_fg_missing(monkeypatch):
    """F&G 也缺 → 仍可由 BTC 动量给出 regime."""
    monkeypatch.setattr(comp, "query_df", lambda *a, **k: _factors_df())
    monkeypatch.setattr(comp, "_btc_price_changes",
                        lambda: {"change_30d": 20.0, "change_7d": 5.0})
    assert comp._detect_regime() == ("BULL", "btc_momentum")


def test_regime_unknown_when_nothing(monkeypatch):
    """ETF 缺 + BTC 价格缺 → UNKNOWN / none (而不是 CHOP)."""
    monkeypatch.setattr(comp, "query_df", lambda *a, **k: _factors_df(fg=50, etf=None))
    monkeypatch.setattr(comp, "_btc_price_changes", lambda: None)
    assert comp._detect_regime() == ("UNKNOWN", "none")

    monkeypatch.setattr(comp, "query_df", lambda *a, **k: pd.DataFrame())
    assert comp._detect_regime() == ("UNKNOWN", "none")


def test_btc_price_changes_from_raw_metrics(monkeypatch):
    """_btc_price_changes 从日级 price_usd 算 7d/30d 涨跌幅 (binance 优先)."""
    dates = pd.date_range("2026-09-01", periods=40, freq="D", tz="UTC")
    rows = []
    for i, d in enumerate(dates):
        rows.append({"ts": d.strftime("%Y-%m-%dT%H:%M:%SZ"), "price": 100.0 + i, "source": "binance"})
        # 同日 coingecko 的干扰价, 不应被采用
        rows.append({"ts": d.strftime("%Y-%m-%dT%H:%M:%SZ"), "price": 999.0, "source": "coingecko"})
    monkeypatch.setattr(comp, "query_df", lambda *a, **k: pd.DataFrame(rows))
    chg = comp._btc_price_changes()
    # 最后一天 139, 7 天前 132, 30 天前 109
    assert chg["change_7d"] == pytest.approx((139 / 132 - 1) * 100)
    assert chg["change_30d"] == pytest.approx((139 / 109 - 1) * 100)


def test_btc_price_changes_insufficient_history(monkeypatch):
    """历史不足 30 天 → None (让 _detect_regime 退回 UNKNOWN)."""
    dates = pd.date_range("2026-09-01", periods=10, freq="D", tz="UTC")
    rows = [{"ts": d.strftime("%Y-%m-%dT%H:%M:%SZ"), "price": 100.0, "source": "okx"} for d in dates]
    monkeypatch.setattr(comp, "query_df", lambda *a, **k: pd.DataFrame(rows))
    assert comp._btc_price_changes() is None
    monkeypatch.setattr(comp, "query_df", lambda *a, **k: pd.DataFrame())
    assert comp._btc_price_changes() is None


# ────────────────────────────────────────────────────────────────────
#  get_latest_ir_weights
# ────────────────────────────────────────────────────────────────────

def _perf_df(rows) -> pd.DataFrame:
    cols = ["date", "factor", "asset_id", "window_days", "forward_days", "ir", "n_obs"]
    return pd.DataFrame(rows, columns=cols)


def _day(offset: int) -> str:
    """相对今天 (UTC) 的日期字符串; 时效过滤以今天为基准, 测试不能写死日期."""
    return (pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=offset)).strftime("%Y-%m-%d")


TODAY, YDAY = _day(0), _day(1)


def test_ir_weights_latest_date_only(monkeypatch):
    """只用每个 (factor, asset_id) 最大 date 的记录, 旧日期不参与平均."""
    monkeypatch.setattr(bt, "query_df", lambda *a, **k: _perf_df([
        (_day(20), "f", "bitcoin", 30, 1, 1.5, 100),   # 非最新日 → 忽略
        (TODAY, "f", "bitcoin", 30, 1, 0.4, 100),
        (TODAY, "f", "bitcoin", 90, 7, 0.8, 100),
        (YDAY, "g", "ethereum", 30, 1, -0.5, 100),  # g 的最新日是昨天
    ]))
    w = bt.get_latest_ir_weights()
    assert w[("f", "bitcoin")] == pytest.approx(0.6)
    assert w[("g", "ethereum")] == pytest.approx(-0.5)


def test_ir_weights_n_eff_filter(monkeypatch):
    """n_eff = n_obs / forward_days < 10 的记录被丢弃 (重叠窗口的假样本)."""
    monkeypatch.setattr(bt, "query_df", lambda *a, **k: _perf_df([
        (TODAY, "funding_composite", "bitcoin", 30, 7, 1.9, 19),   # n_eff 2.7 → 丢
        (TODAY, "funding_composite", "bitcoin", 30, 1, 0.3, 19),   # n_eff 19 → 留
        (TODAY, "x", "bitcoin", 30, 30, 1.0, 290),                # n_eff 9.67 → 丢
        (TODAY, "y", "bitcoin", 30, None, 1.0, 290),              # forward 缺 → 丢
        (TODAY, "z", "bitcoin", 30, 1, None, 290),                # ir 缺 → 丢
    ]))
    w = bt.get_latest_ir_weights()
    assert w == {("funding_composite", "bitcoin"): pytest.approx(0.3)}


def test_ir_weights_clip_and_negative(monkeypatch):
    """IR clip 到 [-2, 2] 后再平均; 负 IR 也返回; asset_id NULL 可作 key."""
    monkeypatch.setattr(bt, "query_df", lambda *a, **k: _perf_df([
        (TODAY, "f", "bitcoin", 30, 1, 13.9, 100),
        (TODAY, "f", "bitcoin", 90, 1, 0.5, 100),
        (TODAY, "n", "bitcoin", 30, 1, -2.21, 100),
        (TODAY, "m", None, 30, 1, -0.72, 100),
    ]))
    w = bt.get_latest_ir_weights()
    assert w[("f", "bitcoin")] == pytest.approx(1.25)       # (2 + 0.5) / 2
    assert w[("n", "bitcoin")] == pytest.approx(-2.0)
    assert w[("m", None)] == pytest.approx(-0.72)


def test_ir_weights_empty(monkeypatch):
    monkeypatch.setattr(bt, "query_df", lambda *a, **k: pd.DataFrame())
    assert bt.get_latest_ir_weights() == {}


def test_ir_weights_zero_sentinel_dropped(monkeypatch):
    """ir == 0.0 是 backtest_factor "算不出" 的占位值, 不能当中性 IR 平均进去."""
    monkeypatch.setattr(bt, "query_df", lambda *a, **k: _perf_df([
        (TODAY, "f", "bitcoin", 30, 1, 0.0, 100),
        (TODAY, "f", "bitcoin", 90, 1, 0.8, 100),
        (TODAY, "z", "bitcoin", 30, 1, 0.0, 100),   # 全是占位值 → 整个因子不参与
    ]))
    w = bt.get_latest_ir_weights()
    assert w == {("f", "bitcoin"): pytest.approx(0.8)}


def test_ir_weights_stale_factor_dropped(monkeypatch):
    """最新计算日早于 IR_MAX_AGE_DAYS 的因子 (数据源已断) 不再参与调权."""
    monkeypatch.setattr(bt, "query_df", lambda *a, **k: _perf_df([
        (_day(bt.IR_MAX_AGE_DAYS + 5), "old", "bitcoin", 30, 1, 1.5, 100),
        (_day(bt.IR_MAX_AGE_DAYS - 1), "fresh", "bitcoin", 30, 1, 0.5, 100),
    ]))
    w = bt.get_latest_ir_weights()
    assert ("old", "bitcoin") not in w
    assert w[("fresh", "bitcoin")] == pytest.approx(0.5)


# ────────────────────────────────────────────────────────────────────
#  _effective_weights
# ────────────────────────────────────────────────────────────────────

def test_effective_weights_negative_ir_lowers(monkeypatch):
    """负 IR → 低于默认; 正 IR → 高于默认; 无 IR 的因子保持默认."""
    monkeypatch.setattr(bt, "get_latest_ir_weights", lambda: {
        ("coinbase_premium", "bitcoin"): -0.72,
        ("coinbase_premium", "ethereum"): -1.28,     # mean -1.0 → ×0.75
        ("funding_composite", "bitcoin"): 1.0,       # ×1.25
    })
    w = comp._effective_weights()
    d = comp.DEFAULT_WEIGHTS
    assert w["coinbase_premium"] == pytest.approx(d["coinbase_premium"] * 0.75, abs=1e-4)
    assert w["funding_composite"] == pytest.approx(d["funding_composite"] * 1.25, abs=1e-4)
    assert w["stablecoin_mint_7d"] == d["stablecoin_mint_7d"]


def test_effective_weights_bounds(monkeypatch):
    """乘数限制在 [0.5, 1.5]: IR=+2 → 1.5×, IR=-2 → 0.5× (即便 IR 超出也不会更极端)."""
    monkeypatch.setattr(bt, "get_latest_ir_weights", lambda: {
        ("funding_composite", "bitcoin"): 2.0,
        ("fear_greed_reversal", "bitcoin"): -2.0,
        ("etf_flow_5d", "bitcoin"): 13.9,            # 防御: 即使上游没 clip
        ("new_factor", "bitcoin"): -9.0,             # 未在 DEFAULT 中 → 基准 0.5
    })
    w = comp._effective_weights()
    d = comp.DEFAULT_WEIGHTS
    assert w["funding_composite"] == pytest.approx(d["funding_composite"] * 1.5, abs=1e-4)
    assert w["fear_greed_reversal"] == pytest.approx(d["fear_greed_reversal"] * 0.5, abs=1e-4)
    assert w["etf_flow_5d"] == pytest.approx(d["etf_flow_5d"] * 1.5, abs=1e-4)
    assert w["new_factor"] == pytest.approx(0.25, abs=1e-4)


def test_effective_weights_fallback_default(monkeypatch):
    """无 IR 数据 / 回测抛错 → 原样 DEFAULT_WEIGHTS."""
    monkeypatch.setattr(bt, "get_latest_ir_weights", lambda: {})
    assert comp._effective_weights() == comp.DEFAULT_WEIGHTS

    def _boom():
        raise RuntimeError("no table")
    monkeypatch.setattr(bt, "get_latest_ir_weights", _boom)
    assert comp._effective_weights() == comp.DEFAULT_WEIGHTS


# ────────────────────────────────────────────────────────────────────
#  compose: regime_source 落到行 + factor_breakdown._meta, 不破坏 signals 插入
# ────────────────────────────────────────────────────────────────────

def test_compose_attaches_regime_source(monkeypatch):
    captured = {}
    monkeypatch.setattr(comp, "latest_factors", lambda: pd.DataFrame([
        {"asset_id": "bitcoin", "factor": "funding_composite", "raw_value": 0.01,
         "signal": 1, "confidence": 0.8},
        {"asset_id": "bitcoin", "factor": "coinbase_premium", "raw_value": float("nan"),
         "signal": -1, "confidence": 0.5},
    ]))
    monkeypatch.setattr(comp, "_detect_regime", lambda: ("VOLATILE", "btc_momentum"))
    monkeypatch.setattr(comp, "_effective_weights", lambda: dict(comp.DEFAULT_WEIGHTS))
    monkeypatch.setattr(comp, "upsert_signal", lambda rows: captured.setdefault("rows", rows))

    out = comp.compose()
    assert len(out) == 1
    row = out[0]
    assert row["regime"] == "VOLATILE"
    assert row["regime_source"] == "btc_momentum"
    bd = json.loads(row["factor_breakdown"])
    assert bd["_meta"]["regime_source"] == "btc_momentum"
    assert bd["_meta"]["signal"] == 0                  # report.daily 的 drivers 过滤会跳过
    assert bd["coinbase_premium"]["raw_value"] is None  # NaN → null, 不写 NaN 进 JSON
    assert "NaN" not in row["factor_breakdown"]
    assert captured["rows"] is out


def test_compose_rows_insertable_with_extra_key(tmp_path):
    """带 regime_source 额外键的行能原样插入 signals 表 (sqlite 按名绑定忽略多余键)."""
    import sqlite3
    from src.db import SCHEMA
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.executescript(SCHEMA)
    conn.executemany(
        """INSERT OR REPLACE INTO signals
           (ts, asset_id, composite, direction, confidence, regime, factor_breakdown)
           VALUES (:ts, :asset_id, :composite, :direction, :confidence, :regime, :factor_breakdown)""",
        [{"ts": "2026-10-12T00:00:00Z", "asset_id": "bitcoin", "composite": 0.1,
          "direction": "NEUTRAL", "confidence": 0.5, "regime": "CHOP",
          "regime_source": "btc_momentum", "factor_breakdown": "{}"}],
    )
    assert conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    conn.close()


def test_momentum_regime_uses_last_signal_regime_for_hysteresis(monkeypatch):
    """推送卡片上的 regime 也要迟滞: 上一次是 VOLATILE, BTC 7d 回落到 4.8% → 仍是 VOLATILE."""
    import pandas as pd

    def fake_query(sql, *a, **k):
        if "FROM signals" in sql:
            return pd.DataFrame({"regime": ["VOLATILE"]})
        return _factors_df(fg=50, etf=None)

    monkeypatch.setattr(comp, "query_df", fake_query)
    monkeypatch.setattr(comp, "_btc_price_changes",
                        lambda: {"change_30d": 2.0, "change_7d": 4.8})
    assert comp._detect_regime() == ("VOLATILE", "btc_momentum")
    # 读不到上一次 → 不加迟滞
    monkeypatch.setattr(comp, "_last_momentum_regime", lambda: None)
    assert comp._detect_regime() == ("CHOP", "btc_momentum")
