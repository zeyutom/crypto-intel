"""Farside Investors BTC 现货 ETF 净流入 (免费, HTML 表格抓取)。

注意: Farside 用 Cloudflare, 在云端机房的请求经常被 403 拦截。
策略:
  1. 先用浏览器风格的完整 headers 请求 farside.co.uk
  2. 若被拦截, 回退到 CoinGlass 的 ETF 公开页面 (反爬较弱)

最终落地的 metric 名 "etf_net_flow_musd" 不变, 因子层无需修改。
"""
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from ..config import CFG
from ..utils import http_get_text, http_get_json, setup_logger

log = setup_logger("farside_etf", "WARNING")


# 真实 Chrome 浏览器的 headers - 提高 Cloudflare 通过率
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _parse_money(x: str) -> float | None:
    if not x:
        return None
    s = x.strip().replace(",", "").replace("$", "").replace("M", "").replace(" ", "")
    if s in ("-", "--", "", "N/A", "n/a"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
        return -v if neg else v
    except Exception:
        return None


def _try_farside() -> list[dict]:
    """Primary: farside.co.uk"""
    url = CFG["sources"]["farside_etf"]["url"]
    html = http_get_text(url, headers=BROWSER_HEADERS, timeout=30)
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if not tables:
        return []
    rows_out = []
    seen = set()
    for table in tables:
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if not tds or len(tds) < 2:
                continue
            first = tds[0].get_text(strip=True)
            dt = None
            for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    dt = datetime.strptime(first, fmt).replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    continue
            if dt is None or dt in seen:
                continue
            seen.add(dt)
            total = _parse_money(tds[-1].get_text(strip=True))
            if total is None:
                continue
            rows_out.append({
                "ts": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "farside_etf", "asset_id": "bitcoin",
                "metric": "etf_net_flow_musd",
                "value": total, "value_text": None,
            })
    return rows_out


def _try_coinglass() -> list[dict]:
    """Fallback: CoinGlass ETF history (公开 endpoint, 不需 key)。"""
    try:
        data = http_get_json(
            "https://fapi.coinglass.com/api/etf/bitcoin/historicalInflowChart",
            headers={"User-Agent": BROWSER_HEADERS["User-Agent"], "Accept": "application/json"},
            timeout=20,
        )
    except Exception as e:
        log.warning("CoinGlass ETF fallback also failed: %s", e)
        return []

    rows = []
    items = data.get("data", []) if isinstance(data, dict) else []
    for it in items[-30:]:
        ts_ms = it.get("date") or it.get("dateString")
        flow = it.get("netInflow") or it.get("flowsInUsd")
        if ts_ms is None or flow is None:
            continue
        try:
            if isinstance(ts_ms, (int, float)):
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            else:
                dt = datetime.strptime(str(ts_ms)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        rows.append({
            "ts": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "farside_etf",
            "asset_id": "bitcoin",
            "metric": "etf_net_flow_musd",
            "value": float(flow) / 1e6,
            "value_text": None,
        })
    return rows


def fetch() -> list[dict]:
    if not CFG["sources"]["farside_etf"]["enabled"]:
        return []

    try:
        rows = _try_farside()
        if rows:
            log.info("Farside primary OK: %d rows", len(rows))
            return rows
        log.warning("Farside primary returned 0 rows, trying CoinGlass fallback")
    except Exception as e:
        log.warning("Farside primary failed (likely Cloudflare 403): %s", e)

    rows = _try_coinglass()
    if rows:
        log.info("CoinGlass fallback OK: %d rows", len(rows))
    else:
        log.warning("Both Farside and CoinGlass failed; ETF data missing this run")
    return rows
