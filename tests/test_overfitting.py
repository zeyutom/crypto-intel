"""Unit tests for src.research.overfitting — PBO / DSR / Bonferroni.

数学敏感模块, 必须验证已知输入输出对.
"""
import sys
import pathlib
import math

import numpy as np
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.research import overfitting as of


# ────────────────────────────────────────────────────────────────────
#  正态分布 inverse-CDF (Acklam) 精度
# ────────────────────────────────────────────────────────────────────

def test_norm_ppf_standard_quantiles():
    """已知点验证 Φ⁻¹: 中位数 0, 上 2.5% 分位 ≈ 1.96."""
    assert abs(of._norm_ppf(0.5)) < 1e-6
    assert abs(of._norm_ppf(0.975) - 1.95996) < 1e-3
    assert abs(of._norm_ppf(0.025) + 1.95996) < 1e-3
    # 99% 单尾 z ≈ 2.326
    assert abs(of._norm_ppf(0.99) - 2.32635) < 1e-3


def test_norm_ppf_extremes():
    """边界: 接近 0/1 时, 数值不爆."""
    z = of._norm_ppf(1e-10)
    assert z < -5.0 and math.isfinite(z)
    z = of._norm_ppf(1 - 1e-10)
    assert z > 5.0 and math.isfinite(z)


def test_norm_cdf_inverse_consistency():
    """Φ(Φ⁻¹(p)) == p (within numerical error)."""
    for p in [0.1, 0.3, 0.5, 0.7, 0.9, 0.95, 0.99]:
        z = of._norm_ppf(p)
        recovered = of._norm_cdf(z)
        assert abs(recovered - p) < 1e-4, f"p={p}: got {recovered}"


# ────────────────────────────────────────────────────────────────────
#  PBO via CSCV
# ────────────────────────────────────────────────────────────────────

def test_pbo_pure_noise_should_be_overfit():
    """纯噪声多策略, PBO 应该 > 0.5 (Bailey paper 经典结果)."""
    rng = np.random.default_rng(7)
    R = rng.normal(0, 0.01, size=(252, 20))  # 1 年 × 20 策略
    res = of.pbo_cscv(R, n_splits=10)
    assert "pbo" in res
    # 纯噪声 PBO 应 ≥ 0.4 (理论上 0.5, 但 finite sample 可能略低)
    assert res["pbo"] >= 0.35, f"got PBO={res['pbo']} for pure noise"
    assert res["verdict"] in ("overfit", "borderline")


def test_pbo_real_alpha_should_be_robust():
    """有真 alpha 的策略, IS 最优 → OOS 也最优, PBO ≈ 0."""
    rng = np.random.default_rng(7)
    R = rng.normal(0, 0.01, size=(252, 20))
    R[:, 0] += 0.004  # 第 0 个加每日 +40bps alpha (强信号)
    res = of.pbo_cscv(R, n_splits=10)
    assert res["pbo"] <= 0.1, f"strong edge should have low PBO, got {res['pbo']}"
    assert res["verdict"] == "robust"


def test_pbo_input_validation():
    """输入维度不对 / 太少时应优雅返回 error."""
    R = np.array([1, 2, 3])  # 1D
    assert "error" in of.pbo_cscv(R)
    R = np.random.randn(5, 5)  # T 太小
    res = of.pbo_cscv(R, n_splits=10)
    assert "error" in res


# ────────────────────────────────────────────────────────────────────
#  Deflated Sharpe Ratio
# ────────────────────────────────────────────────────────────────────

def test_dsr_single_trial_no_deflation():
    """n_trials=1, sr_star=0, DSR == PSR."""
    res = of.deflated_sharpe(
        sr_observed=1.0, n_obs=252, n_trials=1, sr_std_across_trials=0.5,
    )
    assert "dsr" in res
    assert res["sr_threshold"] == 0.0
    # SR=1, n=252 → 应该非常显著
    assert res["dsr"] > 0.99


def test_dsr_many_trials_strong_deflation():
    """很多候选时, 阈值 SR* 升高, DSR 下降."""
    res_1 = of.deflated_sharpe(
        sr_observed=0.5, n_obs=252, n_trials=1, sr_std_across_trials=0.2,
    )
    res_100 = of.deflated_sharpe(
        sr_observed=0.5, n_obs=252, n_trials=100, sr_std_across_trials=0.2,
    )
    # 100 候选下, sr_threshold 显著升高
    assert res_100["sr_threshold"] > res_1["sr_threshold"]
    assert res_100["dsr"] < res_1["dsr"]


def test_dsr_from_sweep_consistent():
    """从 list 接口和单值接口结果一致."""
    sharpes = [0.1, 0.3, 0.5, 0.7, 1.2]
    res = of.deflated_sharpe_from_sweep(sharpes, n_obs=252)
    assert res["sr_observed"] == 1.2
    assert res["n_trials"] == 5
    assert 0 <= res["dsr"] <= 1


# ────────────────────────────────────────────────────────────────────
#  Multiple testing correction
# ────────────────────────────────────────────────────────────────────

def test_bonferroni_threshold_correct():
    """N=20, alpha=0.05 → corrected α=0.0025, z ≈ 3.02."""
    mt = of.multiple_testing_threshold(20, alpha=0.05, method="bonferroni")
    assert mt["alpha_corrected"] == pytest.approx(0.0025, abs=1e-5)
    assert mt["z_threshold"] == pytest.approx(3.02, abs=0.02)


def test_bonferroni_ic_floor_scales_with_sqrt_n():
    """IC 阈值 ∝ 1/√n: 4x sample → ~2x 更松的阈值."""
    mt = of.multiple_testing_threshold(20)
    floor_30 = mt["ic_floor"]["n=30"]
    floor_120 = mt["ic_floor"]["n=120"]
    ratio = floor_30 / floor_120
    assert ratio == pytest.approx(2.0, abs=0.05)  # √(120/30) = 2


def test_sidak_more_lenient_than_bonferroni():
    """Sidak 比 Bonferroni 略松."""
    bonf = of.multiple_testing_threshold(50, alpha=0.05, method="bonferroni")
    sidak = of.multiple_testing_threshold(50, alpha=0.05, method="sidak")
    assert sidak["alpha_corrected"] > bonf["alpha_corrected"]


# ────────────────────────────────────────────────────────────────────
#  IS/OOS degradation
# ────────────────────────────────────────────────────────────────────

def test_is_oos_no_degradation_robust():
    """IS = OOS → robust."""
    res = of.is_oos_degradation(is_metric=1.0, oos_metric=0.95)
    assert res["degradation_pct"] < 0.10
    assert res["verdict"] == "robust"


def test_is_oos_severe_degradation_overfit():
    """OOS 跌 80% → overfit."""
    res = of.is_oos_degradation(is_metric=2.0, oos_metric=0.4)
    assert res["degradation_pct"] > 0.5
    assert res["verdict"] == "overfit"


def test_is_oos_zero_is_protected():
    """IS=0 时不应崩."""
    res = of.is_oos_degradation(is_metric=0.0, oos_metric=0.5)
    assert "verdict" in res  # 不抛异常


# ────────────────────────────────────────────────────────────────────
#  diagnose_backtest 综合
# ────────────────────────────────────────────────────────────────────

def test_diagnose_combines_pbo_dsr_correctly():
    """综合诊断应能区分纯噪声 vs 真信号."""
    rng = np.random.default_rng(42)
    # noise
    R_noise = rng.normal(0, 0.01, size=(252, 20))
    noise_diag = of.diagnose_backtest(R_noise, n_splits=10)
    assert noise_diag["overall_verdict"] in ("overfit", "borderline")

    # signal
    R_sig = rng.normal(0, 0.01, size=(252, 20))
    R_sig[:, 0] += 0.005
    sig_diag = of.diagnose_backtest(R_sig, n_splits=10)
    assert sig_diag["overall_verdict"] == "robust"


def test_thresholds_are_correctly_inverted():
    """PBO 越小越好, DSR 越大越好 — 验证 _verdict 方向."""
    # PBO inverse=True (低=好)
    assert of._verdict(0.1, of.PBO_THRESHOLDS, inverse=True) == "robust"
    assert of._verdict(0.8, of.PBO_THRESHOLDS, inverse=True) == "overfit"
    # DSR inverse=False (高=好)
    assert of._verdict(0.99, of.DSR_THRESHOLDS, inverse=False) == "robust"
    assert of._verdict(0.5, of.DSR_THRESHOLDS, inverse=False) == "overfit"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
