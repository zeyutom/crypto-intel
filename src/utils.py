"""通用工具: HTTP/日志/时间。"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Any
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from rich.logging import RichHandler


def setup_logger(name: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    logger.addHandler(RichHandler(rich_tracebacks=True, show_path=False))
    logger.propagate = False
    return logger


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def http_get_json(url: str, params: dict | None = None, headers: dict | None = None,
                  timeout: float = 20.0, ttl=None) -> Any:
    """Forward 到统一 HttpClient (v0.9 W2-S3).

    保留旧签名以兼容现有调用方; 内部走 token-bucket + 双层缓存.
    失败时抛 RuntimeError (旧版抛 HTTPError, 行为基本对齐).
    """
    from .http_client import http
    data = http.get_json(url, params=params, headers=headers,
                         timeout=timeout, ttl=ttl)
    if data is None:
        raise RuntimeError(f"http_get_json failed: {url}")
    return data


def http_get_text(url: str, params: dict | None = None, headers: dict | None = None,
                  timeout: float = 20.0, ttl=None) -> str:
    """Forward 到统一 HttpClient (text 模式)."""
    from .http_client import http
    h = {"User-Agent": "Mozilla/5.0 CryptoIntel/0.9 (+research)"}
    if headers:
        h.update(headers)
    data = http.get_text(url, params=params, headers=h, timeout=timeout, ttl=ttl)
    if data is None:
        raise RuntimeError(f"http_get_text failed: {url}")
    return data
