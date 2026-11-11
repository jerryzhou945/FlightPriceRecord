"""命令行入口：farerecord <子命令>。"""

from __future__ import annotations

import argparse
import logging
import sys

from farerecord import __version__, db
from farerecord.config import load_config


def _setup_output() -> None:
    # Windows 控制台默认编码可能不是 UTF-8
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_init_db(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    conn = db.connect(cfg.database)
    conn.close()
    print(f"数据库已就绪：{cfg.database}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from farerecord.collector import collect
    from farerecord.sources.ctrip_calendar import CtripCalendarSource

    cfg = load_config(args.config)
    if not cfg.routes:
        print("config 里没有配置航线（routes）。")
        return 2
    conn = db.connect(cfg.database)
    source = CtripCalendarSource(timeout=cfg.collector.timeout_seconds)
    try:
        res = collect(conn, cfg, source)
    finally:
        source.close()
    labels = {"ok": "成功", "partial": "部分成功", "failed": "失败"}
    print(f"抓取{labels[res.status]}：共 {res.n_quotes} 条报价（run #{res.run_id}）")
    for key, n in res.per_route.items():
        print(f"  {key}: {n} 条")
    for err in res.errors:
        print(f"  ⚠ {err}")
    if not args.no_alerts:
        _run_alerts(conn, cfg, res)
    conn.close()
    return 0 if res.status != "failed" else 1


def _run_alerts(conn, cfg, res) -> None:
    """抓取后评估提醒规则（M5 实现）。"""
    try:
        from farerecord.alerts import run_after_collect
    except ImportError:
        return
    run_after_collect(conn, cfg, res)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="farerecord", description="航价记：携程机票价格记录")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="配置文件路径（默认 config.yaml）")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init-db", help="创建/迁移数据库")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("collect", help="抓取一次所有关注航线")
    s.add_argument("--no-alerts", action="store_true", help="只抓取，不发提醒")
    s.set_defaults(func=cmd_collect)
    return p


def main(argv: list[str] | None = None) -> int:
    _setup_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
