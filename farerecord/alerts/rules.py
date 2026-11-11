"""提醒规则：低于目标价、弹性窗口历史新低、24h 大幅降价、连续抓取失败。

去重：同一规则 + 同一 dedupe_key 只要成功发送过一次就不再发。
免打扰：处于免打扰时段时记录为 deferred，下次抓取若条件仍成立会再发送。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from farerecord import repository as repo
from farerecord.airlines import airline_name
from farerecord.analysis.flex import FlexQuery
from farerecord.analysis.history import flex_low_history
from farerecord.config import AppConfig, RouteConfig
from farerecord.models import Trip

RULE_LABELS = {
    "target": "低于目标价",
    "new_low": "历史新低",
    "drop": "大幅降价",
    "fail": "抓取失败",
    "test": "测试消息",
}
_PRIORITY = ["new_low", "target", "drop"]


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
    points = flex_low_history(conn, route.origin, route.dest, q)
    if not points:
        return [], None, {}
    cur = points[-1].best
    price = cur.best_price
    trip_key = f"{route.key}:{cur.depart_date}:{cur.return_date}:{price:.0f}"
    alerts: list[Alert] = []
    info: dict = {}

    if route.target_price and price <= route.target_price:
        alerts.append(Alert("target", trip_key, price))

    earlier = [p.best.best_price for p in points[:-1]]
    if cfg.alerts.new_low and earlier and price < min(earlier):
        info["prev_low"] = min(earlier)
        alerts.append(Alert("new_low", f"{route.key}:{price:.0f}", price))

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
                alerts.append(Alert("drop", f"{route.key}:{now_t.date()}:{price:.0f}", price))
    return alerts, cur, info


def _airline_suffix(code: str | None) -> str:
    return f" · {airline_name(code)} {code}" if code else ""


def build_price_message(
    route: RouteConfig,
    trip: Trip,
    alerts: list[Alert],
    info: dict,
    airlines: tuple[str | None, str | None] = (None, None),
) -> Message:
    """airlines：(去程航司, 回程航司) 二字码，识别不出为 None。"""
    from farerecord.api.app import ctrip_url

    rules = sorted({a.rule for a in alerts}, key=_PRIORITY.index)
    head = RULE_LABELS[rules[0]]
    price = trip.best_price
    title = f"{head} ¥{price:,.0f} {trip.depart_date:%m/%d}→{trip.return_date:%m/%d}"
    url = ctrip_url(route.origin, route.dest, trip.depart_date, trip.return_date)
    lines = [
        f"### 🔻 {route.name} {head} ¥{price:,.0f}",
        "",
        f"- 去程 {trip.depart_date:%m/%d} {_wd(trip.depart_date)} {route.origin}→{route.dest}"
        f" ¥{trip.out_price:,.0f}{_airline_suffix(airlines[0])}",
        f"- 回程 {trip.return_date:%m/%d} {_wd(trip.return_date)} {route.dest}→{route.origin}"
        f" ¥{trip.ret_price:,.0f}{_airline_suffix(airlines[1])}",
        f"- {trip.days} 天"
        + (f" · 目标价 ¥{route.target_price:,.0f}" if route.target_price else ""),
    ]
    if trip.rt_price:
        lines.append(f"- 携程往返实价 ¥{trip.rt_price:,.0f}")
    if "prev_low" in info:
        lines.append(f"- 此前最低 ¥{info['prev_low']:,.0f}")
    if "drop_from" in info:
        lines.append(f"- 24 小时前 ¥{info['drop_from']:,.0f}")
    lines += [
        "",
        "触发：" + "、".join(RULE_LABELS[r] for r in rules),
        "单程组合估价，以携程实际报价为准。",
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
