"""数据库读写。所有时间戳为 UTC ISO 字符串，日期为 YYYY-MM-DD。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from farerecord.models import OnewayQuote, PriceWatch, RoundtripQuote

BEIJING = timezone(timedelta(hours=8))


def utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def beijing_date(iso: str) -> str | None:
    """UTC ISO 时间戳 → 北京时间的日期 YYYY-MM-DD；解析不了返回 None。"""
    try:
        return datetime.fromisoformat(iso).astimezone(BEIJING).date().isoformat()
    except ValueError:
        return None


# ---------- daily archive ----------

_ARCHIVE_SQL = (
    "INSERT INTO daily_price (kind, origin, dest, depart_date, return_date, observed_date, "
    "low, high, n) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1) "
    "ON CONFLICT (kind, origin, dest, depart_date, return_date, observed_date) DO UPDATE SET "
    "low = MIN(low, excluded.low), high = MAX(high, excluded.high), n = n + 1"
)


def _archive(
    conn: sqlite3.Connection, rows: list[tuple[str, str, str, str, str, float]], day: str | None
) -> None:
    """把报价并入当天的留档（最低、最高、次数）。在调用方的事务里执行。"""
    if day is None:
        return
    conn.executemany(_ARCHIVE_SQL, [(k, o, d, dep, r, day, p, p) for k, o, d, dep, r, p in rows])


def daily_prices(
    conn: sqlite3.Connection,
    kind: str,
    origin: str,
    dest: str,
    first: date,
    last: date,
    ret: date | None = None,
) -> list[dict[str, Any]]:
    """出发日在 [first, last] 内的每日留档，按出发日、观测日升序。"""
    sql = (
        "SELECT * FROM daily_price WHERE kind=? AND origin=? AND dest=? "
        "AND depart_date BETWEEN ? AND ?"
    )
    args: list[Any] = [kind, origin, dest, first.isoformat(), last.isoformat()]
    if ret is not None:
        sql += " AND return_date=?"
        args.append(ret.isoformat())
    rows = conn.execute(sql + " ORDER BY depart_date, observed_date", args)
    return [dict(r) for r in rows]


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


def fail_stale_runs(conn: sqlite3.Connection, before: str) -> int:
    """把 before（UTC ISO）之前开始、却一直停在"进行中"的抓取标为失败。

    程序在抓取途中被关掉（比如关了桌面窗口）就会留下这样的记录。
    """
    with conn:
        cur = conn.execute(
            "UPDATE fetch_run SET status='failed', finished_at=started_at, "
            "error='抓取被中断（程序在抓取途中退出）' WHERE status='running' AND started_at < ?",
            (before,),
        )
    return cur.rowcount


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
        (
            run_id,
            q.origin,
            q.dest,
            q.depart_date.isoformat(),
            q.price,
            observed_at,
            q.airline,
            q.airline_price,
        )
        for q in quotes
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO oneway_quote "
            "(run_id, origin, dest, depart_date, price, observed_at, airline, airline_price) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        _archive(
            conn, [("oneway", r[1], r[2], r[3], "", r[4]) for r in rows], beijing_date(observed_at)
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


FINISHED = ("ok", "partial")  # 抓完了的抓取；还在进行中的不用，免得只抓了一半的数据混进来


def _latest_run_for(conn: sqlite3.Connection, table: str, origin: str, dest: str) -> int | None:
    """这个方向最近一次"已经抓完"的抓取里有数据的那一次。"""
    row = conn.execute(
        f"SELECT q.run_id FROM {table} q JOIN fetch_run r ON r.id = q.run_id "
        "WHERE q.origin=? AND q.dest=? AND r.status IN (?, ?) ORDER BY q.run_id DESC LIMIT 1",
        (origin, dest, *FINISHED),
    ).fetchone()
    return row[0] if row else None


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
) -> dict[date, OnewayQuote]:
    """最近一次抓取里每天的单程最低价（带航司信息）。"""
    run_id = _latest_run_for(conn, "oneway_quote", origin, dest)
    if run_id is None:
        return {}
    rows = conn.execute(
        "SELECT depart_date, price, airline, airline_price FROM oneway_quote "
        "WHERE run_id=? AND origin=? AND dest=?",
        (run_id, origin, dest),
    )
    return {
        d: OnewayQuote(origin, dest, d, r["price"], r["airline"], r["airline_price"])
        for r in rows
        for d in (date.fromisoformat(r["depart_date"]),)
    }


def latest_airlines(conn: sqlite3.Connection, origin: str, dest: str) -> dict[date, OnewayQuote]:
    """最近一次抓取里，有航司信息的日期 → 报价。"""
    return {d: q for d, q in latest_oneway_detail(conn, origin, dest).items() if q.airline}


def latest_observed_at(conn: sqlite3.Connection, origin: str, dest: str) -> str | None:
    run_id = _latest_run_for(conn, "oneway_quote", origin, dest)
    if run_id is None:
        return None
    row = conn.execute(
        "SELECT MAX(observed_at) FROM oneway_quote WHERE run_id=? AND origin=? AND dest=?",
        (run_id, origin, dest),
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
    """包含该方向数据、已经抓完的所有抓取 (run_id, observed_at)，按时间升序。"""
    rows = conn.execute(
        "SELECT * FROM (SELECT r.id AS run_id, (SELECT q.observed_at FROM oneway_quote q "
        "WHERE q.origin=? AND q.dest=? AND q.run_id=r.id LIMIT 1) AS t "
        "FROM fetch_run r WHERE r.status IN (?, ?)) WHERE t IS NOT NULL ORDER BY t",
        (origin, dest, *FINISHED),
    )
    return [(r["run_id"], r["t"]) for r in rows]


def oneway_by_run(
    conn: sqlite3.Connection, origin: str, dest: str, first: date, last: date
) -> dict[int, dict[date, float]]:
    """出发日在 [first, last] 内的全部报价，按抓取分组：{run_id: {出发日: 价格}}。"""
    rows = conn.execute(
        "SELECT run_id, depart_date, price FROM oneway_quote "
        "WHERE origin=? AND dest=? AND depart_date BETWEEN ? AND ?",
        (origin, dest, first.isoformat(), last.isoformat()),
    )
    runs: dict[int, dict[date, float]] = {}
    days: dict[str, date] = {}
    for run_id, day, price in rows:
        d = days.get(day)
        if d is None:
            d = days[day] = date.fromisoformat(day)
        runs.setdefault(run_id, {})[d] = price
    return runs


def roundtrip_at_run(
    conn: sqlite3.Connection, run_id: int, origin: str, dest: str
) -> dict[tuple[date, date], float]:
    rows = conn.execute(
        "SELECT depart_date, return_date, price FROM rt_quote "
        "WHERE run_id=? AND origin=? AND dest=?",
        (run_id, origin, dest),
    )
    return {
        (date.fromisoformat(r["depart_date"]), date.fromisoformat(r["return_date"])): r["price"]
        for r in rows
    }


def save_calibration(
    conn: sqlite3.Connection, run_id: int, origin: str, dest: str, ratio: float, samples: int
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO rt_calibration VALUES (?, ?, ?, ?, ?)",
            (run_id, origin, dest, ratio, samples),
        )


def recent_calibrations(
    conn: sqlite3.Connection, origin: str, dest: str, limit: int
) -> list[tuple[float, int]]:
    """最近 limit 次抓取的 (往返校准比例, 样本数)。"""
    rows = conn.execute(
        "SELECT ratio, samples FROM rt_calibration WHERE origin=? AND dest=? "
        "ORDER BY run_id DESC LIMIT ?",
        (origin, dest, limit),
    )
    return [(r["ratio"], r["samples"]) for r in rows]


def prune_runs(conn: sqlite3.Connection, before: str) -> int:
    """删除 before（UTC ISO）之前的抓取及其报价，返回删掉的抓取次数。"""
    with conn:
        cur = conn.execute("DELETE FROM fetch_run WHERE started_at < ?", (before,))
    return cur.rowcount


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


def recent_manual_quotes(
    conn: sqlite3.Connection, origin: str, dest: str, since: str
) -> list[dict[str, Any]]:
    """since（UTC ISO）之后手动录入的真实往返价。"""
    rows = conn.execute(
        "SELECT * FROM manual_quote WHERE origin=? AND dest=? AND observed_at >= ? "
        "ORDER BY observed_at",
        (origin, dest, since),
    )
    return [dict(r) for r in rows]


def manual_quotes(
    conn: sqlite3.Connection, origin: str, dest: str, depart: date, ret: date
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM manual_quote WHERE origin=? AND dest=? AND depart_date=? AND return_date=? "
        "ORDER BY observed_at",
        (origin, dest, depart.isoformat(), ret.isoformat()),
    )
    return [dict(r) for r in rows]


# ---------- real round-trip quotes (携程往返订票页) ----------


def add_real_quote(
    conn: sqlite3.Connection,
    origin: str,
    dest: str,
    depart: date,
    ret: date,
    price: float,
    airline: str | None = None,
    observed_at: str | None = None,
    seats_left: int | None = None,
    next_price: float | None = None,
    source: str = "ctrip",
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO real_quote (origin, dest, depart_date, return_date, price, airline, "
            "observed_at, seats_left, next_price, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                origin,
                dest,
                depart.isoformat(),
                ret.isoformat(),
                price,
                airline,
                observed_at or utcnow_iso(),
                seats_left,
                next_price,
                source,
            ),
        )
        kind = "real" if source == "ctrip" else f"real_{source}"
        _archive(
            conn,
            [(kind, origin, dest, depart.isoformat(), ret.isoformat(), price)],
            beijing_date(observed_at or utcnow_iso()),
        )
    return int(cur.lastrowid)


def real_quotes(
    conn: sqlite3.Connection,
    origin: str,
    dest: str,
    depart: date,
    ret: date,
    source: str = "ctrip",
) -> list[dict[str, Any]]:
    """某个去回组合在某个平台读到过的所有真实往返价，按时间升序。"""
    rows = conn.execute(
        "SELECT * FROM real_quote WHERE origin=? AND dest=? AND depart_date=? AND return_date=? "
        "AND source=? ORDER BY observed_at",
        (origin, dest, depart.isoformat(), ret.isoformat(), source),
    )
    return [dict(r) for r in rows]


def latest_real_quote(
    conn: sqlite3.Connection,
    origin: str,
    dest: str,
    depart: date,
    ret: date,
    source: str = "ctrip",
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM real_quote WHERE origin=? AND dest=? AND depart_date=? AND return_date=? "
        "AND source=? ORDER BY observed_at DESC LIMIT 1",
        (origin, dest, depart.isoformat(), ret.isoformat(), source),
    ).fetchone()
    return dict(row) if row else None


def prune_real_quotes(conn: sqlite3.Connection, before: str) -> int:
    with conn:
        cur = conn.execute("DELETE FROM real_quote WHERE observed_at < ?", (before,))
    return cur.rowcount


# ---------- price watches ----------


def _watch(row: sqlite3.Row) -> PriceWatch:
    return PriceWatch(
        id=row["id"],
        origin=row["origin"],
        dest=row["dest"],
        depart_date=date.fromisoformat(row["depart_date"]),
        return_date=date.fromisoformat(row["return_date"]),
        target_price=row["target_price"],
        enabled=bool(row["enabled"]),
    )


def add_watch(
    conn: sqlite3.Connection,
    origin: str,
    dest: str,
    depart: date,
    ret: date,
    target_price: float,
    created_at: str | None = None,
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO price_watch (origin, dest, depart_date, return_date, target_price, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                origin,
                dest,
                depart.isoformat(),
                ret.isoformat(),
                target_price,
                created_at or utcnow_iso(),
            ),
        )
    return int(cur.lastrowid)


def list_watches(conn: sqlite3.Connection, enabled_only: bool = False) -> list[PriceWatch]:
    sql = "SELECT * FROM price_watch"
    if enabled_only:
        sql += " WHERE enabled = 1"
    return [_watch(r) for r in conn.execute(sql + " ORDER BY depart_date, return_date, id")]


def update_watch(
    conn: sqlite3.Connection,
    watch_id: int,
    *,
    target_price: float | None = None,
    enabled: bool | None = None,
) -> bool:
    """返回这条提醒是否存在。"""
    with conn:
        if target_price is not None:
            conn.execute(
                "UPDATE price_watch SET target_price=? WHERE id=?", (target_price, watch_id)
            )
        if enabled is not None:
            conn.execute("UPDATE price_watch SET enabled=? WHERE id=?", (int(enabled), watch_id))
    return conn.execute("SELECT 1 FROM price_watch WHERE id=?", (watch_id,)).fetchone() is not None


def delete_watch(conn: sqlite3.Connection, watch_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM price_watch WHERE id=?", (watch_id,))
    return cur.rowcount > 0
