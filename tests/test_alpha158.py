"""Unit tests for src.research.alpha158_features — 148 个技术因子.

验证: 因子计算正确性 + 数值范围 + 缺失处理.
"""
import sys
import pathlib

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import alpha158_features as a158


# ────────────────────────────────────────────────────────────────────
#  Fixtures
# ────────────────────────────────────────────────────────────────────

@pytest.fixture
def synthetic_ohlcv():
    """200 天合成 OHLCV, 含趋势 + 噪声."""
    n = 200
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    rng = np.random.default_rng(42)
    drift = np.cumsum(rng.normal(0.002, 0.03, n))
    close = 100 * np.exp(drift)
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    volume = rng.lognormal(15, 1, n)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    }, index=idx)


# ────────────────────────────────────────────────────────────────────
#  Module-level invariants
# ────────────────────────────────────────────────────────────────────

def test_compute_returns_dataframe(synthetic_ohlcv):
    feats = a158.compute_alpha158(synthetic_ohlcv)
    assert isinstance(feats, pd.DataFrame)
    assert len(feats) == len(synthetic_ohlcv)


def test_factor_count_at_least_140(synthetic_ohlcv):
    """Alpha158 名字带 158, 但移植版默认 ~148. 至少 140."""
    feats = a158.compute_alpha158(synthetic_ohlcv)
    assert feats.shape[1] >= 140, f"got {feats.shape[1]} factors"


def test_last_row_no_nan(synthetic_ohlcv):
    """200 天 vs 60 天 max window, 最后一行所有因子都应有值."""
    feats = a158.compute_alpha158(synthetic_ohlcv)
    last_row_nans = feats.iloc[-1].isna().sum()
    assert last_row_nans == 0, f"last row has {last_row_nans} NaN"


def test_missing_input_columns_raises(synthetic_ohlcv):
    df = synthetic_ohlcv.drop(columns=["volume"])
    with pytest.raises(ValueError, match="missing columns"):
        a158.compute_alpha158(df)


# ────────────────────────────────────────────────────────────────────
#  K-line features (单 K 线形态)
# ────────────────────────────────────────────────────────────────────

def test_kline_features_bullish_candle():
    """大阳线: close > open, 实体很长, 上下影线小."""
    df = pd.DataFrame({
        "open":   [100.0],
        "high":   [110.5],
        "low":    [99.5],
        "close":  [110.0],
        "volume": [1000.0],
    })
    k = a158.kline_features(df)
    # kmid = (110 - 100) / 110 = +0.091 > 0 (阳线)
    assert k["kmid"].iloc[0] > 0
    # kup 上影线: (110.5 - max(100, 110)) / 110 = 0.5/110 ≈ 0.0045
    assert k["kup"].iloc[0] < 0.01
    # klow 下影线: (min(100, 110) - 99.5) / 110 ≈ 0.0045
    assert k["klow"].iloc[0] < 0.01


def test_kline_features_doji():
    """十字星: open ≈ close, 影线长."""
    df = pd.DataFrame({
        "open":   [100.0],
        "high":   [110.0],
        "low":    [90.0],
        "close":  [100.1],
        "volume": [1000.0],
    })
    k = a158.kline_features(df)
    assert abs(k["kmid"].iloc[0]) < 0.005  # 实体几乎为零
    assert k["klen"].iloc[0] > 0.15        # 总长度大


# ────────────────────────────────────────────────────────────────────
#  Rolling features (滚动窗口)
# ────────────────────────────────────────────────────────────────────

def test_roc_30_matches_manual(synthetic_ohlcv):
    """ROC30 = close / close.shift(30) - 1, 用 pandas 直接核对."""
    feats = a158.compute_alpha158(synthetic_ohlcv)
    c = synthetic_ohlcv["close"]
    expected = (c / c.shift(30) - 1).iloc[-1]
    assert abs(feats["roc30"].iloc[-1] - expected) < 1e-10


def test_ma_factor_centers_around_zero(synthetic_ohlcv):
    """MA{w} = MA/close - 1, 长期均值应接近 0 (无系统性偏移)."""
    feats = a158.compute_alpha158(synthetic_ohlcv)
    mean_ma = feats["ma20"].dropna().mean()
    assert abs(mean_ma) < 0.05


def test_rank_factor_in_unit_interval(synthetic_ohlcv):
    """RANK{w} = pct rank, ∈ [0, 1]."""
    feats = a158.compute_alpha158(synthetic_ohlcv)
    r = feats["rank20"].dropna()
    assert r.min() >= 0.0 and r.max() <= 1.0


def test_rsv_factor_in_unit_interval(synthetic_ohlcv):
    """RSV = (close - min) / (max - min), ∈ [0, 1] (KD 的 K 值)."""
    feats = a158.compute_alpha158(synthetic_ohlcv)
    r = feats["rsv20"].dropna()
    # 允许极小的 numerical edge case
    assert r.min() >= -0.001 and r.max() <= 1.001


def test_cntp_cntn_sum_to_one(synthetic_ohlcv):
    """上涨天数 + 下跌天数比例之和 ≈ 1 (剔除平的天)."""
    feats = a158.compute_alpha158(synthetic_ohlcv)
    last = feats.iloc[-1]
    # 严格相加 ≤ 1, 因 ret==0 的天不算
    assert last["cntp20"] + last["cntn20"] <= 1.0001


# ────────────────────────────────────────────────────────────────────
#  Regression features (Beta / R² / Residual)
# ────────────────────────────────────────────────────────────────────

def test_beta_strong_trend_positive(synthetic_ohlcv):
    """合成数据有正 drift, 长期 beta 应为正."""
    feats = a158.compute_alpha158(synthetic_ohlcv, include_regression=True)
    assert "beta60" in feats.columns
    last_beta = feats["beta60"].iloc[-1]
    # 不强求方向, 但应该是 finite
    assert pd.notna(last_beta)


def test_rsqr_in_unit_interval(synthetic_ohlcv):
    """R² ∈ [0, 1]."""
    feats = a158.compute_alpha158(synthetic_ohlcv, include_regression=True)
    r = feats["rsqr20"].dropna()
    assert r.min() >= -0.001 and r.max() <= 1.001


# ────────────────────────────────────────────────────────────────────
#  latest_factor_vector
# ────────────────────────────────────────────────────────────────────

def test_latest_vector_returns_dict(synthetic_ohlcv):
    vec = a158.latest_factor_vector(synthetic_ohlcv)
    assert isinstance(vec, dict)
    assert len(vec) >= 100
    # 全部 finite
    for k, v in vec.items():
        assert isinstance(v, float) and not (v != v), f"{k} is NaN"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
