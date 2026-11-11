"""一次抓取：对每条关注航线拉取去程、回程单程日历和往返缓存，写入数据库。"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import TypeVar

from farerecord import repository as repo
from farerecord.airlines import matching_dates
from farerecord.config import AppConfig, RouteConfig
from farerecord.models import OnewayQuote
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


AIRLINE_HORIZON_DAYS = 60  # 没配弹性窗口时，只保证未来这么多天的航司识别


def airline_need_range(route: RouteConfig, today: date, reverse: bool) -> tuple[date, date]:
    """必须识别出航司的日期范围：去程是出发窗口，回程是对应的返回日期范围。"""
    f = route.flex
    if f.start and f.end and f.end >= today:
        lo, hi = f.start, f.end
        if reverse:
            lo, hi = lo + timedelta(days=f.min_days), hi + timedelta(days=f.max_days)
    else:
        lo, hi = today, today + timedelta(days=AIRLINE_HORIZON_DAYS)
    return max(lo, today), hi


def attribute_airlines(
    quotes: list[OnewayQuote],
    previous: dict[date, tuple[float, str | None]],
    need_range: tuple[date, date],
    candidates: list[str],
    max_lookups: int,
    lookup: Callable[[str], dict[date, float] | None],
) -> list[OnewayQuote]:
    """给每天的最低价标上航司。

    - 价格和上一次抓取一样的日期，沿用上次识别的航司，不发请求
    - 其余在 need_range 内的日期，按"上次出最低价最多的航司优先"逐家查询
      （lookup 返回该航司每天的最低价；失败返回 None 并停止），全部对上或查满
      max_lookups 家就停
    - 查完仍对不上的日期记为 ""（已查过、候选航司里没有），价格不变就不再重复查；
      查询中途失败的日期保持 None，下次抓取再试
    """
    lowest = {q.depart_date: q.price for q in quotes}
    found: dict[date, str] = {}
    for d, p in lowest.items():
        prev = previous.get(d)
        if prev and prev[1] is not None and prev[0] == p:
            found[d] = prev[1]
    lo, hi = need_range
    need = {d for d in lowest if lo <= d <= hi and d not in found}
    wins = Counter(a for _, a in previous.values() if a)
    for code in sorted(candidates, key=lambda c: -wins[c])[:max_lookups]:
        if not need:
            break
        prices = lookup(code)
        if prices is None:
            break
        for d in matching_dates(lowest, prices):
            if not found.get(d):
                found[d] = code
        need = {d for d in need if not found.get(d)}
    else:
        found.update(dict.fromkeys(need, ""))
    return [replace(q, airline=found.get(q.depart_date)) for q in quotes]


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

    by_airline = getattr(source, "fetch_oneway_by_airline", None)
    candidates = cfg.collector.airlines if by_airline else []

    def oneway(route: RouteConfig, o: str, d: str, reverse: bool) -> int:
        """抓一个方向的单程日历（并识别航司），返回入库条数。"""
        quotes = call(f"{o}→{d} 单程", lambda: source.fetch_oneway(o, d, today))
        if not quotes:
            return 0
        quotes = [q for q in quotes if q.depart_date >= today]
        if candidates and cfg.collector.airline_lookups > 0:

            def lookup(code: str) -> dict[date, float] | None:
                rows = call(f"{o}→{d} 航司 {code}", lambda: by_airline(o, d, today, code))
                return None if rows is None else {q.depart_date: q.price for q in rows}

            quotes = attribute_airlines(
                quotes,
                repo.latest_oneway_detail(conn, o, d),
                airline_need_range(route, today, reverse),
                candidates,
                cfg.collector.airline_lookups,
                lookup,
            )
        return repo.save_oneway(conn, run_id, quotes, now())

    for route in cfg.routes:
        o, d = route.origin, route.dest
        n = oneway(route, o, d, reverse=False) + oneway(route, d, o, reverse=True)
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
