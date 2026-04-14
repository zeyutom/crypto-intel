"""v0.4 新增 10 个因子的实现 (单文件批量)。

每个 compute_xxx() 返回 list[dict], 与原有 factors 同 schema。
"""
import json
import numpy as np
import pandas as pd
from ..db import query_df
from ..utils import now_iso


# =================== 衍生品 ===================

def compute_open_interest_change() -> list[dict]:
    """OI 24h 变化率 (基于 BTC + ETH)"""
    df = query_df(
        """SELECT asset_id, ts, value FROM raw_metrics
           WHERE source='coinglass' AND metric='oi_total_usd'
           ORDER BY ts"""
    )
    if df.empty:
        return []
    df["ts"] = pd.to_datetime(df["ts"])
    out = []
    ts = now_iso()
    for asset, grp in df.groupby("asset_id"):
        if len(grp) < 2:
            continue
        latest = grp.iloc[-1]["value"]
        cutoff = grp.iloc[-1]["ts"] - pd.Timedelta(hours=24)
        past = grp[grp["ts"] <= cutoff]
        if past.empty:
            past_v = grp.iloc[0]["value"]
        else:
            past_v = past.iloc[-1]["value"]
        if past_v == 0:
            continue
        chg = (latest - past_v) / past_v
        if chg > 0.05:
            sig = 1
        elif chg < -0.05:
            sig = -1
        else:
            sig = 0
        out.append({
            "ts": ts, "asset_id": asset, "factor": "open_interest_change",
            "raw_value": chg, "zscore": None, "signal": sig, "confidence": 0.7,
            "meta": json.dumps({"latest_oi": latest, "past_oi_24h": past_v}),
        })
    return out


def compute_liquidation_heat() -> list[dict]:
    df = query_df(
        """SELECT metric, value FROM raw_metrics
           WHERE source='coinglass' AND metric LIKE 'liquidations_%'
             AND ts = (SELECT MAX(ts) FROM raw_metrics
                       WHERE source='coinglass' AND metric='liquidations_24h_usd')"""
    )
    if df.empty:
        return []
    m = dict(zip(df["metric"], df["value"]))
    total = float(m.get("liquidations_24h_usd", 0))
    longs = float(m.get("liquidations_long_24h_usd", 0))
    shorts = float(m.get("liquidations_short_24h_usd", 0))
    if total == 0:
        return []
    long_ratio = longs / total
    # 信号: 多头清算占主导 = -1 (反向看多), 空头清算占主导 = +1 (反向看空)
    if long_ratio > 0.65:
        sig = -1
    elif long_ratio < 0.35:
        sig = 1
    else:
        sig = 0
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": "liquidation_heat",
        "raw_value": total / 1e6, "zscore": None, "signal": sig, "confidence": 0.7,
        "meta": json.dumps({"total_musd": total/1e6, "long_pct": round(long_ratio*100, 1),
                           "short_pct": round((1-long_ratio)*100, 1)}),
    }]


def compute_long_short_ratio() -> list[dict]:
    df = query_df(
        """SELECT value FROM raw_metrics
           WHERE source='coinglass' AND metric='long_short_ratio'
           ORDER BY ts DESC LIMIT 1"""
    )
    if df.empty:
        return []
    ratio = float(df.iloc[0]["value"])
    if ratio > 2.5:
        sig = -1
    elif ratio < 0.7:
        sig = 1
    else:
        sig = 0
    return [{
        "ts": now_iso(), "asset_id": "bitcoin", "factor": "long_short_ratio",
        "raw_value": ratio, "zscore": None, "signal": sig, "confidence": 0.6,
        "meta": json.dumps({"ratio": ratio}),
    }]


# =================== 全市场宏观 ===================

def compute_btc_dominance_trend() -> list[dict]:
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='cg_global' AND metric='btc_dominance_pct'
           ORDER BY ts"""
    )
    if df.empty:
        return []
    df["ts"] = pd.to_datetime(df["ts"])
    if len(df) < 2:
        # 单点也展示
        cur = float(df.iloc[-1]["value"])
        return [{
            "ts": now_iso(), "asset_id": "market", "factor": "btc_dominance_trend",
            "raw_value": cur, "zscore": None, "signal": 0, "confidence": 0.5,
            "meta": json.dumps({"current_pct": cur, "note": "需要历史才能算趋势"}),
        }]
    cur = float(df.iloc[-1]["value"])
    cutoff = df.iloc[-1]["ts"] - pd.Timedelta(days=7)
    past = df[df["ts"] <= cutoff]
    past_v = float(past.iloc[-1]["value"]) if not past.empty else float(df.iloc[0]["value"])
    chg = cur - past_v  # 百分点变化
    if chg > 1:
        sig = 1
    elif chg < -1:
        sig = -1
    else:
        sig = 0
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": "btc_dominance_trend",
        "raw_value": cur, "zscore": None, "signal": sig, "confidence": 0.7,
        "meta": json.dumps({"current_pct": cur, "past_7d_pct": past_v,
                           "delta_pp": round(chg, 2)}),
    }]


def compute_total_mcap_momentum() -> list[dict]:
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='cg_global' AND metric='total_mcap_usd'
           ORDER BY ts"""
    )
    if df.empty or len(df) < 2:
        return []
    df["ts"] = pd.to_datetime(df["ts"])
    cur = float(df.iloc[-1]["value"])
    cutoff = df.iloc[-1]["ts"] - pd.Timedelta(days=7)
    past = df[df["ts"] <= cutoff]
    past_v = float(past.iloc[-1]["value"]) if not past.empty else float(df.iloc[0]["value"])
    chg = (cur - past_v) / past_v
    if chg > 0.05:
        sig = 1
    elif chg < -0.05:
        sig = -1
    else:
        sig = 0
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": "total_mcap_momentum",
        "raw_value": chg, "zscore": None, "signal": sig, "confidence": 0.7,
        "meta": json.dumps({"current_usd": cur, "past_7d_usd": past_v}),
    }]


# =================== DeFi ===================

def compute_defi_tvl_momentum() -> list[dict]:
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='defillama' AND asset_id='defi_total' AND metric='tvl_usd'
           ORDER BY ts"""
    )
    if df.empty or len(df) < 2:
        return []
    df["ts"] = pd.to_datetime(df["ts"])
    cur = float(df.iloc[-1]["value"])
    cutoff = df.iloc[-1]["ts"] - pd.Timedelta(days=7)
    past = df[df["ts"] <= cutoff]
    past_v = float(past.iloc[-1]["value"]) if not past.empty else float(df.iloc[0]["value"])
    chg = (cur - past_v) / past_v
    if chg > 0.05:
        sig = 1
    elif chg < -0.05:
        sig = -1
    else:
        sig = 0
    return [{
        "ts": now_iso(), "asset_id": "defi_total", "factor": "defi_tvl_momentum",
        "raw_value": chg, "zscore": None, "signal": sig, "confidence": 0.7,
        "meta": json.dumps({"current_tvl": cur, "past_7d_tvl": past_v}),
    }]


# =================== 叙事/热点 ===================

def compute_trending_score() -> list[dict]:
    df = query_df(
        """SELECT value, value_text FROM raw_metrics
           WHERE source='cg_trending' AND metric='trending_coins'
           ORDER BY ts DESC LIMIT 1"""
    )
    if df.empty:
        return []
    n = float(df.iloc[0]["value"])
    txt = df.iloc[0]["value_text"] or ""
    try:
        coins = json.loads(txt)
    except Exception:
        coins = []
    # 简单逻辑: trending 列表里 >= 3 个新代币 (rank > 100 或 rank=None) → 看多 (新故事)
    new_n = sum(1 for c in coins if (c.get("rank") or 0) > 100 or c.get("rank") is None)
    if new_n >= 3:
        sig = 1
    elif new_n <= 1:
        sig = -1
    else:
        sig = 0
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": "trending_score",
        "raw_value": n, "zscore": None, "signal": sig, "confidence": 0.5,
        "meta": json.dumps({"new_coins_in_top7": new_n,
                           "names": [c.get("symbol") for c in coins]}),
    }]


# =================== 宏观联动 ===================

def _pct_returns(prices: pd.Series) -> pd.Series:
    return prices.pct_change().dropna()


def _btc_yfin_corr(yf_asset: str, days: int = 60) -> tuple[float, dict]:
    """Compute rolling correlation between BTC daily returns (binance/okx)
       and the yfinance asset over the past `days` days.
       Returns (corr, meta) or (None, {}) if insufficient data.
    """
    yf_df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='yfinance_macro' AND asset_id=? AND metric='close_price'
           ORDER BY ts""", (yf_asset,)
    )
    btc_df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source IN ('binance','okx','coingecko') AND asset_id='bitcoin'
             AND metric='price_usd'
           ORDER BY ts"""
    )
    if yf_df.empty or btc_df.empty:
        return None, {}
    yf_df["ts"] = pd.to_datetime(yf_df["ts"]).dt.normalize()
    btc_df["ts"] = pd.to_datetime(btc_df["ts"]).dt.normalize()
    yf_daily = yf_df.groupby("ts")["value"].last()
    btc_daily = btc_df.groupby("ts")["value"].last()
    # 对齐
    aligned = pd.concat({"yf": yf_daily, "btc": btc_daily}, axis=1).dropna()
    aligned = aligned.tail(days)
    if len(aligned) < 10:
        return None, {"obs": len(aligned)}
    corr = aligned["yf"].pct_change().corr(aligned["btc"].pct_change())
    return float(corr), {"obs": int(len(aligned))}


def compute_btc_nasdaq_corr() -> list[dict]:
    corr, meta = _btc_yfin_corr("ixic")
    if corr is None:
        return []
    if corr < 0.3:
        sig = 1   # 独立行情
    elif corr > 0.6:
        sig = -1  # 跟随美股, 风险事件易共振
    else:
        sig = 0
    meta["corr"] = round(corr, 3)
    return [{
        "ts": now_iso(), "asset_id": "bitcoin", "factor": "btc_nasdaq_corr",
        "raw_value": corr, "zscore": None, "signal": sig, "confidence": 0.6,
        "meta": json.dumps(meta),
    }]


def compute_btc_gold_corr() -> list[dict]:
    corr, meta = _btc_yfin_corr("gold")
    if corr is None:
        return []
    if corr > 0.4:
        sig = 1   # 数字黄金属性
    elif corr < -0.3:
        sig = -1
    else:
        sig = 0
    meta["corr"] = round(corr, 3)
    return [{
        "ts": now_iso(), "asset_id": "bitcoin", "factor": "btc_gold_corr",
        "raw_value": corr, "zscore": None, "signal": sig, "confidence": 0.5,
        "meta": json.dumps(meta),
    }]


def compute_dxy_inverse() -> list[dict]:
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='yfinance_macro' AND asset_id='dxy' AND metric='close_price'
           ORDER BY ts"""
    )
    if df.empty or len(df) < 5:
        return []
    df["ts"] = pd.to_datetime(df["ts"])
    cur = float(df.iloc[-1]["value"])
    cutoff = df.iloc[-1]["ts"] - pd.Timedelta(days=7)
    past = df[df["ts"] <= cutoff]
    past_v = float(past.iloc[-1]["value"]) if not past.empty else float(df.iloc[0]["value"])
    chg = (cur - past_v) / past_v
    # DXY 下跌 = BTC 看多 (反向)
    if chg < -0.01:
        sig = 1
    elif chg > 0.01:
        sig = -1
    else:
        sig = 0
    return [{
        "ts": now_iso(), "asset_id": "market", "factor": "dxy_inverse",
        "raw_value": chg, "zscore": None, "signal": sig, "confidence": 0.6,
        "meta": json.dumps({"current_dxy": cur, "past_7d_dxy": past_v,
                           "chg_pct": round(chg*100, 3)}),
    }]
