"""读取 config.yaml 与 .env。缺少 config.yaml 时回退到 config.example.yaml。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

import yaml

from farerecord.airlines import DEFAULT_AIRLINES, normalize_codes

ROOT = Path(__file__).resolve().parent.parent
MIN_INTERVAL_MINUTES = 15  # 定时抓取的最短间隔，避免对携程造成压力


@dataclass
class FlexConfig:
    start: date | None = None
    end: date | None = None
    min_days: int = 25
    max_days: int = 35
    ideal_days: int = 30


@dataclass
class RouteConfig:
    name: str
    origin: str
    dest: str
    flex: FlexConfig = field(default_factory=FlexConfig)
    target_price: float | None = None

    @property
    def key(self) -> str:
        return f"{self.origin}-{self.dest}"


@dataclass
class AlertConfig:
    channel: str = "console"  # 微信通道：wxpusher | pushplus | console（不推微信）
    desktop: bool = True  # Windows 桌面通知
    new_low: bool = True
    drop_percent: float = 5.0
    fail_streak: int = 2
    quiet_hours: tuple[time, time] | None = None


@dataclass
class CollectorConfig:
    request_interval_seconds: float = 3.0
    timeout_seconds: float = 25.0
    interval_minutes: int = 30  # 定时抓取间隔；不低于 MIN_INTERVAL_MINUTES
    airlines: list[str] = field(
        default_factory=lambda: list(DEFAULT_AIRLINES)
    )  # 空列表 = 不识别航司
    airline_lookups: int = 8  # 每次识别、每个方向最多额外查几家航司
    airline_refresh_minutes: int = 120  # 航司多久重新识别一次（抓取可以比它频繁）
    retention_days: int = 30  # 只保留最近这么多天的抓取；0 = 不清理


@dataclass
class AppConfig:
    database: Path
    routes: list[RouteConfig]
    alerts: AlertConfig = field(default_factory=AlertConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    source_file: Path | None = None

    @property
    def env_file(self) -> Path | None:
        """密钥所在的 .env（和配置文件同目录）。"""
        return self.source_file.parent / ".env" if self.source_file else None

    def route(self, origin: str, dest: str) -> RouteConfig | None:
        for r in self.routes:
            if r.origin == origin.upper() and r.dest == dest.upper():
                return r
        return None


def load_env(path: Path) -> None:
    """简单解析 .env（KEY=VALUE），不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if key.strip() and value:
            os.environ.setdefault(key.strip(), value)


def _date(v: Any) -> date | None:
    if v in (None, ""):
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def _hhmm(s: str) -> time:
    h, m = s.strip().split(":")
    return time(int(h), int(m))


def parse_quiet_hours(v: Any) -> tuple[time, time] | None:
    if not v:
        return None
    start, end = str(v).split("-")
    return _hhmm(start), _hhmm(end)


def clamp_interval(value: Any) -> int:
    """抓取间隔（分钟）：缺省 30，最短 MIN_INTERVAL_MINUTES，最长一天。"""
    return min(1440, max(MIN_INTERVAL_MINUTES, int(value or 30)))


def parse_config(data: dict[str, Any], root: Path = ROOT) -> AppConfig:
    routes = []
    for r in data.get("routes") or []:
        flex = r.get("flex") or {}
        routes.append(
            RouteConfig(
                name=str(r.get("name") or f"{r['origin']} ⇄ {r['dest']}"),
                origin=str(r["origin"]).upper(),
                dest=str(r["dest"]).upper(),
                flex=FlexConfig(
                    start=_date(flex.get("start")),
                    end=_date(flex.get("end")),
                    min_days=int(flex.get("min_days", 25)),
                    max_days=int(flex.get("max_days", 35)),
                    ideal_days=int(flex.get("ideal_days", 30)),
                ),
                target_price=float(r["target_price"]) if r.get("target_price") else None,
            )
        )
    a = data.get("alerts") or {}
    c = data.get("collector") or {}
    db_path = Path(data.get("database") or "data/farerecord.db")
    return AppConfig(
        database=db_path if db_path.is_absolute() else root / db_path,
        routes=routes,
        alerts=AlertConfig(
            channel=str(a.get("channel", "console")),
            desktop=bool(a.get("desktop", True)),
            new_low=bool(a.get("new_low", True)),
            drop_percent=float(a.get("drop_percent", 5) or 0),
            fail_streak=int(a.get("fail_streak", 2) or 0),
            quiet_hours=parse_quiet_hours(a.get("quiet_hours")),
        ),
        collector=CollectorConfig(
            request_interval_seconds=float(c.get("request_interval_seconds", 3)),
            timeout_seconds=float(c.get("timeout_seconds", 25)),
            interval_minutes=clamp_interval(c.get("interval_minutes")),
            airlines=normalize_codes(c.get("airlines")),
            airline_lookups=max(0, min(20, int(c.get("airline_lookups", 8)))),
            airline_refresh_minutes=max(0, int(c.get("airline_refresh_minutes", 120))),
            retention_days=max(0, int(c.get("retention_days", 30))),
        ),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    if path:
        file = Path(path)
    elif (ROOT / "config.yaml").is_file():
        file = ROOT / "config.yaml"
    else:
        file = ROOT / "config.example.yaml"
    load_env(file.parent / ".env")
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    cfg = parse_config(data, root=file.parent)
    cfg.source_file = file
    return cfg
