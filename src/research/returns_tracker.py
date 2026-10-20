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
import math
import statistics
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from ..utils import setup_logger

log = setup_logger("returns_tracker", "INFO")

META_DIR = Path(__file__).resolve().parents[2] / "data" / "meta"
RESEARCH_DIR = Path(__file__).resolve().parents[2] / "data" / "research"

# 真实快照的最低门槛: 合成回填快照只有 22-29 个硬编码大市值币, 真实筛选 ≥ 100
MIN_REAL_SNAPSHOT_COINS = 100
# 快照时间与 now-lookback 的最大容差 (秒); 之前是 3 天, 会错配到隔壁日期
# 运维注意: 快照约 22:xx UTC 落盘, 周日记分卡在 screen 之后 (~20-22 UTC) 跑;
# 若整整一周前的快照缺失, 前一天的差 ~23-24h (临界)、后一天的差 ~25h (拒绝),
# 偶尔会出现 ok:False 的周 — 这是有意的取舍 (宁可不评, 也不评错日期)。
SNAPSHOT_MATCH_TOLERANCE_SEC = 24 * 3600
# DefiLlama 历史价回退: 返回的数据点时间与入场时间差超过此值视为无效
BTC_FALLBACK_MAX_DRIFT_SEC = 24 * 3600
# 匹配到当前价格的币占 Top-N 的比例低于此值 → 记分卡标记 low_coverage
LOW_COVERAGE_THRESHOLD = 0.7
TOP_N = 30


# ====================================================================
#  收益追踪
# ====================================================================

def _get_json(url: str, params: dict | None = None):
    """走统一 HttpClient: CoinGecko 限速令牌桶 + 429 退避重试 (尊重 Retry-After)。

    ttl=0 不读写缓存 (记分卡要的是当前价)。单独包一层便于测试 monkeypatch。
    """
    from ..http_client import http
    return http.get_json(url, params=params, timeout=30, ttl=0, retries=3)


def _fetch_current_prices(symbols: list[str],
                          pages_missing: list[int] | None = None) -> dict[str, float]:
    """从 CoinGecko 拉取当前价格 (批量, Top 500 = 5 页)。

    返回同时按 coin_id (CoinGecko "id") 和按 symbol (大写) 两种 key 索引的价格表。
    同一 key 以第一次出现 (市值最大) 为准 — 之前后面页里同名 symbol 会覆盖大市值币。

    之前用裸 httpx 且非 200 静默跳过: 周日记分卡紧跟 screen 跑, CoinGecko 配额刚被
    用完, 第 5 页 (rank 401-500) 429 后整页丢失, Top-N 的小市值端全变 unmatched 却
    无任何日志。现在经 HttpClient 退避重试; 仍失败的页号写进 pages_missing (可选出参)
    并告警, 由 verify_returns 标 low_coverage。
    """
    prices: dict[str, float] = {}
    for page in range(1, 6):
        try:
            data = _get_json(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc",
                        "per_page": 100, "page": page, "sparkline": "false"},
            )
        except Exception as e:
            log.warning(f"CoinGecko page {page} 异常: {e}")
            data = None
        if isinstance(data, list):
            _merge_market_page(prices, data)
        else:
            log.warning(f"CoinGecko page {page}/5 重试后仍失败, 该页 (市值排名 "
                        f"{(page - 1) * 100 + 1}-{page * 100}) 的币无法定价")
            if pages_missing is not None:
                pages_missing.append(page)
        # CoinGecko free API: ~10 req/min, 保守等 7s
        if page < 5:
            time.sleep(7)
    return prices


def _merge_market_page(prices: dict[str, float], rows: list[dict]) -> None:
    """把一页 /coins/markets 结果合并进 prices (coin_id + symbol 两种 key, 先到先得)。"""
    for c in rows:
        price = c.get("current_price")
        if not price or price <= 0:
            continue
        cid = c.get("id") or ""
        sym = (c.get("symbol") or "").upper()
        if cid:
            prices.setdefault(cid, price)
        if sym:
            prices.setdefault(sym, price)


def _lookup_price(row: dict, prices: dict[str, float]) -> float | None:
    """快照行 → 当前价格: 有 coin_id 只认 coin_id (查不到算 unmatched), 无 coin_id 才按 symbol。

    有 coin_id 时不能回退 symbol: velodrome-finance 掉出 Top 500 (或第 5 页没拉到)
    时会拿到同名 velo 的价格, 记成 -88% 的假收益。无 coin_id 的旧快照行仍按 symbol,
    同名重复行由 _ambiguous_rows 把关。
    """
    cid = row.get("coin_id")
    if cid:
        return prices.get(cid) or None
    sym = (row.get("symbol") or "").upper()
    if sym and prices.get(sym):
        return prices[sym]
    return None


def _ambiguous_rows(coins: list[dict]) -> set[int]:
    """同一快照里 symbol 重复、且自身无 coin_id 的行索引 (不能按 symbol 安全定价)。

    旧快照不带 coin_id, 两个同名币 (如两个 VELO) 回退 symbol 都会拿到 CoinGecko 里
    市值最大那个的价格, 小的那个收益就错了。这里只让组内市值最大的一行按 symbol 定价
    (与 CoinGecko 先到先得一致), 其余行视为 unmatched; 市值都缺则整组都不定价。
    """
    by_sym: dict[str, list[int]] = {}
    for i, c in enumerate(coins):
        sym = (c.get("symbol") or "").upper()
        if sym:
            by_sym.setdefault(sym, []).append(i)
    out: set[int] = set()
    for idxs in by_sym.values():
        if len(idxs) < 2:
            continue
        caps = {i: coins[i].get("market_cap") or 0 for i in idxs}
        keep = max(idxs, key=caps.get) if any(v > 0 for v in caps.values()) else None
        out.update(i for i in idxs if i != keep and not coins[i].get("coin_id"))
    return out


def _finite_or_none(v) -> float | None:
    """NaN/Infinity → None, 保证结果 JSON 不含 NaN (旧快照 json 里可能带 NaN token)。"""
    try:
        return float(v) if math.isfinite(float(v)) else None
    except (TypeError, ValueError):
        return None


def _fetch_btc_price_at(entry_ts: datetime) -> float | None:
    """BTC 入场价回退: DefiLlama /prices/historical (云端可达的 CORE_SOURCE)。

    旧 save_snapshot 只存 composite 前 200 名, 2026-09-28 之后的真实快照全都没有 BTC 行,
    只靠快照行取入场价会让后续每周记分卡的 vs_btc 永远是 None。
    返回的数据点时间若与入场时间差超过 BTC_FALLBACK_MAX_DRIFT_SEC → 视为无效。
    """
    try:
        from ..adapters import defillama_full as dlf
        data = dlf.historical_prices(int(entry_ts.timestamp()), ["coingecko:bitcoin"])
        row = ((data or {}).get("coins") or {}).get("coingecko:bitcoin") or {}
        price = row.get("price")
        if not price or price <= 0:
            return None
        ts = row.get("timestamp")
        if ts and abs(float(ts) - entry_ts.timestamp()) > BTC_FALLBACK_MAX_DRIFT_SEC:
            log.warning(f"DefiLlama BTC 历史价时间偏差过大 ({ts} vs {entry_ts.isoformat()}), 弃用")
            return None
        return float(price)
    except Exception as e:
        log.warning(f"DefiLlama BTC 历史价回退失败: {e}")
        return None


def _btc_entry_price(old_coins: list[dict], entry_ts: datetime | None) -> tuple[float | None, str | None]:
    """BTC 入场价: 先取快照任意一行 (coin_id=bitcoin / symbol=BTC), 否则 DefiLlama 回退。

    返回 (price, source); source ∈ {"snapshot", "defillama", None}。
    """
    for c in old_coins:
        if c.get("coin_id") == "bitcoin" or (c.get("symbol") or "").upper() == "BTC":
            if (c.get("price") or 0) > 0:
                return c["price"], "snapshot"
    if entry_ts is not None:
        price = _fetch_btc_price_at(entry_ts)
        if price:
            return price, "defillama"
    return None, None


def _is_real_snapshot(path: Path, data: dict) -> bool:
    """判断快照是否为真实筛选结果 (而非 backfill 合成)。

    合成快照特征: 文件名 *_0000.json / _meta.synthetic / _meta.source=backfill /
    只有 22-29 个币 / 前 50 行的 f_* 因子值含 null (只算了 3 个动量因子)。
    """
    if path.name.endswith("_0000.json"):
        return False
    meta = data.get("_meta") or {}
    if meta.get("synthetic") or meta.get("source") == "backfill":
        return False
    coins = data.get("coins") or []
    if len(coins) < MIN_REAL_SNAPSHOT_COINS:
        return False
    for c in coins[:50]:
        for k, v in c.items():
            if k.startswith("f_") and v is None:
                return False
    return True


def _find_real_snapshot(target_date: datetime) -> tuple[Path | None, dict | None, float]:
    """在 META_DIR 里找离 target_date 最近的 **真实** 快照。返回 (path, data, 时间差秒)。"""
    best_path = None
    best_snap = None
    best_diff = float("inf")
    for sp in sorted(META_DIR.glob("snapshot_*.json")):
        try:
            data = json.loads(sp.read_text())
            if not _is_real_snapshot(sp, data):
                continue
            snap_date = datetime.fromisoformat(data["timestamp"])
            if snap_date.tzinfo is None:
                snap_date = snap_date.replace(tzinfo=timezone.utc)
            diff = abs((snap_date - target_date).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_snap = data
                best_path = sp
        except Exception:
            continue
    return best_path, best_snap, best_diff


def verify_returns(lookback_days: int = 7) -> dict:
    """对比历史快照价格 vs 当前价格, 计算 Top-N 实际收益。

    只会选真实快照 (合成回填快照被排除), 快照时间与 now-lookback 容差 24h。

    返回: {ok, snapshot_date, snapshot_file, entry_ts, graded, matched_coins,
           coverage, low_coverage, pages_missing, unmatched, stats, coins,
           vs_btc, btc_entry_missing, btc_entry_source,
           vs_universe_ew, vs_universe_median, universe_n}
    """
    META_DIR.mkdir(parents=True, exist_ok=True)
    target_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    # 找最近的真实快照
    if not list(META_DIR.glob("snapshot_*.json")):
        return {"ok": False, "error": "无历史快照 — 请先运行 screen 积累数据"}

    snap_path, best_snap, best_diff = _find_real_snapshot(target_date)
    if not best_snap:
        return {"ok": False,
                "error": "无真实快照 (只有合成/回填快照) — 请先运行 screen 积累数据"}
    if best_diff > SNAPSHOT_MATCH_TOLERANCE_SEC:
        return {"ok": False,
                "error": (f"未找到 {lookback_days} 天前的真实快照 (容差 24h, "
                          f"最近的是 {best_snap.get('date')} 差 {best_diff / 3600:.1f}h)")}

    # 拉取当前价格
    log.info(f"拉取当前价格 (对比 {best_snap.get('date', 'N/A')} 的快照 {snap_path.name})...")
    old_coins = best_snap.get("coins", [])
    symbols = [c["symbol"] for c in old_coins[:50]]
    pages_missing: list[int] = []
    current_prices = _fetch_current_prices(symbols, pages_missing=pages_missing)

    if not current_prices:
        return {"ok": False, "error": "无法获取当前价格"}

    # 计算 Top-N 收益; 匹配不到当前价格的币记入 unmatched 而不是静默丢弃
    # 同名 symbol 且无 coin_id 的重复行 (旧快照) 不能按 symbol 定价 → 也算 unmatched
    ambiguous = _ambiguous_rows(old_coins)
    coin_returns = []
    unmatched = []
    graded = old_coins[:TOP_N]
    for i, c in enumerate(graded):
        sym = c["symbol"]
        old_price = c.get("price") or 0
        new_price = None if i in ambiguous else _lookup_price(c, current_prices)
        if old_price > 0 and new_price:
            ret = (new_price - old_price) / old_price
            coin_returns.append({
                "symbol": sym,
                "coin_id": c.get("coin_id"),
                "old_price": old_price,
                "new_price": new_price,
                "return_pct": round(ret, 4),
                "composite_score": _finite_or_none(c.get("composite_score", 0)),
            })
        else:
            unmatched.append(sym)

    if not coin_returns:
        return {"ok": False, "error": "无匹配的代币价格数据"}

    coin_returns.sort(key=lambda x: x["return_pct"], reverse=True)

    # 统计
    returns = [c["return_pct"] for c in coin_returns]
    avg_ret = sum(returns) / len(returns)
    median_ret = statistics.median(returns)
    win_rate = sum(1 for r in returns if r > 0) / len(returns)
    best = coin_returns[0]
    worst = coin_returns[-1]
    coverage = len(coin_returns) / len(graded) if graded else 0.0

    # BTC 基准: 独立定价, 不依赖 BTC 是否在 Top-N 里 (甚至不依赖 BTC 是否在快照里)
    entry_ts = None
    try:
        entry_ts = datetime.fromisoformat(best_snap["timestamp"])
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    btc_old, btc_entry_source = _btc_entry_price(old_coins, entry_ts)
    btc_new = current_prices.get("bitcoin") or current_prices.get("BTC")
    vs_btc = None
    btc_entry_missing = btc_old is None
    if btc_old and btc_new:
        btc_ret = (btc_new - btc_old) / btc_old
        vs_btc = avg_ret - btc_ret

    # 全池等权基准: 快照里所有能定价的币的平均/中位收益 (Top-N 真正要跑赢的对象)
    universe_rets = []
    for i, c in enumerate(old_coins):
        old_price = c.get("price") or 0
        new_price = None if i in ambiguous else _lookup_price(c, current_prices)
        if old_price > 0 and new_price:
            universe_rets.append((new_price - old_price) / old_price)
    vs_universe_ew = None
    vs_universe_median = None
    if universe_rets:
        vs_universe_ew = avg_ret - sum(universe_rets) / len(universe_rets)
        vs_universe_median = median_ret - statistics.median(universe_rets)

    result = {
        "ok": True,
        "snapshot_date": best_snap.get("date"),
        "snapshot_file": snap_path.name,
        "entry_ts": best_snap.get("timestamp"),
        "synthetic": False,
        "lookback_days": lookback_days,
        "graded": len(graded),
        "matched_coins": len(coin_returns),
        "coverage": round(coverage, 4),
        # 有页没拉到时小市值端系统性缺失 (Top-N 偏小盘), 覆盖率再高也要标出来
        "low_coverage": coverage < LOW_COVERAGE_THRESHOLD or bool(pages_missing),
        "pages_missing": pages_missing,
        "unmatched": unmatched,
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
        "btc_entry_missing": btc_entry_missing,
        "btc_entry_source": btc_entry_source,
        "vs_universe_ew": round(vs_universe_ew, 4) if vs_universe_ew is not None else None,
        "vs_universe_median": (round(vs_universe_median, 4)
                               if vs_universe_median is not None else None),
        "universe_n": len(universe_rets),
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


def _build_scorecard(r: dict) -> tuple:
    """从 verify_returns 结果构造飞书卡片 (title, lines, color)。纯函数, 可单测。

    合成快照 (backfill) 的"收益"是 3 个动量因子对大市值币的回放, 不是实盘 Top-N,
    绝不能当记分卡推出去 → 直接拒绝。
    """
    if r.get("synthetic"):
        raise ValueError(f"拒绝展示合成快照的记分卡: {r.get('snapshot_file') or r.get('snapshot_date')}")
    s = r.get("stats", {})
    avg = s.get("avg_return", 0) or 0
    color = "green" if avg > 0 else "red"
    arrow = "📈" if avg > 0 else "📉"

    def pct(x):
        return f"{(x or 0) * 100:+.2f}%"

    matched = r.get("matched_coins")
    graded = r.get("graded") or matched
    coverage = r.get("coverage")
    cov_txt = f" (覆盖 {coverage * 100:.0f}%)" if coverage is not None else ""
    lines = [
        f"**复盘窗口**: {r.get('snapshot_date')} → 至今 (~{r.get('lookback_days', 7)}天) · "
        f"匹配 {matched}/{graded} 币{cov_txt}",
    ]
    if r.get("pages_missing"):
        pages = ", ".join(str(p) for p in r["pages_missing"])
        lines.append(f"⚠️ CoinGecko 第 {pages} 页 (每页 100 币, 按市值) 拉取失败: "
                     f"小市值端未定价, 结果偏向大市值")
    if r.get("low_coverage"):
        lines.append(f"⚠️ 覆盖率不足, 未定价: {', '.join(r.get('unmatched') or [])[:120]}")
    lines += [
        "",
        f"{arrow} 平均收益 **{pct(avg)}** · 中位 {pct(s.get('median_return'))} · "
        f"胜率 **{(s.get('win_rate', 0) or 0) * 100:.0f}%**",
    ]
    if r.get("vs_btc") is not None:
        vb = r["vs_btc"]
        src_note = " (入场价: DefiLlama)" if r.get("btc_entry_source") == "defillama" else ""
        lines.append(f"{'🟢' if vb > 0 else '🔴'} vs BTC 超额: **{pct(vb)}**{src_note}")
    elif r.get("btc_entry_missing"):
        lines.append("⚪ vs BTC: 快照缺 BTC 入场价且历史价回退失败, 无法计算")
    else:
        lines.append("⚪ vs BTC: 无法获取当前 BTC 价, 无法计算")
    if r.get("vs_universe_ew") is not None:
        vu = r["vs_universe_ew"]
        lines.append(
            f"{'🟢' if vu > 0 else '🔴'} vs 全池等权 (n={r.get('universe_n')}): **{pct(vu)}** · "
            f"vs 全池中位: {pct(r.get('vs_universe_median'))}"
        )
    coins = r.get("coins", [])
    if coins:
        lines.append("")
        lines.append("**🏆 表现最好**")
        for c in coins[:3]:
            lines.append(f"🟢 {c['symbol']} {pct(c.get('return_pct'))}")
        lines.append("**💧 表现最差**")
        for c in coins[-3:][::-1]:
            lines.append(f"🔴 {c['symbol']} {pct(c.get('return_pct'))}")
    title = f"📋 每周复盘记分卡 · 近{r.get('lookback_days', 7)}天 Top-N 实盘"
    return title, lines, color


def push_returns_scorecard(lookback_days: int = 7) -> dict:
    """每周复盘记分卡: verify_returns → 飞书卡片。纯 quant, 零外部账号。"""
    r = verify_returns(lookback_days=lookback_days)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error"), "pushed": 0}
    try:
        title, lines, color = _build_scorecard(r)
    except ValueError as e:  # 合成快照守卫
        log.warning(str(e))
        return {"ok": False, "error": str(e), "pushed": 0}
    from ..notifier import push_alert
    push = push_alert(title, lines, color=color)
    return {"ok": push.get("ok", False), "pushed": push.get("pushed", 0),
            "stats": r.get("stats"), "matched": r.get("matched_coins"),
            "error": push.get("error")}


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
