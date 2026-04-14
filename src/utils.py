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


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10),
       retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)))
def http_get_json(url: str, params: dict | None = None, headers: dict | None = None,
                  timeout: float = 20.0) -> Any:
    h = {"User-Agent": "CryptoIntel/0.1 (+research)"}
    if headers:
        h.update(headers)
    r = httpx.get(url, params=params, headers=h, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.json()


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10),
       retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)))
def http_get_text(url: str, params: dict | None = None, headers: dict | None = None,
                  timeout: float = 20.0) -> str:
    h = {"User-Agent": "Mozilla/5.0 CryptoIntel/0.1 (+research)"}
    if headers:
        h.update(headers)
    r = httpx.get(url, params=params, headers=h, timeout=timeout, follow_redirects=True)
    r.raise_for_status()
    return r.text
