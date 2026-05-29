"""Top-500 多因子实时筛选器 v2 — 元学习 + Regime Detection。

v2 升级:
  - 10 因子模型 (新增链上活跃、开发者、叙事热度、资金费率)
  - 元学习自动调权 (IC 回测 → 加权平均 IC → 权重更新)
  - Regime Detection (BTC 牛熊判断 → 动态因子权重)
  - 因子加速度异动检测 (TVL 暴涨、资金费率极端等)
  - 每次筛选保存快照供回测

数据源 (全部公开免费):
  - CoinGecko /coins/markets + /coins/{id} (市值+开发者+社区)
  - DeFiLlama /protocols + /v2/chains (TVL)
  - Binance spot + futures (成交量+资金费率)
"""
from __future__ import annotations
import json
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("screener", "INFO")
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "research"

# ── 稳定币列表 (排除) ──
STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDE", "USDD",
    "PYUSD", "GUSD", "USDP", "FRAX", "LUSD", "CRVUSD", "EURC",
    "USDS", "USD0", "SUSD", "MIM", "USDJ", "DOLA", "EURS",
    "ALUSD", "GHO", "CUSD", "HAY", "USDX",
}

# ── 链名 → 代币映射 (用于匹配 DeFiLlama chain TVL) ──
CHAIN_TO_TOKEN = {
    "Ethereum": "ETH",  "Solana": "SOL",    "BSC": "BNB",
    "Avalanche": "AVAX","Polygon": "POL",   "Arbitrum": "ARB",
    "Optimism": "OP",   "Base": "BASE",     "Sui": "SUI",
    "Aptos": "APT",     "Near": "NEAR",     "Fantom": "FTM",
    "Ton": "TON",       "Tron": "TRX",      "Cosmos Hub": "ATOM",
    "Injective": "INJ", "Sei": "SEI",       "Mantle": "MNT",
    "Cronos": "CRO",    "Cardano": "ADA",   "Algorand": "ALGO",
    "Polkadot": "DOT",  "Hedera": "HBAR",   "Celo": "CELO",
    "Moonbeam": "GLMR", "Gnosis": "GNO",    "Kava": "KAVA",
    "Scroll": "SCR",    "zkSync Era": "ZK",  "Starknet": "STRK",
    "Linea": "LINEA",   "Manta": "MANTA",   "Blast": "BLAST",
    "Mode": "MODE",     "Merlin": "MERL",
}


# ====================================================================
#  数据拉取 (全部公开 API, 无需 key)
# ====================================================================

def _get(url: str, params: dict = None, retries: int = 3,
         backoff: float = 10.0, timeout: int = 30) -> dict | list | None:
    """v0.9: forward 到统一 HttpClient.

    把原来每个模块自己写的 _get 收敛到 src/http_client.py: 全局 token bucket
    防止 CoinGecko/Binance/DefiLlama 限速串扰, 双层缓存自动 dedupe.
    """
    from ..http_client import http
    return http.get_json(url, params=params, timeout=timeout,
                         ttl="hot", retries=retries)


def fetch_coingecko_top500() -> list[dict]:
    """CoinGecko 免费 API: 5 页 x 100 = Top 500 市值代币。

    返回字段: id, symbol, name, current_price, market_cap,
    market_cap_rank, total_volume, price_change_percentage_24h,
    price_change_percentage_7d_in_currency,
    price_change_percentage_30d_in_currency, ath, ath_change_percentage,
    circulating_supply, total_supply, ...
    """
    all_coins = []
    for page in range(1, 6):
        log.info(f"  CoinGecko page {page}/5 ...")
        data = _get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": 100,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "7d,30d",
            },
            retries=3,
            backoff=15.0,
        )
        if data and isinstance(data, list):
            all_coins.extend(data)
            log.info(f"    ✓ {len(data)} coins")
        else:
            log.warning(f"    ✗ page {page} 失败")
        # CoinGecko free API: ~10 req/min, 保守等 8s
        if page < 5:
            time.sleep(8)
    return all_coins


def fetch_defillama_protocols() -> dict[str, float]:
    """DeFiLlama /protocols: 所有 DeFi 协议 TVL。返回 {SYMBOL: tvl_usd}。"""
    log.info("  DeFiLlama protocols ...")
    data = _get("https://api.llama.fi/protocols", timeout=30)
    if not data or not isinstance(data, list):
        log.warning("    ✗ DeFiLlama protocols 失败")
        return {}
    tvl_map: dict[str, float] = {}
    for p in data:
        sym = (p.get("symbol") or "").upper().strip()
        tvl = p.get("tvl") or 0
        if sym and tvl > 0:
            # 同 symbol 可能有多个协议 (如 AAVE v2/v3), 累加
            tvl_map[sym] = tvl_map.get(sym, 0) + tvl
    log.info(f"    ✓ {len(tvl_map)} protocols with TVL")
    return tvl_map


def fetch_defillama_chains() -> dict[str, float]:
    """DeFiLlama /v2/chains: 每条链的 TVL。返回 {chain_name: tvl_usd}。"""
    log.info("  DeFiLlama chains ...")
    data = _get("https://api.llama.fi/v2/chains", timeout=30)
    if not data or not isinstance(data, list):
        log.warning("    ✗ DeFiLlama chains 失败")
        return {}
    result = {}
    for c in data:
        name = c.get("name", "")
        tvl = c.get("tvl", 0)
        if name and tvl and tvl > 0:
            result[name] = tvl
    log.info(f"    ✓ {len(result)} chains")
    return result


def fetch_binance_volumes() -> dict[str, float]:
    """Binance 24hr ticker: 所有 USDT 对的 24h 成交额 (quote volume)。"""
    log.info("  Binance 24hr tickers ...")
    data = _get("https://api.binance.com/api/v3/ticker/24hr", timeout=30)
    if not data or not isinstance(data, list):
        # Binance 在某些地区 IP 被封 (451), 这不是致命错误
        log.warning("    ✗ Binance API 不可用 (可能是 IP 限制), 跳过")
        return {}
    vol_map: dict[str, float] = {}
    for t in data:
        sym = t.get("symbol", "")
        if sym.endswith("USDT"):
            base = sym[:-4]
            try:
                vol = float(t.get("quoteVolume", 0))
                if vol > 0:
                    vol_map[base] = vol
            except (ValueError, TypeError):
                pass
    log.info(f"    ✓ {len(vol_map)} USDT pairs")
    return vol_map


# ====================================================================
#  多因子打分
# ====================================================================

def score_coins(coins: list[dict],
                tvl_protocol: dict[str, float],
                tvl_chain: dict[str, float],
                binance_vol: dict[str, float],
                onchain_data: dict[str, dict] = None,
                funding_rates: dict[str, float] = None,
                weights: dict[str, float] = None,
                real_onchain: dict = None) -> list[dict]:
    """10 因子量化打分 (元学习动态权重)。

    因子:
      1. momentum_30d     — 中期趋势 sigmoid
      2. momentum_7d      — 短期势头
      3. ath_drawdown      — 回调深度
      4. volume_turnover   — 日换手率
      5. tvl_mcap          — TVL/市值
      6. market_cap_size   — 市值规模
      7. onchain_activity  — 链上+社区活跃度
      8. dev_activity      — 开发者活跃度
      9. funding_rate      — 永续合约资金费率
     10. narrative_heat    — 叙事热度
    """
    from .factors_extended import (
        calc_onchain_activity_score, calc_dev_activity_score,
        calc_funding_rate_score, calc_narrative_heat_score,
    )
    from .onchain_real import calc_real_onchain_score

    onchain_data = onchain_data or {}
    funding_rates = funding_rates or {}
    real_onchain = real_onchain or {}

    # 默认权重 (如果元学习没有提供)
    if not weights:
        weights = {
            "momentum_30d": 0.18, "momentum_7d": 0.10,
            "ath_drawdown": 0.10, "volume_turnover": 0.10,
            "tvl_mcap": 0.15, "market_cap_size": 0.05,
            "onchain_activity": 0.10, "dev_activity": 0.07,
            "funding_rate": 0.07, "narrative_heat": 0.08,
        }

    scored = []
    for c in coins:
        sym = (c.get("symbol") or "").upper()
        mcap = c.get("market_cap") or 0
        price = c.get("current_price") or 0
        rank = c.get("market_cap_rank") or 999

        # 过滤
        if mcap < 1_000_000 or price <= 0:
            continue
        if sym in STABLECOINS:
            continue
        if len(sym) > 2 and sym[0] in ("W", "S") and sym[1:] in ("ETH", "SOL", "BTC", "MATIC", "AVAX"):
            continue

        chg_30d = c.get("price_change_percentage_30d_in_currency") or 0
        chg_7d = c.get("price_change_percentage_7d_in_currency") or 0
        chg_24h = c.get("price_change_percentage_24h") or 0
        ath = c.get("ath") or price
        ath_pct = c.get("ath_change_percentage") or 0
        vol_24h = c.get("total_volume") or 0

        # TVL 匹配
        tvl = tvl_protocol.get(sym, 0)
        for chain_name, chain_sym in CHAIN_TO_TOKEN.items():
            if chain_sym == sym:
                chain_tvl = tvl_chain.get(chain_name, 0)
                if chain_tvl > tvl:
                    tvl = chain_tvl
                break

        bn_vol = binance_vol.get(sym, 0)
        total_vol = max(vol_24h, bn_vol)

        # ── 10 因子计算 ──
        # F1: 30d 动量
        f_momentum_30d = 2 / (1 + math.exp(-chg_30d / 30)) - 1

        # F2: 7d 动量
        f_momentum_7d = 2 / (1 + math.exp(-chg_7d / 15)) - 1

        # F3: ATH 回撤
        drawdown = abs(ath_pct) / 100
        if drawdown < 0.20:
            f_ath = 0.10
        elif drawdown < 0.50:
            f_ath = 0.30 + drawdown
        elif drawdown < 0.85:
            f_ath = 0.70
        else:
            f_ath = 0.40

        # F4: 成交量/市值比
        turnover = total_vol / mcap if mcap > 0 else 0
        f_volume = min(1.0, turnover / 0.30)

        # F5: TVL/市值比
        tvl_ratio = tvl / mcap if mcap > 0 and tvl > 0 else 0
        f_tvl = min(1.0, tvl_ratio / 0.50)

        # F6: 市值规模
        if mcap < 5e8:
            f_size = 0.80
        elif mcap < 2e9:
            f_size = 0.60
        elif mcap < 1e10:
            f_size = 0.40
        elif mcap < 5e10:
            f_size = 0.20
        else:
            f_size = 0.10

        # F7: 链上+社区活跃度 (CoinGecko + 真实链上数据融合)
        coin_ext = onchain_data.get(sym, {})
        f_onchain_cg = calc_onchain_activity_score(coin_ext)
        f_onchain_real = calc_real_onchain_score(sym, real_onchain)
        # 有真实链上数据时: 60% 真实 + 40% CoinGecko; 否则用 CoinGecko
        if f_onchain_real > 0:
            f_onchain = f_onchain_real * 0.6 + f_onchain_cg * 0.4
        else:
            f_onchain = f_onchain_cg

        # F8: 开发者活跃度
        f_dev = calc_dev_activity_score(coin_ext)

        # F9: 资金费率
        fr = funding_rates.get(sym, 0)
        f_funding = calc_funding_rate_score(fr)

        # F10: 叙事热度
        f_narrative = calc_narrative_heat_score(coin_ext)

        # ── 加权综合 (动态权重) ──
        factor_values = {
            "momentum_30d": f_momentum_30d,
            "momentum_7d": f_momentum_7d,
            "ath_drawdown": f_ath,
            "volume_turnover": f_volume,
            "tvl_mcap": f_tvl,
            "market_cap_size": f_size,
            "onchain_activity": f_onchain,
            "dev_activity": f_dev,
            "funding_rate": f_funding,
            "narrative_heat": f_narrative,
        }

        composite = sum(
            factor_values.get(k, 0) * weights.get(k, 0)
            for k in weights
        )

        scored.append({
            "rank": rank,
            "symbol": sym,
            "name": c.get("name", ""),
            "coin_id": c.get("id", ""),
            "price": price,
            "market_cap": mcap,
            "volume_24h": total_vol,
            "change_24h": round(chg_24h, 2),
            "change_7d": round(chg_7d, 2),
            "change_30d": round(chg_30d, 2),
            "ath": ath,
            "ath_drawdown_pct": round(ath_pct, 1),
            "tvl": tvl,
            "tvl_mcap_ratio": round(tvl_ratio, 4),
            "turnover": round(turnover, 4),
            "funding_rate": round(fr, 6),
            # 所有因子值 (用于回测 IC)
            "f_momentum_30d": round(f_momentum_30d, 3),
            "f_momentum_7d": round(f_momentum_7d, 3),
            "f_ath_drawdown": round(f_ath, 3),
            "f_volume_turnover": round(f_volume, 3),
            "f_tvl_mcap": round(f_tvl, 3),
            "f_market_cap_size": round(f_size, 3),
            "f_onchain_activity": round(f_onchain, 3),
            "f_dev_activity": round(f_dev, 3),
            "f_funding_rate": round(f_funding, 3),
            "f_narrative_heat": round(f_narrative, 3),
            "composite_score": round(composite, 4),
        })

    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    return scored


# ====================================================================
#  主流程
# ====================================================================

def run_screen(top_n: int = 30) -> dict:
    """运行完整筛选 v2: 拉数据 → 元学习权重 → Regime → 打分 → 快照 → 异动检测。"""
    from .factors_extended import fetch_onchain_activity, fetch_funding_rates
    from .meta_learner import (
        load_factor_config, get_current_weights, save_snapshot,
        detect_regime, apply_regime_adjustment, generate_factor_report,
    )
    from .factor_bridge import (
        load_pipeline_signals, calc_market_overlay,
        apply_market_overlay, get_pipeline_summary,
    )
    from .onchain_real import fetch_real_onchain_data, calc_real_onchain_score

    log.info("=" * 60)
    log.info(f"Top-500 多因子实时筛选 v2 (元学习 + Regime)")
    log.info("=" * 60)

    start = datetime.now(timezone.utc)

    # ── Step 1: 元学习因子权重加载 ──
    log.info("[0/6] 加载元学习因子配置...")
    factor_cfg = load_factor_config()
    base_weights = get_current_weights(factor_cfg)
    log.info(f"  当前因子权重: {base_weights}")

    # ── Step 2: 拉取 6 个数据源 ──
    log.info("[1/6] CoinGecko Top 500 市值...")
    coins = fetch_coingecko_top500()
    if not coins:
        return {"ok": False, "error": "CoinGecko API 无数据 — 请检查网络或稍后重试"}

    log.info("[2/6] DeFiLlama 协议 TVL...")
    tvl_proto = fetch_defillama_protocols()

    log.info("[3/6] DeFiLlama 链 TVL...")
    tvl_chain = fetch_defillama_chains()

    log.info("[4/6] Binance 24h 成交量...")
    bn_vol = fetch_binance_volumes()

    log.info("[5/6] CoinGecko 链上+社区数据 (Top 50)...")
    # 提取 coin_ids 给 onchain fetcher
    coin_ids = [c.get("id", "") for c in coins if c.get("id")][:50]
    onchain_data = fetch_onchain_activity(coin_ids)

    log.info("[6/7] Binance Futures 资金费率...")
    funding_rates = fetch_funding_rates()

    log.info("[7/7] 真实链上数据 (BTC + DEX volumes)...")
    real_onchain = fetch_real_onchain_data()

    # ── Step 3: Regime Detection (BTC 牛熊判断) ──
    log.info("Regime Detection...")
    btc_data = None
    for c in coins:
        if (c.get("symbol") or "").upper() == "BTC":
            btc_data = {
                "change_30d": c.get("price_change_percentage_30d_in_currency") or 0,
                "change_7d": c.get("price_change_percentage_7d_in_currency") or 0,
                "change_24h": c.get("price_change_percentage_24h") or 0,
                "price": c.get("current_price") or 0,
            }
            break
    regime = detect_regime(btc_data)
    log.info(f"  市场 Regime: {regime.upper()}")

    # 根据 regime 调整权重
    weights = apply_regime_adjustment(base_weights, regime)
    log.info(f"  Regime 调整后权重: {weights}")

    # ── Step 4: 10 因子打分 ──
    log.info(f"开始 10 因子打分 ({len(coins)} 个代币, 动态权重)...")
    scored = score_coins(coins, tvl_proto, tvl_chain, bn_vol,
                         onchain_data=onchain_data,
                         funding_rates=funding_rates,
                         weights=weights,
                         real_onchain=real_onchain)

    # ── Step 5: Pipeline 因子叠加 (市场级信号) ──
    log.info("加载 pipeline 市场级因子...")
    pipeline_signals = load_pipeline_signals()
    market_overlay = calc_market_overlay(pipeline_signals)
    if abs(market_overlay - 1.0) > 0.001:
        scored = apply_market_overlay(scored, market_overlay)
        log.info(f"  Market overlay 已应用: x{market_overlay:.3f}")
    pipeline_summary = get_pipeline_summary(pipeline_signals)

    # ── Step 6: 保存快照 (供未来 IC 回测) ──
    log.info("保存因子快照...")
    snapshot_path = save_snapshot(scored, weights)

    # ── Step 7: 异动信号检测 ──
    anomalies = detect_anomalies(scored, funding_rates)

    end = datetime.now(timezone.utc)

    # 因子健康报告
    factor_report = generate_factor_report()

    result = {
        "ok": True,
        "total_screened": len(scored),
        "top_n": top_n,
        "regime": regime,
        "btc_data": btc_data,
        "data_sources": {
            "coingecko": len(coins),
            "defillama_protocols": len(tvl_proto),
            "defillama_chains": len(tvl_chain),
            "binance_pairs": len(bn_vol),
            "onchain_coins": len(onchain_data),
            "funding_pairs": len(funding_rates),
            "real_onchain": bool(real_onchain.get("btc")),
            "dex_chains": len(real_onchain.get("dex_volumes", {})),
        },
        "weights_used": weights,
        "base_weights": base_weights,
        "factor_report": factor_report,
        "anomalies": anomalies,
        "market_overlay": market_overlay,
        "pipeline_summary": pipeline_summary,
        "snapshot_path": str(snapshot_path),
        "timestamp": end.isoformat(),
        "duration_seconds": round((end - start).total_seconds(), 1),
        "top": scored[:top_n],
        "all": scored,
    }

    # 保存 JSON
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    date_str = end.strftime("%Y%m%d_%H%M")
    json_path = DATA_DIR / f"screen_{date_str}.json"
    save = {k: v for k, v in result.items() if k not in ("all",)}
    json_path.write_text(json.dumps(save, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    log.info(f"JSON 已保存: {json_path}")

    return result


# ====================================================================
#  异动信号检测
# ====================================================================

def detect_anomalies(scored: list[dict],
                     funding_rates: dict[str, float]) -> list[dict]:
    """检测因子异动信号 — 可能的 Alpha 机会。

    异动类型:
    1. TVL/MCap 极端高 (>1.0x) — 可能严重低估
    2. 资金费率极端负 (<-0.005) — 空头拥挤, 逼空风险
    3. 资金费率极端正 (>0.005) — 多头过热, 回调风险
    4. 30d 动量+高成交量 — 强势突破信号
    5. 深度回调+高开发活跃 — 被市场忽略的优质项目
    """
    signals = []

    for c in scored[:100]:  # 只检测前 100 名
        sym = c["symbol"]

        # 1. TVL 严重低估
        if c.get("tvl_mcap_ratio", 0) > 1.0:
            signals.append({
                "symbol": sym,
                "type": "tvl_undervalued",
                "severity": "high",
                "detail": f"TVL/MCap={c['tvl_mcap_ratio']:.2f}x — 链上锁仓远超代币市值",
            })

        # 2. 资金费率极端
        fr = funding_rates.get(sym, 0)
        if fr < -0.005:
            signals.append({
                "symbol": sym,
                "type": "funding_extreme_negative",
                "severity": "high",
                "detail": f"资金费率={fr:.4%} — 空头极度拥挤, 潜在逼空",
            })
        elif fr > 0.005:
            signals.append({
                "symbol": sym,
                "type": "funding_extreme_positive",
                "severity": "medium",
                "detail": f"资金费率={fr:.4%} — 多头过热, 回调风险",
            })

        # 3. 强势突破 (30d>30% + 高换手)
        if c.get("change_30d", 0) > 30 and c.get("turnover", 0) > 0.15:
            signals.append({
                "symbol": sym,
                "type": "momentum_breakout",
                "severity": "medium",
                "detail": f"30d +{c['change_30d']:.1f}% + 换手率{c['turnover']:.2%}",
            })

        # 4. 深度回调但开发活跃
        if c.get("ath_drawdown_pct", 0) < -80 and c.get("f_dev_activity", 0) > 0.5:
            signals.append({
                "symbol": sym,
                "type": "deep_value_dev_active",
                "severity": "medium",
                "detail": f"ATH 回撤 {c['ath_drawdown_pct']:.0f}% 但开发活跃度={c['f_dev_activity']:.2f}",
            })

        # 5. 综合得分极高但市值较小 (潜力股)
        if c.get("composite_score", 0) > 0.55 and c.get("market_cap", 0) < 2e9:
            signals.append({
                "symbol": sym,
                "type": "high_score_small_cap",
                "severity": "low",
                "detail": f"综合分={c['composite_score']:.4f} + 市值仅{c['market_cap']/1e9:.2f}B",
            })

    log.info(f"  异动信号: {len(signals)} 条")
    return signals


# ====================================================================
#  HTML 报告
# ====================================================================

def generate_screen_report(result: dict) -> Path:
    """从筛选结果生成精美暗色 HTML 报告 v2 (10 因子 + Regime + 异动)。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top = result.get("top", [])
    sources = result.get("data_sources", {})
    duration = result.get("duration_seconds", 0)
    regime = result.get("regime", "unknown")
    weights = result.get("weights_used", {})
    anomalies = result.get("anomalies", [])
    btc_data = result.get("btc_data") or {}
    factor_report = result.get("factor_report", {})

    regime_labels = {
        "bull": ("🐂 牛市", "#4ade80"),
        "bear": ("🐻 熊市", "#f87171"),
        "sideways": ("➡️ 震荡", "#fbbf24"),
        "volatile": ("🌊 高波动", "#818cf8"),
        "unknown": ("❓ 未知", "#64748b"),
    }
    regime_label, regime_color = regime_labels.get(regime, regime_labels["unknown"])

    def fmt(n, prefix="$"):
        if n is None or n == 0:
            return "—"
        if abs(n) >= 1e12:
            return f"{prefix}{n/1e12:.2f}T"
        if abs(n) >= 1e9:
            return f"{prefix}{n/1e9:.2f}B"
        if abs(n) >= 1e6:
            return f"{prefix}{n/1e6:.1f}M"
        if abs(n) >= 1e3:
            return f"{prefix}{n/1e3:.1f}K"
        return f"{prefix}{n:.2f}"

    def chg(v):
        if v is None:
            return '<td style="color:#64748b">—</td>'
        c = "#4ade80" if v > 0 else "#f87171" if v < 0 else "#94a3b8"
        s = "+" if v > 0 else ""
        return f'<td style="color:{c};font-weight:600">{s}{v:.1f}%</td>'

    def bar(score, mx=0.65):
        pct = min(100, max(0, score / mx * 100))
        c = "#4ade80" if pct >= 65 else "#fbbf24" if pct >= 35 else "#f87171"
        return (f'<div style="display:flex;align-items:center;gap:6px">'
                f'<div style="background:#1e293b;border-radius:4px;height:10px;'
                f'flex:1;overflow:hidden">'
                f'<div style="background:{c};height:100%;width:{pct:.0f}%"></div>'
                f'</div><span style="font-weight:700;color:{c};'
                f'min-width:42px;text-align:right">{score:.3f}</span></div>')

    def fval_color(v, threshold=0.3):
        if v > threshold:
            return "#4ade80"
        elif v > 0.1:
            return "#fbbf24"
        return "#64748b"

    # 主表
    rows = ""
    for i, c in enumerate(top[:30], 1):
        tvl_color = "#4ade80" if c["tvl_mcap_ratio"] > 0.5 else \
                    "#60a5fa" if c["tvl_mcap_ratio"] > 0.1 else "#64748b"
        rows += f"""<tr>
<td style="font-weight:700;color:#818cf8">{i}</td>
<td><b style="color:#f1f5f9">{c['symbol']}</b>
<span style="color:#64748b;font-size:11px"> {c['name'][:22]}</span></td>
<td style="font-weight:600">${c['price']:,.4f}</td>
<td>{fmt(c['market_cap'])}</td>
<td>{fmt(c['volume_24h'])}</td>
{chg(c['change_24h'])}{chg(c['change_7d'])}{chg(c['change_30d'])}
<td>{fmt(c['tvl'])}</td>
<td style="color:{tvl_color};font-weight:600">{c['tvl_mcap_ratio']:.2f}x</td>
<td style="color:#94a3b8">{c['ath_drawdown_pct']:.0f}%</td>
<td style="min-width:130px">{bar(c['composite_score'])}</td>
</tr>"""

    # 10 因子分解 Top 10
    frows = ""
    for i, c in enumerate(top[:10], 1):
        frows += f"""<tr>
<td style="font-weight:700;color:#818cf8">{i}</td>
<td style="font-weight:700">{c['symbol']}</td>
<td style="color:{'#4ade80' if c['f_momentum_30d']>0 else '#f87171'}">{c['f_momentum_30d']:+.3f}</td>
<td style="color:{'#4ade80' if c['f_momentum_7d']>0 else '#f87171'}">{c['f_momentum_7d']:+.3f}</td>
<td>{c['f_ath_drawdown']:.3f}</td>
<td>{c['f_volume_turnover']:.3f}</td>
<td style="color:{fval_color(c['f_tvl_mcap'])}">{c['f_tvl_mcap']:.3f}</td>
<td>{c['f_market_cap_size']:.3f}</td>
<td style="color:{fval_color(c['f_onchain_activity'])}">{c['f_onchain_activity']:.3f}</td>
<td style="color:{fval_color(c['f_dev_activity'])}">{c['f_dev_activity']:.3f}</td>
<td style="color:{'#4ade80' if c['f_funding_rate']>0.6 else '#f87171' if c['f_funding_rate']<0.3 else '#fbbf24'}">{c['f_funding_rate']:.3f}</td>
<td style="color:{fval_color(c['f_narrative_heat'])}">{c['f_narrative_heat']:.3f}</td>
<td style="font-weight:700;color:#818cf8">{c['composite_score']:.4f}</td>
</tr>"""

    # 因子权重表
    w_rows = ""
    factor_labels = {
        "momentum_30d": "30d 动量", "momentum_7d": "7d 动量",
        "ath_drawdown": "ATH 回撤", "volume_turnover": "成交量",
        "tvl_mcap": "TVL/MCap", "market_cap_size": "市值规模",
        "onchain_activity": "链上活跃", "dev_activity": "开发者",
        "funding_rate": "资金费率", "narrative_heat": "叙事热度",
    }
    base_w = result.get("base_weights", {})
    for fname, label in factor_labels.items():
        bw = base_w.get(fname, 0)
        aw = weights.get(fname, 0)
        diff = aw - bw
        diff_str = f"+{diff:.1%}" if diff > 0.001 else f"{diff:.1%}" if diff < -0.001 else "—"
        diff_color = "#4ade80" if diff > 0.001 else "#f87171" if diff < -0.001 else "#64748b"
        fr = factor_report.get("factors", {}).get(fname, {})
        ic_str = f"{fr.get('avg_ic_10', 0):+.3f}" if fr.get("ic_records", 0) >= 3 else "待积累"
        status = fr.get("status", "healthy")
        status_colors = {"strong": "#4ade80", "healthy": "#60a5fa", "noisy": "#fbbf24", "weak_negative": "#f87171"}
        w_rows += f"""<tr>
<td>{label}</td>
<td>{bw:.1%}</td>
<td style="font-weight:700">{aw:.1%}</td>
<td style="color:{diff_color}">{diff_str}</td>
<td>{ic_str}</td>
<td style="color:{status_colors.get(status, '#64748b')}">{status}</td>
</tr>"""

    # 异动信号
    anomaly_rows = ""
    severity_colors = {"high": "#f87171", "medium": "#fbbf24", "low": "#60a5fa"}
    for a in anomalies[:20]:
        sc = severity_colors.get(a.get("severity", "low"), "#64748b")
        anomaly_rows += f"""<tr>
<td style="font-weight:700">{a['symbol']}</td>
<td style="color:{sc};font-weight:600">{a['severity'].upper()}</td>
<td>{a['type']}</td>
<td style="color:#94a3b8;font-size:12px">{a['detail']}</td>
</tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Top-500 多因子 Alpha 筛选 v2</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0f172a;color:#e2e8f0;font-family:'SF Pro Display','PingFang SC',system-ui,sans-serif}}
.c{{max-width:1400px;margin:0 auto;padding:28px 18px}}
.hd{{text-align:center;padding:36px 0 26px;border-bottom:1px solid #1e293b;margin-bottom:28px}}
.hd h1{{font-size:30px;font-weight:800;background:linear-gradient(135deg,#4ade80,#60a5fa,#a78bfa);
 -webkit-background-clip:text;-webkit-text-fill-color:transparent}}
.hd .sub{{color:#94a3b8;margin-top:6px;font-size:14px}}
.regime-badge{{display:inline-block;padding:6px 16px;border-radius:20px;font-weight:700;
 font-size:15px;margin-top:10px;border:2px solid}}
.sec{{margin-bottom:28px}}
.st{{font-size:17px;font-weight:700;color:#f1f5f9;margin-bottom:12px;
 padding-bottom:7px;border-bottom:2px solid #334155}}
.card{{background:#1e293b;border-radius:12px;padding:18px;margin-bottom:14px}}
.cg{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}
.m{{background:#1e293b;border-radius:10px;padding:14px;text-align:center}}
.m .v{{font-size:22px;font-weight:800;color:#f1f5f9}}
.m .l{{font-size:11px;color:#64748b;margin-top:3px}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{background:#334155;color:#94a3b8;padding:7px 8px;text-align:left;
 font-size:11px;font-weight:600;position:sticky;top:0}}
td{{padding:7px 8px;border-bottom:1px solid rgba(30,41,59,.6)}}
tr:hover td{{background:rgba(99,102,241,.06)}}
.anomaly-high{{border-left:3px solid #f87171}}
.anomaly-medium{{border-left:3px solid #fbbf24}}
.ft{{text-align:center;color:#475569;font-size:11px;margin-top:36px;
 padding-top:18px;border-top:1px solid #1e293b}}
</style></head><body>
<div class="c">

<div class="hd">
<h1>Top-500 多因子 Alpha 筛选 v2</h1>
<div class="sub">10 因子 · 元学习动态调权 · Regime Detection · 异动信号<br>
CoinGecko ({sources.get('coingecko',0)}) · DeFiLlama ({sources.get('defillama_protocols',0)} protocols + {sources.get('defillama_chains',0)} chains) · Binance ({sources.get('binance_pairs',0)} spot + {sources.get('funding_pairs',0)} futures) · 链上数据 ({sources.get('onchain_coins',0)}) · {duration}s · {now_str}</div>
<div class="regime-badge" style="color:{regime_color};border-color:{regime_color}">
市场状态: {regime_label} &nbsp;|&nbsp; BTC 30d: {btc_data.get('change_30d',0):+.1f}% &nbsp; 7d: {btc_data.get('change_7d',0):+.1f}%
</div>
</div>

<div class="sec">
<div class="st">📊 数据源概览</div>
<div class="cg">
<div class="m"><div class="v">{result.get('total_screened',0)}</div><div class="l">有效代币</div></div>
<div class="m"><div class="v">{sources.get('coingecko',0)}</div><div class="l">CoinGecko</div></div>
<div class="m"><div class="v">{sources.get('defillama_protocols',0)}</div><div class="l">DeFi 协议</div></div>
<div class="m"><div class="v">{sources.get('binance_pairs',0)}</div><div class="l">Binance 对</div></div>
<div class="m"><div class="v">{sources.get('onchain_coins',0)}</div><div class="l">链上数据</div></div>
<div class="m"><div class="v">{sources.get('funding_pairs',0)}</div><div class="l">资金费率</div></div>
</div></div>

<div class="sec">
<div class="st">🏆 Top 30 Alpha 排名</div>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>#</th><th>代币</th><th>价格</th><th>市值</th><th>24h量</th>
<th>24h</th><th>7d</th><th>30d</th>
<th>TVL</th><th>TVL/MCap</th><th>ATH回撤</th><th>综合分</th></tr>
{rows}
</table></div></div>

<div class="sec">
<div class="st">🔬 Top 10 — 10 因子分解</div>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>#</th><th>代币</th>
<th>30d动量</th><th>7d动量</th><th>ATH回撤</th><th>成交量</th>
<th>TVL</th><th>规模</th><th>链上</th><th>开发者</th><th>费率</th><th>叙事</th>
<th>综合</th></tr>
{frows}
</table></div></div>

<div class="sec">
<div class="st">⚙️ 因子权重 (元学习 + Regime 调整: {regime.upper()})</div>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>因子</th><th>基础权重</th><th>当前权重</th><th>Regime 调整</th><th>平均 IC</th><th>状态</th></tr>
{w_rows}
</table>
<p style="color:#64748b;font-size:11px;margin-top:10px">
IC = Information Coefficient (因子值与未来收益的 Spearman 秩相关)。IC &gt; 0.05 为有效因子, &lt; -0.05 为反向因子。
权重通过 IC 加权指数衰减自动调整, 并叠加 Regime 乘数。</p>
</div></div>

{"" if not anomalies else f'''<div class="sec">
<div class="st">🚨 异动信号 ({len(anomalies)} 条)</div>
<div class="card" style="overflow-x:auto">
<table>
<tr><th>代币</th><th>严重度</th><th>类型</th><th>详情</th></tr>
{anomaly_rows}
</table></div></div>'''}

<div class="sec">
<div class="st">📐 方法论 v2</div>
<div class="card" style="color:#94a3b8;font-size:13px;line-height:1.9">
<p><b>数据源 (6 个公开免费 API):</b></p>
<p>① CoinGecko /coins/markets — Top 500 市值, 价格, 涨跌, ATH, 成交量</p>
<p>② CoinGecko /coins/id — Top 50 开发者+社区数据 (commits, PRs, Twitter, Reddit)</p>
<p>③ DeFiLlama /protocols — DeFi 协议 TVL</p>
<p>④ DeFiLlama /v2/chains — 链总 TVL</p>
<p>⑤ Binance spot /ticker/24hr — 成交额</p>
<p>⑥ Binance futures /premiumIndex — 永续资金费率</p>
<p style="margin-top:10px"><b>10 因子量化模型 (元学习自动调权):</b></p>
<p>① 30d 动量 — sigmoid, 中期趋势 &nbsp;② 7d 动量 — 短期势头</p>
<p>③ ATH 回撤 — 回调深度评分 &nbsp;④ 成交量/市值 — 换手率</p>
<p>⑤ TVL/市值 — 链上锁仓价值 &nbsp;⑥ 市值规模 — 中小盘弹性</p>
<p>⑦ 链上+社区活跃 — commits+PR+社交 &nbsp;⑧ 开发者活跃 — 代码层面</p>
<p>⑨ 资金费率 — 空头拥挤信号 &nbsp;⑩ 叙事热度 — 情绪+社区热度</p>
<p style="margin-top:10px"><b>元学习闭环:</b> 每次筛选 → 保存快照 → N天后 IC 回测 → 权重自动进化</p>
<p><b>Regime Detection:</b> BTC 30d/7d 涨跌 → 牛/熊/震荡/高波动 → 因子乘数调整</p>
<p style="margin-top:10px"><b>排除规则:</b> 稳定币 · Wrapped/Staked 代币 · 市值 &lt;$1M</p>
<p style="margin-top:10px;color:#fbbf24">⚠️ 量化因子模型存在滞后性, 不构成投资建议。</p>
</div></div>

<div class="ft">
Crypto Intel v2 · 10-Factor Meta-Learning Screener · {now_str}<br>
数据: CoinGecko / DeFiLlama / Binance 公开 API 实时获取 · 因子权重由元学习引擎自动优化
</div>
</div></body></html>"""

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    path = DATA_DIR / f"screen_{date_str}.html"
    path.write_text(html, encoding="utf-8")
    log.info(f"HTML 报告: {path}")
    return path
