"""FastAPI 应用：/api/* JSON 接口 + 挂载 web/ 静态前端。"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from farerecord import db, settings
from farerecord import repository as repo
from farerecord.airlines import airline_json
from farerecord.alerts import recent_log, send_test
from farerecord.alerts.notifiers import build_notifier, make_notifier
from farerecord.alerts.rules import watch_trip
from farerecord.analysis.flex import FlexQuery, search, suggestions
from farerecord.analysis.history import flex_low_history
from farerecord.analysis.stats import PriceStats, compute
from farerecord.collector import collect_and_alert, next_due_at
from farerecord.config import ROOT, AppConfig, load_config, parse_quiet_hours
from farerecord.models import PriceWatch, Trip
from farerecord.settings import effective_config
from farerecord.sources.base import PriceSource

WEB_DIR = ROOT / "web"


def ctrip_url(origin: str, dest: str, depart: date, ret: date | None = None) -> str:
    o, d = origin.lower(), dest.lower()
    if ret:
        return (
            f"https://flights.ctrip.com/online/list/round-{o}-{d}"
            f"?depdate={depart}_{ret}&cabin=y_s&adult=1&child=0&infant=0"
        )
    return (
        f"https://flights.ctrip.com/online/list/oneway-{o}-{d}"
        f"?depdate={depart}&cabin=y_s&adult=1&child=0&infant=0"
    )


Airlines = tuple[dict[date, str], dict[date, str]]  # (去程, 回程) 日期 → 航司二字码


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
        "price": t.price,
        "rt_price": t.rt_price,
        "best_price": t.best_price,
        "ctrip_url": ctrip_url(origin, dest, t.depart_date, t.return_date),
    }


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
) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="航价记", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.cfg = cfg
    app.state.collect_lock = threading.Lock()  # 手动抓取和定时抓取共用，避免同时请求携程

    def norm(origin: str, dest: str) -> tuple[str, str]:
        o, d = origin.upper(), dest.upper()
        if len(o) != 3 or len(d) != 3 or not (o + d).isalpha():
            raise HTTPException(400, "城市码应为 3 个字母，如 CHI、SHA")
        return o, d

    @app.get("/api/routes")
    def routes(conn: Conn) -> list[dict[str, Any]]:
        current = effective_config(conn, cfg)
        return [
            {
                "key": r.key,
                "name": r.name,
                "origin": r.origin,
                "dest": r.dest,
                "target_price": r.target_price,
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

    @app.get("/api/calendar")
    def calendar(
        conn: Conn,
        origin: str,
        dest: str,
        month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    ) -> dict[str, Any]:
        o, d = norm(origin, dest)
        detail = {
            k: v
            for k, v in repo.latest_oneway_detail(conn, o, d).items()
            if k.isoformat()[:7] == month
        }
        tier = _tiers([p for p, _ in detail.values()])
        days = [
            {
                "date": k.isoformat(),
                "price": p,
                "tier": tier(p),
                "airline": airline_json(a),
                "ctrip_url": ctrip_url(o, d, k),
            }
            for k, (p, a) in sorted(detail.items())
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
        res = search(out, back, q, repo.latest_roundtrip(conn, o, d))
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
                for p in flex_low_history(conn, o, d, q)
            ]
        return {
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
        if ret:
            rows = repo.combo_series(conn, o, d, depart, ret)
            series = [(t, a + b) for t, a, b in rows]
            legs = {"out": rows[-1][1], "ret": rows[-1][2]} if rows else None
            manual = repo.manual_quotes(conn, o, d, depart, ret)
        else:
            series = repo.oneway_series(conn, o, d, depart)
            manual = []
        return {
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
            "ctrip_url": ctrip_url(o, d, depart, ret),
        }

    @app.get("/api/status")
    def status(conn: Conn) -> dict[str, Any]:
        runs = repo.recent_runs(conn, 10)
        last_ok = next((r for r in runs if r["status"] in ("ok", "partial")), None)
        total = conn.execute("SELECT COUNT(*) FROM oneway_quote").fetchone()[0]
        interval = cfg.collector.interval_minutes
        due = next_due_at(conn, interval)
        return {
            "interval_minutes": interval,
            "next_due_at": due.isoformat() if due else None,
            "collecting": app.state.collect_lock.locked(),
            "runs": runs,
            "fail_streak": repo.fail_streak(conn),
            "last_ok_at": last_ok["finished_at"] if last_ok else None,
            "total_quotes": total,
            "n_runs": conn.execute("SELECT COUNT(*) FROM fetch_run").fetchone()[0],
        }

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
            route = cfg.route(o, d)
            if route is None:
                raise HTTPException(404, f"没有配置航线 {o}-{d}")
            settings.save(conn, settings.TARGET_PREFIX + route.key, sent["target_price"])
        for key in settings.ALERT_KEYS:
            if key in sent and sent[key] is not None:
                settings.save(conn, f"alerts.{key}", sent[key])
        return alerts(conn, body.origin, body.dest)["settings"]

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
