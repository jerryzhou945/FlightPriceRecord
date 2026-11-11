"""抓取后评估提醒规则并推送。"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from farerecord import repository as repo
from farerecord.alerts.notifiers import Notifier, NotifyError, make_notifier
from farerecord.alerts.rules import (
    RULE_LABELS,
    Message,
    already_sent,
    build_price_message,
    evaluate_route,
    fail_message,
    in_quiet_hours,
)
from farerecord.config import AppConfig, RouteConfig
from farerecord.models import Trip

log = logging.getLogger(__name__)

__all__ = ["RULE_LABELS", "pending_messages", "run_after_collect", "send_message"]


def trip_airlines(
    conn: sqlite3.Connection, route: RouteConfig, trip: Trip
) -> tuple[str | None, str | None]:
    """组合的 (去程航司, 回程航司)，取最近一次抓取识别的结果。"""
    o, d = route.origin, route.dest
    return (
        repo.latest_airlines(conn, o, d).get(trip.depart_date),
        repo.latest_airlines(conn, d, o).get(trip.return_date),
    )


def pending_messages(conn: sqlite3.Connection, cfg: AppConfig, now: datetime) -> list[Message]:
    msgs: list[Message] = []
    for route in cfg.routes:
        alerts, trip, info = evaluate_route(conn, cfg, route, now.date())
        fresh = [a for a in alerts if not already_sent(conn, a)]
        if fresh and trip:
            msgs.append(
                build_price_message(route, trip, fresh, info, trip_airlines(conn, route, trip))
            )
    fm = fail_message(conn, cfg)
    if fm and not already_sent(conn, fm.alerts[0]):
        msgs.append(fm)
    return msgs


def _log(conn: sqlite3.Connection, msg: Message, channel: str, status: str, now: datetime) -> None:
    ts = now.astimezone().isoformat(timespec="seconds")
    with conn:
        for a in msg.alerts:
            conn.execute(
                "INSERT INTO alert_log "
                "(rule, dedupe_key, price, channel, status, message, sent_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (a.rule, a.dedupe_key, a.price, channel, status, msg.title, ts),
            )


def send_message(conn: sqlite3.Connection, msg: Message, notifier: Notifier, now: datetime) -> bool:
    try:
        notifier.send(msg.title, msg.content, msg.url)
    except NotifyError as exc:
        log.warning("推送失败：%s", exc)
        _log(conn, msg, notifier.name, "failed", now)
        return False
    _log(conn, msg, notifier.name, "sent", now)
    return True


def run_after_collect(
    conn: sqlite3.Connection,
    cfg: AppConfig,
    result: object | None = None,
    notifier: Notifier | None = None,
    now: datetime | None = None,
) -> list[Message]:
    """在每次抓取后调用。返回成功发送的消息。"""
    now = now or datetime.now().astimezone()
    notifier = notifier or make_notifier(cfg.alerts.channel)
    msgs = pending_messages(conn, cfg, now)
    if not msgs:
        return []
    if in_quiet_hours(now, cfg.alerts.quiet_hours):
        for m in msgs:
            _log(conn, m, notifier.name, "deferred", now)
        return []
    sent = [m for m in msgs if send_message(conn, m, notifier, now)]
    if sent:
        print(f"已推送 {len(sent)} 条提醒（{notifier.label}）")
    return sent


def recent_log(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute("SELECT * FROM alert_log ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        d["rule_label"] = RULE_LABELS.get(d["rule"], d["rule"])
        out.append(d)
    return out
