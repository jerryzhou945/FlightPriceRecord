"""FastAPI 应用：/api/* JSON 接口 + 挂载 web/ 静态前端。"""

from __future__ import annotations

import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from farerecord import db, realprice, settings
from farerecord import repository as repo
from farerecord.airlines import airline_by_name, airline_json
from farerecord.alerts import recent_log, send_test
from farerecord.alerts.notifiers import WxPusherSimpleNotifier, build_notifier, make_notifier
from farerecord.alerts.rules import watch_trip
from farerecord.analysis import advice as buy_advice
from farerecord.analysis import calibration
from farerecord.analysis.calibration import Calibration
from farerecord.analysis.flex import FlexQuery, search, suggestions
from farerecord.analysis.history import flex_low_history
from farerecord.analysis.stats import PriceStats, compute
from farerecord.cities import CITY_GROUPS
from farerecord.collector import collect_and_alert, next_due_at
from farerecord.config import (
    MIN_INTERVAL_MINUTES,
    ROOT,
    AppConfig,
    load_config,
    parse_quiet_hours,
)
from farerecord.envfile import update_env_file
from farerecord.models import OnewayQuote, PriceWatch, Trip
from farerecord.settings import effective_config
from farerecord.sources import ly_page
from farerecord.sources.base import PriceSource
from farerecord.sources.ctrip_page import round_url

WEB_DIR = ROOT / "web"


def ctrip_url(origin: str, dest: str, depart: date, ret: date | None = None) -> str:
    o, d = origin.lower(), dest.lower()
    if ret:
        return round_url(origin, dest, depart, ret)
    return (
        f"https://flights.ctrip.com/online/list/oneway-{o}-{d}"
        f"?depdate={depart}&cabin=y_s&adult=1&child=0&infant=0"
    )


Airlines = tuple[dict[date, OnewayQuote], dict[date, OnewayQuote]]  # (去程, 回程) 带航司的报价


def trip_json(t: Trip, origin: str, dest: str, airlines: Airlines | None = None) -> dict[str, Any]:
    out_al, ret_al = airlines or ({}, {})
    return {
        "out_airline": airline_json(out_al.get(t.depart_date)),
        "ret_airline": airline_json(ret_al.get(t.return_date)),
        "depart": t.depart_date.isoformat(),
        "return": t.return_date.isoformat(),
        "days": t.days,
        "out": t.out_price,
        "ret": t.ret_price,
        "oneway_sum": t.price,  # 分开买两张单程
        "price": t.estimate,  # 往返预估（校准后）
        "rt_price": t.rt_price,
        "best_price": t.best_price,
        "ctrip_url": ctrip_url(origin, dest, t.depart_date, t.return_date),
    }


def calibration_json(c: Calibration) -> dict[str, Any]:
    return {"ratio": round(c.ratio, 4), "source": c.source, "samples": c.samples}


def stats_json(s: PriceStats | None) -> dict[str, Any] | None:
    if s is None:
        return None
    return {
        "current": s.current,
        "observed_at": s.observed_at,
        "low": s.low,
        "low_at": s.low_at,
        "high": s.high,
        "n": s.n,
        "cheaper_than_pct": s.cheaper_than_pct,
        "change_24h": s.change_24h,
        "is_new_low": s.is_new_low,
        "window_days": s.window_days,
    }


def _tiers(values: list[float]) -> Callable[[float], int]:
    """按五分位把价格分成 0（最便宜）到 4（最贵）。"""
    s = sorted(values)
    if not s:
        return lambda v: 2
    cuts = [s[int((len(s) - 1) * f)] for f in (0.2, 0.4, 0.6, 0.8)]
    return lambda v: sum(1 for c in cuts if v > c)


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.cfg.database)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class ManualQuoteIn(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    dest: str = Field(min_length=3, max_length=3)
    depart: date
    ret: date
    price: float = Field(gt=0, lt=1_000_000)
    note: str | None = Field(default=None, max_length=200)


class WatchIn(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    dest: str = Field(min_length=3, max_length=3)
    depart: date
    ret: date
    target_price: float = Field(gt=0, lt=1_000_000)


class WatchPatch(BaseModel):
    target_price: float | None = Field(default=None, gt=0, lt=1_000_000)
    enabled: bool | None = None


class PinnedIn(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    dest: str = Field(min_length=3, max_length=3)
    depart: date | None = None  # 两个日期都不传表示取消固定
    ret: date | None = None


class RouteIn(BaseModel):
    origin: str = Field(min_length=3, max_length=3)
    dest: str = Field(min_length=3, max_length=3)


class CollectorSettingsIn(BaseModel):
    interval_minutes: int = Field(ge=MIN_INTERVAL_MINUTES, le=1440)


class WechatIn(BaseModel):
    provider: Literal["wxpusher_spt", "wxpusher", "pushplus", "off"]
    spt: str | None = Field(default=None, max_length=200)
    app_token: str | None = Field(default=None, max_length=200)
    uid: str | None = Field(default=None, max_length=200)
    token: str | None = Field(default=None, max_length=200)


def _token(value: str | None, pattern: str, hint: str) -> str:
    """校验口令格式；错误信息里不回显口令本身。"""
    value = (value or "").strip()
    if not re.fullmatch(pattern, value):
        raise HTTPException(400, hint)
    return value


def wechat_provider(notifier: Any) -> str:
    if isinstance(notifier, WxPusherSimpleNotifier):
        return "wxpusher_spt"
    return notifier.name


INTERVAL_OPTIONS = [15, 30, 60, 120]  # 界面上可选的抓取间隔（分钟）


class AlertSettingsIn(BaseModel):
    desktop: bool | None = None
    new_low: bool | None = None
    drop_percent: float | None = Field(default=None, ge=0, le=90)
    fail_streak: int | None = Field(default=None, ge=0, le=50)
    quiet_hours: str | None = Field(default=None, max_length=11)
    # 目标价跟着航线走；传 null 表示取消目标价
    target_price: float | None = Field(default=None, gt=0, lt=1_000_000)
    origin: str | None = None
    dest: str | None = None


def create_app(
    cfg: AppConfig | None = None,
    source_factory: Callable[[AppConfig], PriceSource] | None = None,
    web_dir: Path | None = WEB_DIR,
    env_file: Path | None = None,
) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="航价记", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.cfg = cfg
    app.state.env_file = env_file or cfg.env_file  # 微信口令写到这里
    app.state.collect_lock = threading.Lock()  # 手动抓取和定时抓取共用，避免同时请求携程
    app.state.real_monitor = None  # 桌面端启动后放进来：读携程往返订票页的后台监测
    app.state.ly_login = None  # 桌面端放进来：打开一个窗口让用户自己登录同程

    def pinned_json(p: realprice.Pinned | None) -> dict[str, Any] | None:
        if p is None:
            return None
        return {"depart": p.depart.isoformat(), "return": p.ret.isoformat()}

    def real_json(
        conn: sqlite3.Connection, o: str, d: str, depart: date, ret: date, window: int
    ) -> dict[str, Any]:
        """这个组合从携程往返订票页读到的真实往返价：走势、统计、最近一次核价的结果。"""
        quotes = repo.real_quotes(conn, o, d, depart, ret)
        series = [(q["observed_at"], q["price"]) for q in quotes]
        pinned = realprice.get_pinned(conn, o, d)
        is_pinned = pinned is not None and (pinned.depart, pinned.ret) == (depart, ret)
        age = (
            datetime.now(UTC) - datetime.fromisoformat(quotes[-1]["observed_at"])
            if quotes
            else None
        )
        return {
            "pinned": is_pinned,
            "series": [
                [q["observed_at"], q["price"], q["airline"], q["seats_left"]] for q in quotes
            ],
            "seats_left": quotes[-1]["seats_left"] if quotes else None,  # 携程的"剩 N 张"
            "next_price": quotes[-1]["next_price"] if quotes else None,  # 页面上的下一档价格
            "stats": stats_json(compute(series, window_days=window)),
            "airline": airline_by_name(quotes[-1]["airline"]) if quotes else None,
            "fresh": age is not None and age <= realprice.FRESH,
            "check": realprice.check_state(conn, pinned) if is_pinned and pinned else None,
        }

    def ly_json(
        conn: sqlite3.Connection, o: str, d: str, depart: date, ret: date, window: int
    ) -> dict[str, Any]:
        """同程往返结果页读到的价格（需要用户在桌面端登录过同程）。"""
        quotes = repo.real_quotes(conn, o, d, depart, ret, "ly")
        pinned = realprice.get_pinned(conn, o, d)
        is_pinned = pinned is not None and (pinned.depart, pinned.ret) == (depart, ret)
        last = quotes[-1] if quotes else None
        age = datetime.now(UTC) - datetime.fromisoformat(last["observed_at"]) if last else None
        return {
            "enabled": bool(settings.load(conn).get(realprice.LY_ENABLED_KEY)),
            "series": [
                [q["observed_at"], q["price"], q["airline"], q["seats_left"]] for q in quotes
            ],
            "stats": stats_json(compute([(q["observed_at"], q["price"]) for q in quotes], window)),
            "seats_left": last["seats_left"] if last else None,
            "next_price": last["next_price"] if last else None,
            "fresh": age is not None and age <= realprice.FRESH,
            "check": realprice.check_state(conn, pinned, "ly") if is_pinned and pinned else None,
            "url": ly_page.round_url(o, d, depart, ret),
        }

    def platforms_json(
        conn: sqlite3.Connection, o: str, d: str, depart: date, ret: date
    ) -> dict[str, Any] | None:
        """同一行程在携程、同程的实价对比和"哪家更稳妥"的推荐；只有一家有价格时为 None。"""
        now = datetime.now(UTC)
        views = {
            source: buy_advice.platform_view(
                name,
                [
                    (q["observed_at"], q["price"])
                    for q in repo.real_quotes(conn, o, d, depart, ret, source)
                ],
                now,
                depart,
            )
            for source, name in realprice.SOURCES.items()
        }
        pick = buy_advice.pick_platform(views["ctrip"], views["ly"])
        if pick is None:
            return None
        return {
            "pick": pick.pick,
            "reason": pick.reason,
            "checklist": pick.checklist,
            "rows": [
                {
                    "site": v.site,
                    "price": v.price,
                    "observed_at": v.observed_at,
                    "low_24h": v.low_24h,
                    "high_24h": v.high_24h,
                    "jumps_24h": v.jumps_24h,
                    "fresh": v.fresh,
                }
                for v in pick.rows
            ],
        }

    def norm(origin: str, dest: str) -> tuple[str, str]:
        o, d = origin.upper(), dest.upper()
        if len(o) != 3 or len(d) != 3 or not (o + d).isalpha():
            raise HTTPException(400, "城市码应为 3 个字母，如 CHI、SHA")
        return o, d

    @app.get("/api/routes")
    def routes(conn: Conn) -> list[dict[str, Any]]:
        current = effective_config(conn, cfg)
        fixed = {r.key for r in cfg.routes}
        return [
            {
                "key": r.key,
                "name": r.name,
                "custom": r.key not in fixed,  # 界面里添加的，可以删
                "origin": r.origin,
                "dest": r.dest,
                "target_price": r.target_price,
                "pinned": pinned_json(realprice.get_pinned(conn, r.origin, r.dest)),
                "flex": {
                    "start": r.flex.start.isoformat() if r.flex.start else None,
                    "end": r.flex.end.isoformat() if r.flex.end else None,
                    "min_days": r.flex.min_days,
                    "max_days": r.flex.max_days,
                    "ideal_days": r.flex.ideal_days,
                },
            }
            for r in current.routes
        ]

    @app.get("/api/cities")
    def cities() -> list[dict[str, Any]]:
        """界面里可选的出发地 / 目的地（携程城市三字码）。"""
        return [
            {"group": g, "cities": [{"code": c, "name": n} for c, n in items]}
            for g, items in CITY_GROUPS
        ]

    @app.post("/api/routes", status_code=201)
    def add_route(conn: Conn, body: RouteIn) -> dict[str, Any]:
        """添加一条要监测的航线（比如换个出发地）；下次抓取开始记录它的价格。"""
        o, d = norm(body.origin, body.dest)
        if o == d:
            raise HTTPException(400, "出发地和目的地不能相同")
        try:
            route = settings.add_route(conn, cfg, o, d)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"key": route.key, "name": route.name, "origin": o, "dest": d}

    @app.delete("/api/routes/{origin}/{dest}", status_code=204)
    def delete_route(conn: Conn, origin: str, dest: str) -> None:
        """删掉界面里添加的航线：取消它的固定行程、停用它的到价提醒；已抓到的价格保留。"""
        o, d = norm(origin, dest)
        if cfg.route(o, d):
            raise HTTPException(400, "这条航线写在 config.yaml 里，请在配置文件里删")
        if not settings.remove_route(conn, o, d):
            raise HTTPException(404, "没有这条航线")
        realprice.set_pinned(conn, o, d, None, None)
        for w in repo.list_watches(conn):
            if (w.origin, w.dest) == (o, d):
                repo.update_watch(conn, w.id, enabled=False)

    @app.get("/api/calendar")
    def calendar(
        conn: Conn,
        origin: str,
        dest: str,
        month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    ) -> dict[str, Any]:
        o, d = norm(origin, dest)
        detail = {
            k: q
            for k, q in repo.latest_oneway_detail(conn, o, d).items()
            if k.isoformat()[:7] == month
        }
        tier = _tiers([q.price for q in detail.values()])
        days = [
            {
                "date": k.isoformat(),
                "price": q.price,
                "tier": tier(q.price),
                "airline": airline_json(q),
                "ctrip_url": ctrip_url(o, d, k),
            }
            for k, q in sorted(detail.items())
        ]
        low = min(days, key=lambda x: x["price"]) if days else None
        return {
            "origin": o,
            "dest": d,
            "month": month,
            "observed_at": repo.latest_observed_at(conn, o, d),
            "days": days,
            "low": low,
        }

    @app.get("/api/flex")
    def flex(
        conn: Conn,
        origin: str,
        dest: str,
        start: date,
        end: date,
        min_days: Annotated[int, Query(alias="min", ge=1, le=120)],
        max_days: Annotated[int, Query(alias="max", ge=1, le=120)],
        ideal: Annotated[int | None, Query(ge=1, le=120)] = None,
        top: Annotated[int, Query(ge=1, le=50)] = 10,
        with_history: bool = True,
    ) -> dict[str, Any]:
        o, d = norm(origin, dest)
        try:
            q = FlexQuery(start, end, min_days, max_days, ideal)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        out = repo.latest_oneway(conn, o, d)
        back = repo.latest_oneway(conn, d, o)
        cal = calibration.current(conn, o, d)
        res = search(out, back, q, repo.latest_roundtrip(conn, o, d), cal.ratio)
        al: Airlines = (repo.latest_airlines(conn, o, d), repo.latest_airlines(conn, d, o))
        dates = q.depart_dates
        stays = q.stays
        di = {x: i for i, x in enumerate(dates)}
        si = {x: i for i, x in enumerate(stays)}
        cells = [[di[t.depart_date], si[t.days], t.best_price] for t in res.trips]
        lo_out = min(res.trips, key=lambda t: (t.out_price, t.depart_date), default=None)
        lo_ret = min(res.trips, key=lambda t: (t.ret_price, t.return_date), default=None)
        hist = []
        if with_history:
            hist = [
                {"t": p.observed_at, "price": p.best.best_price, **trip_json(p.best, o, d)}
                for p in flex_low_history(conn, o, d, q, cal.ratio)
            ]
        return {
            "calibration": calibration_json(cal),
            "origin": o,
            "dest": d,
            "query": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "min": min_days,
                "max": max_days,
                "ideal": ideal,
            },
            "observed_at": repo.latest_observed_at(conn, o, d),
            "depart_dates": [x.isoformat() for x in dates],
            "stays": stays,
            "cells": cells,
            "count": len(res.trips),
            "best": trip_json(res.best, o, d, al) if res.best else None,
            "best_ideal": trip_json(res.best_ideal, o, d, al) if res.best_ideal else None,
            "ideal_saving": res.ideal_saving,
            "top": [trip_json(t, o, d, al) for t in res.top(top)],
            "by_days": [
                {"days": s, **trip_json(t, o, d, al)} for s, t in sorted(res.by_days.items())
            ],
            "leg_lows": {
                "out": {
                    "date": lo_out.depart_date.isoformat(),
                    "price": lo_out.out_price,
                    "airline": airline_json(al[0].get(lo_out.depart_date)),
                }
                if lo_out
                else None,
                "ret": {
                    "date": lo_ret.return_date.isoformat(),
                    "price": lo_ret.ret_price,
                    "airline": airline_json(al[1].get(lo_ret.return_date)),
                }
                if lo_ret
                else None,
            },
            "suggestions": suggestions(res),
            "low_history": hist,
            "low_stats": stats_json(compute([(h["t"], h["price"]) for h in hist])),
        }

    @app.get("/api/history")
    def history(
        conn: Conn,
        origin: str,
        dest: str,
        depart: date,
        ret: date | None = None,
        window: Annotated[int, Query(ge=1, le=365)] = 14,
    ) -> dict[str, Any]:
        o, d = norm(origin, dest)
        if ret is not None and ret <= depart:
            raise HTTPException(400, "返回日期必须晚于出发日期")
        legs = None
        cal = calibration.current(conn, o, d) if ret else Calibration()
        if ret:
            rows = repo.combo_series(conn, o, d, depart, ret)
            series = [(t, cal.apply(a + b)) for t, a, b in rows]
            if rows:
                legs = {"out": rows[-1][1], "ret": rows[-1][2], "sum": rows[-1][1] + rows[-1][2]}
            manual = repo.manual_quotes(conn, o, d, depart, ret)
        else:
            series = repo.oneway_series(conn, o, d, depart)
            manual = []
        return {
            "calibration": calibration_json(cal),
            "legs": legs,
            "out_airline": airline_json(repo.latest_airlines(conn, o, d).get(depart)),
            "ret_airline": airline_json(repo.latest_airlines(conn, d, o).get(ret)) if ret else None,
            "origin": o,
            "dest": d,
            "depart": depart.isoformat(),
            "return": ret.isoformat() if ret else None,
            "series": [[t, p] for t, p in series],
            "stats": stats_json(compute(series, window_days=window)),
            "manual": manual,
            "real": real_json(conn, o, d, depart, ret, window) if ret else None,
            "ly": ly_json(conn, o, d, depart, ret, window) if ret else None,
            "ctrip_url": ctrip_url(o, d, depart, ret),
        }

    @app.get("/api/advice")
    def advice(conn: Conn, origin: str, dest: str, depart: date, ret: date) -> dict[str, Any]:
        """买还是等：离出发天数、旺季、监测以来的价格位置、往年同期、附近更便宜的日期。"""
        o, d = norm(origin, dest)
        if ret <= depart:
            raise HTTPException(400, "返回日期必须晚于出发日期")
        today = datetime.now().astimezone().date()  # 和出发日同一个口径：本机日期
        real = repo.real_quotes(conn, o, d, depart, ret)
        if real:
            series, basis = [(q["observed_at"], q["price"]) for q in real], "real"
        else:
            cal = calibration.current(conn, o, d)
            rows = repo.combo_series(conn, o, d, depart, ret)
            series, basis = [(t, cal.apply(a + b)) for t, a, b in rows], "estimate"
        current = series[-1][1] if series else 0.0
        out_q = repo.latest_oneway_detail(conn, o, d)
        back_q = repo.latest_oneway_detail(conn, d, o)
        alts = buy_advice.cheaper_nearby(out_q, back_q, depart, ret, current, today)
        days_left = (depart - today).days
        ly = depart - timedelta(days=365)
        rows = repo.daily_prices(
            conn, "oneway", o, d, ly - timedelta(days=3), ly + timedelta(days=3)
        )
        last_year = buy_advice.last_year_outlook(rows, depart, days_left)
        latest = real[-1] if real else {}
        a = buy_advice.advise(
            today,
            depart,
            ret,
            series,
            basis,
            alternatives=alts,
            last_year=last_year,
            seats_left=latest.get("seats_left"),
            next_price=latest.get("next_price"),
            elsewhere=[
                ("同程", q["price"])
                for q in [repo.latest_real_quote(conn, o, d, depart, ret, "ly")]
                if q
                and datetime.now(UTC) - datetime.fromisoformat(q["observed_at"]) <= realprice.FRESH
            ],
        )
        archive_from = conn.execute("SELECT MIN(observed_date) FROM daily_price").fetchone()[0]
        return {
            "verdict": a.verdict,
            "headline": a.headline,
            "reasons": a.reasons,
            "days_left": a.days_left,
            "peak": a.peak,
            "deadline": a.deadline.isoformat(),
            "target": a.target,
            "current": a.current,
            "low": a.low,
            "low_at": a.low_at,
            "high": a.high,
            "basis": a.basis,
            "seats_left": a.seats_left,
            "next_price": a.next_price,
            "n": len(series),
            "since": series[0][0] if series else None,
            "jumps": [
                {"at": j.at, "before": j.before, "after": j.after, "days_left": j.days_left}
                for j in a.jumps[-6:]
            ],
            "fences": [f.isoformat() for f in a.fences],
            "alternatives": [
                {
                    "depart": x.depart.isoformat(),
                    "return": x.ret.isoformat(),
                    "days": (x.ret - x.depart).days,
                    "oneway_sum": x.oneway_sum,
                    "expected": x.expected,
                    "saving": x.saving,
                    "out_airline": airline_json(x.out_quote),
                    "ret_airline": airline_json(x.ret_quote),
                    "ctrip_url": ctrip_url(o, d, x.depart, x.ret),
                }
                for x in a.alternatives
            ],
            "last_year": {
                "depart": a.last_year.depart.isoformat(),
                "change_pct": round(a.last_year.change_pct, 4),
                "days_left": a.last_year.days_left,
                "later_days_left": a.last_year.later_days_left,
            }
            if a.last_year
            else None,
            "archive_from": archive_from,
            "platforms": platforms_json(conn, o, d, depart, ret),
        }

    @app.put("/api/pinned")
    def save_pinned(conn: Conn, body: PinnedIn) -> dict[str, Any]:
        """把一个具体的去回日期固定在首页，并开始监测它的携程往返实价。"""
        o, d = norm(body.origin, body.dest)
        if effective_config(conn, cfg).route(o, d) is None:
            raise HTTPException(404, "没有监测这条航线")
        if body.depart is None or body.ret is None:
            realprice.set_pinned(conn, o, d, None, None)
            return {"pinned": None}
        if body.ret <= body.depart:
            raise HTTPException(400, "返回日期必须晚于出发日期")
        if (body.ret - body.depart).days > realprice.MAX_STAY_DAYS:
            raise HTTPException(400, f"行程最长 {realprice.MAX_STAY_DAYS} 天")
        if body.depart < datetime.now().astimezone().date():
            raise HTTPException(400, "出发日期已经过去了")
        realprice.set_pinned(conn, o, d, body.depart, body.ret)
        monitor = app.state.real_monitor
        if monitor is not None:
            monitor.kick()  # 马上去读一次真实往返价
        return {"pinned": pinned_json(realprice.get_pinned(conn, o, d))}

    @app.post("/api/ly/login", status_code=202)
    def ly_login(conn: Conn) -> dict[str, Any]:
        """开启同程监测，并在桌面端弹出同程登录窗口——账号密码由用户自己在那个窗口里输入。"""
        opener = app.state.ly_login
        if opener is None:
            raise HTTPException(409, "只有桌面端能登录同程，请用 farerecord app 打开")
        settings.save(conn, realprice.LY_ENABLED_KEY, True)
        for r in effective_config(conn, cfg).routes:  # 清掉"需要登录"的记录，登录后马上重读
            settings.save(conn, f"real_check_ly:{r.key}", None)
        opener()
        return {"enabled": True}

    @app.delete("/api/ly", status_code=204)
    def ly_off(conn: Conn) -> None:
        """关闭同程监测（已读到的价格保留），并删掉本机记住的同程登录状态。"""
        from farerecord import ly_session

        settings.save(conn, realprice.LY_ENABLED_KEY, False)
        ly_session.forget(ly_session.path_for(cfg.database))

    @app.post("/api/real-check", status_code=202)
    def real_check() -> dict[str, Any]:
        """让后台马上去携程往返订票页核一次价（只有桌面端有这个能力）。"""
        monitor = app.state.real_monitor
        if monitor is None:
            raise HTTPException(409, "只有桌面端能读取携程往返价，请用 farerecord app 打开")
        monitor.kick()
        return {"started": True}

    @app.get("/api/status")
    def status(conn: Conn) -> dict[str, Any]:
        runs = repo.recent_runs(conn, 10)
        last_ok = next((r for r in runs if r["status"] in ("ok", "partial")), None)
        total = conn.execute("SELECT COUNT(*) FROM oneway_quote").fetchone()[0]
        collector = effective_config(conn, cfg).collector
        interval = collector.interval_minutes
        due = next_due_at(conn, interval)
        return {
            "interval_minutes": interval,
            "interval_options": INTERVAL_OPTIONS,
            "retention_days": collector.retention_days,
            "next_due_at": due.isoformat() if due else None,
            "collecting": app.state.collect_lock.locked(),
            "real_monitor": app.state.real_monitor is not None,
            "ly_enabled": bool(settings.load(conn).get(realprice.LY_ENABLED_KEY)),
            "ly_login": app.state.ly_login is not None,
            "real_checking": bool(getattr(app.state.real_monitor, "busy", False)),
            "runs": runs,
            "fail_streak": repo.fail_streak(conn),
            "last_ok_at": last_ok["finished_at"] if last_ok else None,
            "total_quotes": total,
            "n_runs": conn.execute("SELECT COUNT(*) FROM fetch_run").fetchone()[0],
        }

    @app.put("/api/settings/collector")
    def save_collector_settings(conn: Conn, body: CollectorSettingsIn) -> dict[str, Any]:
        """界面里改抓取间隔。"""
        settings.save(conn, settings.INTERVAL_KEY, body.interval_minutes)
        return {"interval_minutes": effective_config(conn, cfg).collector.interval_minutes}

    def watch_json(conn: sqlite3.Connection, w: PriceWatch) -> dict[str, Any]:
        trip = watch_trip(conn, w)
        price = trip.best_price if trip else None
        return {
            "id": w.id,
            "origin": w.origin,
            "dest": w.dest,
            "depart": w.depart_date.isoformat(),
            "return": w.return_date.isoformat(),
            "days": (w.return_date - w.depart_date).days,
            "target_price": w.target_price,
            "enabled": w.enabled,
            "current": price,
            "hit": price is not None and price <= w.target_price,
            "out_airline": airline_json(
                repo.latest_airlines(conn, w.origin, w.dest).get(w.depart_date)
            ),
            "ret_airline": airline_json(
                repo.latest_airlines(conn, w.dest, w.origin).get(w.return_date)
            ),
            "ctrip_url": ctrip_url(w.origin, w.dest, w.depart_date, w.return_date),
        }

    @app.get("/api/alerts")
    def alerts(conn: Conn, origin: str | None = None, dest: str | None = None) -> dict[str, Any]:
        current = effective_config(conn, cfg)
        a = current.alerts
        wechat = make_notifier(a.channel)
        has_wechat = wechat.name in ("wxpusher", "pushplus")
        route = current.route(origin, dest) if origin and dest else None
        route = route or (current.routes[0] if current.routes else None)
        qh = f"{a.quiet_hours[0]:%H:%M}-{a.quiet_hours[1]:%H:%M}" if a.quiet_hours else ""
        target = route.target_price if route else None
        rules = [
            {
                "key": "target",
                "title": f"低于目标价 ¥{target:,.0f}" if target else "低于目标价",
                "detail": "弹性窗口内最低往返估价跌破目标",
                "enabled": bool(target),
            },
            {
                "key": "new_low",
                "title": "出现历史新低",
                "detail": "比此前每次抓取的窗口最低价都低",
                "enabled": a.new_low,
            },
            {
                "key": "drop",
                "title": f"24 小时跌幅超过 {a.drop_percent:g}%",
                "detail": "和约 24 小时前的窗口最低价相比",
                "enabled": a.drop_percent > 0,
            },
            {
                "key": "fail",
                "title": f"连续 {a.fail_streak} 次抓取失败",
                "detail": "携程接口变化或网络问题时提醒你",
                "enabled": a.fail_streak > 0,
            },
            {
                "key": "quiet",
                "title": "夜间免打扰",
                "detail": f"{qh} 之间的提醒推迟到之后的抓取再发" if qh else "未设置",
                "enabled": bool(qh),
            },
        ]
        return {
            "channel": wechat.name if has_wechat else "console",
            "channel_label": build_notifier(a).label,
            "configured": has_wechat and wechat.configured,
            "desktop_supported": sys.platform == "win32",
            "wechat": {
                "provider": wechat_provider(wechat) if has_wechat else None,
                "configured": has_wechat and wechat.configured,
                "can_configure": app.state.env_file is not None,
                "spt_qr": WxPusherSimpleNotifier.QR_URL,
            },
            "settings": {
                "desktop": a.desktop,
                "new_low": a.new_low,
                "drop_percent": a.drop_percent,
                "fail_streak": a.fail_streak,
                "quiet_hours": qh,
                "target_price": target,
            },
            "rules": rules,
            "watches": [watch_json(conn, w) for w in repo.list_watches(conn)],
            "log": recent_log(conn, 20),
        }

    @app.put("/api/alerts/settings")
    def save_alert_settings(conn: Conn, body: AlertSettingsIn) -> dict[str, Any]:
        """界面里改提醒开关和目标价；只保存请求里出现的字段。"""
        sent = body.model_dump(exclude_unset=True)
        if "quiet_hours" in sent:
            try:
                parse_quiet_hours(sent["quiet_hours"])
            except ValueError as exc:
                raise HTTPException(400, "免打扰时段格式应为 23:00-08:00") from exc
        if "target_price" in sent:
            o, d = norm(body.origin or "", body.dest or "")
            route = effective_config(conn, cfg).route(o, d)
            if route is None:
                raise HTTPException(404, f"没有监测航线 {o}-{d}")
            settings.save(conn, settings.TARGET_PREFIX + route.key, sent["target_price"])
        for key in settings.ALERT_KEYS:
            if key in sent and sent[key] is not None:
                settings.save(conn, f"alerts.{key}", sent[key])
        return alerts(conn, body.origin, body.dest)["settings"]

    @app.put("/api/alerts/wechat")
    def save_wechat(conn: Conn, body: WechatIn) -> dict[str, Any]:
        """界面里配置微信推送。口令写进 .env 和当前进程的环境变量，不存数据库、不返回。"""
        env_file: Path | None = app.state.env_file
        if body.provider == "off":
            settings.save(conn, "alerts.channel", "console")
            return {"provider": None, "configured": False}
        if env_file is None:
            raise HTTPException(400, "找不到 .env 的位置，请直接编辑 .env 文件")
        if body.provider == "wxpusher_spt":
            values = {
                "WXPUSHER_SPT": _token(body.spt, r"SPT_[A-Za-z0-9]{8,}", "SPT 应以 SPT_ 开头")
            }
        elif body.provider == "wxpusher":
            values = {
                "WXPUSHER_APP_TOKEN": _token(
                    body.app_token, r"AT_[A-Za-z0-9]{8,}", "APP_TOKEN 应以 AT_ 开头"
                ),
                "WXPUSHER_UID": _token(body.uid, r"UID_[A-Za-z0-9]{8,}", "UID 应以 UID_ 开头"),
                "WXPUSHER_SPT": None,  # 有 SPT 时会优先走极简推送
            }
        else:
            values = {
                "PUSHPLUS_TOKEN": _token(
                    body.token, r"[A-Za-z0-9]{16,64}", "PushPlus token 格式不对"
                )
            }
        update_env_file(env_file, values)
        channel = "pushplus" if body.provider == "pushplus" else "wxpusher"
        settings.save(conn, "alerts.channel", channel)
        return {"provider": body.provider, "configured": make_notifier(channel).configured}

    @app.post("/api/alerts/test")
    def test_alert(conn: Conn) -> dict[str, Any]:
        ok, notifier = send_test(conn, cfg)
        if notifier.name == "console":
            raise HTTPException(400, "桌面通知已关闭，微信推送也没有配置，没有可以发送的通道")
        if not ok:
            raise HTTPException(502, f"{notifier.label} 发送失败")
        return {"sent": True, "channel_label": notifier.label}

    @app.post("/api/watches", status_code=201)
    def add_watch(conn: Conn, body: WatchIn) -> dict[str, Any]:
        o, d = norm(body.origin, body.dest)
        if body.ret <= body.depart:
            raise HTTPException(400, "返回日期必须晚于出发日期")
        for w in repo.list_watches(conn):
            if (w.origin, w.dest, w.depart_date, w.return_date) == (o, d, body.depart, body.ret):
                repo.update_watch(conn, w.id, target_price=body.target_price, enabled=True)
                return {"id": w.id}
        return {"id": repo.add_watch(conn, o, d, body.depart, body.ret, body.target_price)}

    @app.patch("/api/watches/{watch_id}")
    def update_watch(conn: Conn, watch_id: int, body: WatchPatch) -> dict[str, Any]:
        found = repo.update_watch(
            conn, watch_id, target_price=body.target_price, enabled=body.enabled
        )
        if not found:
            raise HTTPException(404, "这条提醒不存在")
        return {"id": watch_id}

    @app.delete("/api/watches/{watch_id}", status_code=204)
    def delete_watch(conn: Conn, watch_id: int) -> None:
        if not repo.delete_watch(conn, watch_id):
            raise HTTPException(404, "这条提醒不存在")

    @app.post("/api/manual-quote", status_code=201)
    def manual_quote(conn: Conn, body: ManualQuoteIn) -> dict[str, Any]:
        if body.ret <= body.depart:
            raise HTTPException(400, "返回日期必须晚于出发日期")
        qid = repo.add_manual_quote(
            conn,
            body.origin.upper(),
            body.dest.upper(),
            body.depart,
            body.ret,
            body.price,
            body.note,
        )
        return {"id": qid}

    @app.post("/api/collect")
    def collect_now(conn: Conn) -> dict[str, Any]:
        lock: threading.Lock = app.state.collect_lock
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "正在抓取中，请稍等")
        try:
            res = collect_and_alert(conn, cfg, source_factory)
        finally:
            lock.release()
        return {
            "run_id": res.run_id,
            "status": res.status,
            "n_quotes": res.n_quotes,
            "errors": res.errors,
        }

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):  # type: ignore[no-untyped-def]
        resp = await call_next(request)
        if not request.url.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-cache")
        return resp

    if web_dir and web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
