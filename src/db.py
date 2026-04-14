"""SQLite 数据层: raw_metrics / factors / signals / reviews / events。"""
from __future__ import annotations
import sqlite3
from contextlib import contextmanager
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
