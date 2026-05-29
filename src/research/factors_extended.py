"""扩展因子数据源 — 链上活跃度、开发者、叙事热度、资金费率。

所有 API 均为公开免费, 无需 key。
"""
from __future__ import annotations
import time
from ..utils import setup_logger

log = setup_logger("factors_ext", "INFO")


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

    log.info(f"    ✓ {len(result)} coins with dev+community data")
    return result


# ====================================================================
#  资金费率因子 (Binance Futures)
# ====================================================================

def fetch_funding_rates() -> dict[str, float]:
    """Binance Futures 永续合约 funding rate。

    返回 {SYMBOL: funding_rate} (如 BTC: 0.0001 = 0.01%)
    负费率 = 空头付多头 = 可能超卖
    """
    log.info("  Binance Futures funding rates...")
    data = _get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=20)
    if not data or not isinstance(data, list):
        log.warning("    ✗ Binance Futures API 不可用")
        return {}

    rates = {}
    for item in data:
        sym = item.get("symbol", "")
        if sym.endswith("USDT"):
            base = sym[:-4]
            try:
                rate = float(item.get("lastFundingRate", 0))
                rates[base] = rate
            except (ValueError, TypeError):
                pass

    log.info(f"    ✓ {len(rates)} perpetual pairs")
    return rates


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
