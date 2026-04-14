"""Streamlit 仪表盘共享工具: 密码门、数据加载、配色。"""
from __future__ import annotations
import streamlit as st
import pandas as pd
import json
from datetime import datetime, timezone
from .db import query_df, latest_factors, latest_reviews
from .config import CFG
from .factors._metadata import asset_cn, regime_cn, factor_meta, signal_label


# ============ 主题色 ============
COLOR = {
    "bg":      "#0b1020",
    "panel":   "#131933",
    "line":    "#2a3464",
    "muted":   "#9aa3c7",
    "accent":  "#5ee0c1",
    "blue":    "#6aa6ff",
    "purple":  "#a884ff",
    "ok":      "#58d68d",
    "warn":    "#ffb454",
    "alert":   "#ff6b8b",
}


# ============ 密码门 ============
def require_password() -> bool:
    """放在每个 page 的最顶部, 未登录则展示密码框并停在这里。"""
    if st.session_state.get("authenticated"):
        return True
    try:
        pw_correct = st.secrets.get("dashboard_password", "")
    except Exception:
        # 本地无 secrets.toml 时, 显示提示
        pw_correct = ""

    st.markdown(f"""
    <div style='max-width: 380px; margin: 80px auto; text-align: center;'>
      <h1 style='background: linear-gradient(90deg,#6aa6ff,#5ee0c1,#a884ff);
                 -webkit-background-clip: text; background-clip: text;
                 color: transparent; font-size: 36px; margin: 0;'>Crypto Intel</h1>
      <p style='color: {COLOR["muted"]}; margin-top: 8px;'>面向投资经理的加密货币情报仪表盘</p>
    </div>
    """, unsafe_allow_html=True)

    with st.form("login_form"):
        pw = st.text_input("访问密码", type="password",
                           placeholder="请输入仪表盘密码")
        submitted = st.form_submit_button("进入仪表盘", use_container_width=True)
        if submitted:
            if not pw_correct:
                st.error("⚠ 未配置密码 (`dashboard_password`)。请在 Streamlit Cloud → Settings → Secrets "
                         "里粘贴: `dashboard_password = \"你的密码\"`,或者本地创建 `.streamlit/secrets.toml`。")
            elif pw == pw_correct:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("密码错误。")
    st.stop()


# ============ 公共页面顶栏 ============
def page_header(title: str, subtitle: str = "") -> None:
    src_count = int(query_df(
        "SELECT COUNT(DISTINCT source) AS c FROM raw_metrics WHERE source != '_meta'"
    ).iloc[0]["c"])
    factor_count = int(query_df("SELECT COUNT(DISTINCT factor) AS c FROM factors").iloc[0]["c"])

    sig_df = query_df("SELECT regime FROM signals ORDER BY ts DESC LIMIT 1")
    regime = sig_df.iloc[0]["regime"] if not sig_df.empty else "UNKNOWN"
    rgm_cn = regime_cn(regime)[0]

    last_run = query_df(
        "SELECT MAX(ts) AS last_ts FROM raw_metrics WHERE source != '_meta'"
    ).iloc[0]["last_ts"]
    last_run_str = last_run[:16].replace("T", " ") + " UTC" if last_run else "—"

    is_demo = bool(int(query_df(
        "SELECT COUNT(*) AS c FROM raw_metrics WHERE source='_meta' AND metric='is_demo'"
    ).iloc[0]["c"]) > 0)

    st.markdown(f"""
    <div style='display:flex; justify-content:space-between; align-items:flex-end; margin-bottom:12px;'>
      <div>
        <h1 style='margin:0; font-size:28px;
                   background: linear-gradient(90deg,#6aa6ff,#5ee0c1,#a884ff);
                   -webkit-background-clip: text; background-clip: text; color: transparent;'>{title}</h1>
        <div style='color:{COLOR["muted"]}; font-size:13px; margin-top:4px;'>{subtitle}</div>
      </div>
      <div style='text-align:right; font-size:12.5px; color:{COLOR["muted"]};'>
        <div>Regime: <b style='color:{COLOR["accent"]}'>{rgm_cn}</b></div>
        <div>数据源 {src_count}/6 · 因子 {factor_count}</div>
        <div>最近采集: {last_run_str}</div>
      </div>
    </div>
    """, unsafe_allow_html=True)

    if is_demo:
        st.warning("⚠ 本实例当前展示 DEMO 数据 (seed_demo.py 注入)。"
                   "真实数据请运行 `python -m src.cli all` 或在 GitHub Actions 启用每日刷新。")


def sidebar_branding() -> None:
    """侧边栏 logo + 登出。"""
    with st.sidebar:
        st.markdown(f"""
        <div style='padding: 12px 0 18px; text-align:center;'>
          <div style='font-size:22px; font-weight:600;
               background: linear-gradient(90deg,#6aa6ff,#5ee0c1,#a884ff);
               -webkit-background-clip: text; background-clip: text; color: transparent;'>
            Crypto Intel
          </div>
          <div style='color:{COLOR["muted"]}; font-size:11.5px; margin-top:2px;'>v0.3 · 投研工作流</div>
        </div>
        """, unsafe_allow_html=True)
        st.divider()
        if st.button("🔄 立即刷新数据", use_container_width=True,
                     help="触发一次完整的数据采集 + 因子计算 + 报告生成"):
            with st.spinner("采集中... 约 30 秒"):
                from .pipeline import run_all_once
                result = run_all_once()
                st.success("刷新完成!")
                with st.expander("查看刷新结果"):
                    st.json(result)
                st.rerun()
        if st.button("登出", use_container_width=True):
            st.session_state.pop("authenticated", None)
            st.rerun()
        st.divider()
        st.caption("⚠️ 本仪表盘自动生成, 仅供参考, 非投资建议。")


# ============ 数据加载 (带缓存) ============
@st.cache_data(ttl=60)
def load_factor_cards() -> list[dict]:
    fact_df = latest_factors()
    cards = []
    seen = set()
    for _, r in fact_df.iterrows():
        fname = r["factor"]
        if fname in seen and (r.get("asset_id") or "") != "bitcoin":
            continue
        seen.add(fname)
        meta_obj = factor_meta(fname)
        try:
            raw_meta = json.loads(r["meta"]) if r["meta"] else {}
        except Exception:
            raw_meta = {}
        sig = int(r["signal"]) if r["signal"] is not None else 0
        cards.append({
            "factor": fname,
            "cn_name": meta_obj.cn_name if meta_obj else fname,
            "category": meta_obj.category if meta_obj else "—",
            "raw_value": r["raw_value"],
            "raw_value_fmt": _fmt_raw(fname, r["raw_value"]),
            "fmt_unit": meta_obj.fmt_unit if meta_obj else "",
            "signal": sig,
            "confidence": float(r["confidence"]) if r["confidence"] is not None else 0,
            "one_line": meta_obj.one_line if meta_obj else "",
            "why_matters": meta_obj.why_matters if meta_obj else "",
            "how_to_read": meta_obj.how_to_read.get(sig, "") if meta_obj else "",
            "pm_action": meta_obj.pm_action.get(sig, "") if meta_obj else "",
            "raw_meta": raw_meta,
        })
    return cards


def _fmt_raw(factor: str, v) -> str:
    if v is None:
        return "N/A"
    if factor == "funding_composite":
        return f"{v*100:.4f}% (8h)"
    if factor == "coinbase_premium":
        return f"{v*100:+.3f}%"
    if factor == "stablecoin_mint_7d":
        return f"${v/1e9:+.2f}B"
    if factor == "fear_greed_reversal":
        return f"{v:.0f}/100"
    if factor == "etf_flow_5d":
        return f"${v:+.1f}M"
    return str(v)


@st.cache_data(ttl=60)
def load_signals() -> pd.DataFrame:
    return query_df(
        """SELECT s.* FROM signals s
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM signals GROUP BY asset_id) m
           ON (s.asset_id IS m.a_ OR (s.asset_id IS NULL AND m.a_ IS NULL))
           AND s.ts=m.mts"""
    )


@st.cache_data(ttl=60)
def load_snapshot() -> pd.DataFrame:
    snap = query_df(
        """SELECT r.asset_id AS asset_id, r.value AS price FROM raw_metrics r
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                 WHERE source='binance' AND metric='price_usd'
                 GROUP BY asset_id) m
           ON r.asset_id=m.a_ AND r.ts=m.mts
           WHERE r.source='binance' AND r.metric='price_usd'"""
    )
    chg = query_df(
        """SELECT r.asset_id AS asset_id, r.value AS change_24h FROM raw_metrics r
           JOIN (SELECT asset_id AS a_, MAX(ts) AS mts FROM raw_metrics
                 WHERE source='binance' AND metric='change_24h_pct'
                 GROUP BY asset_id) m
           ON r.asset_id=m.a_ AND r.ts=m.mts
           WHERE r.source='binance' AND r.metric='change_24h_pct'"""
    )
    if snap.empty:
        return pd.DataFrame()
    df = snap.merge(chg, on="asset_id", how="left")
    df["symbol"] = df["asset_id"].apply(
        lambda a: next((x["symbol"] for x in CFG["universe"] if x["id"] == a), a))
    df["cn_name"] = df["asset_id"].apply(asset_cn)
    return df


@st.cache_data(ttl=60)
def load_etf_history() -> pd.DataFrame:
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='farside_etf' AND metric='etf_net_flow_musd'
           ORDER BY ts DESC LIMIT 30"""
    )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df.sort_values("ts")


@st.cache_data(ttl=60)
def load_factor_history(factor: str) -> pd.DataFrame:
    df = query_df(
        "SELECT ts, raw_value, signal FROM factors WHERE factor=? ORDER BY ts",
        (factor,),
    )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df


@st.cache_data(ttl=60)
def load_fg_history() -> pd.DataFrame:
    df = query_df(
        """SELECT ts, value, value_text FROM raw_metrics
           WHERE source='feargreed' AND metric='fear_greed_index'
           ORDER BY ts"""
    )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df


@st.cache_data(ttl=60)
def load_stablecoin_history() -> pd.DataFrame:
    df = query_df(
        """SELECT ts, value FROM raw_metrics
           WHERE source='defillama' AND asset_id='stablecoins'
             AND metric='total_circulating_usd'
           ORDER BY ts"""
    )
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    return df


@st.cache_data(ttl=60)
def load_reviews() -> pd.DataFrame:
    return latest_reviews()


@st.cache_data(ttl=60)
def load_source_status() -> list[dict]:
    sources_df = query_df(
        """SELECT source, COUNT(*) AS n, MAX(ts) AS last_ts FROM raw_metrics
           WHERE source != '_meta' GROUP BY source"""
    )
    expected = ["coingecko", "binance", "coinbase", "defillama", "feargreed", "farside_etf"]
    actual = set(sources_df["source"].tolist())
    out = []
    for s in expected:
        if s in actual:
            row = sources_df[sources_df["source"] == s].iloc[0]
            out.append({"source": s, "ok": True, "n": int(row["n"]),
                        "last_ts": row["last_ts"][:16].replace("T", " ")})
        else:
            out.append({"source": s, "ok": False, "n": 0, "last_ts": "—"})
    return out


def signal_pill(signal: int) -> str:
    """返回 HTML 信号 pill。"""
    if signal == 1:
        return f'<span style="background:rgba(88,214,141,0.15); color:{COLOR["ok"]}; padding:2px 8px; border-radius:999px; font-size:11px;">看多 +1</span>'
    if signal == -1:
        return f'<span style="background:rgba(255,107,139,0.15); color:{COLOR["alert"]}; padding:2px 8px; border-radius:999px; font-size:11px;">看空 -1</span>'
    return f'<span style="background:rgba(154,163,199,0.15); color:{COLOR["muted"]}; padding:2px 8px; border-radius:999px; font-size:11px;">中性 0</span>'
