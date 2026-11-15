"""界面里改的设置，存在数据库 setting 表，覆盖 config.yaml 里的同名项。

config.yaml 仍然是默认值来源（也方便手改）；界面上的开关、目标价写到这里，
这样不用改写用户的 YAML 文件。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

from farerecord.cities import route_name
from farerecord.config import AppConfig, FlexConfig, RouteConfig, clamp_interval, parse_quiet_hours

# 界面可改的提醒设置：键 → 允许的类型
ALERT_KEYS: dict[str, type | tuple[type, ...]] = {
    "channel": str,
    "desktop": bool,
    "new_low": bool,
    "drop_percent": (int, float),
    "fail_streak": int,
    "quiet_hours": str,
}
TARGET_PREFIX = "target_price:"
INTERVAL_KEY = "collector.interval_minutes"
EXTRA_ROUTES_KEY = "routes.extra"  # 界面里添加的航线：[{"origin", "dest"}]
MAX_ROUTES = 6  # 每条航线每次抓取约 20 个请求、1 分钟左右，航线太多一次抓取会很久


def load(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        r["key"]: json.loads(r["value"]) for r in conn.execute("SELECT key, value FROM setting")
    }


def save(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with conn:
        conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def extra_routes(stored: dict[str, Any], cfg: AppConfig) -> list[RouteConfig]:
    """界面里添加的航线。弹性窗口沿用第一条航线的设置；和 config.yaml 重复的忽略。"""
    base = cfg.routes[0].flex if cfg.routes else FlexConfig()
    seen = {r.key for r in cfg.routes}
    out = []
    for item in stored.get(EXTRA_ROUTES_KEY) or []:
        o, d = str(item.get("origin", "")).upper(), str(item.get("dest", "")).upper()
        if len(o) != 3 or len(d) != 3 or o == d or f"{o}-{d}" in seen:
            continue
        seen.add(f"{o}-{d}")
        out.append(RouteConfig(name=route_name(o, d), origin=o, dest=d, flex=replace(base)))
    return out


def add_route(conn: sqlite3.Connection, cfg: AppConfig, origin: str, dest: str) -> RouteConfig:
    """添加一条航线；已存在或超过上限时抛 ValueError（信息可直接给用户看）。"""
    current = effective_config(conn, cfg)
    if current.route(origin, dest):
        raise ValueError("这条航线已经在监测了")
    if len(current.routes) >= MAX_ROUTES:
        raise ValueError(f"最多同时监测 {MAX_ROUTES} 条航线，先删掉一条")
    items = list(load(conn).get(EXTRA_ROUTES_KEY) or [])
    items.append({"origin": origin.upper(), "dest": dest.upper()})
    save(conn, EXTRA_ROUTES_KEY, items)
    route = effective_config(conn, cfg).route(origin, dest)
    assert route is not None
    return route


def remove_route(conn: sqlite3.Connection, origin: str, dest: str) -> bool:
    """删掉界面里添加的航线（config.yaml 里的删不了）；返回是否删掉了。"""
    o, d = origin.upper(), dest.upper()
    items = list(load(conn).get(EXTRA_ROUTES_KEY) or [])
    kept = [i for i in items if (i.get("origin"), i.get("dest")) != (o, d)]
    if len(kept) == len(items):
        return False
    save(conn, EXTRA_ROUTES_KEY, kept)
    return True


def effective_config(conn: sqlite3.Connection, cfg: AppConfig) -> AppConfig:
    """config.yaml 的配置叠加上界面里保存的设置。"""
    stored = load(conn)
    if not stored:
        return cfg
    changes: dict[str, Any] = {}
    for key in ALERT_KEYS:
        name = f"alerts.{key}"
        if name not in stored:
            continue
        value = stored[name]
        changes[key] = parse_quiet_hours(value) if key == "quiet_hours" else value
    routes = [
        replace(r, target_price=stored[TARGET_PREFIX + r.key])
        if TARGET_PREFIX + r.key in stored
        else r
        for r in cfg.routes + extra_routes(stored, cfg)
    ]
    collector = cfg.collector
    if INTERVAL_KEY in stored:
        collector = replace(collector, interval_minutes=clamp_interval(stored[INTERVAL_KEY]))
    return replace(cfg, alerts=replace(cfg.alerts, **changes), routes=routes, collector=collector)
