"""Farside Investors BTC 现货 ETF 净流入 (免费, HTML 表格抓取)。

注意: Farside 是网页表格, 结构偶尔会变, 此处兼容多种列布局。
若 fetch 失败 (网络 / 结构变化), 返回空列表并打 warning。
"""
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from ..config import CFG
from ..utils import http_get_text, setup_logger

log = setup_logger("farside_etf", "WARNING")


def _parse_money(x: str) -> float | None:
    """Farside 表格里 '--' / '(123.4)' / '123.4' 都有,需要统一。"""
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


def fetch() -> list[dict]:
    if not CFG["sources"]["farside_etf"]["enabled"]:
        return []
    url = CFG["sources"]["farside_etf"]["url"]
    try:
        html = http_get_text(url)
    except Exception as e:
        log.warning("Farside fetch failed (网络问题或站点变化): %s", e)
        return []

    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if not tables:
        log.warning("Farside 页面没找到 table 元素, 站点结构可能已变化")
        return []

    rows_out = []
    seen_dates = set()
    for table in tables:
        body_rows = table.find_all("tr")
        for tr in body_rows:
            tds = tr.find_all("td")
            if not tds or len(tds) < 2:
                continue
            first = tds[0].get_text(strip=True)
            # 接受多种日期格式
            dt = None
            for fmt in ("%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"):
                try:
                    dt = datetime.strptime(first, fmt).replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    continue
            if dt is None:
                continue
            if dt in seen_dates:
                continue
            seen_dates.add(dt)
            # 最后一列通常是 Total
            total_txt = tds[-1].get_text(strip=True)
            total = _parse_money(total_txt)
            if total is None:
                continue
            ts = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            rows_out.append({
                "ts": ts, "source": "farside_etf", "asset_id": "bitcoin",
                "metric": "etf_net_flow_musd",
                "value": total, "value_text": None,
            })

    if not rows_out:
        log.warning("Farside 解析结果为 0 行, 站点结构可能已变化, 请人工核查 %s", url)
    return rows_out
