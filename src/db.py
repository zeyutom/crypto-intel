"""SQLite 数据层: raw_metrics / factors / signals / reviews / events。"""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
import pandas as pd
from .config import CFG, ROOT

DB_PATH = ROOT / CFG["output"]["db_path"]
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
-- 原始数据: 所有源的 K/V 时序
CREATE TABLE IF NOT EXISTS raw_metrics (
    ts TEXT NOT NULL,            -- ISO8601 UTC
    source TEXT NOT NULL,        -- coingecko / binance / ...
    asset_id TEXT,               -- bitcoin / ethereum / ... (nullable: 全局指标)
    metric TEXT NOT NULL,        -- price_usd / funding_rate / mint_7d / ...
    value REAL,                  -- 数值
    value_text TEXT,             -- 文本(用于新闻标题等)
    PRIMARY KEY (ts, source, asset_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_raw_asset_metric ON raw_metrics(asset_id, metric, ts);

-- 因子层: 计算后的标准化数值
CREATE TABLE IF NOT EXISTS factors (
    ts TEXT NOT NULL,
    asset_id TEXT,
    factor TEXT NOT NULL,
    raw_value REAL,              -- 原始值
    zscore REAL,                 -- 截面/时序 z
    signal INTEGER,              -- -1 / 0 / 1
    confidence REAL,             -- 0-1
    meta TEXT,                   -- JSON: 计算细节
    PRIMARY KEY (ts, asset_id, factor)
);
CREATE INDEX IF NOT EXISTS idx_factors_ts ON factors(ts);

-- 信号层: 多因子合成
CREATE TABLE IF NOT EXISTS signals (
    ts TEXT NOT NULL,
    asset_id TEXT,
    composite REAL,
    direction TEXT,              -- BULL / BEAR / NEUTRAL
    confidence REAL,
    regime TEXT,                 -- BULL / BEAR / CHOP / CRISIS
    factor_breakdown TEXT,       -- JSON
    PRIMARY KEY (ts, asset_id)
);

-- 复核层: 各种健康度指标
CREATE TABLE IF NOT EXISTS reviews (
    ts TEXT NOT NULL,
    check_name TEXT NOT NULL,    -- price_cross / ic_monitor / drift / ...
    subject TEXT,                -- asset / factor / source
    severity TEXT,               -- OK / WARN / ALERT
    detail TEXT,                 -- JSON
    PRIMARY KEY (ts, check_name, subject)
);

-- 事件层: 新闻/解锁/ETF等
CREATE TABLE IF NOT EXISTS events (
    ts TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    asset_id TEXT,
    title TEXT,
    url TEXT,
    meta TEXT,
    PRIMARY KEY (ts, source, title)
);

-- ============ v0.5 自我进化层 ============

-- 因子快照: 每日 snapshot, 为 IC/IR 回测提供历史基准
-- 记录: 当日每个因子的 raw_value + signal + 对应 asset 的 current_price
CREATE TABLE IF NOT EXISTS factor_snapshots (
    date TEXT NOT NULL,          -- YYYY-MM-DD (UTC)
    factor TEXT NOT NULL,
    asset_id TEXT,
    raw_value REAL,
    signal INTEGER,
    current_price REAL,          -- 快照时的价格 (用于回算未来收益)
    meta TEXT,
    PRIMARY KEY (date, factor, asset_id)
);
CREATE INDEX IF NOT EXISTS idx_snap_factor ON factor_snapshots(factor, date);

-- 因子有效性: 滚动 IC/IR 计算结果 (每日重算)
CREATE TABLE IF NOT EXISTS factor_performance (
    date TEXT NOT NULL,           -- 计算日期
    factor TEXT NOT NULL,
    asset_id TEXT,
    window_days INTEGER,          -- 回看窗口 (30/90/180)
    forward_days INTEGER,         -- 前瞻收益窗口 (1/7/30)
    ic REAL,                      -- Information Coefficient
    ir REAL,                      -- Information Ratio
    n_obs INTEGER,                -- 有效观测数
    mean_return REAL,             -- 信号=1 时的平均未来收益
    PRIMARY KEY (date, factor, asset_id, window_days, forward_days)
);
CREATE INDEX IF NOT EXISTS idx_perf_factor ON factor_performance(factor, date);

-- PM 反馈: 仪表盘上 👍/👎/评论
CREATE TABLE IF NOT EXISTS brief_feedback (
    ts TEXT NOT NULL,             -- 反馈时间
    brief_ts TEXT NOT NULL,       -- 对应哪一条 briefing
    rating INTEGER,               -- 1 = 👍, -1 = 👎, 0 = 中性
    comment TEXT,                 -- 自由文本
    source TEXT,                  -- streamlit / feishu / manual
    PRIMARY KEY (ts, brief_ts)
);

-- 自我进化日志: 周复盘 / 因子提议 / 源发现 / prompt 迭代的记录
CREATE TABLE IF NOT EXISTS evolution_log (
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,           -- weekly_review / factor_proposal / source_discovery / prompt_evolve
    title TEXT,
    content TEXT,                 -- Markdown
    action TEXT,                  -- accepted / rejected / pending
    meta TEXT,                    -- JSON
    PRIMARY KEY (ts, kind, title)
);
CREATE INDEX IF NOT EXISTS idx_evo_kind ON evolution_log(kind, ts);

-- 活跃 narrative 追踪: 由 Claude 维护, 仪表盘可视化
CREATE TABLE IF NOT EXISTS narratives (
    date TEXT NOT NULL,           -- 观测日期
    narrative TEXT NOT NULL,      -- AI / RWA / Memecoin / DePIN / ...
    heat_score REAL,              -- 0-100, 热度分
    top_tokens TEXT,              -- JSON array of representative tokens
    trigger_events TEXT,          -- 最近的助推事件 JSON
    delta_7d REAL,                -- 近 7 日热度变化
    PRIMARY KEY (date, narrative)
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as c:
        c.executescript(SCHEMA)


def upsert_raw(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO raw_metrics
               (ts, source, asset_id, metric, value, value_text)
               VALUES (:ts, :source, :asset_id, :metric, :value, :value_text)""",
            [{"ts": r["ts"], "source": r["source"], "asset_id": r.get("asset_id"),
              "metric": r["metric"], "value": r.get("value"),
              "value_text": r.get("value_text")} for r in rows],
        )
    return len(rows)


def upsert_factor(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO factors
               (ts, asset_id, factor, raw_value, zscore, signal, confidence, meta)
               VALUES (:ts, :asset_id, :factor, :raw_value, :zscore, :signal, :confidence, :meta)""",
            rows,
        )
    return len(rows)


def upsert_review(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO reviews
               (ts, check_name, subject, severity, detail)
               VALUES (:ts, :check_name, :subject, :severity, :detail)""",
            rows,
        )
    return len(rows)


def upsert_signal(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO signals
               (ts, asset_id, composite, direction, confidence, regime, factor_breakdown)
               VALUES (:ts, :asset_id, :composite, :direction, :confidence, :regime, :factor_breakdown)""",
            rows,
        )
    return len(rows)


def upsert_event(rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO events
               (ts, source, event_type, asset_id, title, url, meta)
               VALUES (:ts, :source, :event_type, :asset_id, :title, :url, :meta)""",
            rows,
        )
    return len(rows)


def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    with get_conn() as c:
        return pd.read_sql_query(sql, c, params=params)


def latest_factors() -> pd.DataFrame:
    """拉取每个 (asset_id, factor) 最近一条记录。"""
    return query_df(
        """SELECT f.*
           FROM factors f
           JOIN (SELECT asset_id AS a_, factor AS f_, MAX(ts) AS mts
                 FROM factors GROUP BY asset_id, factor) m
           ON (f.asset_id IS m.a_ OR (f.asset_id IS NULL AND m.a_ IS NULL))
           AND f.factor = m.f_ AND f.ts = m.mts"""
    )


def latest_reviews() -> pd.DataFrame:
    return query_df(
        """SELECT r.*
           FROM reviews r
           JOIN (SELECT check_name AS c_, subject AS s_, MAX(ts) AS mts
                 FROM reviews GROUP BY check_name, subject) m
           ON r.check_name = m.c_
           AND (r.subject IS m.s_ OR (r.subject IS NULL AND m.s_ IS NULL))
           AND r.ts = m.mts
           ORDER BY r.ts DESC"""
    )


def recent_events(limit: int = 20) -> pd.DataFrame:
    return query_df(
        "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
    )


# ============ 行情: 每个资产的最新报价 (通知 / 仪表盘 / 日报 / AI 简报 / 快照共用) ============

# 价格源优先级, 只在同一批采集 (QUOTE_FRESH_WINDOW_HOURS 内) 之间比较
QUOTE_SOURCES = ("binance", "okx", "coingecko")
QUOTE_FRESH_WINDOW_HOURS = 6


def latest_quotes(sources: Iterable[str] = QUOTE_SOURCES,
                  window_hours: float = QUOTE_FRESH_WINDOW_HOURS) -> pd.DataFrame:
    """每个资产一条最新报价: 列 asset_id / price / change_24h / source / ts。

    先看新鲜度、再看源优先级: 只在该资产最新一条 price_usd 的 window_hours 小时内
    按 sources 顺序挑一条; change_24h 取同一 (source, ts, asset) 的 change_24h_pct 行,
    价格和涨跌必然来自同一条报价 (缺则 NaN, 不拿别的源/别的时刻凑)。

    之前各处 SQL 都是 ORDER BY 源优先级在前、ts 在后: 云端 binance 常年 451, 那条
    2026-06-05 的 binance 行压过每天新鲜的 okx/coingecko, 飞书卡片 / 仪表盘 / 日报 /
    Gemini 输入 / factor_snapshots 的 BTC 价冻在 $62,810 达 4.5 个月。
    ts 存成 ISO 'YYYY-MM-DDTHH:MM:SSZ' 文本, 截止时间必须用同格式的 strftime 构造再做
    字符串比较 — datetime() 输出 'YYYY-MM-DD HH:MM:SS', ' ' < 'T' 会让截止日当天的
    旧行全部算新鲜 (24h 前的 binance 行照样胜出)。
    """
    sources = tuple(sources)
    if not sources:
        return pd.DataFrame(columns=["asset_id", "price", "change_24h", "source", "ts"])
    placeholders = ",".join("?" * len(sources))
    rank_case = " ".join(f"WHEN ? THEN {i}" for i in range(len(sources)))
    sql = f"""
        WITH p AS (
            SELECT asset_id, source, ts, value,
                   strftime('%Y-%m-%dT%H:%M:%SZ',
                            MAX(ts) OVER (PARTITION BY asset_id), ?) AS cutoff
            FROM raw_metrics
            WHERE metric = 'price_usd' AND source IN ({placeholders})
              AND value IS NOT NULL
        ), ranked AS (
            SELECT asset_id, source, ts, value AS price,
                   ROW_NUMBER() OVER (PARTITION BY asset_id
                       ORDER BY CASE source {rank_case} ELSE {len(sources)} END,
                                ts DESC) AS rn
            FROM p
            -- cutoff 解析失败 (NULL) 时不过滤, 退回纯优先级
            WHERE cutoff IS NULL OR ts >= cutoff
        )
        SELECT r.asset_id, r.price, c.value AS change_24h, r.source, r.ts
        FROM ranked r
        LEFT JOIN raw_metrics c
          ON c.metric = 'change_24h_pct' AND c.source = r.source
         AND c.ts = r.ts AND c.asset_id = r.asset_id
        WHERE r.rn = 1
        ORDER BY r.asset_id
    """
    params = (f"-{float(window_hours)} hours", *sources, *sources)
    return query_df(sql, params)


# ============ 数据源新鲜度 (日报 / 仪表盘 "数据源覆盖度") ============

# 期望出现的源 (日报模板表头写的是 "/6", 增删需同步 src/report/template.html)
EXPECTED_SOURCES = ("coingecko", "binance", "coinbase", "defillama", "feargreed", "farside_etf")
# 源最新 ts 距全库最新一次采集超过这么久 → 视为过期 (本次没采到)
SOURCE_MAX_AGE_HOURS_DEFAULT = 26
SOURCE_MAX_AGE_HOURS = {
    "yfinance_macro": 96,   # 周末 / 假期没有新 K 线
    "farside_etf": 96,      # 只有交易日数据, 周一早上最新一条是上周五
    "feargreed": 48,        # 日频, ts 打在当天 00:00Z
}


def _parse_ts(ts) -> datetime | None:
    """raw_metrics.ts (ISO, 'Z' 或 +00:00 结尾) → aware datetime; 解析失败 None。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def source_status(expected: Iterable[str] = EXPECTED_SOURCES) -> list[dict]:
    """各期望源的状态 (保持 expected 顺序): [{source, ok, stale, n, last_ts}]。

    ok 表示 "新鲜": 该源最新 ts 距全库最新 ts (最近一次采集) 不超过
    SOURCE_MAX_AGE_HOURS (默认 26h)。有数据但过期 → ok=False, stale=True,
    last_ts 显示为 "⚠ 过期 (YYYY-MM-DD HH:MM)"; 从未采到 → ok=False, last_ts "—"。

    之前只要 raw_metrics 里出现过就算 ok: 云端 binance 常年 451, 日报却一直写着
    "binance ✔ 采到 16 行 2026-06-05 05:45", 覆盖度警告永远不触发。
    """
    df = query_df(
        """SELECT source, COUNT(*) AS n, MAX(ts) AS last_ts FROM raw_metrics
           WHERE source != '_meta' GROUP BY source"""
    )
    stats = {r.source: (int(r.n), r.last_ts) for r in df.itertuples(index=False)}
    parsed = [_parse_ts(last) for _, last in stats.values()]
    ref = max((t for t in parsed if t is not None), default=None)
    out = []
    for s in expected:
        if s not in stats:
            out.append({"source": s, "ok": False, "stale": False, "n": 0, "last_ts": "—"})
            continue
        n, last = stats[s]
        last_dt = _parse_ts(last)
        max_age = timedelta(hours=SOURCE_MAX_AGE_HOURS.get(s, SOURCE_MAX_AGE_HOURS_DEFAULT))
        fresh = last_dt is not None and ref is not None and ref - last_dt <= max_age
        label = str(last or "")[:16].replace("T", " ") or "—"
        out.append({"source": s, "ok": fresh, "stale": not fresh, "n": n,
                    "last_ts": label if fresh else f"⚠ 过期 ({label})"})
    return out


# ============ v0.5 快照/反馈/进化日志 写入 ============

def upsert_factor_snapshot(rows) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO factor_snapshots
               (date, factor, asset_id, raw_value, signal, current_price, meta)
               VALUES (:date, :factor, :asset_id, :raw_value, :signal, :current_price, :meta)""",
            rows,
        )
    return len(rows)


def upsert_factor_performance(rows) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO factor_performance
               (date, factor, asset_id, window_days, forward_days, ic, ir, n_obs, mean_return)
               VALUES (:date, :factor, :asset_id, :window_days, :forward_days,
                       :ic, :ir, :n_obs, :mean_return)""",
            rows,
        )
    return len(rows)


def insert_feedback(brief_ts: str, rating: int, comment: str,
                    source: str = "streamlit") -> None:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO brief_feedback
               (ts, brief_ts, rating, comment, source)
               VALUES (?, ?, ?, ?, ?)""",
            (ts, brief_ts, int(rating), comment or "", source),
        )


def insert_evolution_log(kind: str, title: str, content: str,
                         action: str = "pending", meta: str = "{}") -> None:
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO evolution_log
               (ts, kind, title, content, action, meta)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ts, kind, title, content, action, meta),
        )


def upsert_narratives(rows) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with get_conn() as c:
        c.executemany(
            """INSERT OR REPLACE INTO narratives
               (date, narrative, heat_score, top_tokens, trigger_events, delta_7d)
               VALUES (:date, :narrative, :heat_score, :top_tokens, :trigger_events, :delta_7d)""",
            rows,
        )
    return len(rows)
