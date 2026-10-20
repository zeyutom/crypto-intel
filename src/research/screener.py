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
import inspect
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("screener", "INFO")
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "research"
# 快照目录 (与 meta_learner.META_DIR 一致, 单独定义便于测试 monkeypatch)
META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"

# ── 稳定币列表 (排除) ──
STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "USDE", "USDD",
    "PYUSD", "GUSD", "USDP", "FRAX", "LUSD", "CRVUSD", "EURC",
    "USDS", "USD0", "SUSD", "MIM", "USDJ", "DOLA", "EURS",
    "ALUSD", "GHO", "CUSD", "HAY", "USDX",
    # 2026-10 回测审计: 下列稳定币曾长期混入 Top-30
    "USDG", "JUPUSD", "XUSD", "USAT", "USD1", "RLUSD", "BUIDL", "USDTB",
    "USDF", "USDY", "USDO", "SYRUPUSDC", "USDAI", "AUSD", "USDB", "USR",
    "FXUSD", "DEUSD", "MUSD", "RUSD", "USDL", "ZUSD", "EUSD", "USYC",
    "OUSG", "USTB", "CUSDO",
}

# ── 非加密资产 (排除): 代币化股票/ETF、黄金等商品 ──
NON_CRYPTO_ASSETS = {
    # 代币化股票 / ETF (xStocks, Backed 等)
    "SPCXB", "SNDKB", "CRCLX", "CRCLB", "TSLAX", "NVDAX", "AAPLX", "SPYX",
    "MSTRX", "COINX", "GOOGLX", "AMZNX", "METAX", "QQQX", "HOODX",
    # 黄金 / 商品
    "PAXG", "XAUT", "KAU", "KAG", "XAUM",
}
# 代币化股票 symbol 通常以 X/B 结尾, CoinGecko id 含这些关键词
_TOKENIZED_ID_HINTS = ("xstock", "backed", "tokenized")

# ── Wrapped / LST 代币 (排除): 价格完全跟随底层资产, 无独立 alpha ──
WRAPPED_LST = {
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "EETH", "CBBTC", "CBETH",
    "RETH", "METH", "EZETH", "RSETH", "SOLVBTC", "LBTC", "TBTC", "BTCB",
    "JITOSOL", "MSOL", "BNSOL", "JUPSOL", "WBNB", "WBETH", "SFRXETH",
    "FRXETH", "OSETH", "PUFETH", "SWETH", "ANKRETH", "FBTC", "CLBTC",
    "UNIBTC", "PUMPBTC", "WHYPE", "KHYPE", "STHYPE", "LSETH", "RSWETH",
    "EBTC", "STBTC", "CMETH", "SUPEROETHB", "WOETH", "SLISBNB", "ASBNB",
    "BSOL", "HSOL", "JSOL", "DSOL", "VSOL", "SAVAX",
}

# 冻结报价判定: 当前价与最近 N 个真实快照完全相同
FROZEN_SNAPSHOTS = 3
# 真实快照门槛: 回填的合成快照 (_meta.synthetic) 只有 ~18 个币
REAL_SNAPSHOT_MIN_COINS = 100
# CoinGecko last_updated 超过此时长视为陈旧报价
STALE_QUOTE_HOURS = 24
# 这些原因排除的币仍把当日报价写进快照 _meta.excluded_rows, 供冻结判定延续历史
# (否则被排除的那天快照里没有它, 接下来 3 次运行历史凑不满 3 条, 冻结币 4 天只排除 1 天)
HISTORY_KEPT_EXCLUSIONS = ("frozen", "stale")
# load_recent_snapshot_prices 结果里按 coin_id 索引的 key 前缀
# (symbol 一律大写, 小写前缀不会与之冲突)
_ID_KEY_PREFIX = "id:"

# DeFiLlama /protocols 中不算 DeFi TVL 的类别: CEX = 交易所托管储备 (用户资金),
# 曾让 BNB/OKB/GT/MX/BP/BORG/NEXO 的 f_tvl_mcap 恒为 1.0, BP/MX/OKB 挤进 Top-10
EXCLUDED_PROTOCOL_CATEGORIES = {"CEX"}

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
    skipped_cex = 0
    for p in data:
        if p.get("category") in EXCLUDED_PROTOCOL_CATEGORIES:
            skipped_cex += 1
            continue
        sym = (p.get("symbol") or "").upper().strip()
        tvl = p.get("tvl") or 0
        if sym and tvl > 0:
            # 同 symbol 可能有多个协议 (如 AAVE v2/v3), 累加
            tvl_map[sym] = tvl_map.get(sym, 0) + tvl
    log.info(f"    ✓ {len(tvl_map)} protocols with TVL (跳过 {skipped_cex} 个 CEX 储备)")
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
#  排除规则 (稳定币 / wrapped / 非加密资产 / 冻结或陈旧报价)
# ====================================================================

def _id_key(coin_id: str) -> str:
    """load_recent_snapshot_prices 结果里 coin_id 维度的 key。"""
    return _ID_KEY_PREFIX + coin_id.lower()


def load_recent_snapshot_prices(n: int = FROZEN_SNAPSHOTS,
                                min_coins: int = REAL_SNAPSHOT_MIN_COINS
                                ) -> dict[str, list[tuple[float, float | None]]]:
    """读取最近 n 个真实快照的 (价格, 市值): {key: [(price, mcap)_latest, ...]}。

    key 有两种: 大写 SYMBOL (旧快照无 coin_id 时也能用), 以及 _id_key(coin_id)
    (同名币如两个 VELO 各有各的历史); 查询用 _prior_history, coin_id 优先、symbol 回退。

    同时带上市值: CoinGecko 对 >= $1 的价格只给 2 位小数, 单看价格会把
    慢涨慢跌的真币 (如 LPT 连续 3 天 1.79) 误判为冻结, 市值 (price x 供应量,
    整数精度) 才能区分 "报价冻结" 和 "价格四舍五入后碰巧相同"。
    除 coins 外还合并 _meta.excluded_rows (当日因冻结/陈旧被排除的币): 否则冻结币
    被排除那天快照里没有它, 后 3 次运行历史凑不满, 4 天里只排除 1 天。
    跳过回填的合成快照 (_meta.synthetic 或币数 < min_coins); 同一快照内同一 key
    只取第一条, 保证每个快照最多贡献一条记录。
    目录缺失 / 文件损坏一律容忍, 返回能读到的部分。
    """
    prices: dict[str, list[tuple[float, float | None]]] = {}
    try:
        files = sorted(META_DIR.glob("snapshot_*.json"), reverse=True)
    except OSError:
        return prices
    found = 0
    for f in files:
        if found >= n:
            break
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        coins = snap.get("coins") or []
        meta = snap.get("_meta") or {}
        if meta.get("synthetic") or len(coins) < min_coins:
            continue
        found += 1
        excluded_rows = meta.get("excluded_rows")
        rows = list(coins) + (list(excluded_rows)
                              if isinstance(excluded_rows, list) else [])
        seen: set[str] = set()
        for c in rows:
            if not isinstance(c, dict):
                continue
            p = c.get("price")
            if not isinstance(p, (int, float)):
                continue
            m = c.get("market_cap")
            entry = (p, m if isinstance(m, (int, float)) else None)
            sym = str(c.get("symbol") or "").upper()
            cid = c.get("coin_id")
            keys = ([sym] if sym else []) + (
                [_id_key(cid)] if isinstance(cid, str) and cid else [])
            for k in keys:
                if k in seen:
                    continue
                seen.add(k)
                prices.setdefault(k, []).append(entry)
    return prices


def _prior_history(prior_prices: dict[str, list[tuple[float, float | None]]],
                   coin_id: str, sym: str) -> list[tuple[float, float | None]]:
    """某币的历史报价: 有 coin_id 历史就用它, 否则回退 symbol (旧快照无 coin_id)。"""
    if coin_id:
        hist = prior_prices.get(_id_key(coin_id))
        if hist:
            return hist
    return prior_prices.get(sym) or []


def _is_frozen_quote(price: float, mcap: float, chg_24h,
                     hist: list[tuple[float, float | None]]) -> bool:
    """价格与市值都和最近 FROZEN_SNAPSHOTS 个真实快照完全一致, 且 24h 涨跌为 0。

    三个条件缺一不可: 只比价格会被 2 位小数四舍五入撞车 (LPT 案例);
    冻结报价 (REUR 案例) 的 24h 涨跌恒为 0、市值分毫不动。
    历史里市值缺失时无法确认, 宁可放过。
    """
    if len(hist) < FROZEN_SNAPSHOTS or price <= 0 or mcap <= 0:
        return False
    if chg_24h is not None and not math.isclose(chg_24h, 0.0, abs_tol=1e-9):
        return False
    for p, m in hist:
        if m is None:
            return False
        if not (math.isclose(p, price, rel_tol=1e-9)
                and math.isclose(m, mcap, rel_tol=1e-9)):
            return False
    return True


def _is_stale_quote(last_updated, now: datetime) -> bool:
    """CoinGecko last_updated (ISO, 'Z' 结尾) 距 now 超过 STALE_QUOTE_HOURS。
    字段缺失或无法解析时不判定为陈旧 (宁可放过)。"""
    if not last_updated or not isinstance(last_updated, str):
        return False
    try:
        ts = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return now - ts > timedelta(hours=STALE_QUOTE_HOURS)


def is_excluded(c: dict,
                prior_prices: dict[str, list[tuple[float, float | None]]] | None = None,
                now: datetime | None = None) -> str | None:
    """判断 CoinGecko 代币是否应排除, 返回原因类别或 None。

    类别: "stable" / "wrapped" / "non_crypto" / "frozen" / "stale"
    (市值 < $1M、价格 <= 0 的基础过滤仍在 score_coins 里, 不计入这里)。

    2026-10 回测审计: REUR 价格 74 个快照纹丝不动、代币化股票、黄金和
    二十余个未登记稳定币污染 Top-30 达 87 天, 故新增 (c)(d)(e) 三类规则。
    """
    sym = (c.get("symbol") or "").upper()
    cid = (c.get("id") or "").lower()
    price = c.get("current_price") or 0
    chg_30d = c.get("price_change_percentage_30d_in_currency") or 0
    chg_7d = c.get("price_change_percentage_7d_in_currency") or 0

    # (a) 稳定币名单
    if sym in STABLECOINS:
        return "stable"
    # (b) 代币化股票 / ETF / 黄金
    if sym in NON_CRYPTO_ASSETS:
        return "non_crypto"
    if sym[-1:] in ("X", "B") and any(h in cid for h in _TOKENIZED_ID_HINTS):
        return "non_crypto"
    # (e) wrapped / LST: 名单 + 原有 W/S 前缀启发式
    if sym in WRAPPED_LST:
        return "wrapped"
    if len(sym) > 2 and sym[0] in ("W", "S") and sym[1:] in ("ETH", "SOL", "BTC", "MATIC", "AVAX"):
        return "wrapped"
    # (c) 锚定启发式: 价格贴着 $1 且 7d/30d 几乎不动
    # (7d/30d 缺失按 0 处理: 刚上线、贴着 $1 又没有历史的币同样当稳定币排除,
    #  新币本身也没有可打分的动量)
    if 0.98 <= price <= 1.02 and abs(chg_30d) < 3 and abs(chg_7d) < 2:
        return "stable"
    # (d1) CoinGecko 报价陈旧
    if _is_stale_quote(c.get("last_updated"), now or datetime.now(timezone.utc)):
        return "stale"
    # (d2) 冻结报价: 价格+市值与最近 FROZEN_SNAPSHOTS 个真实快照一致且 24h 涨跌为 0
    if prior_prices and _is_frozen_quote(price, c.get("market_cap") or 0,
                                         c.get("price_change_percentage_24h"),
                                         _prior_history(prior_prices, cid, sym)):
        return "frozen"
    return None


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
                real_onchain: dict = None,
                prior_prices: dict[str, list[tuple[float, float | None]]] = None,
                exclusion_stats: dict[str, int] = None,
                now: datetime | None = None,
                excluded_rows: list[dict] = None) -> list[dict]:
    """10 因子量化打分 (元学习动态权重)。

    prior_prices: load_recent_snapshot_prices() 的结果, 用于冻结报价判定。
    exclusion_stats: 可选 dict, 原地写入各类排除计数
      {"stable": n, "wrapped": n, "non_crypto": n, "frozen": n, "stale": n}。
    excluded_rows: 可选 list, 原地追加因 HISTORY_KEPT_EXCLUSIONS 被排除的币
      {symbol, coin_id, price, market_cap, reason}; run_screen 写进快照
      _meta.excluded_rows (不进 coins, IC / 记分卡输入不变)。
    now: 判定陈旧报价用的"当前时刻", 默认 datetime.now(UTC); 测试传固定时刻,
      否则用写死日期构造的 last_updated 过一天就全被判 stale。

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

    excluded = {"stable": 0, "wrapped": 0, "non_crypto": 0, "frozen": 0, "stale": 0}
    now = now or datetime.now(timezone.utc)

    scored = []
    for c in coins:
        sym = (c.get("symbol") or "").upper()
        mcap = c.get("market_cap") or 0
        price = c.get("current_price") or 0
        rank = c.get("market_cap_rank") or 999

        # 过滤
        if mcap < 1_000_000 or price <= 0:
            continue
        reason = is_excluded(c, prior_prices, now)
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            if excluded_rows is not None and reason in HISTORY_KEPT_EXCLUSIONS:
                excluded_rows.append({
                    "symbol": sym,
                    "coin_id": c.get("id", ""),
                    "price": price,
                    "market_cap": mcap,
                    "reason": reason,
                })
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
            "last_updated": c.get("last_updated"),
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
    log.info(f"  排除: {excluded}")
    if exclusion_stats is not None:
        exclusion_stats.update(excluded)
    return scored


# ====================================================================
#  主流程
# ====================================================================

def run_screen(top_n: int = 30) -> dict:
    """运行完整筛选 v2: 拉数据 → 元学习权重 → Regime → 打分 → 快照 → 异动检测。"""
    from . import factors_extended as fx
    from .factors_extended import (
        fetch_onchain_activity, fetch_funding_rates, ONCHAIN_META_KEY,
    )
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
    onchain_meta = onchain_data.get(ONCHAIN_META_KEY) or {}
    onchain_coins = len([k for k in onchain_data if k != ONCHAIN_META_KEY])

    log.info("[6/7] 永续资金费率 (Binance → OKX → Bybit)...")
    funding_rates = fetch_funding_rates()
    funding_source = fx.FUNDING_SOURCE

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
    # 上一次的 regime (最新真实快照) 用于迟滞, 避免贴着阈值的小波动让卡片来回切换
    from .meta_learner import latest_snapshot_meta
    prev_regime = latest_snapshot_meta().get("regime")
    regime = detect_regime(btc_data, prev_regime=prev_regime)
    log.info(f"  市场 Regime: {regime.upper()} (上次: {prev_regime or 'n/a'})")

    # 根据 regime 调整权重
    weights = apply_regime_adjustment(base_weights, regime)
    log.info(f"  Regime 调整后权重: {weights}")

    # ── Step 4: 10 因子打分 ──
    log.info(f"开始 10 因子打分 ({len(coins)} 个代币, 动态权重)...")
    # 最近 3 个真实快照的价格+市值 → 冻结报价判定 (REUR 之类)
    prior_prices = load_recent_snapshot_prices()
    excluded: dict[str, int] = {}
    excluded_rows: list[dict] = []
    scored = score_coins(coins, tvl_proto, tvl_chain, bn_vol,
                         onchain_data=onchain_data,
                         funding_rates=funding_rates,
                         weights=weights,
                         real_onchain=real_onchain,
                         prior_prices=prior_prices,
                         exclusion_stats=excluded,
                         excluded_rows=excluded_rows)

    # ── Step 5: Pipeline 因子叠加 (市场级信号) ──
    log.info("加载 pipeline 市场级因子...")
    pipeline_signals = load_pipeline_signals()
    market_overlay = calc_market_overlay(pipeline_signals)
    if abs(market_overlay - 1.0) > 0.001:
        scored = apply_market_overlay(scored, market_overlay)
        log.info(f"  Market overlay 已应用: x{market_overlay:.3f}")
    pipeline_summary = get_pipeline_summary(pipeline_signals)

    # 数据源概览 (写入 result 和快照 meta, 便于事后审计因子是否退化)
    sources = {
        "coingecko": len(coins),
        "defillama_protocols": len(tvl_proto),
        "defillama_chains": len(tvl_chain),
        "binance_pairs": len(bn_vol),
        "onchain_coins": onchain_coins,
        "onchain_coins_with_data": onchain_meta.get("coins_with_any_data"),
        "onchain_coins_with_dev_data": onchain_meta.get("coins_with_dev_data"),
        "funding_source": funding_source,
        "funding_pairs": len(funding_rates),
        "real_onchain": bool(real_onchain.get("btc")),
        "dex_chains": len(real_onchain.get("dex_volumes", {})),
        "excluded": excluded,
    }

    # ── Step 6: 保存快照 (供未来 IC 回测) ──
    log.info("保存因子快照...")
    snapshot_meta = {
        "base_weights": base_weights,
        "regime": regime,
        "weights_in_force": weights,
        "sources": sources,
        # 冻结/陈旧报价被排除的币: 只供下次冻结判定延续历史, 不进 coins
        "excluded_rows": excluded_rows,
    }
    # 按签名判断而不是 try/except TypeError: 后者会把 save_snapshot 内部的
    # 真实 TypeError 吞掉并重跑一遍写盘
    if "meta" in inspect.signature(save_snapshot).parameters:
        snapshot_path = save_snapshot(scored, weights, meta=snapshot_meta)
    else:
        # meta_learner 尚未支持 meta 参数时退回旧签名
        snapshot_path = save_snapshot(scored, weights)

    # ── Step 7: 异动信号检测 ──
    anomalies = detect_anomalies(scored, funding_rates)

    end = datetime.now(timezone.utc)

    # 因子健康报告
    factor_report = generate_factor_report(regime=regime)

    result = {
        "ok": True,
        "total_screened": len(scored),
        "top_n": top_n,
        "regime": regime,
        "btc_data": btc_data,
        "data_sources": sources,
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
    # 规则 5 用相对阈值 (综合分前 10%): 权重归一化方式和启用因子会变, 综合分的
    # 绝对水平随之整体平移, 固定 0.55 曾让命中数从 0 跳到 41
    scores = sorted((c.get("composite_score") or 0) for c in scored)
    top_decile = scores[int(0.9 * (len(scores) - 1))] if scores else float("inf")

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

        # 5. 综合得分极高 (全池前 10%) 但市值较小 (潜力股)
        if c.get("composite_score", 0) > top_decile and c.get("market_cap", 0) < 2e9:
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
<p>⑥ Binance futures /premiumIndex (451 时 OKX → Bybit 兜底) — 永续资金费率</p>
<p style="margin-top:10px"><b>10 因子量化模型 (元学习自动调权):</b></p>
<p>① 30d 动量 — sigmoid, 中期趋势 &nbsp;② 7d 动量 — 短期势头</p>
<p>③ ATH 回撤 — 回调深度评分 &nbsp;④ 成交量/市值 — 换手率</p>
<p>⑤ TVL/市值 — 链上锁仓价值 &nbsp;⑥ 市值规模 — 中小盘弹性</p>
<p>⑦ 链上+社区活跃 — commits+PR+社交 &nbsp;⑧ 开发者活跃 — 代码层面</p>
<p>⑨ 资金费率 — 空头拥挤信号 &nbsp;⑩ 叙事热度 — 情绪+社区热度</p>
<p style="margin-top:10px"><b>元学习闭环:</b> 每次筛选 → 保存快照 → N天后 IC 回测 → 权重自动进化</p>
<p><b>Regime Detection:</b> BTC 30d/7d 涨跌 → 牛/熊/震荡/高波动 → 因子乘数调整</p>
<p style="margin-top:10px"><b>排除规则:</b> 稳定币 (名单+锚定启发式) · Wrapped/LST 代币 · 代币化股票/黄金 · 冻结/陈旧报价 · 市值 &lt;$1M</p>
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
