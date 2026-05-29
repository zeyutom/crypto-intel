"""收益追踪 + 筛选结果飞书推送。

功能:
  1. verify_returns(lookback_days) — 读取历史快照, 对比当前价格, 算出 Top-N 实际收益
  2. push_screen_to_feishu(result) — 把筛选结果摘要推送到飞书群
"""
from __future__ import annotations
import json
import os
import hmac
import base64
import hashlib
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("returns_tracker", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"
RESEARCH_DIR = Path(__file__).resolve().parents[2] / "data" / "research"


# ====================================================================
#  收益追踪
# ====================================================================

def _fetch_current_prices(symbols: list[str]) -> dict[str, float]:
    """从 CoinGecko 拉取当前价格 (批量, 用 symbol→id 映射)。"""
    import httpx
    # 先用 /coins/markets 拉 top 500 (含 symbol → price)
    prices = {}
    for page in range(1, 6):
        try:
            r = httpx.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 100, "page": page, "sparkline": "false"},
                headers={"User-Agent": "CryptoIntel/2.0", "Accept": "application/json"},
                timeout=30, follow_redirects=True,
            )
            if r.status_code == 200:
                for c in r.json():
                    sym = (c.get("symbol") or "").upper()
                    price = c.get("current_price")
                    if sym and price:
                        prices[sym] = price
            if page < 5:
                time.sleep(7)
        except Exception as e:
            log.warning(f"CoinGecko page {page} 失败: {e}")
            time.sleep(5)
    return prices


def verify_returns(lookback_days: int = 7) -> dict:
    """对比历史快照价格 vs 当前价格, 计算 Top-N 实际收益。

    返回: {ok, snapshot_date, matched_coins, stats, coins, vs_btc}
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    target_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # 找最近的快照
    snapshots = sorted(META_DIR.glob("snapshot_*.json"))
    if not snapshots:
        return {"ok": False, "error": "无历史快照 — 请先运行 screen 积累数据"}

    best_snap = None
    best_diff = float("inf")
    for sp in snapshots:
        try:
            data = json.loads(sp.read_text())
            snap_date = datetime.fromisoformat(data["timestamp"])
            diff = abs((snap_date - target_date).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_snap = data
        except Exception:
            continue

    if not best_snap or best_diff > 3 * 86400:
        return {"ok": False, "error": f"未找到 {lookback_days} 天前的快照 (容差 3 天)"}

    # 拉取当前价格
    log.info(f"拉取当前价格 (对比 {best_snap.get('date', 'N/A')} 的快照)...")
    old_coins = best_snap.get("coins", [])
    symbols = [c["symbol"] for c in old_coins[:50]]
    current_prices = _fetch_current_prices(symbols)

    if not current_prices:
        return {"ok": False, "error": "无法获取当前价格"}

    # 计算收益
    coin_returns = []
    btc_old = None
    btc_new = None
    for c in old_coins[:30]:  # Top 30
        sym = c["symbol"]
        old_price = c.get("price", 0)
        new_price = current_prices.get(sym, 0)
        if old_price > 0 and new_price > 0:
            ret = (new_price - old_price) / old_price
            coin_returns.append({
                "symbol": sym,
                "old_price": old_price,
                "new_price": new_price,
                "return_pct": round(ret, 4),
                "composite_score": c.get("composite_score", 0),
            })
            if sym == "BTC":
                btc_old = old_price
                btc_new = new_price

    if not coin_returns:
        return {"ok": False, "error": "无匹配的代币价格数据"}

    coin_returns.sort(key=lambda x: x["return_pct"], reverse=True)

    # 统计
    returns = [c["return_pct"] for c in coin_returns]
    avg_ret = sum(returns) / len(returns)
    sorted_rets = sorted(returns)
    median_ret = sorted_rets[len(sorted_rets) // 2]
    win_rate = sum(1 for r in returns if r > 0) / len(returns)
    best = coin_returns[0]
    worst = coin_returns[-1]

    vs_btc = None
    if btc_old and btc_new:
        btc_ret = (btc_new - btc_old) / btc_old
        vs_btc = avg_ret - btc_ret

    result = {
        "ok": True,
        "snapshot_date": best_snap.get("date"),
        "lookback_days": lookback_days,
        "matched_coins": len(coin_returns),
        "stats": {
            "avg_return": round(avg_ret, 4),
            "median_return": round(median_ret, 4),
            "win_rate": round(win_rate, 2),
            "best_coin": best["symbol"],
            "best_return": best["return_pct"],
            "worst_coin": worst["symbol"],
            "worst_return": worst["return_pct"],
        },
        "vs_btc": round(vs_btc, 4) if vs_btc is not None else None,
        "coins": coin_returns,
    }

    # 保存收益追踪结果
    track_path = META_DIR / f"returns_{best_snap.get('date', 'unknown')}_{lookback_days}d.json"
    track_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    log.info(f"收益追踪已保存: {track_path.name}")

    return result


# ====================================================================
#  筛选结果飞书推送
# ====================================================================

def _load_feishu_groups() -> list[dict]:
    """加载飞书群配置 (复用 notifier 的逻辑)。"""
    groups = []
    for i in range(1, 10):
        url = os.environ.get(f"FEISHU_GROUP_{i}_URL", "")
        if not url:
            if i == 1:
                url = os.environ.get("FEISHU_WEBHOOK_URL", "")
            if not url:
                continue
        name = os.environ.get(f"FEISHU_GROUP_{i}_NAME", f"群{i}")
        secret = os.environ.get(f"FEISHU_GROUP_{i}_SECRET", "")
        if not secret and i == 1:
            secret = os.environ.get("FEISHU_SECRET", "")
        groups.append({"name": name, "url": url, "secret": secret})
    return groups


def _sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def push_screen_to_feishu(result: dict) -> dict:
    """把筛选结果推送到飞书群 (Interactive Card)。"""
    import httpx

    groups = _load_feishu_groups()
    if not groups:
        return {"ok": False, "error": "未配置飞书群 (FEISHU_GROUP_N_URL)"}

    regime = result.get("regime", "unknown")
    regime_labels = {
        "bull": "🐂 牛市", "bear": "🐻 熊市",
        "sideways": "➡️ 震荡", "volatile": "🌊 高波动",
        "unknown": "❓ 未知",
    }
    regime_colors = {
        "bull": "green", "bear": "red", "sideways": "yellow",
        "volatile": "purple", "unknown": "grey",
    }

    top = result.get("top", [])[:10]
    anomalies = result.get("anomalies", [])[:5]
    sources = result.get("data_sources", {})
    btc_data = result.get("btc_data") or {}
    bj_now = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")

    # 构建 Top 10 表格
    top_lines = []
    for i, c in enumerate(top, 1):
        chg30 = c.get("change_30d", 0)
        arrow = "📈" if chg30 > 0 else "📉" if chg30 < 0 else "➡️"
        top_lines.append(
            f"{i}. **{c['symbol']}** ${c['price']:,.2f}  "
            f"MCap {c['market_cap']/1e9:.1f}B  "
            f"30d {chg30:+.1f}% {arrow}  "
            f"Score **{c['composite_score']:.4f}**"
        )

    # 异动信号
    anomaly_lines = []
    for a in anomalies:
        emoji = "🔴" if a["severity"] == "high" else "🟡" if a["severity"] == "medium" else "🔵"
        anomaly_lines.append(f"{emoji} **{a['symbol']}** {a['detail']}")

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"📊 多因子 Alpha 筛选 | {regime_labels.get(regime, regime)}"},
                "template": regime_colors.get(regime, "blue"),
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md",
                    "content": (
                        f"**市场状态:** {regime_labels.get(regime, regime)}\n"
                        f"**BTC:** 30d {btc_data.get('change_30d', 0):+.1f}%  "
                        f"7d {btc_data.get('change_7d', 0):+.1f}%\n"
                        f"**数据源:** CoinGecko({sources.get('coingecko', 0)}) · "
                        f"DeFiLlama({sources.get('defillama_protocols', 0)}) · "
                        f"Binance({sources.get('binance_pairs', 0)}+{sources.get('funding_pairs', 0)})\n"
                        f"**有效代币:** {result.get('total_screened', 0)} · "
                        f"**耗时:** {result.get('duration_seconds', 0)}s"
                    )}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md",
                    "content": "**🏆 Top 10 Alpha**\n" + "\n".join(top_lines)}},
            ],
        },
    }

    # 异动信号 (如果有)
    if anomaly_lines:
        card["card"]["elements"].append({"tag": "hr"})
        card["card"]["elements"].append({
            "tag": "div", "text": {"tag": "lark_md",
                "content": f"**⚡ 异动信号 ({len(anomalies)} 条)**\n" + "\n".join(anomaly_lines)}
        })

    # 时间戳
    card["card"]["elements"].append({"tag": "hr"})
    card["card"]["elements"].append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                       "content": f"Crypto Intel v2 · 10因子元学习 · {bj_now} 北京时间"}],
    })

    # 推送
    ok_count = 0
    for g in groups:
        try:
            payload = dict(card)
            if g["secret"]:
                ts = str(int(time.time()))
                payload["timestamp"] = ts
                payload["sign"] = _sign(ts, g["secret"])

            r = httpx.post(g["url"], json=payload, timeout=15)
            resp = r.json()
            if resp.get("code") == 0 or resp.get("StatusCode") == 0:
                log.info(f"  ✓ 飞书推送成功: {g['name']}")
                ok_count += 1
            else:
                log.warning(f"  ✗ 飞书推送失败 ({g['name']}): {resp}")
        except Exception as e:
            log.warning(f"  ✗ 飞书推送异常 ({g['name']}): {e}")

    return {"ok": ok_count > 0, "pushed": ok_count, "total": len(groups)}
