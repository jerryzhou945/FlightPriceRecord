"""界面里改的设置，存在数据库 setting 表，覆盖 config.yaml 里的同名项。

config.yaml 仍然是默认值来源（也方便手改）；界面上的开关、目标价写到这里，
这样不用改写用户的 YAML 文件。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

from farerecord.config import AppConfig, parse_quiet_hours

# 界面可改的提醒设置：键 → 允许的类型
ALERT_KEYS: dict[str, type | tuple[type, ...]] = {
    "desktop": bool,
    "new_low": bool,
    "drop_percent": (int, float),
    "fail_streak": int,
    "quiet_hours": str,
}
TARGET_PREFIX = "target_price:"


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
        for r in cfg.routes
    ]
    return replace(cfg, alerts=replace(cfg.alerts, **changes), routes=routes)
