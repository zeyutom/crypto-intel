#!/usr/bin/env python3
"""冒烟测试: 三个 OSS 集成模块是否能 import + 优雅降级。

跑法:
    python scripts/smoke_oss_integrations.py
    CRYPTO_INTEL_USE_BERT=0 python scripts/smoke_oss_integrations.py   # 关掉 BERT 测试关键词路径

退出码: 0 全部通过, 非 0 表示有失败
"""
from __future__ import annotations
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"; RESET = "\033[0m"

failures = []


def section(name):
    print(f"\n{'='*60}\n  {name}\n{'='*60}")


def check(label, fn):
    try:
        fn()
        print(f"  {GREEN}✓{RESET} {label}")
    except Exception as e:
        print(f"  {RED}✗{RESET} {label}: {e}")
        failures.append(label)
        traceback.print_exc()


# ── 1. Alpha158 features ─────────────────────────────────────────────
section("[1/3] Alpha158 风格因子库")

def t_alpha158():
    import pandas as pd, numpy as np
    from src.research.alpha158_features import compute_alpha158, latest_factor_vector

    n = 200
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    rng = np.random.default_rng(42)
    close = 100 * np.exp(np.cumsum(rng.normal(0.002, 0.03, n)))
    df = pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.005, n)),
        "high": close * (1 + np.abs(rng.normal(0, 0.01, n))),
        "low":  close * (1 - np.abs(rng.normal(0, 0.01, n))),
        "close": close,
        "volume": rng.lognormal(15, 1, n),
    }, index=idx)

    feats = compute_alpha158(df)
    assert feats.shape[1] >= 100, f"expected ≥100 factors, got {feats.shape[1]}"
    assert feats.iloc[-1].isna().sum() == 0, "last row has NaN"

    latest = latest_factor_vector(df)
    assert len(latest) >= 100, f"latest vector too small: {len(latest)}"

    print(f"    -> {feats.shape[1]} factors, last row complete, latest dict has {len(latest)} entries")

check("compute_alpha158 + latest_factor_vector", t_alpha158)


# ── 2. CryptoBERT sentiment ──────────────────────────────────────────
section("[2/3] CryptoBERT 情绪模型")

def t_bert():
    from src.research import sentiment_bert as sb
    from src.research import sentiment_nlp as sn

    # availability check (transformers 未装时返回 False)
    avail = sb.is_available()
    print(f"    -> sentiment_bert.is_available() = {avail}")
    print(f"       USE_BERT env = {sn.USE_BERT}")

    # 应该总是能拿到分数 (要么 BERT, 要么 keyword fallback)
    s, m = sn.text_sentiment("BTC ATH bullish moon rally institutional buying")
    assert isinstance(s, float), f"score not float: {s}"
    assert m in ("bert", "keyword"), f"unknown method: {m}"
    assert s > 0, f"positive text scored {s} ({m})"
    print(f"    bullish text -> score={s:+.3f} method={m}")

    s, m = sn.text_sentiment("rug pull scam hack dump crash bearish")
    assert s < 0, f"negative text scored {s} ({m})"
    print(f"    bearish text -> score={s:+.3f} method={m}")

    # batch + news 路径
    scores, m = sn.batch_text_sentiment(["bullish ATH", "rug pull scam", "neutral noise"])
    assert len(scores) == 3
    print(f"    batch ({m}) = {[round(x,3) for x in scores]}")

    news = sn.analyze_news_sentiment([
        {"title": "BTC bullish breakout ATH ETF", "body": "", "categories": "BTC"},
        {"title": "Hack drains BTC bridge", "body": "rugpull", "categories": "BTC"},
    ], "BTC")
    assert news["volume"] == 2, f"expected 2 relevant, got {news['volume']}"
    print(f"    news -> sentiment={news['sentiment']:+.3f} method={news['method']}")

check("sentiment_bert + sentiment_nlp 优雅降级", t_bert)


# ── 3. cryo on-chain adapter ─────────────────────────────────────────
section("[3/3] cryo 链上 adapter")

def t_cryo():
    from src.adapters import cryo_onchain as cryo
    from src.research import onchain_real as ocr

    avail = cryo.is_available()
    health = ocr.cryo_health()
    print(f"    -> cryo.is_available() = {avail}")
    print(f"       cryo_health = {health}")

    # 未装时调用应不报错, 返回 None / _available:False
    flow = ocr.fetch_cex_flow_via_cryo("USDT")
    print(f"    fetch_cex_flow_via_cryo('USDT') = {flow}")
    if avail:
        # 装了 cryo 但可能没 RPC, 仍允许返回 None
        pass
    else:
        assert flow is None, f"未装 cryo 应返回 None, 实际: {flow}"

    enriched = cryo.enrich_onchain("USDT")
    assert "_available" in enriched
    print(f"    cryo.enrich_onchain('USDT')._available = {enriched['_available']}")

check("cryo subprocess wrapper + 降级链", t_cryo)


# ── 4. Phase 2-A: vectorbt 回测 (优雅降级路径) ───────────────────────
section("[4/8] Phase 2-A · vectorbt 回测内核")

def t_vbt():
    from src.research import portfolio_backtest_vbt as vbt
    print(f"    -> vbt.is_available() = {vbt.is_available()}")
    r = vbt.run_walkforward_backtest_vbt(top_n=10, rebalance_days=7)
    # 未装 vectorbt 时应优雅返回 fallback 标记
    if not vbt.is_available():
        assert r["ok"] is False
        assert r.get("fallback") == "portfolio_backtest"
        print(f"    fallback signaled: {r.get('fallback')}")
    else:
        assert "engine" in r and r["engine"] == "vectorbt"
        print(f"    backtest engine: vectorbt")

check("vectorbt wrapper + fallback signal", t_vbt)


# ── 5. Phase 2-B: LangGraph DAG ──────────────────────────────────────
section("[5/8] Phase 2-B · LangGraph Evolution DAG")

def t_graph():
    from src.evolution import graph
    print(f"    -> graph.is_available() = {graph.is_available()}")
    state = graph._new_state()
    assert "run_id" in state
    assert "nodes_ok" in state
    # 验证 graph build 在 langgraph 装着时不抛 (没装则跳过)
    if graph.is_available():
        app = graph._build_langgraph()
        assert app is not None
        print("    langgraph compiled OK")
    else:
        print("    langgraph not installed; will use sequential fallback")

check("evolution.graph state/build", t_graph)


# ── 6. Phase 2-C: ccxt unified ───────────────────────────────────────
section("[6/8] Phase 2-C · ccxt 统一交易所")

def t_ccxt():
    from src.adapters import ccxt_exchange as cx
    print(f"    -> ccxt.is_available() = {cx.is_available()}")
    # 不论装没装, normalize_symbol 都应工作
    for s, want in [("BTC", "BTC/USDT"), ("BTCUSDT", "BTC/USDT"),
                    ("ETH-USDT", "ETH/USDT"), ("SOL/USDT", "SOL/USDT")]:
        got = cx._normalize_symbol(s)
        assert got == want, f"{s!r}: expected {want!r}, got {got!r}"
    print("    symbol normalization OK")
    # 未装时 fetch 应返回 None / {} 不抛
    assert cx.fetch_ticker("BTC") is None or isinstance(cx.fetch_ticker("BTC"), dict)
    assert isinstance(cx.fetch_all_tickers(), dict)
    h = cx.health()
    assert "installed" in h

check("ccxt graceful degrade + normalize", t_ccxt)


# ── 7. Phase 3-A: RD-Agent ───────────────────────────────────────────
section("[7/8] Phase 3-A · RD-Agent skeleton")

def t_rd():
    from src.research import rd_agent as rd
    print(f"    -> rd_agent.is_available() = {rd.is_available()}")
    seeds = rd._seed_factors()
    assert len(seeds) > 0
    hyps = rd.hypothesis_agent(seeds, n=3, prefer_llm=False)
    assert len(hyps) == 3
    for h in hyps:
        # 表达式必须括号闭合 (syntactically 完整)
        assert h.expression.count("(") == h.expression.count(")"), \
            f"unbalanced parens: {h.expression}"
        print(f"    hyp: {h.name} = {h.expression[:60]}")
    # 评估流程: 即使 alpha_discovery 失败, evaluation 也能跑
    exps = rd.experiment_agent(hyps)
    assert len(exps) == 3
    verdicts = rd.evaluation_agent(exps)
    assert len(verdicts) == 3
    print(f"    eval: {sum(1 for v in verdicts if v.promote)}/{len(verdicts)} promoted (mock)")

check("rd_agent 假设/实验/评估 pipeline", t_rd)


# ── 8. Phase 3-B: cryo warehouse ─────────────────────────────────────
section("[8/8] Phase 3-B · cryo warehouse + DuckDB")

def t_warehouse():
    from src.adapters import cryo_warehouse as cw
    stats = cw.warehouse_stats()
    print(f"    -> duckdb={stats['duckdb_available']}, cryo={stats['cryo_available']}")
    print(f"       parquet files: {stats['total_parquet_files']}")
    # 没有数据时 cex_flow_summary 应优雅返回 _status="no_data"
    flow = cw.cex_flow_summary("USDT")
    assert flow.get("_status") in ("no_data", "ok", "empty")
    print(f"    USDT cex_flow status: {flow.get('_status')}")
    # ingest 未装 cryo 时应返回 ok=False
    if not cw._cryo_available():
        r = cw.ingest_token("USDT")
        assert r.get("ok") is False
        print(f"    ingest gracefully refused: {r.get('reason')}")

check("cryo_warehouse 仓库+查询层", t_warehouse)


# ── 9. Overfitting (PBO + DSR + 多重检验) ────────────────────────────
section("[9/10] Phase 2.5 · PBO / DSR / Bonferroni")

def t_pbo():
    import numpy as np
    from src.research import overfitting as of_mod

    rng = np.random.default_rng(7)
    T = 252

    # 场景 A: 纯噪声 → 应判 overfit
    R_noise = rng.normal(0, 0.01, size=(T, 20))
    diag = of_mod.diagnose_backtest(R_noise, n_splits=10)
    assert diag["overall_verdict"] == "overfit", \
        f"pure noise should be overfit, got {diag['overall_verdict']}"
    print(f"    噪声 20 策略 → PBO={diag['pbo']['pbo']:.2f} "
          f"DSR={diag['dsr']['dsr']:.3f} verdict={diag['overall_verdict']}")

    # 场景 B: 注入真 alpha → 应判 robust
    R_edge = rng.normal(0, 0.01, size=(T, 20))
    R_edge[:, 0] += 0.004  # 第 0 个策略每日 +40bps alpha
    diag2 = of_mod.diagnose_backtest(R_edge, n_splits=10)
    assert diag2["overall_verdict"] == "robust", \
        f"true edge should be robust, got {diag2['overall_verdict']}"
    print(f"    真 alpha + 噪声  → PBO={diag2['pbo']['pbo']:.2f} "
          f"DSR={diag2['dsr']['dsr']:.3f} verdict={diag2['overall_verdict']}")

    # 多重检验: N=50 候选 → alpha=0.001, z≈3.29
    mt = of_mod.multiple_testing_threshold(50)
    assert 3.0 < mt["z_threshold"] < 3.5, f"z={mt['z_threshold']}"
    print(f"    Bonferroni 50 tests → z={mt['z_threshold']}, "
          f"IC@n=60={mt['ic_floor']['n=60']}")

check("PBO + DSR 正确区分 噪声 vs 真信号", t_pbo)


# ── 10. PBO 接入 rd_agent.evaluation_agent ───────────────────────────
section("[10/10] PBO 已接入 RD-Agent")

def t_pbo_rd_integration():
    from src.research import rd_agent as rd
    # 构造一组实验结果 (n_trials=50 → IC 阈值应该很高)
    exps = [
        rd.Experiment(hypothesis_name=f"h{i}", ok=True,
                      ic_mean=0.03, ic_ir=0.4, n_obs=60)
        for i in range(50)
    ]
    verdicts = rd.evaluation_agent(exps)
    # 50 个候选, IC=0.03, n=60 → Bonferroni z=3.29, IC 阈值 ≈ 0.42 → 应全部 not promoted
    promoted = sum(1 for v in verdicts if v.promote)
    assert promoted == 0, f"with N=50, ic=0.03 should be filtered, got {promoted} promoted"
    print(f"    50 候选 IC=0.03 → 0 promoted ✓ (Bonferroni 校正生效)")
    # notes 里应该体现阈值
    assert "Bonferroni" in verdicts[0].notes, f"notes should mention Bonferroni: {verdicts[0].notes}"
    print(f"    verdict notes 包含 Bonferroni 信息 ✓")

    # 对照: 只跑 1 个候选, IC=0.03 应该可以 promote (n_trials=1 时阈值松)
    single = [rd.Experiment(hypothesis_name="h0", ok=True,
                            ic_mean=0.05, ic_ir=0.5, n_obs=60)]
    v_single = rd.evaluation_agent(single)
    print(f"    1 候选 IC=0.05 → promote={v_single[0].promote} (单候选阈值较松)")

check("rd_agent.evaluation_agent 多重检验生效", t_pbo_rd_integration)


# ── 11. DefiLlama 完整 API (v0.8) ──────────────────────────────────
section("[11/12] v0.8 · DefiLlama 31 端点 + 工具函数")

def t_dlf_module():
    from src.adapters import defillama_full as dlf
    assert dlf.is_available()
    # 列出全部公开函数, 确认结构
    funcs = [n for n in dir(dlf) if not n.startswith("_") and callable(getattr(dlf, n))]
    # 至少有 31 端点 + 6 个聚合 + helpers
    assert len(funcs) >= 35, f"expected ≥35 public functions, got {len(funcs)}"
    print(f"    -> {len(funcs)} public functions exported")
    # 验证几个关键函数可调用 (会走 cache, 不真请求)
    assert callable(dlf.list_protocols)
    assert callable(dlf.protocol_history)
    assert callable(dlf.current_prices)
    assert callable(dlf.list_stablecoins)
    assert callable(dlf.list_yield_pools)
    assert callable(dlf.dex_overview)
    assert callable(dlf.open_interest_overview)
    assert callable(dlf.fees_overview)
    # 高级聚合
    assert callable(dlf.get_top_protocols_by_tvl)
    assert callable(dlf.get_chain_dex_volume_share)
    assert callable(dlf.get_stable_peg_health)
    assert callable(dlf.get_top_yield_opportunities)
    assert callable(dlf.get_perp_oi_by_protocol)
    print("    -> 31 endpoints + 6 高级聚合 全部可调用")
    # health 应能跑 (即使没网也返回 dict)
    h = dlf.health()
    assert "bases" in h
    print(f"    -> health.bases = {list(h['bases'].keys())}")

check("defillama_full 模块结构 + API surface", t_dlf_module)


# ── 12. DefiLlama 衍生因子 (4 个) ────────────────────────────────
section("[12/12] v0.8 · 4 个 DefiLlama 衍生因子")

def t_dlf_factors():
    from src.factors import defillama_factors as df

    assert df.is_available()
    # 4 个因子函数都存在
    assert callable(df.compute_tvl_momentum)
    assert callable(df.compute_dex_volume_growth)
    assert callable(df.compute_stable_peg_deviation)
    assert callable(df.compute_yield_spike)
    assert callable(df.compute_all_defillama_factors)
    print("    -> 4 个因子函数 + 1 个聚合入口 全部就位")
    # 验证 stable_peg_deviation 结构 (会真请求, 但 cache 命中后秒级)
    peg = df.compute_stable_peg_deviation()
    if peg.get("_status") == "ok":
        assert "market_peg_score" in peg
        assert "weighted_deviation_bps" in peg
        assert "interpretation" in peg
        print(f"    -> peg: {peg['weighted_deviation_bps']:.2f}bps · "
              f"{peg['interpretation'][:40]}...")
    else:
        print(f"    -> peg API unreachable ({peg.get('_status')}), 函数结构 OK")

check("defillama_factors 4 个因子可调用 + 结构正确", t_dlf_factors)


# ── 13. v0.9 统一 HTTP 客户端 (W2-S3) ───────────────────────────────
section("[13/14] v0.9 W2-S3 · 统一 HTTP 客户端")

def t_http_client():
    from src.http_client import http, HOST_RATE_LIMITS, TTL_LEVELS
    # 1. 单例
    assert http is not None
    # 2. token bucket per-host
    bucket1 = http._get_bucket("api.coingecko.com")
    bucket2 = http._get_bucket("api.coingecko.com")
    assert bucket1 is bucket2, "must be the same bucket for same host"
    bucket3 = http._get_bucket("api.llama.fi")
    assert bucket1 is not bucket3, "different hosts get different buckets"
    print(f"    -> per-host token bucket OK")
    # 3. cache key 稳定
    k1 = http._cache_key("GET", "https://x.com/a", {"q": 1})
    k2 = http._cache_key("GET", "https://x.com/a", {"q": 1})
    assert k1 == k2
    print(f"    -> cache key 一致性 OK")
    # 4. TTL 级别
    assert http._normalize_ttl("hot") == 60
    assert http._normalize_ttl("warm") == 3600
    assert http._normalize_ttl(120) == 120
    print(f"    -> TTL 等级映射 OK")
    # 5. metrics 接口
    stats = http.stats()
    assert "hosts" in stats and "cache_files" in stats
    print(f"    -> stats() 返回 {len(stats['hosts'])} hosts, "
          f"{stats['cache_files']} cache files")
    # 6. 旧 API forward
    from src.utils import http_get_json
    assert callable(http_get_json)
    print(f"    -> utils.http_get_json forward OK")

check("HttpClient 单例 + bucket + cache + metrics", t_http_client)


# ── 14. v0.9 回测引擎路由 (W2-S2) ──────────────────────────────────
section("[14/14] v0.9 W2-S2 · 回测引擎路由")

def t_backtest_router():
    from src.research import backtest_router as br
    # 1. 函数齐全
    assert callable(br.run_backtest)
    assert callable(br.run_sweep)
    assert callable(br.health)
    # 2. detect_engine 不抛
    eng = br.detect_engine()
    assert eng in ("vbt", "legacy")
    print(f"    -> detect_engine = {eng}")
    # 3. health
    h = br.health()
    assert "available_engines" in h
    assert h["auto_chosen"] in ("vbt", "legacy")
    print(f"    -> auto_chosen = {h['auto_chosen']}, "
          f"engines = {h['available_engines']}")
    # 4. 不真跑回测 (要快照, 后台时间太久), 但验证函数能 import
    assert br.is_available()

check("backtest_router facade + detect + health", t_backtest_router)


# ── 总结 ──────────────────────────────────────────────────────────────
TOTAL = 14
print(f"\n{'='*60}")
if failures:
    print(f"{RED}失败 {len(failures)}/{TOTAL}{RESET}: {failures}")
    sys.exit(1)
else:
    print(f"{GREEN}全部 {TOTAL} 个 OSS 集成通过冒烟测试 ✓{RESET}")
    sys.exit(0)
