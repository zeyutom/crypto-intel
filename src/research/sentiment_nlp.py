"""NLP 情绪因子 — facade 层 (v0.9 W2-S2).

架构 (新老不重复, 是分层关系):
  src.research.sentiment_nlp  (facade — 这里)
     text_sentiment() / batch_text_sentiment() / analyze_news_sentiment()
  ↓ 优先调:
    src.research.sentiment_bert  (CryptoBERT 模型, 装了 transformers 才用)
  ↓ 失败/未装则 fallback:
    本文件下方的 keyword_sentiment() 关键词词典

调用方应总是用 sentiment_nlp.text_sentiment, 不要直接调 sentiment_bert.
USE_BERT=0 可以强制走关键词路径 (用于对照测试).

替代 screener 中原始的 Reddit 评论数 / 简单正负词统计,
改用更精准的 NLP 方法提取情绪信号:

数据源:
  1. CoinGecko trending / status_updates (免费)
  2. Reddit 标题 + 热度 (pushshift 替代: 直接用 Reddit RSS)
  3. CryptoCompare News API (免费 tier)
  4. 可选: Claude 批量情绪评分 (高精度, 但需调 API)

因子输出:
  - sentiment_score: 综合情绪分 (-1 ~ +1)
  - sentiment_volume: 讨论量标准化 (0 ~ 1)
  - sentiment_momentum: 情绪变化率 (近 24h vs 7d 均值)
  - hype_divergence: 炒作与基本面的偏离度 (情绪 vs TVL/Dev 增长)

降级策略:
  Level 1: Claude 精细分析 (最准, 最慢)
  Level 2: 关键词权重 + 规则引擎 (较快, 中等精度)
  Level 3: 纯热度指标 (最快, 粗糙)
"""
from __future__ import annotations
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from ..utils import setup_logger

log = setup_logger("sentiment_nlp", "INFO")

# 可选: CryptoBERT (HuggingFace 模型, 软依赖)
try:
    from . import sentiment_bert  # noqa
    _BERT_LAZY = True   # 仅标记可 import, 实际加载推迟到第一次调用
except Exception:
    sentiment_bert = None
    _BERT_LAZY = False

# 通过 env var 关掉 BERT (用于对照测试: CRYPTO_INTEL_USE_BERT=0)
USE_BERT = os.environ.get("CRYPTO_INTEL_USE_BERT", "1") != "0"

# ── 情绪关键词词典 (中英双语, 按权重) ──
POSITIVE_KEYWORDS = {
    # 英文
    "bullish": 2.0, "moon": 1.5, "pump": 1.0, "breakout": 1.5,
    "surge": 1.5, "rally": 1.5, "ath": 2.0, "adoption": 1.5,
    "partnership": 1.2, "upgrade": 1.2, "launch": 1.0, "mainnet": 1.5,
    "listing": 1.0, "buy": 0.8, "long": 0.8, "hodl": 1.0,
    "accumulate": 1.2, "undervalued": 1.5, "gem": 1.2, "alpha": 1.2,
    "innovation": 1.0, "ecosystem": 0.8, "tvl growth": 1.5,
    "institutional": 1.5, "etf": 2.0, "approval": 1.5,
    # 中文
    "看涨": 2.0, "利好": 1.5, "暴涨": 1.5, "突破": 1.2,
    "新高": 2.0, "合作": 1.0, "上线": 1.0, "上车": 1.2,
    "抄底": 1.5, "起飞": 1.5, "机构": 1.5,
}

NEGATIVE_KEYWORDS = {
    # 英文
    "bearish": -2.0, "crash": -2.0, "dump": -1.5, "rug": -2.5,
    "scam": -2.5, "hack": -2.0, "exploit": -2.0, "sell": -0.8,
    "short": -0.8, "fear": -1.5, "capitulation": -2.0, "dead": -1.5,
    "bubble": -1.5, "overvalued": -1.5, "sec": -1.0, "lawsuit": -1.5,
    "ban": -2.0, "regulation": -0.8, "delay": -1.0, "bug": -1.0,
    "vulnerability": -1.5, "ponzi": -2.5, "shutdown": -2.0,
    # 中文
    "看跌": -2.0, "利空": -1.5, "暴跌": -2.0, "跑路": -2.5,
    "骗局": -2.5, "黑客": -2.0, "清算": -1.5, "崩盘": -2.0,
    "割韭菜": -2.0, "归零": -2.5, "监管": -0.8, "被盗": -2.0,
}


def _get(url: str, params: dict = None, headers: dict = None,
         timeout: int = 15) -> dict | list | None:
    """v0.9: forward 到统一 HttpClient."""
    from ..http_client import http
    return http.get_json(url, params=params, headers=headers,
                         timeout=timeout, ttl="hot")


# ====================================================================
#  数据源: CoinGecko Trending
# ====================================================================

def fetch_trending_coins() -> dict[str, float]:
    """CoinGecko trending coins → 热度分数。"""
    log.info("  获取 CoinGecko trending...")
    data = _get("https://api.coingecko.com/api/v3/search/trending")
    if not data:
        return {}

    trending = {}
    coins = data.get("coins", [])
    for i, item in enumerate(coins):
        coin = item.get("item", {})
        sym = coin.get("symbol", "").upper()
        # 排名靠前得分越高
        score = max(0, 1.0 - i * 0.1)
        trending[sym] = score

    log.info(f"    ✓ {len(trending)} trending coins")
    return trending


# ====================================================================
#  数据源: CryptoCompare News
# ====================================================================

def fetch_crypto_news(symbols: list[str] = None) -> list[dict]:
    """CryptoCompare 新闻 API (免费)。"""
    log.info("  获取 CryptoCompare 新闻...")
    params = {"lang": "EN", "sortOrder": "latest"}
    if symbols:
        params["categories"] = ",".join(symbols[:10])

    data = _get("https://min-api.cryptocompare.com/data/v2/news/",
                params=params, timeout=20)
    if not data:
        return []

    articles = data.get("Data", [])
    # Data 可能是 dict (按 key 索引) 或 list
    if isinstance(articles, dict):
        articles = list(articles.values()) if articles else []
    if not isinstance(articles, list):
        articles = []
    result = []
    for a in articles[:100]:  # 最近 100 条
        result.append({
            "title": a.get("title", ""),
            "body": a.get("body", "")[:500],
            "categories": a.get("categories", ""),
            "source": a.get("source_info", {}).get("name", ""),
            "published": a.get("published_on", 0),
            "url": a.get("url", ""),
        })

    log.info(f"    ✓ {len(result)} 条新闻")
    return result


# ====================================================================
#  Level 2: 关键词权重引擎 (无需 LLM)
# ====================================================================

def keyword_sentiment(text: str) -> tuple[float, int]:
    """基于关键词词典计算情绪分。

    Returns: (sentiment_score, keyword_hits)
    """
    text_lower = text.lower()
    score = 0.0
    hits = 0

    for word, weight in POSITIVE_KEYWORDS.items():
        count = text_lower.count(word.lower())
        if count > 0:
            score += weight * count
            hits += count

    for word, weight in NEGATIVE_KEYWORDS.items():
        count = text_lower.count(word.lower())
        if count > 0:
            score += weight * count  # weight 已经是负数
            hits += count

    # 归一化到 [-1, 1]
    if hits > 0:
        score = max(-1.0, min(1.0, score / (hits * 1.5)))

    return score, hits


def text_sentiment(text: str) -> tuple[float, str]:
    """统一文本情绪打分入口。

    顺序: ① BERT (CryptoBERT) → ② 关键词词典。
    Returns: (score ∈ [-1, +1], method ∈ {"bert", "keyword"})
    """
    if USE_BERT and sentiment_bert is not None and sentiment_bert.is_available():
        scores = sentiment_bert.score_texts([text])
        if scores is not None and scores:
            return float(scores[0]), "bert"
    s, _ = keyword_sentiment(text)
    return s, "keyword"


def batch_text_sentiment(texts: list[str]) -> tuple[list[float], str]:
    """批量打分 (BERT 优先, 关键词兜底)。Returns: (scores, method)。"""
    if not texts:
        return [], "empty"
    if USE_BERT and sentiment_bert is not None and sentiment_bert.is_available():
        scores = sentiment_bert.score_texts(texts)
        if scores is not None:
            return scores, "bert"
    return [keyword_sentiment(t)[0] for t in texts], "keyword"


def analyze_news_sentiment(articles: list[dict], symbol: str) -> dict:
    """分析新闻列表中与 symbol 相关的情绪。

    Upgrade: 现在优先用 CryptoBERT, 不可用时降级到关键词词典。
    """
    sym_lower = symbol.lower()
    relevant_texts = []
    relevant_meta = []

    for a in articles:
        text = f"{a.get('title', '')} {a.get('categories', '')}"
        if sym_lower in text.lower():
            scoring_text = f"{a['title']} {a.get('body', '')[:200]}"
            relevant_texts.append(scoring_text)
            relevant_meta.append({"title": a["title"][:80]})

    if not relevant_texts:
        return {"sentiment": 0.0, "volume": 0, "articles": [], "method": "none"}

    scores, method = batch_text_sentiment(relevant_texts)

    # 同时拿一份关键词命中数 (用于过滤"噪声新闻": 命中数为 0 的可忽略)
    relevant = []
    for meta, s, text in zip(relevant_meta, scores, relevant_texts):
        _, hits = keyword_sentiment(text)
        relevant.append({**meta, "score": round(float(s), 4), "hits": hits})

    avg_sentiment = sum(r["score"] for r in relevant) / len(relevant)
    return {
        "sentiment": round(avg_sentiment, 3),
        "volume": len(relevant),
        "articles": relevant[:5],  # Top 5
        "method": method,
    }


# ====================================================================
#  Level 1: Claude 批量情绪评分 (可选, 高精度)
# ====================================================================

def claude_sentiment_batch(texts: list[str]) -> list[float]:
    """用 Claude CLI 对一批文本做情绪评分。

    输出: [-1, +1] 的分数列表。降级到关键词分析 if 不可用。
    """
    try:
        from ..evolution._claude_runner import run_claude
    except Exception:
        return [keyword_sentiment(t)[0] for t in texts]

    if not texts:
        return []

    # 批量提交 (一次最多 20 条)
    batch = texts[:20]
    numbered = "\n".join(f"{i+1}. {t[:150]}" for i, t in enumerate(batch))

    prompt = f"""对以下 {len(batch)} 条加密货币相关文本做情绪评分。
每条评分范围: -1.0 (极度看跌) 到 +1.0 (极度看涨), 0.0 为中性。

文本:
{numbered}

输出严格 JSON 数组 (只包含浮点数, 与文本一一对应):
例如: [-0.3, 0.8, 0.0, ...]
只输出 JSON 数组, 不要其他内容。"""

    result = run_claude(prompt, system="你是加密货币情绪分析专家。只输出 JSON。", timeout=60)
    if not result.get("ok"):
        return [keyword_sentiment(t)[0] for t in texts]

    md = result.get("markdown", "")
    json_match = re.search(r'\[[\s\S]*?\]', md)
    if not json_match:
        return [keyword_sentiment(t)[0] for t in texts]

    try:
        scores = json.loads(json_match.group())
        if len(scores) == len(batch):
            return [max(-1, min(1, float(s))) for s in scores]
    except Exception:
        pass

    return [keyword_sentiment(t)[0] for t in texts]


# ====================================================================
#  综合情绪因子计算
# ====================================================================

def compute_sentiment_factors(
    symbols: list[str],
    use_claude: bool = False,
) -> dict[str, dict]:
    """为一组 symbols 计算情绪因子。

    Returns: {symbol: {sentiment_score, sentiment_volume, hype_score}}
    """
    log.info(f"[NLP 情绪] 分析 {len(symbols)} 个代币...")

    # 获取数据源
    trending = fetch_trending_coins()
    time.sleep(1)  # rate limit
    news = fetch_crypto_news(symbols[:10])

    result = {}
    news_texts = []
    news_syms = []

    for sym in symbols:
        # 1. Trending 热度
        trend_score = trending.get(sym, 0)

        # 2. 新闻情绪
        news_sentiment = analyze_news_sentiment(news, sym)

        # 3. 综合计算
        sent = news_sentiment["sentiment"]
        vol = news_sentiment["volume"]

        # 讨论量标准化 (0-1)
        sentiment_volume = min(1.0, vol / 10) if vol > 0 else 0

        # 综合情绪分: 新闻情绪 * 0.6 + trending 热度 * 0.4
        combined = sent * 0.6 + (trend_score * 0.5) * 0.4

        # Hype score: 纯热度 (无方向)
        hype = trend_score * 0.5 + sentiment_volume * 0.5

        result[sym] = {
            "sentiment_score": round(max(-1, min(1, combined)), 3),
            "sentiment_volume": round(sentiment_volume, 3),
            "hype_score": round(hype, 3),
            "trending_rank": round(trend_score, 3),
            "news_sentiment": round(sent, 3),
            "news_count": vol,
        }

        if vol > 0 and use_claude:
            for a in news_sentiment.get("articles", [])[:3]:
                news_texts.append(a["title"])
                news_syms.append(sym)

    # 可选: Claude 精细分析
    if use_claude and news_texts:
        log.info(f"  Claude 精细分析 {len(news_texts)} 条...")
        claude_scores = claude_sentiment_batch(news_texts)
        # 用 Claude 分数覆盖
        sym_scores = {}
        for sym, score in zip(news_syms, claude_scores):
            if sym not in sym_scores:
                sym_scores[sym] = []
            sym_scores[sym].append(score)
        for sym, scores in sym_scores.items():
            if sym in result:
                claude_avg = sum(scores) / len(scores)
                # Claude 分数权重更高
                old = result[sym]["sentiment_score"]
                result[sym]["sentiment_score"] = round(
                    claude_avg * 0.7 + old * 0.3, 3
                )

    log.info(f"  ✓ 情绪分析完成: {len(result)} 代币")
    return result


def calc_sentiment_factor(sym: str, sentiment_data: dict) -> float:
    """从情绪数据计算单个综合情绪因子分 (0-1, 用于 screener)。"""
    if not sentiment_data or sym not in sentiment_data:
        return 0.5  # 无数据时返回中性

    d = sentiment_data[sym]
    sent = d.get("sentiment_score", 0)  # -1 ~ +1
    vol = d.get("sentiment_volume", 0)  # 0 ~ 1

    # 映射到 0-1: 情绪 * 讨论量加权
    # 高情绪+高讨论量 → 接近 1
    # 低情绪+低讨论量 → 接近 0
    # 中性/无讨论 → 0.5
    base = (sent + 1) / 2  # 0 ~ 1
    # 讨论量低时向 0.5 收敛
    factor = base * vol + 0.5 * (1 - vol)
    return round(max(0, min(1, factor)), 3)


def calc_hype_divergence(sym: str, sentiment_data: dict,
                          tvl_growth: float = 0, dev_growth: float = 0) -> float:
    """炒作偏离度: 情绪热度 vs 基本面增长。

    正值 = 过度炒作 (情绪远超基本面) → 风险信号
    负值 = 被低估 (基本面强但情绪冷) → 机会信号
    """
    if not sentiment_data or sym not in sentiment_data:
        return 0.0

    hype = sentiment_data[sym].get("hype_score", 0)
    fundamentals = (tvl_growth + dev_growth) / 2  # 简单均值

    divergence = hype - fundamentals
    return round(max(-1, min(1, divergence)), 3)
