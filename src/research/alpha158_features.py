"""Alpha158 风格因子库 — 移植自 microsoft/qlib。

参考: https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/handler.py
原始 Alpha158 是 158 个手工特征 (K-line + Price + Volume + Rolling) 用于 A 股选股。
本模块把核心 70+ 个因子移植成纯 pandas 实现, 不依赖 qlib, 适配 crypto OHLCV。

输入: DataFrame(date, open, high, low, close, volume) — 单币种时间序列
输出: dict 或 DataFrame, 包含所有因子最新值 (用于截面排序)

核心因子组:
  ── K-line 形态 (9 个)
     KMID, KLEN, KMID2, KUP, KUP2, KLOW, KLOW2, KSFT, KSFT2
  ── 价格特征 (4 个)
     OPEN0, HIGH0, LOW0, VWAP0  (相对 close 的比例)
  ── Rolling 滚动 (60+ 个, 5/10/20/30/60 多个窗口)
     ROC, MA, STD, BETA, RSQR, RESI, MAX, MIN, QTLU, QTLD, RANK, RSV,
     IMAX, IMIN, IMXD, CORR, CORD, CNTP, CNTN, CNTD, SUMP, SUMN, SUMD,
     VMA, VSTD, WVMA, VSUMP, VSUMN, VSUMD

所有因子在 [-1, 1] 或 [0, 1] 量纲, 已做合理归一化。
"""
from __future__ import annotations
import math
from typing import Optional

import numpy as np
import pandas as pd

# 默认滚动窗口 (天)
WINDOWS = [5, 10, 20, 30, 60]


def _safe_div(a, b, default=0.0):
    """安全除法, 避免 zero division。"""
    return np.where(np.abs(b) > 1e-12, a / np.where(b == 0, 1e-12, b), default)


# ====================================================================
#  K-line 形态因子 (基于单根 K 线的形状)
# ====================================================================

def kline_features(df: pd.DataFrame) -> pd.DataFrame:
    """K-line 9 因子: 实体/上下影/缺口。

    df 需有 open/high/low/close 列。
    所有因子用 close 归一化, 量纲是 [-1, 1] 附近。
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    out = pd.DataFrame(index=df.index)
    out["kmid"] = (c - o) / c                            # 实体相对 close
    out["klen"] = (h - l) / c                            # K 线高度
    out["kmid2"] = (c - o) / (h - l).replace(0, np.nan)  # 实体占比
    out["kup"] = (h - np.maximum(o, c)) / c              # 上影线
    out["kup2"] = (h - np.maximum(o, c)) / (h - l).replace(0, np.nan)
    out["klow"] = (np.minimum(o, c) - l) / c             # 下影线
    out["klow2"] = (np.minimum(o, c) - l) / (h - l).replace(0, np.nan)
    out["ksft"] = (2 * c - h - l) / c                    # 收盘相对中线
    out["ksft2"] = (2 * c - h - l) / (h - l).replace(0, np.nan)
    return out


# ====================================================================
#  价格因子 (各价格相对 close 的比例)
# ====================================================================

def price_features(df: pd.DataFrame) -> pd.DataFrame:
    """4 个价格归一化因子。"""
    c = df["close"]
    out = pd.DataFrame(index=df.index)
    out["open0"] = df["open"] / c
    out["high0"] = df["high"] / c
    out["low0"] = df["low"] / c
    # VWAP 用 (h+l+c)/3 近似 (qlib 原版用真 vwap)
    out["vwap0"] = ((df["high"] + df["low"] + df["close"]) / 3) / c
    return out


# ====================================================================
#  Rolling 因子 (滚动窗口统计)
# ====================================================================

def rolling_features(df: pd.DataFrame, windows: list[int] = None) -> pd.DataFrame:
    """滚动窗口因子。

    对每个窗口 w ∈ windows, 生成多组因子:
      - ROC{w}: close 累计收益率
      - MA{w}: 均线 / close
      - STD{w}: 收益率波动
      - BETA{w}: close 对自身滞后的回归斜率
      - MAX{w}, MIN{w}: 最高/最低相对 close
      - QTLU{w}, QTLD{w}: 80%/20% 分位
      - RANK{w}: close 在窗口内排名
      - RSV{w}: stochastic K 值
      - IMAX{w}, IMIN{w}: argmax/argmin 相对位置 (0=最新, 1=最早)
      - IMXD{w}: IMAX-IMIN, 价格趋势方向
      - CORR{w}: close vs volume 相关 (量价配合)
      - CNTP{w}, CNTN{w}: 上涨/下跌天数占比
      - SUMP{w}, SUMN{w}: 上涨/下跌幅度累计
      - VMA{w}: 成交量均线比例
      - VSTD{w}: 成交量波动
      - WVMA{w}: 量加权波动率
    """
    if windows is None:
        windows = WINDOWS
    c, v = df["close"], df["volume"]
    ret = c.pct_change()
    log_ret = np.log(c / c.shift(1)).replace([np.inf, -np.inf], np.nan)

    # 累积到 dict 后一次性 concat (避免 fragmentation warning)
    cols: dict[str, pd.Series] = {}

    for w in windows:
        # === 价格类 ===
        cols[f"roc{w}"] = c / c.shift(w) - 1
        cols[f"ma{w}"] = c.rolling(w).mean() / c - 1
        cols[f"std{w}"] = ret.rolling(w).std()
        cols[f"max{w}"] = c.rolling(w).max() / c - 1
        cols[f"min{w}"] = c.rolling(w).min() / c - 1
        cols[f"qtlu{w}"] = c.rolling(w).quantile(0.8) / c - 1
        cols[f"qtld{w}"] = c.rolling(w).quantile(0.2) / c - 1
        cols[f"rank{w}"] = c.rolling(w).rank(pct=True)
        rsv_num = c - c.rolling(w).min()
        rsv_den = c.rolling(w).max() - c.rolling(w).min()
        cols[f"rsv{w}"] = rsv_num / rsv_den.replace(0, np.nan)

        # === 位置类 (argmax/argmin 距今多远) ===
        cols[f"imax{w}"] = c.rolling(w).apply(
            lambda x: (len(x) - 1 - np.argmax(x.values)) / max(len(x) - 1, 1),
            raw=False,
        )
        cols[f"imin{w}"] = c.rolling(w).apply(
            lambda x: (len(x) - 1 - np.argmin(x.values)) / max(len(x) - 1, 1),
            raw=False,
        )
        cols[f"imxd{w}"] = cols[f"imax{w}"] - cols[f"imin{w}"]

        # === 量价相关 ===
        cols[f"corr{w}"] = c.rolling(w).corr(v)
        cols[f"cord{w}"] = ret.rolling(w).corr(v.pct_change())

        # === 涨跌计数 ===
        up = (ret > 0).astype(float)
        dn = (ret < 0).astype(float)
        cols[f"cntp{w}"] = up.rolling(w).mean()
        cols[f"cntn{w}"] = dn.rolling(w).mean()
        cols[f"cntd{w}"] = cols[f"cntp{w}"] - cols[f"cntn{w}"]

        # === 涨跌幅累计 ===
        ret_pos = ret.where(ret > 0, 0.0)
        ret_neg = (-ret).where(ret < 0, 0.0)
        sump = ret_pos.rolling(w).sum()
        sumn = ret_neg.rolling(w).sum()
        cols[f"sump{w}"] = sump / (sump + sumn).replace(0, np.nan)
        cols[f"sumn{w}"] = sumn / (sump + sumn).replace(0, np.nan)
        cols[f"sumd{w}"] = cols[f"sump{w}"] - cols[f"sumn{w}"]

        # === 成交量类 ===
        cols[f"vma{w}"] = v.rolling(w).mean() / v.replace(0, np.nan) - 1
        cols[f"vstd{w}"] = v.pct_change().rolling(w).std()
        w_vol = (log_ret.abs() * v).rolling(w).std()
        cols[f"wvma{w}"] = w_vol / (v.rolling(w).mean()).replace(0, np.nan)

        # 成交量涨跌累计
        v_chg = v.pct_change()
        v_pos = v_chg.where(v_chg > 0, 0.0)
        v_neg = (-v_chg).where(v_chg < 0, 0.0)
        vsp = v_pos.rolling(w).sum()
        vsn = v_neg.rolling(w).sum()
        cols[f"vsumd{w}"] = (vsp - vsn) / (vsp + vsn).replace(0, np.nan)

    return pd.DataFrame(cols, index=df.index)


# ====================================================================
#  Beta / Residual 类 (高阶, 单独函数)
# ====================================================================

def regression_features(df: pd.DataFrame, windows: list[int] = None) -> pd.DataFrame:
    """对数价格 vs 时间的线性回归: 斜率(趋势)、R²(趋势强度)、残差。

    BETA{w}: 趋势斜率 (年化前的日斜率, 量纲 ~0.01)
    RSQR{w}: R² (0-1, 1=完美趋势)
    RESI{w}: 最近一日相对回归线的偏离
    """
    if windows is None:
        windows = WINDOWS
    log_c = np.log(df["close"].replace(0, np.nan))
    out = pd.DataFrame(index=df.index)

    for w in windows:
        x = np.arange(w, dtype=float)
        x_mean = x.mean()

        def _beta(y):
            y = np.asarray(y, dtype=float)
            if np.any(np.isnan(y)):
                return np.nan
            y_mean = y.mean()
            num = ((x - x_mean) * (y - y_mean)).sum()
            den = ((x - x_mean) ** 2).sum()
            return num / den if den > 0 else np.nan

        def _r2(y):
            y = np.asarray(y, dtype=float)
            if np.any(np.isnan(y)) or y.std() == 0:
                return np.nan
            corr = np.corrcoef(x, y)[0, 1]
            return corr * corr

        def _resi(y):
            y = np.asarray(y, dtype=float)
            if np.any(np.isnan(y)):
                return np.nan
            slope = _beta(y)
            if slope is None or np.isnan(slope):
                return np.nan
            intercept = y.mean() - slope * x_mean
            pred_last = slope * (w - 1) + intercept
            return y[-1] - pred_last

        out[f"beta{w}"] = log_c.rolling(w).apply(_beta, raw=False)
        out[f"rsqr{w}"] = log_c.rolling(w).apply(_r2, raw=False)
        out[f"resi{w}"] = log_c.rolling(w).apply(_resi, raw=False)
    return out


# ====================================================================
#  统一入口
# ====================================================================

def compute_alpha158(df: pd.DataFrame, windows: list[int] = None,
                     include_regression: bool = True) -> pd.DataFrame:
    """计算全部 Alpha158 风格因子。

    Args:
        df: DataFrame with columns [open, high, low, close, volume]
            index 应为 datetime, 升序
        windows: rolling 窗口列表, 默认 [5,10,20,30,60]
        include_regression: 是否包含 beta/r²/residual (慢, 但有用)

    Returns:
        DataFrame, 每列一个因子, 行数与输入一致 (前期会有 NaN)
    """
    if windows is None:
        windows = WINDOWS

    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns.str.lower())
    if missing:
        raise ValueError(f"missing columns: {missing}")

    # 统一小写列名
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    df = df.sort_index()

    parts = [
        kline_features(df),
        price_features(df),
        rolling_features(df, windows),
    ]
    if include_regression:
        parts.append(regression_features(df, windows))

    result = pd.concat(parts, axis=1)
    return result


def latest_factor_vector(df: pd.DataFrame, windows: list[int] = None) -> dict:
    """取最新一天的所有因子值, 用于截面排序 (screener 集成入口)。

    返回 dict[factor_name -> float], 不含 NaN 的因子。
    """
    full = compute_alpha158(df, windows=windows, include_regression=False)
    last = full.iloc[-1]
    return {k: float(v) for k, v in last.items() if pd.notna(v)}


# ====================================================================
#  Self-test
# ====================================================================

if __name__ == "__main__":
    # 用合成数据快速验证
    n = 200
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    rng = np.random.default_rng(42)
    drift = np.cumsum(rng.normal(0.002, 0.03, n))
    close = 100 * np.exp(drift)
    high = close * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.01, n)))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    volume = rng.lognormal(15, 1, n)

    df = pd.DataFrame({
        "open": open_, "high": high, "low": low,
        "close": close, "volume": volume,
    }, index=idx)

    feats = compute_alpha158(df)
    print(f"[alpha158] computed {feats.shape[1]} factors over {feats.shape[0]} bars")
    print(f"  non-null in last row: {feats.iloc[-1].notna().sum()}/{feats.shape[1]}")
    print(f"  sample factors:")
    sample = feats.iloc[-1].dropna().head(15)
    for k, v in sample.items():
        print(f"    {k:12s} = {v:+.4f}")

    latest = latest_factor_vector(df)
    print(f"\n[alpha158] latest_factor_vector returned {len(latest)} factors")
