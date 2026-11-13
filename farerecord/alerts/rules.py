"""提醒规则：低于目标价、弹性窗口历史新低、24h 大幅降价、连续抓取失败。

去重：同一规则 + 同一 dedupe_key 只要成功发送过一次就不再发。
免打扰：处于免打扰时段时记录为 deferred，下次抓取若条件仍成立会再发送。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from farerecord import repository as repo
from farerecord.airlines import airline_text
from farerecord.analysis import calibration
from farerecord.analysis.flex import FlexQuery
from farerecord.analysis.history import flex_low_history
from farerecord.config import AppConfig, RouteConfig
from farerecord.models import OnewayQuote, PriceWatch, Trip

RULE_LABELS = {
    "watch": "到达提醒价",
    "target": "低于目标价",
    "new_low": "历史新低",
    "drop": "大幅降价",
    "fail": "抓取失败",
    "test": "测试消息",
}
_PRIORITY = ["watch", "new_low", "target", "drop"]


@dataclass
class Alert:
    rule: str
    dedupe_key: str
    price: float | None = None


@dataclass
class Message:
    title: str
    content: str
    url: str | None
    alerts: list[Alert]


def _wd(d: date) -> str:
    return "周" + "一二三四五六日"[d.weekday()]


def route_query(route: RouteConfig, today: date) -> FlexQuery | None:
    f = route.flex
    if not f.start or not f.end or f.end < today:
        return None
    return FlexQuery(max(f.start, today), f.end, f.min_days, f.max_days, f.ideal_days)


def in_quiet_hours(now: datetime, quiet: tuple[time, time] | None) -> bool:
    if not quiet:
        return False
    start, end = quiet
    t = now.time()
    return start <= t < end if start <= end else (t >= start or t < end)


def evaluate_route(
    conn: sqlite3.Connection, cfg: AppConfig, route: RouteConfig, today: date
) -> tuple[list[Alert], Trip | None, dict]:
    """返回 (触发的提醒, 当前最低组合, 附加信息)。"""
    q = route_query(route, today)
    if q is None:
        return [], None, {}
    ratio = calibration.current(conn, route.origin, route.dest).ratio
    points = flex_low_history(conn, route.origin, route.dest, q, ratio)
    if not points:
        return [], None, {}
    cur = points[-1].best
    price = cur.best_price
    # 去重键用不含校准的价格：校准比例慢慢变化时，同一个真实报价不会被当成新的再提醒一遍
    raw = cur.raw_price
    trip_key = f"{route.key}:{cur.depart_date}:{cur.return_date}:{raw:.0f}"
    alerts: list[Alert] = []
    info: dict = {}

    if route.target_price and price <= route.target_price:
        alerts.append(Alert("target", trip_key, price))

    earlier = [p.best.best_price for p in points[:-1]]
    if cfg.alerts.new_low and earlier and price < min(earlier):
        info["prev_low"] = min(earlier)
        alerts.append(Alert("new_low", f"{route.key}:{raw:.0f}", price))

    pct = cfg.alerts.drop_percent
    if pct > 0 and len(points) >= 2:
        now_t = datetime.fromisoformat(points[-1].observed_at)
        before = [
            p
            for p in points[:-1]
            if datetime.fromisoformat(p.observed_at) <= now_t - timedelta(hours=24)
        ]
        if before:
            ref = before[-1].best.best_price
            if ref > 0 and (ref - price) / ref * 100 >= pct:
                info["drop_from"] = ref
                alerts.append(Alert("drop", f"{route.key}:{now_t.date()}:{raw:.0f}", price))
    return alerts, cur, info


def watch_trip(conn: sqlite3.Connection, watch: PriceWatch) -> Trip | None:
    """到价提醒对应组合的当前价格；任意一段没有报价返回 None。"""
    o, d = watch.origin, watch.dest
    out = repo.latest_oneway(conn, o, d).get(watch.depart_date)
    back = repo.latest_oneway(conn, d, o).get(watch.return_date)
    if not out or not back:
        return None
    rt = repo.latest_roundtrip(conn, o, d).get((watch.depart_date, watch.return_date))
    ratio = calibration.current(conn, o, d).ratio
    return Trip(watch.depart_date, watch.return_date, out, back, rt, ratio)


def evaluate_watch(
    conn: sqlite3.Connection, watch: PriceWatch, today: date
) -> tuple[Alert | None, Trip | None]:
    """返回 (触发的提醒或 None, 当前组合价格)。已出发或被关闭的提醒不触发。"""
    if not watch.enabled or watch.depart_date < today:
        return None, None
    trip = watch_trip(conn, watch)
    if trip is None or trip.best_price > watch.target_price:
        return None, trip
    return Alert("watch", f"{watch.id}:{trip.raw_price:.0f}", trip.best_price), trip


def _airline_suffix(quote: OnewayQuote | None) -> str:
    text = airline_text(quote)
    return f" · {text}" if text else ""


def build_price_message(
    route: RouteConfig,
    trip: Trip,
    alerts: list[Alert],
    info: dict,
    airlines: tuple[OnewayQuote | None, OnewayQuote | None] = (None, None),
) -> Message:
    """airlines：(去程, 回程) 带航司信息的报价，没有为 None。"""
    from farerecord.api.app import ctrip_url

    rules = sorted({a.rule for a in alerts}, key=_PRIORITY.index)
    head = RULE_LABELS[rules[0]]
    price = trip.best_price
    title = f"{head} ¥{price:,.0f} {trip.depart_date:%m/%d}→{trip.return_date:%m/%d}"
    url = ctrip_url(route.origin, route.dest, trip.depart_date, trip.return_date)
    lines = [
        f"### 🔻 {route.name} {head} ¥{price:,.0f}",
        "",
        f"- {trip.depart_date:%m/%d} {_wd(trip.depart_date)} → {trip.return_date:%m/%d}"
        f" {_wd(trip.return_date)} · {trip.days} 天"
        + (f" · 目标价 ¥{route.target_price:,.0f}" if route.target_price else ""),
    ]
    if trip.rt_price:
        lines.append(f"- 携程往返实价 ¥{trip.rt_price:,.0f}")
    elif trip.ratio < 1:
        lines.append(f"- 往返预估 ¥{trip.estimate:,.0f}，分开买两张单程 ¥{trip.price:,.0f}")
    lines += [
        f"- 去程单程 ¥{trip.out_price:,.0f}{_airline_suffix(airlines[0])}",
        f"- 回程单程 ¥{trip.ret_price:,.0f}{_airline_suffix(airlines[1])}",
    ]
    if "prev_low" in info:
        lines.append(f"- 此前最低 ¥{info['prev_low']:,.0f}")
    if "drop_from" in info:
        lines.append(f"- 24 小时前 ¥{info['drop_from']:,.0f}")
    lines += [
        "",
        "触发：" + "、".join(RULE_LABELS[r] for r in rules),
        "价格是按单程价推算的预估，以携程实际报价为准。",
        f"[去携程核价]({url})",
    ]
    return Message(title, "\n".join(lines), url, alerts)


def already_sent(conn: sqlite3.Connection, alert: Alert) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alert_log WHERE rule=? AND dedupe_key=? AND status='sent' LIMIT 1",
        (alert.rule, alert.dedupe_key),
    ).fetchone()
    return row is not None


def fail_message(conn: sqlite3.Connection, cfg: AppConfig) -> Message | None:
    n = cfg.alerts.fail_streak
    if n <= 0:
        return None
    streak = repo.fail_streak(conn)
    if streak < n:
        return None
    runs = repo.recent_runs(conn, streak)
    first_failed = runs[-1]["id"]
    err = (runs[0].get("error") or "未知错误").splitlines()[0]
    content = (
        f"### ⚠️ 航价记连续 {streak} 次抓取失败\n\n最近错误：{err}\n\n"
        "可能是携程接口变化或网络问题。请运行 `farerecord collect` 查看详情。"
    )
    return Message(
        f"连续 {streak} 次抓取失败", content, None, [Alert("fail", f"run:{first_failed}")]
    )
