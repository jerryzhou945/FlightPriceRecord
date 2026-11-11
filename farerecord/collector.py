"""一次抓取：对每条关注航线拉取去程、回程单程日历和往返缓存，写入数据库。"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TypeVar

from farerecord import repository as repo
from farerecord.config import AppConfig
from farerecord.sources.base import PriceSource, RetryableSourceError, SourceError
from farerecord.sources.ctrip_calendar import CST

log = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class CollectResult:
    run_id: int
    status: str  # ok | partial | failed
    n_quotes: int = 0
    errors: list[str] = field(default_factory=list)
    per_route: dict[str, int] = field(default_factory=dict)


def today_cst() -> date:
    return datetime.now(CST).date()


DUE_SLACK = timedelta(minutes=5)  # 定时器有抖动，提前这么久也算到点


def next_due_at(conn: sqlite3.Connection, interval_minutes: int) -> datetime | None:
    """下一次定时抓取的时间（UTC）；从没抓过返回 None，表示现在就该抓。"""
    last = repo.last_run_started_at(conn)
    if not last:
        return None
    return datetime.fromisoformat(last) + timedelta(minutes=interval_minutes)


def is_due(conn: sqlite3.Connection, interval_minutes: int, now: datetime | None = None) -> bool:
    """距离上一次抓取是否已经过了一个间隔。任务计划和桌面端都用它，避免重复抓取。"""
    due = next_due_at(conn, interval_minutes)
    return due is None or (now or datetime.now(UTC)) >= due - DUE_SLACK


def collect(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    source: PriceSource,
    *,
    today: date | None = None,
    now: Callable[[], str] = repo.utcnow_iso,
    sleep: Callable[[float], None] = time.sleep,
) -> CollectResult:
    today = today or today_cst()
    interval = cfg.collector.request_interval_seconds
    run_id = repo.start_run(conn, source.name, now())
    result = CollectResult(run_id=run_id, status="failed")
    first_call = True

    def call(label: str, fn: Callable[[], T]) -> T | None:
        nonlocal first_call
        if not first_call:
            sleep(interval)
        first_call = False
        for attempt in (1, 2):
            try:
                return fn()
            except RetryableSourceError as exc:
                if attempt == 1:
                    log.warning("%s 暂时失败，稍后重试：%s", label, exc)
                    sleep(interval * 2)
                    continue
                result.errors.append(f"{label}：{exc}")
            except SourceError as exc:
                result.errors.append(f"{label}：{exc}")
                break
        return None

    for route in cfg.routes:
        o, d = route.origin, route.dest
        n = 0
        out = call(f"{o}→{d} 单程", lambda o=o, d=d: source.fetch_oneway(o, d, today))
        if out:
            n += repo.save_oneway(conn, run_id, [q for q in out if q.depart_date >= today], now())
        back = call(f"{d}→{o} 单程", lambda o=o, d=d: source.fetch_oneway(d, o, today))
        if back:
            n += repo.save_oneway(conn, run_id, [q for q in back if q.depart_date >= today], now())
        depart = route.flex.start if route.flex.start and route.flex.start > today else today
        depart = max(depart, today + timedelta(days=1))
        rt = call(
            f"{o}⇄{d} 往返缓存",
            lambda o=o, d=d, dep=depart: source.fetch_roundtrip_cache(
                o, d, dep, dep + timedelta(days=7)
            ),
        )
        if rt:
            n += repo.save_roundtrip(conn, run_id, [q for q in rt if q.depart_date >= today], now())
        result.per_route[route.key] = n
        result.n_quotes += n

    if result.n_quotes and not result.errors:
        result.status = "ok"
    elif result.n_quotes:
        result.status = "partial"
    repo.finish_run(
        conn,
        run_id,
        result.status,
        result.n_quotes,
        "\n".join(result.errors) or None,
        now(),
    )
    return result


def collect_and_alert(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    source_factory: Callable[[AppConfig], PriceSource] | None = None,
) -> CollectResult:
    """抓取一次并评估提醒。网页“立即抓取”、桌面端定时器共用。"""
    if source_factory is None:
        from farerecord.sources.ctrip_calendar import CtripCalendarSource

        source: PriceSource = CtripCalendarSource(timeout=cfg.collector.timeout_seconds)
    else:
        source = source_factory(cfg)
    try:
        result = collect(conn, cfg, source)
    finally:
        close = getattr(source, "close", None)
        if close:
            close()
    from farerecord.alerts import run_after_collect

    run_after_collect(conn, cfg, result)
    return result
