"""FastAPI 应用：/api/* JSON 接口 + 挂载 web/ 静态前端。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from farerecord import db
from farerecord import repository as repo
from farerecord.analysis.flex import FlexQuery, search, suggestions
from farerecord.analysis.history import combo_history, flex_low_history
from farerecord.analysis.stats import PriceStats, compute
from farerecord.config import ROOT, AppConfig, load_config
from farerecord.models import Trip
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


def trip_json(t: Trip, origin: str, dest: str) -> dict[str, Any]:
    return {
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


def create_app(
    cfg: AppConfig | None = None,
    source_factory: Callable[[AppConfig], PriceSource] | None = None,
    web_dir: Path | None = WEB_DIR,
) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="航价记", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.cfg = cfg

    def norm(origin: str, dest: str) -> tuple[str, str]:
        o, d = origin.upper(), dest.upper()
        if len(o) != 3 or len(d) != 3 or not (o + d).isalpha():
            raise HTTPException(400, "城市码应为 3 个字母，如 CHI、SHA")
        return o, d

    @app.get("/api/routes")
    def routes() -> list[dict[str, Any]]:
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
            for r in cfg.routes
        ]

    @app.get("/api/calendar")
    def calendar(
        conn: Conn,
        origin: str,
        dest: str,
        month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$")],
    ) -> dict[str, Any]:
        o, d = norm(origin, dest)
        prices = {
            k: v for k, v in repo.latest_oneway(conn, o, d).items() if k.isoformat()[:7] == month
        }
        tier = _tiers(list(prices.values()))
        days = [
            {"date": k.isoformat(), "price": v, "tier": tier(v), "ctrip_url": ctrip_url(o, d, k)}
            for k, v in sorted(prices.items())
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
        dates = q.depart_dates
        stays = q.stays
        di = {x: i for i, x in enumerate(dates)}
        si = {x: i for i, x in enumerate(stays)}
        cells = [[di[t.depart_date], si[t.days], t.best_price] for t in res.trips]
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
            "best": trip_json(res.best, o, d) if res.best else None,
            "best_ideal": trip_json(res.best_ideal, o, d) if res.best_ideal else None,
            "ideal_saving": res.ideal_saving,
            "top": [trip_json(t, o, d) for t in res.top(top)],
            "by_days": [{"days": s, **trip_json(t, o, d)} for s, t in sorted(res.by_days.items())],
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
        if ret:
            series = combo_history(conn, o, d, depart, ret)
            manual = repo.manual_quotes(conn, o, d, depart, ret)
        else:
            series = repo.oneway_series(conn, o, d, depart)
            manual = []
        return {
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
        return {
            "runs": runs,
            "fail_streak": repo.fail_streak(conn),
            "last_ok_at": last_ok["finished_at"] if last_ok else None,
            "total_quotes": total,
            "n_runs": conn.execute("SELECT COUNT(*) FROM fetch_run").fetchone()[0],
        }

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
        from farerecord.collector import collect

        if source_factory is None:
            from farerecord.sources.ctrip_calendar import CtripCalendarSource

            src: PriceSource = CtripCalendarSource(timeout=cfg.collector.timeout_seconds)
        else:
            src = source_factory(cfg)
        try:
            res = collect(conn, cfg, src)
        finally:
            close = getattr(src, "close", None)
            if close:
                close()
        try:
            from farerecord.alerts import run_after_collect
        except ImportError:
            pass
        else:
            run_after_collect(conn, cfg, res)
        return {
            "run_id": res.run_id,
            "status": res.status,
            "n_quotes": res.n_quotes,
            "errors": res.errors,
        }

    if web_dir and web_dir.is_dir():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
