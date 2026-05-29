"""Unit tests for src.http_client — 统一 HTTP 工具.

不联网, 只测试 token bucket / cache / metrics / forward 逻辑.
"""
import sys
import pathlib
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from src.http_client import (
    HttpClient, TokenBucket, HOST_RATE_LIMITS, TTL_LEVELS,
    http, http_get_json,
)


# ────────────────────────────────────────────────────────────────────
#  TokenBucket
# ────────────────────────────────────────────────────────────────────

def test_bucket_initial_capacity():
    b = TokenBucket(capacity=5, refill_per_sec=1)
    # 初始应该是满的
    assert b.tokens == 5.0


def test_bucket_drains_then_refills():
    b = TokenBucket(capacity=2, refill_per_sec=10)  # 0.1s 一个
    # 取 2 个, 立刻空
    assert b.take(2, max_wait=0.5)
    # 再取 1 个, 应该等 0.1s
    t0 = time.time()
    ok = b.take(1, max_wait=0.5)
    assert ok
    assert 0.05 < time.time() - t0 < 0.3


def test_bucket_wait_timeout():
    b = TokenBucket(capacity=1, refill_per_sec=0.01)  # 100s 才回 1 个
    b.take(1)
    # 立刻再取, max_wait=0.2s, 应该返回 False
    ok = b.take(1, max_wait=0.2)
    assert ok is False


# ────────────────────────────────────────────────────────────────────
#  HttpClient: cache + bucket + ttl
# ────────────────────────────────────────────────────────────────────

def test_singleton_is_module_level():
    from src.http_client import http as h2
    assert http is h2


def test_per_host_bucket_isolated():
    c = HttpClient()
    b1 = c._get_bucket("api.coingecko.com")
    b2 = c._get_bucket("api.coingecko.com")
    b3 = c._get_bucket("api.llama.fi")
    assert b1 is b2
    assert b1 is not b3


def test_cache_key_deterministic():
    c = HttpClient()
    k1 = c._cache_key("GET", "https://x.com/a", {"q": 1, "z": 2})
    k2 = c._cache_key("GET", "https://x.com/a", {"z": 2, "q": 1})
    # params 顺序不影响 key
    assert k1 == k2


def test_ttl_normalize_levels():
    c = HttpClient()
    assert c._normalize_ttl("hot") == 60
    assert c._normalize_ttl("warm") == 3600
    assert c._normalize_ttl("cold") == 86400
    assert c._normalize_ttl(120) == 120
    assert c._normalize_ttl(None) == 60   # default


def test_ttl_normalize_invalid_returns_default():
    c = HttpClient()
    assert c._normalize_ttl("nonsense") == 60


def test_in_memory_cache_roundtrip():
    c = HttpClient()
    key = "test_key_xyz"
    c._set_cached(key, {"foo": "bar"})
    got = c._get_cached(key, ttl=60)
    assert got == {"foo": "bar"}


def test_in_memory_cache_expires(monkeypatch, tmp_path):
    """sleep > ttl 后应过期. 把磁盘 cache 重定向到 tmp 避免污染."""
    import src.http_client as m
    monkeypatch.setattr(m, "CACHE_DIR", tmp_path)
    c = HttpClient()
    key = "test_expiring_unique"
    c._set_cached(key, "v")
    time.sleep(1.2)
    assert c._get_cached(key, ttl=1) is None
    c._set_cached(key, "v2")
    assert c._get_cached(key, ttl=10) == "v2"


# ────────────────────────────────────────────────────────────────────
#  Metrics
# ────────────────────────────────────────────────────────────────────

def test_metrics_initial_empty():
    c = HttpClient()
    stats = c.stats()
    assert stats["cache_files"] >= 0
    assert isinstance(stats["hosts"], list)


def test_reset_metrics_clears_counters():
    c = HttpClient()
    c.metrics["test.host"].calls = 5
    c.metrics["test.host"].cached = 3
    c.reset_metrics()
    assert "test.host" not in c.metrics or c.metrics["test.host"].calls == 0


# ────────────────────────────────────────────────────────────────────
#  HOST_RATE_LIMITS 配置正确性
# ────────────────────────────────────────────────────────────────────

def test_coingecko_rate_limit_conservative():
    """CoinGecko 免费版限速 10-30, 我们配的应该 ≤ 30."""
    assert HOST_RATE_LIMITS["api.coingecko.com"] <= 30


def test_defillama_hosts_all_configured():
    """4 个 DefiLlama base url 都应该有 rate limit 配置."""
    for h in ["api.llama.fi", "coins.llama.fi",
              "stablecoins.llama.fi", "yields.llama.fi"]:
        assert h in HOST_RATE_LIMITS, f"missing rate limit for {h}"


# ────────────────────────────────────────────────────────────────────
#  Backward-compat forward
# ────────────────────────────────────────────────────────────────────

def test_legacy_http_get_json_forwards_to_http(monkeypatch):
    """utils.http_get_json 应该走新 HttpClient."""
    captured = {}
    def fake_get_json(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return {"ok": True}
    monkeypatch.setattr(http, "get_json", fake_get_json)
    result = http_get_json("https://test.example.com/a", params={"q": 1})
    assert captured["url"] == "https://test.example.com/a"
    assert captured["kwargs"]["params"] == {"q": 1}
    assert result == {"ok": True}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
