"""CryptoBERT 情绪模型 — 替换/补强 sentiment_nlp.py 的关键词词典。

模型来源:
  - ElKulako/cryptobert (HuggingFace): 在 3.2M 加密社媒 + 2M StockTwits 上
    fine-tune 的 BERT, 输出 bearish/neutral/bullish 三分类。
  - 备选: kk08/CryptoBERT (二分类 positive/negative, 基于 FinBERT)
  - Fallback: ProsusAI/finbert (经典金融情绪基线)

设计原则:
  - 软依赖 transformers + torch (没装时返回 None, 降级到关键词法)
  - 模型只加载一次 (module-level cache)
  - CPU 也能跑, 推理慢但够用 (~50-100 texts/s on M-series)
  - 批量推理减少开销
  - 输出量纲 ∈ [-1, +1], 兼容现有 sentiment_score 接口

集成方式:
  方式 A: 在 sentiment_nlp.py 里检查 bert 是否可用, 优先用 bert,
          否则降级到关键词法 (本模块只提供函数, 由 sentiment_nlp 决定)
  方式 B: 单独跑, 输出独立的 bert_sentiment 因子, 让元学习决定权重

本文件实现方式 A 的基础设施 + 一个直接可用的 score_texts() API。
"""
from __future__ import annotations
import os
from typing import Optional

from ..utils import setup_logger

log = setup_logger("sentiment_bert", "INFO")

# 默认模型 (按优先级)
DEFAULT_MODELS = [
    "ElKulako/cryptobert",          # 三分类: bearish/neutral/bullish
    "kk08/CryptoBERT",              # 二分类: negative/positive
    "ProsusAI/finbert",             # fallback: 通用金融情绪
]

# Label → 数值映射 (统一到 [-1, +1])
LABEL_TO_SCORE = {
    # ElKulako/cryptobert
    "bearish": -1.0, "neutral": 0.0, "bullish": +1.0,
    "Bearish": -1.0, "Neutral": 0.0, "Bullish": +1.0,
    "BEARISH": -1.0, "NEUTRAL": 0.0, "BULLISH": +1.0,
    # kk08/CryptoBERT
    "negative": -1.0, "positive": +1.0,
    "Negative": -1.0, "Positive": +1.0,
    "NEGATIVE": -1.0, "POSITIVE": +1.0,
    # ProsusAI/finbert
    "LABEL_0": -1.0,  # negative
    "LABEL_1": 0.0,   # neutral
    "LABEL_2": +1.0,  # positive
}

# Module-level 缓存
_pipeline = None
_model_id = None
_load_attempted = False


def _try_load(model_id: str = None):
    """惰性加载 HuggingFace pipeline。失败返回 None。"""
    global _pipeline, _model_id, _load_attempted
    if _pipeline is not None:
        return _pipeline
    if _load_attempted and _pipeline is None:
        return None
    _load_attempted = True

    candidates = [model_id] if model_id else DEFAULT_MODELS

    try:
        from transformers import pipeline
    except ImportError:
        log.warning("transformers not installed (pip install transformers torch)")
        return None

    for mid in candidates:
        if not mid:
            continue
        try:
            log.info(f"  loading sentiment model: {mid}")
            _pipeline = pipeline(
                "text-classification",
                model=mid,
                tokenizer=mid,
                # 关键: top_k=None 让它返回所有类别的概率, 自己加权
                top_k=None,
                # CPU 兼容
                device=-1,
            )
            _model_id = mid
            log.info(f"  ✓ loaded {mid}")
            return _pipeline
        except Exception as e:
            log.warning(f"  failed to load {mid}: {e}")
            continue

    log.warning("  no sentiment model loaded")
    return None


def is_available() -> bool:
    """检测是否可用 (用于 sentiment_nlp.py 优雅降级判断)。"""
    return _try_load() is not None


def model_id() -> Optional[str]:
    """返回当前加载的模型 id (None 表示未加载)。"""
    _try_load()
    return _model_id


def _score_one_pred(pred: list[dict]) -> float:
    """把单条预测的所有类别概率加权成 [-1,+1]。

    pred 形如 [{"label": "Bullish", "score": 0.7}, {"label": "Bearish", "score": 0.2}, ...]
    返回 sum(label_score * prob)
    """
    if not isinstance(pred, list):
        # 兼容老版 pipeline 返回 dict 的情况
        pred = [pred]
    total = 0.0
    for p in pred:
        label = p.get("label", "")
        prob = float(p.get("score", 0))
        weight = LABEL_TO_SCORE.get(label, 0.0)
        total += weight * prob
    return float(total)


def score_texts(texts: list[str], batch_size: int = 16,
                truncate_chars: int = 512) -> Optional[list[float]]:
    """批量给文本打情绪分。

    Args:
        texts: 文本列表
        batch_size: 批量大小 (BERT 推理)
        truncate_chars: 文本截断长度 (避免 tokenizer 报错)

    Returns:
        与 texts 等长的 float list, 每个 ∈ [-1, +1]
        如果模型不可用, 返回 None (调用方应降级)
    """
    if not texts:
        return []
    pipe = _try_load()
    if pipe is None:
        return None

    # 截断 + 去空 + tokenizer 最大 512 token
    clean = [(t or "")[:truncate_chars] for t in texts]

    scores: list[float] = []
    try:
        # transformers pipeline 支持 batch_size 参数
        results = pipe(clean, batch_size=batch_size, truncation=True, max_length=512)
        # 当 top_k=None 时, 每个结果是 list[dict]; 否则是 dict
        for r in results:
            scores.append(_score_one_pred(r))
    except Exception as e:
        log.warning(f"  batch inference failed: {e}, falling back to per-text")
        for t in clean:
            try:
                r = pipe(t, truncation=True, max_length=512)
                scores.append(_score_one_pred(r if isinstance(r, list) else [r]))
            except Exception:
                scores.append(0.0)
    return scores


def aggregate_sentiment(texts: list[str]) -> dict:
    """对一组文本(同一币种近期讨论)做综合情绪指标。

    Returns:
        {
          "n_texts": int,
          "mean_score": float ∈ [-1, +1],
          "std_score": float,
          "share_bullish": float (>0.3 算 bullish),
          "share_bearish": float (<-0.3 算 bearish),
          "model": str,
          "_status": "ok" | "fallback" | "unavailable"
        }
    """
    if not texts:
        return {"n_texts": 0, "_status": "empty"}

    scores = score_texts(texts)
    if scores is None:
        return {
            "n_texts": len(texts),
            "_status": "unavailable",
            "_hint": "pip install transformers torch  (or use sentiment_nlp.py keyword fallback)",
        }

    import statistics as stat
    n = len(scores)
    mean = stat.mean(scores)
    std = stat.pstdev(scores) if n > 1 else 0.0
    bullish = sum(1 for s in scores if s > 0.3) / n
    bearish = sum(1 for s in scores if s < -0.3) / n

    return {
        "n_texts": n,
        "mean_score": round(mean, 4),
        "std_score": round(std, 4),
        "share_bullish": round(bullish, 4),
        "share_bearish": round(bearish, 4),
        "model": _model_id,
        "_status": "ok",
    }


# ====================================================================
#  Self-test
# ====================================================================

if __name__ == "__main__":
    print(f"[sentiment_bert] available: {is_available()}")
    if is_available():
        sample = [
            "BTC just broke ATH! Bullish AF, moon incoming.",
            "ETH gas fees are absurd, this network is dying.",
            "Tomorrow is Fed meeting. Let's see.",
            "Solana TVL keeps growing, fundamentals strong.",
            "Memecoin rugpull again, devs running with funds.",
        ]
        result = aggregate_sentiment(sample)
        print(f"  model: {result.get('model')}")
        print(f"  mean_score: {result.get('mean_score')}")
        print(f"  share_bullish: {result.get('share_bullish')}")
        print(f"  share_bearish: {result.get('share_bearish')}")
        scores = score_texts(sample)
        for t, s in zip(sample, scores):
            print(f"    [{s:+.3f}] {t[:60]}")
    else:
        print("  install: pip install transformers torch --break-system-packages")
        print("  model:   ElKulako/cryptobert (auto-download ~120MB on first run)")
