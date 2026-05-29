"""统一 HTTP 客户端 (W2-S3).

把项目里 9 处独立实现的 _get / _http_get / http_get_json 收敛到这一个模块,
解决:
  - 速率限制: per-host token bucket → CoinGecko 限速时全项目协调
  - 缓存: 内存 + 磁盘双层, TTL 分级 (热 60s / 温 1h / 冷 24h)
  - 重试: 429/5xx 自动 backoff, 网络错误重试
  - 可观测性: per-host 计数 (call/cached/failed) → /metrics endpoint 用得上
  - 用户代理统一 "CryptoIntel/0.9"

API:
    from src.http_client import http  # 单例
    data = http.get_json("https://api.coingecko.com/api/v3/coins/markets",
                         params={"vs_currency": "usd"}, ttl=300)
    data = http.get_json(url, ttl="hot")   # 别名: hot=60s, warm=3600s, cold=86400s

兼容旧 API:
    from src.utils import http_get_json       # 仍可用 (内部转发到 http)
    from src.http_client import http_get_json # 推荐
"""
from __future__ import annotations
import json
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union
from urllib.parse import urlparse

from .utils import setup_logger

log = setup_logger("http_client", "INFO")

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "http"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# TTL 等级 (秒)
TTL_LEVELS = {
    "hot": 60,
    "warm": 3600,
    "cold": 86400,
    "frozen": 7 * 86400,
}

# Per-host 速率配置 (req/min)
# 没显式配的 host 默认 60 req/min
# v0.9: CoinGecko 调到 10 req/min (官方限速波动 5-30, 取超保守)
HOST_RATE_LIMITS = {
    "api.coingecko.com": 10,           # 实测多 adapter 同跑会 429, 10 才稳
    "api.binance.com": 600,            # weight-based, 大致换算
    "fapi.binance.com": 600,
    "www.okx.com": 300,
    "api.exchange.coinbase.com": 300,
    "api.llama.fi": 300,
    "coins.llama.fi": 300,
    "stablecoins.llama.fi": 300,
    "yields.llama.fi": 300,
    "min-api.cryptocompare.com": 50,   # 免费 100k/月, 取保守
    "open.feishu.cn": 30,              # 飞书 webhook
    "blockchain.info": 60,
    "fapi.coinglass.com": 30,          # 已基本停止服务, 慢点没事
    "api.alternative.me": 30,          # Fear & Greed
    "farside.co.uk": 6,                # Cloudflare 重, 慢慢跑
}

DEFAULT_RATE = 60   # 未知 host 的默认值
DEFAULT_TTL = 60    # 没传 ttl 时, 默认 60s
DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = "CryptoIntel/0.9 (+research)"

# 重试策略
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.5  # 指数退避: 1.5s, 2.25s, 3.4s


# ────────────────────────────────────────────────────────────────────
#  Token bucket (per-host)
# ────────────────────────────────────────────────────────────────────

@dataclass
class TokenBucket:
    capacity: float
    refill_per_sec: float
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        self.tokens = self.capacity

    def take(self, n: int = 1, max_wait: float = 30.0) -> bool:
        """尝试拿 n 个 token, 不够等到够 (但最多等 max_wait 秒)."""
        deadline = time.time() + max_wait
        while True:
            with self._lock:
                now = time.time()
                elapsed = now - self.last_refill
                self.tokens = min(
                    self.capacity, self.tokens + elapsed * self.refill_per_sec
                )
                self.last_refill = now
                if self.tokens >= n:
                    self.tokens -= n
                    return True
                # 计算需要等多久
                need_more = n - self.tokens
                wait_s = need_more / self.refill_per_sec
            if time.time() + wait_s > deadline:
                # 等不到了
                return False
            time.sleep(min(wait_s, 0.5))


# ────────────────────────────────────────────────────────────────────
#  Metrics (per-host call counter)
# ────────────────────────────────────────────────────────────────────

@dataclass
class HostMetrics:
    calls: int = 0
    cached: int = 0
    errors: int = 0
    rate_limited: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.calls, 1)


# ────────────────────────────────────────────────────────────────────
#  Client 单例
# ────────────────────────────────────────────────────────────────────

class HttpClient:
    """全局 HTTP 客户端 (线程安全 singleton)."""

    def __init__(self):
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_lock = threading.Lock()
        self._mem_cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self.metrics: dict[str, HostMetrics] = defaultdict(HostMetrics)

    # ── 速率限制 ─────────────────────────────────────────────────────

    def _get_bucket(self, host: str) -> TokenBucket:
        with self._buckets_lock:
            if host not in self._buckets:
                rpm = HOST_RATE_LIMITS.get(host, DEFAULT_RATE)
                # token bucket: 用 burst capacity = rpm/4, refill = rpm/60
                self._buckets[host] = TokenBucket(
                    capacity=max(2.0, rpm / 4.0),
                    refill_per_sec=rpm / 60.0,
                )
            return self._buckets[host]

    # ── 缓存层 ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_ttl(ttl) -> int:
        if ttl is None:
            return DEFAULT_TTL
        if isinstance(ttl, str):
            return TTL_LEVELS.get(ttl, DEFAULT_TTL)
        try:
            return int(ttl)
        except (ValueError, TypeError):
            return DEFAULT_TTL

    @staticmethod
    def _cache_key(method: str, url: str, params: dict = None) -> str:
        p = json.dumps(params or {}, sort_keys=True, default=str)
        safe = f"{method}_{url}_{p}".replace("/", "_").replace(":", "_").replace("?", "_")
        return safe[:240]

    def _get_cached(self, key: str, ttl: int) -> Optional[Any]:
        # 内存
        with self._cache_lock:
            if key in self._mem_cache:
                ts, val = self._mem_cache[key]
                if time.time() - ts < ttl:
                    return val
        # 磁盘
        p = CACHE_DIR / f"{key}.json"
        if p.exists():
            age = time.time() - p.stat().st_mtime
            if age < ttl:
                try:
                    data = json.loads(p.read_text())
                    with self._cache_lock:
                        self._mem_cache[key] = (time.time(), data)
                    return data
                except Exception:
                    pass
        return None

    def _set_cached(self, key: str, value: Any):
        with self._cache_lock:
            self._mem_cache[key] = (time.time(), value)
        try:
            (CACHE_DIR / f"{key}.json").write_text(
                json.dumps(value, default=str, ensure_ascii=False)
            )
        except Exception:
            pass

    # ── 主入口 ───────────────────────────────────────────────────────

    def get(
        self,
        url: str,
        params: dict = None,
        headers: dict = None,
        timeout: float = DEFAULT_TIMEOUT,
        ttl: Union[int, str, None] = None,
        retries: int = MAX_RETRIES,
        skip_cache: bool = False,
        is_json: bool = True,
    ) -> Optional[Any]:
        """统一 GET. ttl 支持 'hot'/'warm'/'cold'/'frozen' 或秒数."""
        try:
            import httpx
        except ImportError:
            log.error("httpx not installed, pip install httpx")
            return None

        host = urlparse(url).netloc or "unknown"
        ttl_sec = self._normalize_ttl(ttl)
        cache_key = self._cache_key("GET", url, params)

        # 1. 查缓存
        if not skip_cache and ttl_sec > 0:
            cached = self._get_cached(cache_key, ttl_sec)
            if cached is not None:
                self.metrics[host].cached += 1
                return cached

        # 2. 速率限制
        if not self._get_bucket(host).take(1):
            log.warning(f"  rate-limit wait timeout for {host}")
            self.metrics[host].rate_limited += 1
            return None

        # 3. 真正请求 (带重试)
        merged_headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
        if headers:
            merged_headers.update(headers)

        t0 = time.time()
        last_err: Optional[str] = None
        for attempt in range(retries + 1):
            try:
                r = httpx.get(url, params=params, headers=merged_headers,
                              timeout=timeout, follow_redirects=True)
                elapsed_ms = (time.time() - t0) * 1000
                self.metrics[host].calls += 1
                self.metrics[host].total_latency_ms += elapsed_ms

                if r.status_code == 200:
                    data = r.json() if is_json else r.text
                    if ttl_sec > 0:
                        self._set_cached(cache_key, data)
                    return data

                if r.status_code in RETRY_STATUSES:
                    if r.status_code == 429:
                        self.metrics[host].rate_limited += 1
                    last_err = f"HTTP {r.status_code}"
                    if attempt < retries:
                        wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                        # 尊重 Retry-After header
                        retry_after = r.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait = max(wait, float(retry_after))
                            except ValueError:
                                pass
                        log.warning(f"  {host} {r.status_code}, retry in {wait:.1f}s")
                        time.sleep(wait)
                        continue

                # 非可重试错误
                log.warning(f"  {host} HTTP {r.status_code} (not retrying)")
                self.metrics[host].errors += 1
                return None

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_err = str(e)[:80]
                if attempt < retries:
                    wait = RETRY_BACKOFF_BASE ** (attempt + 1)
                    log.warning(f"  {host} {type(e).__name__}, retry in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                self.metrics[host].errors += 1
                log.warning(f"  {host} {type(e).__name__}: {e}")
                return None
            except Exception as e:
                log.warning(f"  {host} unexpected: {e}")
                self.metrics[host].errors += 1
                return None

        self.metrics[host].errors += 1
        log.warning(f"  {host} 重试 {retries} 次仍失败: {last_err}")
        return None

    def get_json(self, url: str, **kwargs) -> Optional[Any]:
        """get + JSON 解析 (别名)."""
        kwargs.setdefault("is_json", True)
        return self.get(url, **kwargs)

    def get_text(self, url: str, **kwargs) -> Optional[str]:
        kwargs["is_json"] = False
        return self.get(url, **kwargs)

    # ── 工具 ─────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """返回 per-host 调用统计 + 缓存大小."""
        rows = []
        for host, m in self.metrics.items():
            rows.append({
                "host": host,
                "calls": m.calls,
                "cached": m.cached,
                "errors": m.errors,
                "rate_limited": m.rate_limited,
                "avg_latency_ms": round(m.avg_latency_ms, 1),
                "cache_hit_rate": round(
                    m.cached / max(m.calls + m.cached, 1) * 100, 1
                ),
            })
        rows.sort(key=lambda x: x["calls"] + x["cached"], reverse=True)

        # 缓存磁盘占用
        cache_files = list(CACHE_DIR.glob("*.json"))
        cache_mb = sum(f.stat().st_size for f in cache_files) / 1024 / 1024
        return {
            "hosts": rows,
            "cache_files": len(cache_files),
            "cache_mb": round(cache_mb, 2),
            "memory_entries": len(self._mem_cache),
        }

    def reset_metrics(self):
        self.metrics.clear()

    def clear_cache(self, host_filter: str = None):
        """清缓存. host_filter 不为 None 时只清匹配的."""
        with self._cache_lock:
            if host_filter:
                self._mem_cache = {
                    k: v for k, v in self._mem_cache.items()
                    if host_filter not in k
                }
            else:
                self._mem_cache.clear()
        cleared = 0
        for f in CACHE_DIR.glob("*.json"):
            if host_filter and host_filter not in f.name:
                continue
            try:
                f.unlink()
                cleared += 1
            except Exception:
                pass
        log.info(f"cleared {cleared} cache files (filter={host_filter})")


# Global singleton
http = HttpClient()


# ────────────────────────────────────────────────────────────────────
#  向后兼容包装 (旧 API forward 到新 http)
# ────────────────────────────────────────────────────────────────────

def http_get_json(url: str, params: dict = None, headers: dict = None,
                  timeout: float = DEFAULT_TIMEOUT, ttl=None) -> Optional[Any]:
    """与 src.utils.http_get_json 兼容. 推荐直接用 http.get_json."""
    return http.get_json(url, params=params, headers=headers,
                         timeout=timeout, ttl=ttl)


def health() -> dict:
    """诊断: 各 host 配额 + 缓存大小."""
    s = http.stats()
    return {
        "rate_limits_configured": HOST_RATE_LIMITS,
        "ttl_levels": TTL_LEVELS,
        "stats": s,
    }


# ────────────────────────────────────────────────────────────────────
#  Self-test
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. CoinGecko 测试 (限速 25 req/min)
    print("=== test 1: CoinGecko ping ===")
    r = http.get_json("https://api.coingecko.com/api/v3/ping", ttl="hot")
    print(f"  result: {r}")

    # 2. 命中缓存
    print("\n=== test 2: cache hit ===")
    r2 = http.get_json("https://api.coingecko.com/api/v3/ping", ttl="hot")
    print(f"  cached call result: {r2}")

    # 3. 多 host 并发风格 (顺序)
    print("\n=== test 3: multi-host ===")
    for url in [
        "https://api.llama.fi/v2/chains",
        "https://stablecoins.llama.fi/stablecoinchains",
    ]:
        data = http.get_json(url, ttl="warm")
        print(f"  {url}: {'ok' if data else 'fail'}")

    # 4. stats
    print("\n=== test 4: stats ===")
    for row in http.stats()["hosts"]:
        print(f"  {row['host']:30s}  calls={row['calls']} cached={row['cached']} "
              f"errors={row['errors']} avg={row['avg_latency_ms']}ms")
