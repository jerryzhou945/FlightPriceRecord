"""固定监测的行程：定时到携程往返订票页读真实往返价。

首页可以把一个具体的去回日期"固定"下来（比如 12/28 去、1/20 回）。固定之后，
桌面端窗口开着时会按间隔在后台打开携程的往返订票页，读出页面上的往返含税价并记下来，
首页的大数字和走势就用这个真实价格，而不是推算的预估。

每条航线只固定一个行程，所以每次只打开一个页面；间隔不短于 30 分钟。
读不到（页面要求登录、出现验证码、没加载出来）会记下原因并显示在界面上，
被拦住时隔 6 小时才再试——不做任何绕过，这时请点"去携程核价"自己看。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from farerecord import db, settings
from farerecord import repository as repo
from farerecord.config import AppConfig
from farerecord.sources.base import SourceError
from farerecord.sources.ctrip_page import PageBlocked, PageEmpty, read_roundtrip

log = logging.getLogger(__name__)

PINNED_PREFIX = "pinned_trip:"  # setting 表里：pinned_trip:CHI-SHA → {"depart", "return"}
CHECK_PREFIX = "real_check:"  # 最近一次核价的结果
MIN_INTERVAL_MINUTES = 30
BLOCK_BACKOFF = timedelta(hours=6)  # 被要求登录/验证后，隔这么久才再试
DUE_SLACK = timedelta(minutes=2)
FRESH = timedelta(hours=3)  # 这么久以内读到的实价才当作"现在的价格"用于提醒
MAX_STAY_DAYS = 180

Reader = Callable[[str, str], dict[str, Any]]


@dataclass(frozen=True)
class Pinned:
    origin: str
    dest: str
    depart: date
    ret: date

    @property
    def route_key(self) -> str:
        return f"{self.origin}-{self.dest}"

    @property
    def trip_key(self) -> str:
        return f"{self.depart}_{self.ret}"


def get_pinned(conn: sqlite3.Connection, origin: str, dest: str) -> Pinned | None:
    value = settings.load(conn).get(f"{PINNED_PREFIX}{origin}-{dest}")
    if not value:
        return None
    try:
        return Pinned(
            origin, dest, date.fromisoformat(value["depart"]), date.fromisoformat(value["return"])
        )
    except (KeyError, TypeError, ValueError):
        return None


def set_pinned(
    conn: sqlite3.Connection, origin: str, dest: str, depart: date | None, ret: date | None
) -> None:
    """固定一个行程；depart 传 None 表示取消固定。"""
    value = {"depart": depart.isoformat(), "return": ret.isoformat()} if depart and ret else None
    settings.save(conn, f"{PINNED_PREFIX}{origin}-{dest}", value)


def pinned_trips(conn: sqlite3.Connection, cfg: AppConfig, today: date) -> list[Pinned]:
    """所有航线上固定的行程；已经出发的不再监测。"""
    found = (get_pinned(conn, r.origin, r.dest) for r in cfg.routes)
    return [p for p in found if p and p.depart >= today]


def check_state(conn: sqlite3.Connection, pinned: Pinned) -> dict[str, Any] | None:
    """这个行程最近一次核价的结果；没核过（或上次核的是别的日期）返回 None。"""
    state = settings.load(conn).get(CHECK_PREFIX + pinned.route_key)
    return state if state and state.get("trip") == pinned.trip_key else None


def check_interval(cfg: AppConfig) -> timedelta:
    return timedelta(minutes=max(MIN_INTERVAL_MINUTES, cfg.collector.interval_minutes))


def is_due(
    conn: sqlite3.Connection, pinned: Pinned, interval: timedelta, now: datetime | None = None
) -> bool:
    state = check_state(conn, pinned)
    if state is None:
        return True
    wait = BLOCK_BACKOFF if state["status"] == "blocked" else interval
    return (now or datetime.now(UTC)) >= datetime.fromisoformat(state["at"]) + wait - DUE_SLACK


def check(
    conn: sqlite3.Connection, pinned: Pinned, reader: Reader, now: datetime | None = None
) -> dict[str, Any]:
    """核一次价并记下结果。返回 {at, trip, status, message, price}。"""
    at = (now or datetime.now(UTC)).replace(microsecond=0).isoformat()
    state: dict[str, Any] = {"at": at, "trip": pinned.trip_key, "price": None, "message": None}
    # 先占住这一轮：任务计划和桌面端可能同时到点，后到的看到刚核过就跳过
    settings.save(conn, CHECK_PREFIX + pinned.route_key, {**state, "status": "running"})
    try:
        quote = read_roundtrip(reader, pinned.origin, pinned.dest, pinned.depart, pinned.ret)
    except PageBlocked as exc:
        state.update(status="blocked", message=str(exc))
    except PageEmpty as exc:
        state.update(status="empty", message=str(exc))
    except SourceError as exc:
        state.update(status="error", message=str(exc))
    except Exception as exc:  # noqa: BLE001 — 浏览器窗口的各种意外，都要记下来而不是静默失败
        log.exception("读取携程往返页出错")
        state.update(status="error", message=f"读取携程往返页出错：{type(exc).__name__}")
    else:
        repo.add_real_quote(
            conn,
            pinned.origin,
            pinned.dest,
            pinned.depart,
            pinned.ret,
            quote.price,
            quote.airline,
            at,
        )
        state.update(status="ok", price=quote.price)
    if state["status"] != "ok":
        log.warning("携程往返价核价未成功（%s）：%s", pinned.route_key, state["message"])
    settings.save(conn, CHECK_PREFIX + pinned.route_key, state)
    return state


def run_due(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    reader: Reader,
    *,
    force: bool = False,
    now: datetime | None = None,
    alerts: bool = True,
) -> list[dict[str, Any]]:
    """到点的固定行程各核一次价；读到新价格后评估到价提醒。"""
    cfg = settings.effective_config(conn, cfg)
    moment = now or datetime.now(UTC)
    interval = check_interval(cfg)
    results = [
        check(conn, p, reader, moment)
        for p in pinned_trips(conn, cfg, moment.astimezone().date())
        if force or is_due(conn, p, interval, moment)
    ]
    if alerts and any(r["status"] == "ok" for r in results):
        from farerecord.alerts import run_after_collect

        run_after_collect(conn, cfg)
    return results


def any_due(conn: sqlite3.Connection, cfg: AppConfig, now: datetime | None = None) -> bool:
    cfg = settings.effective_config(conn, cfg)
    moment = now or datetime.now(UTC)
    interval = check_interval(cfg)
    return any(
        is_due(conn, p, interval, moment)
        for p in pinned_trips(conn, cfg, moment.astimezone().date())
    )


def fresh_real_price(
    conn: sqlite3.Connection,
    origin: str,
    dest: str,
    depart: date,
    ret: date,
    now: datetime | None = None,
) -> float | None:
    """这个组合最近读到的真实往返价；太旧的不算。"""
    quote = repo.latest_real_quote(conn, origin, dest, depart, ret)
    if not quote:
        return None
    age = (now or datetime.now(UTC)) - datetime.fromisoformat(quote["observed_at"])
    return quote["price"] if age <= FRESH else None


class RealPriceMonitor:
    """桌面端开着时在后台线程里按间隔核价。kick() 让它马上核一次（刚固定行程、点了立即抓取）。"""

    def __init__(self, cfg: AppConfig, reader: Reader, check_seconds: float = 30.0) -> None:
        self.cfg = cfg
        self.reader = reader
        self.check_seconds = check_seconds
        self.busy = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self, force: bool = False) -> list[dict[str, Any]]:
        conn = db.connect(self.cfg.database)
        try:
            if not force and not any_due(conn, self.cfg):
                return []
            self.busy = True
            return run_due(conn, self.cfg, self.reader, force=force)
        finally:
            self.busy = False
            conn.close()

    def kick(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        self._stop.wait(4.0)  # 等主窗口先显示出来
        while not self._stop.is_set():
            force = self._wake.is_set()
            self._wake.clear()
            try:
                self.tick(force)
            except Exception:  # 监测线程不能因为一次意外就停掉
                log.exception("携程往返价监测出错")
            self._wake.wait(self.check_seconds)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="real-price", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def run_standalone(cfg: AppConfig, webview: Any) -> list[dict[str, Any]]:
    """任务计划里用：桌面端没开着时，临时开一个看不见的浏览器窗口把到点的固定行程核一遍。"""
    from farerecord.sources.ctrip_page import standalone_session

    results: list[dict[str, Any]] = []

    def work(reader: Reader) -> None:
        conn = db.connect(cfg.database)
        try:
            results.extend(run_due(conn, cfg, reader))
        finally:
            conn.close()

    # 和桌面端主窗口分开存放浏览器数据，两边同时运行时互不影响
    standalone_session(webview, Path(cfg.database).parent / "webview-task", work)
    return results
