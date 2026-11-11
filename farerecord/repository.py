"""数据库读写。所有时间戳为 UTC ISO 字符串，日期为 YYYY-MM-DD。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

from farerecord.models import OnewayQuote, RoundtripQuote


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# ---------- fetch_run ----------


def start_run(conn: sqlite3.Connection, source: str, started_at: str | None = None) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO fetch_run (source, started_at) VALUES (?, ?)",
            (source, started_at or utcnow_iso()),
        )
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    n_quotes: int,
    error: str | None = None,
    finished_at: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "UPDATE fetch_run SET status=?, n_quotes=?, error=?, finished_at=? WHERE id=?",
            (status, n_quotes, error, finished_at or utcnow_iso(), run_id),
        )


def recent_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM fetch_run ORDER BY id DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def last_run_started_at(conn: sqlite3.Connection) -> str | None:
    """最近一次抓取（不论成败）的开始时间。"""
    row = conn.execute("SELECT started_at FROM fetch_run ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def fail_streak(conn: sqlite3.Connection) -> int:
    """从最近一次往前数，连续失败（failed）的抓取次数。"""
    n = 0
    for r in conn.execute(
        "SELECT status FROM fetch_run WHERE status != 'running' ORDER BY id DESC"
    ):
        if r["status"] != "failed":
            break
        n += 1
    return n


# ---------- quotes ----------


def save_oneway(
    conn: sqlite3.Connection, run_id: int, quotes: Iterable[OnewayQuote], observed_at: str
) -> int:
    rows = [
        (run_id, q.origin, q.dest, q.depart_date.isoformat(), q.price, observed_at, q.airline)
        for q in quotes
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO oneway_quote "
            "(run_id, origin, dest, depart_date, price, observed_at, airline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def save_roundtrip(
    conn: sqlite3.Connection, run_id: int, quotes: Iterable[RoundtripQuote], observed_at: str
) -> int:
    rows = [
        (
            run_id,
            q.origin,
            q.dest,
            q.depart_date.isoformat(),
            q.return_date.isoformat(),
            q.price,
            observed_at,
        )
        for q in quotes
    ]
    with conn:
        conn.executemany("INSERT OR REPLACE INTO rt_quote VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def _latest_run_for(conn: sqlite3.Connection, table: str, origin: str, dest: str) -> int | None:
    row = conn.execute(
        f"SELECT MAX(run_id) FROM {table} WHERE origin=? AND dest=?", (origin, dest)
    ).fetchone()
    return row[0]


def latest_oneway(conn: sqlite3.Connection, origin: str, dest: str) -> dict[date, float]:
    """最近一次抓到该方向数据时，每天的单程最低价。"""
    run_id = _latest_run_for(conn, "oneway_quote", origin, dest)
    if run_id is None:
        return {}
    rows = conn.execute(
        "SELECT depart_date, price FROM oneway_quote WHERE run_id=? AND origin=? AND dest=?",
        (run_id, origin, dest),
    )
    return {date.fromisoformat(r["depart_date"]): r["price"] for r in rows}


def latest_oneway_detail(
    conn: sqlite3.Connection, origin: str, dest: str
) -> dict[date, tuple[float, str | None]]:
    """最近一次抓取里每天的 (单程最低价, 航司二字码或 None)。"""
    run_id = _latest_run_for(conn, "oneway_quote", origin, dest)
    if run_id is None:
        return {}
    rows = conn.execute(
        "SELECT depart_date, price, airline FROM oneway_quote "
        "WHERE run_id=? AND origin=? AND dest=?",
        (run_id, origin, dest),
    )
    return {date.fromisoformat(r["depart_date"]): (r["price"], r["airline"]) for r in rows}


def latest_airlines(conn: sqlite3.Connection, origin: str, dest: str) -> dict[date, str]:
    """最近一次抓取里，已识别出航司的日期 → 航司二字码。"""
    return {d: a for d, (_, a) in latest_oneway_detail(conn, origin, dest).items() if a}


def latest_observed_at(conn: sqlite3.Connection, origin: str, dest: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(observed_at) FROM oneway_quote WHERE origin=? AND dest=?", (origin, dest)
    ).fetchone()
    return row[0]


def latest_roundtrip(
    conn: sqlite3.Connection, origin: str, dest: str
) -> dict[tuple[date, date], float]:
    run_id = _latest_run_for(conn, "rt_quote", origin, dest)
    if run_id is None:
        return {}
    rows = conn.execute(
        "SELECT depart_date, return_date, price FROM rt_quote "
        "WHERE run_id=? AND origin=? AND dest=?",
        (run_id, origin, dest),
    )
    return {
        (date.fromisoformat(r["depart_date"]), date.fromisoformat(r["return_date"])): r["price"]
        for r in rows
    }


def oneway_series(
    conn: sqlite3.Connection, origin: str, dest: str, depart: date
) -> list[tuple[str, float]]:
    rows = conn.execute(
        "SELECT observed_at, price FROM oneway_quote "
        "WHERE origin=? AND dest=? AND depart_date=? ORDER BY observed_at",
        (origin, dest, depart.isoformat()),
    )
    return [(r["observed_at"], r["price"]) for r in rows]


def combo_series(
    conn: sqlite3.Connection, origin: str, dest: str, depart: date, ret: date
) -> list[tuple[str, float, float]]:
    """某个去回组合的历史：每次抓取里的 (时间, 去程价, 回程价)。

    只取两段在同一次抓取里都有价的记录。
    """
    rows = conn.execute(
        """
        SELECT o.observed_at AS t, o.price AS out_p, r.price AS ret_p
        FROM oneway_quote o
        JOIN oneway_quote r ON r.run_id = o.run_id
        WHERE o.origin=? AND o.dest=? AND o.depart_date=?
          AND r.origin=? AND r.dest=? AND r.depart_date=?
        ORDER BY o.observed_at
        """,
        (origin, dest, depart.isoformat(), dest, origin, ret.isoformat()),
    )
    return [(r["t"], r["out_p"], r["ret_p"]) for r in rows]


def runs_with_route(conn: sqlite3.Connection, origin: str, dest: str) -> list[tuple[int, str]]:
    """包含该方向数据的所有抓取 (run_id, observed_at)，按时间升序。"""
    rows = conn.execute(
        "SELECT run_id, MIN(observed_at) AS t FROM oneway_quote WHERE origin=? AND dest=? "
        "GROUP BY run_id ORDER BY t",
        (origin, dest),
    )
    return [(r["run_id"], r["t"]) for r in rows]


def oneway_at_run(
    conn: sqlite3.Connection, run_id: int, origin: str, dest: str
) -> dict[date, float]:
    rows = conn.execute(
        "SELECT depart_date, price FROM oneway_quote WHERE run_id=? AND origin=? AND dest=?",
        (run_id, origin, dest),
    )
    return {date.fromisoformat(r["depart_date"]): r["price"] for r in rows}


# ---------- manual quotes ----------


def add_manual_quote(
    conn: sqlite3.Connection,
    origin: str,
    dest: str,
    depart: date,
    ret: date,
    price: float,
    note: str | None = None,
    observed_at: str | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO manual_quote (origin, dest, depart_date, return_date, price, note, "
            "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                origin,
                dest,
                depart.isoformat(),
                ret.isoformat(),
                price,
                note,
                observed_at or utcnow_iso(),
            ),
        )
    return int(cur.lastrowid)


def manual_quotes(
    conn: sqlite3.Connection, origin: str, dest: str, depart: date, ret: date
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM manual_quote WHERE origin=? AND dest=? AND depart_date=? AND return_date=? "
        "ORDER BY observed_at",
        (origin, dest, depart.isoformat(), ret.isoformat()),
    )
    return [dict(r) for r in rows]
