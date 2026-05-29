"""过拟合控制 — PBO (Probability of Backtest Overfitting) + DSR + 多重检验校正.

参考文献:
  - Bailey, Borwein, López de Prado, Zhu (2014) "The Probability of Backtest
    Overfitting", J. Computational Finance.
  - Bailey & López de Prado (2014) "The Deflated Sharpe Ratio: Correcting for
    Selection Bias, Backtest Overfitting, and Non-Normality", J. Portfolio Mgmt.

核心 API:
  - pbo_cscv(returns_matrix, n_splits=16) -> dict
      用 Combinatorially Symmetric Cross-Validation 估计 PBO:
      把时间分成 S 段, 取 S/2 段 IS, 剩余 OOS;
      统计 "IS 最优策略在 OOS 排到下半区" 的比率, 即 PBO.

  - deflated_sharpe(sharpes, observed_sr, n_obs) -> dict
      把"在 N 个候选里挑出来的 best Sharpe"校正成"真实显著性":
      PSR = Φ((SR_obs - SR*) · √n / σ_SR), DSR = PSR with N inflation.

  - multiple_testing_threshold(n_tests, alpha=0.05, method="bonferroni") -> float
      多重检验下的 IC / t-stat 阈值.

  - is_oos_degradation(is_metric, oos_metric) -> dict
      量化 IS → OOS 退化程度.

设计原则:
  - 纯 numpy/scipy, 零外部依赖, 0.001s 级速度
  - 输入是收益率矩阵 (T x N) 或 Sharpe 列表, 不依赖具体回测引擎
  - 输出带 verdict ("overfit"/"borderline"/"robust") 方便接 CLI/UI
"""
from __future__ import annotations
import math
from itertools import combinations
from typing import Optional

import numpy as np

# 阈值约定 (业界经验值, 可调)
PBO_THRESHOLDS = {"robust": 0.30, "borderline": 0.50}     # PBO < 30% 算稳; > 50% 算过拟合
DSR_THRESHOLDS = {"robust": 0.95, "borderline": 0.80}     # PSR/DSR ≥ 95% 显著
DEGRADE_THRESHOLDS = {"robust": 0.25, "borderline": 0.50}  # OOS Sharpe 退化 < 25% 算稳


def _verdict(value: float, thresholds: dict, inverse: bool = False) -> str:
    """根据阈值返回三档评级."""
    if inverse:
        if value <= thresholds["robust"]:
            return "robust"
        if value <= thresholds["borderline"]:
            return "borderline"
        return "overfit"
    else:
        if value >= thresholds["robust"]:
            return "robust"
        if value >= thresholds["borderline"]:
            return "borderline"
        return "overfit"


# ────────────────────────────────────────────────────────────────────
#  PBO via Combinatorially Symmetric Cross-Validation (CSCV)
# ────────────────────────────────────────────────────────────────────

def pbo_cscv(
    returns_matrix: np.ndarray,
    n_splits: int = 16,
    metric: str = "sharpe",
    max_combos: int = 2000,
) -> dict:
    """估计组合策略的 Probability of Backtest Overfitting.

    Args:
        returns_matrix: shape (T, N) — T 个时间步, N 个候选策略/参数配置的收益率
        n_splits: 把时间维切成多少段 (Bailey 推荐 ≥ 10, 16 是典型值)
        metric: "sharpe" | "mean" | "sortino" — IS 排名用的指标
        max_combos: 组合数上限 (n_splits=16 时是 C(16,8)=12870; 我们抽样)

    Returns:
        {
          "pbo": float ∈ [0, 1],        # < 0.5 算稳健, > 0.5 严重过拟合
          "verdict": "robust" | "borderline" | "overfit",
          "n_strategies": int,
          "n_combos_tested": int,
          "median_oos_rank": float ∈ [0, 1],   # IS 最优策略的 OOS 排位中位数
          "median_logit": float,        # logit(rank), 越正越好
          "interpretation": str,
        }
    """
    R = np.asarray(returns_matrix, dtype=float)
    if R.ndim != 2:
        return {"error": "expected 2D returns matrix (T x N)"}
    T, N = R.shape
    if T < n_splits * 2:
        return {"error": f"too few rows: T={T} < 2*n_splits={2*n_splits}"}
    if N < 2:
        return {"error": "need ≥2 strategies to compare"}

    # 把 T 行均分成 n_splits 段
    split_size = T // n_splits
    split_ids = np.repeat(np.arange(n_splits), split_size)
    split_ids = np.concatenate([split_ids, np.full(T - len(split_ids), n_splits - 1)])

    def _score(returns: np.ndarray) -> np.ndarray:
        """returns: (T', N) → N 个策略的 metric."""
        if metric == "sharpe":
            mu = returns.mean(axis=0)
            sd = returns.std(axis=0, ddof=1)
            sd = np.where(sd < 1e-10, 1e-10, sd)
            return mu / sd
        if metric == "sortino":
            mu = returns.mean(axis=0)
            downside = np.where(returns < 0, returns, 0)
            sd_dn = downside.std(axis=0, ddof=1)
            sd_dn = np.where(sd_dn < 1e-10, 1e-10, sd_dn)
            return mu / sd_dn
        # mean
        return returns.mean(axis=0)

    # 生成所有 IS / OOS 对称分割 (S 选 S/2)
    half = n_splits // 2
    all_combos = list(combinations(range(n_splits), half))
    if len(all_combos) > max_combos:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(all_combos), size=max_combos, replace=False)
        all_combos = [all_combos[i] for i in idx]

    logits = []  # 每个 combo 一个 logit(rank)
    oos_ranks = []
    for is_split in all_combos:
        is_mask = np.isin(split_ids, is_split)
        oos_mask = ~is_mask
        is_returns = R[is_mask]
        oos_returns = R[oos_mask]
        if len(is_returns) < 2 or len(oos_returns) < 2:
            continue

        is_score = _score(is_returns)
        oos_score = _score(oos_returns)

        # IS 最优策略的索引
        best_is = int(np.argmax(is_score))
        # 该策略在 OOS 的排位 (0=最差, 1=最好)
        oos_ranks_arr = np.argsort(np.argsort(oos_score))  # 0..N-1
        rank = oos_ranks_arr[best_is] / max(N - 1, 1)
        oos_ranks.append(rank)
        # logit: 排名靠后 → logit 负, 排名靠前 → logit 正
        eps = 1e-6
        rank_clip = min(max(rank, eps), 1 - eps)
        logits.append(math.log(rank_clip / (1 - rank_clip)))

    if not logits:
        return {"error": "no valid splits"}

    logits_arr = np.asarray(logits)
    # PBO = P(logit < 0) = P(IS最优 在 OOS 排到下半区)
    pbo = float((logits_arr < 0).mean())
    median_rank = float(np.median(oos_ranks))
    median_logit = float(np.median(logits_arr))
    verdict = _verdict(pbo, PBO_THRESHOLDS, inverse=True)

    if verdict == "robust":
        interp = (
            f"PBO={pbo:.2f} < 0.30. IS 最优策略在 OOS 中位数排位 {median_rank:.2f}, "
            f"过拟合风险低."
        )
    elif verdict == "borderline":
        interp = (
            f"PBO={pbo:.2f} (0.30-0.50). 警惕——多次重跑结果不稳定."
        )
    else:
        interp = (
            f"PBO={pbo:.2f} > 0.50. 严重过拟合——IS 上的最优配置在 OOS 跑得不如随机."
        )

    return {
        "pbo": round(pbo, 4),
        "verdict": verdict,
        "n_strategies": N,
        "n_combos_tested": len(logits),
        "median_oos_rank": round(median_rank, 4),
        "median_logit": round(median_logit, 4),
        "interpretation": interp,
        "metric": metric,
    }


# ────────────────────────────────────────────────────────────────────
#  Deflated Sharpe Ratio (Bailey & López de Prado 2014)
# ────────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """标准正态 CDF (避免依赖 scipy)."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    """标准正态分布的 inverse CDF (Acklam 2003 高精度近似).

    精度 < 1.15e-9, 比 Abramowitz 26.2.23 准很多.
    优先用 scipy 如果装了。
    """
    try:
        from scipy.stats import norm
        return float(norm.ppf(p))
    except ImportError:
        pass
    if p <= 0.0:
        return -float("inf")
    if p >= 1.0:
        return float("inf")

    # Acklam (2003) 近似
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    p_low = 0.02425
    p_high = 1 - p_low

    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > p_high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def deflated_sharpe(
    sr_observed: float,
    n_obs: int,
    n_trials: int,
    sr_std_across_trials: float,
    skew: float = 0.0,
    kurt: float = 3.0,
) -> dict:
    """计算 Deflated Sharpe Ratio.

    给定: 在 N 个候选里跑出的 best Sharpe = sr_observed (基于 n_obs 个观测),
    候选间 Sharpe 标准差 = sr_std_across_trials.

    DSR 校正了三件事:
      ① 多重检验 — 候选越多, "看起来好" 越容易出现
      ② 短样本 — Sharpe 估计本身有噪声
      ③ 非正态 — skew/kurt 偏离正态时 Sharpe 不可靠

    Args:
        sr_observed: 实际看到的 Sharpe (年化)
        n_obs: 观测数
        n_trials: 候选/试验数
        sr_std_across_trials: 候选间 Sharpe 标准差
        skew: 收益率偏度 (默认 0 = 正态)
        kurt: 收益率峰度 (默认 3 = 正态)

    Returns:
        {
          "dsr": float ∈ [0, 1],   # 越接近 1 越显著真实
          "psr": float,            # Probabilistic Sharpe Ratio (单策略版)
          "sr_threshold": float,   # 在 alpha=0.05 下需要超过的 Sharpe
          "verdict": "robust" | "borderline" | "overfit",
          "interpretation": str,
        }
    """
    if n_obs <= 1 or n_trials < 1:
        return {"error": "need n_obs > 1 and n_trials >= 1"}

    # SR* — 在 n_trials 个独立 N(0, σ²) 候选中, 期望的最大 Sharpe
    # Bailey 2014 公式: SR* = σ_SR · ((1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)))
    # γ 是 Euler-Mascheroni
    if n_trials == 1:
        sr_star = 0.0  # 单候选, 没多重检验
    else:
        gamma = 0.5772156649  # Euler-Mascheroni
        sr_star = sr_std_across_trials * (
            (1 - gamma) * _norm_ppf(1 - 1.0 / n_trials)
            + gamma * _norm_ppf(1 - 1.0 / (n_trials * math.e))
        )

    # PSR = Φ( (SR_obs - SR*) · √(n-1) / √(1 - skew·SR + (kurt-1)/4·SR²) )
    sr = sr_observed
    denom_sq = 1 - skew * sr + ((kurt - 1) / 4.0) * (sr ** 2)
    denom = math.sqrt(max(denom_sq, 1e-10))
    z = (sr - sr_star) * math.sqrt(max(n_obs - 1, 1)) / denom
    psr = _norm_cdf(z)
    dsr = psr  # DSR = PSR with SR* deflation already baked in

    verdict = _verdict(dsr, DSR_THRESHOLDS)

    if verdict == "robust":
        interp = f"DSR={dsr:.3f} ≥ 0.95. 在 {n_trials} 个候选中胜出的 SR 真实显著."
    elif verdict == "borderline":
        interp = f"DSR={dsr:.3f} (0.80-0.95). 显著但不稳健, 建议加 walk-forward."
    else:
        interp = f"DSR={dsr:.3f} < 0.80. 多重检验校正后不显著, 大概率是噪声."

    return {
        "dsr": round(dsr, 4),
        "psr": round(psr, 4),
        "sr_threshold": round(sr_star, 4),
        "sr_observed": round(sr, 4),
        "n_obs": n_obs,
        "n_trials": n_trials,
        "skew": round(skew, 4),
        "kurt": round(kurt, 4),
        "verdict": verdict,
        "interpretation": interp,
    }


def deflated_sharpe_from_sweep(sharpes: list[float], n_obs: int) -> dict:
    """从一组参数扫描的 Sharpe 列表里, 评估 best 的 DSR."""
    arr = np.asarray(sharpes, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 2:
        return {"error": "need ≥2 sharpes"}
    return deflated_sharpe(
        sr_observed=float(arr.max()),
        n_obs=n_obs,
        n_trials=len(arr),
        sr_std_across_trials=float(arr.std(ddof=1)),
    )


# ────────────────────────────────────────────────────────────────────
#  多重检验校正 (Bonferroni / BH-FDR)
# ────────────────────────────────────────────────────────────────────

def multiple_testing_threshold(
    n_tests: int,
    alpha: float = 0.05,
    method: str = "bonferroni",
) -> dict:
    """计算多重检验下的显著性阈值.

    Args:
        n_tests: 同时跑的检验数 (如 LLM 生成的因子候选数)
        alpha: 整体 type-I error
        method: "bonferroni" | "sidak" | "bh" (Benjamini-Hochberg FDR)

    Returns:
        {
          "alpha_corrected": float,      # 单次检验需要达到的 p 值
          "z_threshold": float,          # 对应的 z-score 阈值 (双尾)
          "ic_floor": float,             # 在 30/60/120 天观测下的 IC 阈值
          "method": str,
        }
    """
    if method == "bonferroni":
        alpha_c = alpha / max(n_tests, 1)
    elif method == "sidak":
        alpha_c = 1 - (1 - alpha) ** (1.0 / max(n_tests, 1))
    elif method == "bh":
        # BH 给的是排序后的临界值; 这里返回 worst-case = alpha/n
        alpha_c = alpha / max(n_tests, 1)
    else:
        return {"error": f"unknown method: {method}"}

    # 双尾 z 阈值: P(|Z| > z) = alpha_c → z = Φ⁻¹(1 - alpha_c/2)
    z_t = _norm_ppf(1 - alpha_c / 2)

    # IC 阈值: |IC| > z_t / √n
    ic_floor = {
        "n=30": round(z_t / math.sqrt(30), 4),
        "n=60": round(z_t / math.sqrt(60), 4),
        "n=120": round(z_t / math.sqrt(120), 4),
        "n=252": round(z_t / math.sqrt(252), 4),
    }

    return {
        "n_tests": n_tests,
        "alpha_raw": alpha,
        "alpha_corrected": round(alpha_c, 6),
        "z_threshold": round(z_t, 4),
        "ic_floor": ic_floor,
        "method": method,
    }


# ────────────────────────────────────────────────────────────────────
#  IS → OOS 退化
# ────────────────────────────────────────────────────────────────────

def is_oos_degradation(is_metric: float, oos_metric: float) -> dict:
    """量化 IS → OOS 性能退化.

    Returns:
        {
          "is": float,
          "oos": float,
          "degradation_pct": float,      # (IS - OOS) / |IS|
          "verdict": "robust" | "borderline" | "overfit",
        }
    """
    if abs(is_metric) < 1e-10:
        return {"is": is_metric, "oos": oos_metric, "degradation_pct": 0.0,
                "verdict": "n/a"}
    deg = (is_metric - oos_metric) / abs(is_metric)
    verdict = _verdict(deg, DEGRADE_THRESHOLDS, inverse=True)
    return {
        "is": round(is_metric, 4),
        "oos": round(oos_metric, 4),
        "degradation_pct": round(deg, 4),
        "verdict": verdict,
        "interpretation": (
            f"IS={is_metric:.3f} → OOS={oos_metric:.3f}, 退化 {deg*100:+.1f}%"
        ),
    }


# ────────────────────────────────────────────────────────────────────
#  综合诊断
# ────────────────────────────────────────────────────────────────────

def diagnose_backtest(
    returns_matrix: np.ndarray,
    n_splits: int = 16,
) -> dict:
    """对一份 (T x N) 收益矩阵跑完整过拟合诊断."""
    R = np.asarray(returns_matrix, dtype=float)
    if R.ndim != 2 or R.shape[1] < 2:
        return {"error": "need 2D matrix with ≥2 strategies"}

    T, N = R.shape

    # PBO
    pbo = pbo_cscv(R, n_splits=n_splits)

    # 各策略 Sharpe
    mu = R.mean(axis=0)
    sd = R.std(axis=0, ddof=1)
    sd = np.where(sd < 1e-10, 1e-10, sd)
    sharpes = mu / sd
    # 注: 这里是 per-period Sharpe, 真实使用应乘 √annualization

    # DSR
    dsr = deflated_sharpe_from_sweep(sharpes.tolist(), n_obs=T)

    # 多重检验阈值
    mt = multiple_testing_threshold(N)

    # 综合 verdict
    verdicts = [pbo.get("verdict"), dsr.get("verdict")]
    if "overfit" in verdicts:
        overall = "overfit"
    elif "borderline" in verdicts:
        overall = "borderline"
    else:
        overall = "robust"

    return {
        "shape": {"T": T, "N": N},
        "pbo": pbo,
        "dsr": dsr,
        "multiple_testing": mt,
        "best_sharpe": round(float(sharpes.max()), 4),
        "worst_sharpe": round(float(sharpes.min()), 4),
        "median_sharpe": round(float(np.median(sharpes)), 4),
        "overall_verdict": overall,
        "summary": (
            f"PBO={pbo.get('pbo', 'n/a')} ({pbo.get('verdict', '')}), "
            f"DSR={dsr.get('dsr', 'n/a')} ({dsr.get('verdict', '')}), "
            f"overall: {overall}"
        ),
    }


def is_available() -> bool:
    return True


# ────────────────────────────────────────────────────────────────────
#  Self-test (合成数据验证)
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    rng = np.random.default_rng(42)
    T, N = 252, 20  # 1 年, 20 个候选策略

    # 场景 1: 全噪声 (应判 overfit)
    print("=== Scenario 1: pure noise (20 random strategies) ===")
    R_noise = rng.normal(0, 0.01, size=(T, N))
    res = diagnose_backtest(R_noise, n_splits=10)
    print(json.dumps(res, indent=2, default=str))

    # 场景 2: 有 1 个真信号 + 19 个噪声
    print("\n=== Scenario 2: 1 real edge + 19 noise ===")
    R_mixed = rng.normal(0, 0.01, size=(T, N))
    R_mixed[:, 0] += 0.005  # 给第 0 个加一个真 alpha
    res2 = diagnose_backtest(R_mixed, n_splits=10)
    print(json.dumps(res2, indent=2, default=str))

    # 场景 3: 多重检验阈值
    print("\n=== Multi-testing threshold (50 candidates) ===")
    print(json.dumps(multiple_testing_threshold(50), indent=2))
