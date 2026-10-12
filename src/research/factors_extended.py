"""扩展因子数据源 — 链上活跃度、开发者、叙事热度、资金费率。

所有 API 均为公开免费, 无需 key。
"""
from __future__ import annotations
import time
from ..utils import setup_logger

log = setup_logger("factors_ext", "INFO")

# fetch_onchain_activity 返回 dict 里的元信息 key。symbol 全部 .upper(),
# 小写的 "__meta__" 永远不会和任何代币 symbol 撞上。
ONCHAIN_META_KEY = "__meta__"

# 最近一次 fetch_funding_rates 成功的数据源 ("binance" / "okx" / "bybit"),
# 全部失败为 None。供 screener 写入 result["data_sources"]["funding_source"]。
FUNDING_SOURCE: str | None = None


def _get(url: str, params: dict = None, retries: int = 2,
         backoff: float = 5.0, timeout: int = 20):
    """v0.9: forward 到统一 HttpClient."""
    from ..http_client import http
    return http.get_json(url, params=params, timeout=timeout,
                         ttl="hot", retries=retries)


# ====================================================================
#  链上活跃度因子 (CoinGecko developer/community data)
# ====================================================================

def fetch_onchain_activity(coin_ids: list[str],
                           batch_delay: float = 7.0) -> dict[str, dict]:
    """从 CoinGecko /coins/{id} 获取链上和社区指标。

    返回 {SYMBOL: {dev_score, community_score, ...}}

    注意: 免费 API 限速, 只拉 top N 个 (默认 top 50)
    """
    log.info(f"  CoinGecko 链上+社区数据 ({len(coin_ids)} coins)...")
    result = {}
    # 2026-08-28 起 CoinGecko 免费 API 不再返回 developer_data/community_data,
    # 因子曾静默退化为全 0。这里分别统计 dev / community 真正有数据的币数,
    # 任一为 0 都告警 (dev_activity 只吃 developer_data, 社区有数据救不了它)。
    coins_with_dev_data = 0
    coins_with_community_data = 0
    coins_with_any_data = 0
    for i, cid in enumerate(coin_ids[:50]):  # 限制 50 个避免限速
        data = _get(
            f"https://api.coingecko.com/api/v3/coins/{cid}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "false",
                "community_data": "true",
                "developer_data": "true",
                "sparkline": "false",
            },
            retries=2,
            backoff=10.0,
        )
        if data:
            sym = (data.get("symbol") or "").upper()
            dev = data.get("developer_data") or {}
            comm = data.get("community_data") or {}
            sentiment = data.get("sentiment_votes_up_percentage") or 50
            has_dev, has_comm = _has_any_value(dev), _has_any_value(comm)
            coins_with_dev_data += has_dev
            coins_with_community_data += has_comm
            coins_with_any_data += has_dev or has_comm

            result[sym] = {
                # 开发者指标
                "github_forks": dev.get("forks") or 0,
                "github_stars": dev.get("stars") or 0,
                "github_subscribers": dev.get("subscribers") or 0,
                "github_total_issues": dev.get("total_issues") or 0,
                "github_closed_issues": dev.get("closed_issues") or 0,
                "github_pull_requests_merged": dev.get("pull_requests_merged") or 0,
                "github_pull_request_contributors": dev.get("pull_request_contributors") or 0,
                "commit_count_4_weeks": dev.get("commit_count_4_weeks") or 0,
                "code_additions_4_weeks": dev.get("code_additions_deletions_4_weeks", {}).get("additions") or 0,
                "code_deletions_4_weeks": dev.get("code_additions_deletions_4_weeks", {}).get("deletions") or 0,
                # 社区指标
                "twitter_followers": comm.get("twitter_followers") or 0,
                "reddit_subscribers": comm.get("reddit_subscribers") or 0,
                "reddit_avg_posts_48h": comm.get("reddit_average_posts_48h") or 0,
                "reddit_avg_comments_48h": comm.get("reddit_average_comments_48h") or 0,
                "telegram_members": comm.get("telegram_channel_user_count") or 0,
                # 情绪
                "sentiment_up_pct": sentiment,
            }

        if i < len(coin_ids[:50]) - 1:
            time.sleep(batch_delay)  # CoinGecko 限速

    log.info(f"    ✓ {len(result)} coins fetched, "
             f"dev={coins_with_dev_data} community={coins_with_community_data} "
             f"with non-empty data")
    if result and coins_with_any_data == 0:
        log.warning("    ⚠ developer_data/community_data 字段为空 — 因子将退化为常数")
    elif result and coins_with_dev_data == 0:
        log.warning("    ⚠ developer_data 字段为空 — dev_activity 因子将退化为常数")
    elif result and coins_with_community_data == 0:
        log.warning("    ⚠ community_data 字段为空 — onchain/narrative 因子将退化")
    # 向后兼容: 调用方按 symbol 取值, 遍历时需跳过 ONCHAIN_META_KEY
    result[ONCHAIN_META_KEY] = {
        "coins_with_dev_data": coins_with_dev_data,
        "coins_with_community_data": coins_with_community_data,
        "coins_with_any_data": coins_with_any_data,
    }
    return result


def _has_any_value(d: dict) -> bool:
    """developer_data/community_data 是否有任何非空、非零的字段。"""
    if not isinstance(d, dict):
        return False
    for v in d.values():
        if isinstance(v, dict):
            if _has_any_value(v):
                return True
        elif v not in (None, 0, 0.0, "", [], {}):
            return True
    return False


# ====================================================================
#  资金费率因子 (Binance → OKX → Bybit 兜底链)
# ====================================================================

def _parse_binance_funding(data) -> dict[str, float]:
    """Binance /fapi/v1/premiumIndex: [{symbol: "BTCUSDT", lastFundingRate: "0.0001"}]"""
    if not data or not isinstance(data, list):
        return {}
    rates = {}
    for item in data:
        sym = item.get("symbol", "")
        if sym.endswith("USDT"):
            base = sym[:-4]
            try:
                rates[base] = float(item.get("lastFundingRate", 0))
            except (ValueError, TypeError):
                pass
    return rates


def _parse_okx_funding(data) -> dict[str, float]:
    """OKX /api/v5/public/funding-rate?instId=ANY:
    {code: "0", data: [{instId: "BTC-USDT-SWAP", fundingRate: "0.0001", ...}]}

    只取 *-USDT-SWAP (USD 本位和交割合约跳过)。
    """
    if not isinstance(data, dict) or str(data.get("code", "")) != "0":
        return {}
    rates = {}
    for item in data.get("data") or []:
        inst = item.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        base = inst.split("-")[0]
        try:
            rates[base] = float(item.get("fundingRate") or 0)
        except (ValueError, TypeError):
            pass
    return rates


def _parse_bybit_funding(data) -> dict[str, float]:
    """Bybit /v5/market/tickers?category=linear:
    {retCode: 0, result: {list: [{symbol: "BTCUSDT", fundingRate: "0.0001", ...}]}}
    """
    if not isinstance(data, dict) or data.get("retCode") not in (0, "0"):
        return {}
    rates = {}
    for item in (data.get("result") or {}).get("list") or []:
        sym = item.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            rates[sym[:-4]] = float(item.get("fundingRate") or 0)
        except (ValueError, TypeError):
            pass
    return rates


# 兜底顺序: Binance (fapi 在云端/国内返回 451) → OKX → Bybit
_FUNDING_SOURCES = (
    ("binance", "https://fapi.binance.com/fapi/v1/premiumIndex", None,
     _parse_binance_funding),
    ("okx", "https://www.okx.com/api/v5/public/funding-rate", {"instId": "ANY"},
     _parse_okx_funding),
    ("bybit", "https://api.bybit.com/v5/market/tickers", {"category": "linear"},
     _parse_bybit_funding),
)


def fetch_funding_rates() -> dict[str, float]:
    """永续合约 funding rate, Binance → OKX → Bybit 逐个兜底。

    返回 {SYMBOL: funding_rate} (如 BTC: 0.0001 = 0.01%)
    负费率 = 空头付多头 = 可能超卖

    2026-05 起 fapi.binance.com 对云端和本地都返回 HTTP 451, 因子曾连续
    数月恒为 0.5 却占 5-24% 权重。全部失败返回 {} (元学习会自动停用常数因子),
    成功的数据源记录在模块变量 FUNDING_SOURCE。
    """
    global FUNDING_SOURCE
    FUNDING_SOURCE = None
    log.info("  Perpetual funding rates (Binance → OKX → Bybit)...")
    for name, url, params, parse in _FUNDING_SOURCES:
        data = _get(url, params=params, timeout=20)
        rates = parse(data)
        if rates:
            FUNDING_SOURCE = name
            log.info(f"    ✓ {name}: {len(rates)} perpetual pairs")
            return rates
        log.warning(f"    ✗ {name} funding rate 不可用, 尝试下一个数据源")

    log.warning("    ✗ 所有资金费率数据源均不可用, 因子退化为常数 0.5")
    return {}


# ====================================================================
#  因子计算函数
# ====================================================================

def calc_onchain_activity_score(coin_data: dict) -> float:
    """链上活跃度因子 (0-1)。综合 commit、PR、社区活跃度。"""
    if not coin_data:
        return 0.0

    # 开发者维度 (0-0.5)
    commits = coin_data.get("commit_count_4_weeks", 0)
    prs = coin_data.get("github_pull_requests_merged", 0)
    contributors = coin_data.get("github_pull_request_contributors", 0)

    dev_score = 0
    if commits > 100:
        dev_score = 0.5
    elif commits > 50:
        dev_score = 0.4
    elif commits > 20:
        dev_score = 0.3
    elif commits > 5:
        dev_score = 0.2
    elif commits > 0:
        dev_score = 0.1

    # PR + contributors 加分
    if prs > 50:
        dev_score = min(0.5, dev_score + 0.1)
    if contributors > 20:
        dev_score = min(0.5, dev_score + 0.05)

    # 社区维度 (0-0.5)
    twitter = coin_data.get("twitter_followers", 0)
    reddit_activity = coin_data.get("reddit_avg_posts_48h", 0) + \
                      coin_data.get("reddit_avg_comments_48h", 0)
    telegram = coin_data.get("telegram_members", 0)

    comm_score = 0
    if twitter > 1_000_000:
        comm_score = 0.3
    elif twitter > 500_000:
        comm_score = 0.25
    elif twitter > 100_000:
        comm_score = 0.2
    elif twitter > 10_000:
        comm_score = 0.1

    if reddit_activity > 20:
        comm_score = min(0.5, comm_score + 0.1)
    if telegram > 100_000:
        comm_score = min(0.5, comm_score + 0.1)

    return round(dev_score + comm_score, 3)


def calc_dev_activity_score(coin_data: dict) -> float:
    """纯开发活跃因子 (0-1)。聚焦代码层面。"""
    if not coin_data:
        return 0.0

    commits = coin_data.get("commit_count_4_weeks", 0)
    additions = coin_data.get("code_additions_4_weeks", 0)
    deletions = abs(coin_data.get("code_deletions_4_weeks", 0))
    prs = coin_data.get("github_pull_requests_merged", 0)
    stars = coin_data.get("github_stars", 0)

    # commits 是核心指标
    import math
    score = 0
    if commits > 0:
        score = min(1.0, math.log(commits + 1) / math.log(200))

    # 代码变更量加分
    code_churn = additions + deletions
    if code_churn > 10000:
        score = min(1.0, score + 0.15)
    elif code_churn > 1000:
        score = min(1.0, score + 0.08)

    # stars 作为质量信号
    if stars > 10000:
        score = min(1.0, score + 0.1)
    elif stars > 1000:
        score = min(1.0, score + 0.05)

    return round(score, 3)


def calc_funding_rate_score(rate: float) -> float:
    """资金费率因子 (0-1)。

    逻辑: 负费率 = 空头拥挤 = 反转信号 (高分)
          极端正费率 = 多头拥挤 = 回调风险 (低分)
          温和正费率 = 正常 (中等分)
    """
    if rate == 0:
        return 0.5  # 无数据, 中性

    # rate 通常在 -0.01 ~ +0.01 之间
    # 负费率 → 高分 (空头付多头, 超卖)
    if rate < -0.005:
        return 0.95  # 极端负费率
    elif rate < -0.001:
        return 0.80
    elif rate < 0:
        return 0.65
    elif rate < 0.0005:
        return 0.50  # 温和正费率
    elif rate < 0.002:
        return 0.35
    elif rate < 0.005:
        return 0.20  # 较高正费率
    else:
        return 0.05  # 极端正费率, 多头过热


def calc_narrative_heat_score(coin_data: dict) -> float:
    """叙事热度因子 (0-1)。基于社区活跃度和情绪。"""
    if not coin_data:
        return 0.0

    sentiment = coin_data.get("sentiment_up_pct", 50) / 100  # 0-1
    reddit_activity = coin_data.get("reddit_avg_posts_48h", 0) + \
                      coin_data.get("reddit_avg_comments_48h", 0)
    twitter = coin_data.get("twitter_followers", 0)

    # 情绪基础分
    heat = sentiment * 0.4

    # 活跃度加分
    if reddit_activity > 50:
        heat += 0.3
    elif reddit_activity > 10:
        heat += 0.2
    elif reddit_activity > 2:
        heat += 0.1

    # 大社区基础热度
    if twitter > 1_000_000:
        heat += 0.3
    elif twitter > 300_000:
        heat += 0.2
    elif twitter > 50_000:
        heat += 0.1

    return round(min(1.0, heat), 3)
