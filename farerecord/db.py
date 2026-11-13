"""SQLite 建表与连接。用 PRAGMA user_version 做简单迁移。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 9

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE fetch_run (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source      TEXT NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL DEFAULT 'running',   -- running | ok | partial | failed
        error       TEXT,
        n_quotes    INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE oneway_quote (
        run_id      INTEGER NOT NULL REFERENCES fetch_run(id) ON DELETE CASCADE,
        origin      TEXT NOT NULL,
        dest        TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        price       REAL NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (run_id, origin, dest, depart_date)
    );
    CREATE INDEX ix_oneway_route_date ON oneway_quote(origin, dest, depart_date);
    CREATE TABLE rt_quote (
        run_id      INTEGER NOT NULL REFERENCES fetch_run(id) ON DELETE CASCADE,
        origin      TEXT NOT NULL,
        dest        TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        return_date TEXT NOT NULL,
        price       REAL NOT NULL,
        observed_at TEXT NOT NULL,
        PRIMARY KEY (run_id, origin, dest, depart_date, return_date)
    );
    CREATE INDEX ix_rt_route ON rt_quote(origin, dest, depart_date, return_date);
    CREATE TABLE manual_quote (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        origin      TEXT NOT NULL,
        dest        TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        return_date TEXT NOT NULL,
        price       REAL NOT NULL,
        note        TEXT,
        observed_at TEXT NOT NULL
    );
    CREATE TABLE alert_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        rule        TEXT NOT NULL,
        dedupe_key  TEXT NOT NULL,
        price       REAL,
        channel     TEXT NOT NULL,
        status      TEXT NOT NULL,      -- sent | failed | deferred
        message     TEXT,
        sent_at     TEXT NOT NULL
    );
    CREATE INDEX ix_alert_dedupe ON alert_log(rule, dedupe_key);
    """,
    2: """
    ALTER TABLE oneway_quote ADD COLUMN airline TEXT;   -- 出最低价的航司二字码，可为空
    """,
    3: """
    CREATE TABLE price_watch (                 -- 界面里给某个去回组合设的到价提醒
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        origin       TEXT NOT NULL,
        dest         TEXT NOT NULL,
        depart_date  TEXT NOT NULL,
        return_date  TEXT NOT NULL,
        target_price REAL NOT NULL,
        enabled      INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL
    );
    CREATE TABLE setting (                     -- 界面里改的设置，覆盖 config.yaml
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL                    -- JSON
    );
    """,
    4: """
    ALTER TABLE oneway_quote ADD COLUMN airline_price REAL;   -- airline 这家航司当天的最低价
    UPDATE oneway_quote SET airline = NULL WHERE airline = ''; -- 旧版只查精确匹配，重新查参考价
    """,
    5: """
    CREATE TABLE rt_calibration (              -- 每次抓取算出的"往返票 ÷ 两张单程之和"
        run_id  INTEGER NOT NULL REFERENCES fetch_run(id) ON DELETE CASCADE,
        origin  TEXT NOT NULL,
        dest    TEXT NOT NULL,
        ratio   REAL NOT NULL,
        samples INTEGER NOT NULL,
        PRIMARY KEY (run_id, origin, dest)
    );
    CREATE INDEX ix_oneway_run ON oneway_quote(run_id);   -- 按抓取清理旧数据时用
    CREATE INDEX ix_rt_run ON rt_quote(run_id);
    """,
    6: """
    CREATE TABLE real_quote (                  -- 从携程往返订票页上读到的真实往返含税价
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        origin      TEXT NOT NULL,
        dest        TEXT NOT NULL,
        depart_date TEXT NOT NULL,
        return_date TEXT NOT NULL,
        price       REAL NOT NULL,
        airline     TEXT,                      -- 页面上最低价那一行的航司名，可为空
        observed_at TEXT NOT NULL
    );
    CREATE INDEX ix_real_trip ON real_quote(origin, dest, depart_date, return_date, observed_at);
    """,
    7: """
    CREATE TABLE daily_price (                 -- 每天的价格留档，不随旧抓取清理；用于和往年同期对比
        kind          TEXT NOT NULL,           -- oneway：日历单程最低价；real：携程往返实价
        origin        TEXT NOT NULL,
        dest          TEXT NOT NULL,
        depart_date   TEXT NOT NULL,
        return_date   TEXT NOT NULL DEFAULT '',  -- 单程为空串
        observed_date TEXT NOT NULL,           -- 观测日（北京时间 YYYY-MM-DD）
        low           REAL NOT NULL,
        high          REAL NOT NULL,
        n             INTEGER NOT NULL,
        PRIMARY KEY (kind, origin, dest, depart_date, return_date, observed_date)
    );
    INSERT INTO daily_price
        SELECT 'oneway', origin, dest, depart_date, '', date(observed_at, '+8 hours'),
               MIN(price), MAX(price), COUNT(*)
        FROM oneway_quote WHERE date(observed_at, '+8 hours') IS NOT NULL
        GROUP BY origin, dest, depart_date, date(observed_at, '+8 hours');
    INSERT INTO daily_price
        SELECT 'real', origin, dest, depart_date, return_date, date(observed_at, '+8 hours'),
               MIN(price), MAX(price), COUNT(*)
        FROM real_quote WHERE date(observed_at, '+8 hours') IS NOT NULL
        GROUP BY origin, dest, depart_date, return_date, date(observed_at, '+8 hours');
    """,
    8: """
    ALTER TABLE real_quote ADD COLUMN seats_left INTEGER;   -- 页面上最低价的"剩 N 张"，没提示为空
    ALTER TABLE real_quote ADD COLUMN next_price REAL;      -- 页面上的下一档价格
    """,
    9: """
    ALTER TABLE real_quote ADD COLUMN source TEXT NOT NULL DEFAULT 'ctrip';  -- ctrip | ly（同程）
    """,
}


def connect(path: str | Path) -> sqlite3.Connection:
    """打开数据库（不存在则创建）并迁移到最新版本。传 ":memory:" 用于测试。"""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version in range(current + 1, SCHEMA_VERSION + 1):
        try:
            with conn:
                conn.executescript(
                    f"BEGIN IMMEDIATE;\n{_MIGRATIONS[version]}\nPRAGMA user_version = {version};"
                )
        except sqlite3.OperationalError:
            # 另一个进程可能已在等写锁期间完成同一迁移。
            if conn.execute("PRAGMA user_version").fetchone()[0] < version:
                raise
    return conn.execute("PRAGMA user_version").fetchone()[0]
