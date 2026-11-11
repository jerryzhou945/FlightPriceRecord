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
    from farerecord.collector import CollectionNotDue, collect_and_alert

    cfg = load_config(args.config)
    if not cfg.routes:
        print("config 里没有配置航线（routes）。")
        return 2
    conn = db.connect(cfg.database)
    try:
        res = collect_and_alert(conn, cfg, if_due=args.if_due, alerts=not args.no_alerts)
    except CollectionNotDue:
        print(f"距离上次抓取不到 {cfg.collector.interval_minutes} 分钟，跳过。")
        return 0
    finally:
        conn.close()
    labels = {"ok": "成功", "partial": "部分成功", "failed": "失败"}
    print(f"抓取{labels[res.status]}：共 {res.n_quotes} 条报价（run #{res.run_id}）")
    for key, n in res.per_route.items():
        print(f"  {key}: {n} 条")
    for err in res.errors:
        print(f"  ⚠ {err}")
    return 0 if res.status != "failed" else 1


def _route_or_default(cfg, spec: str | None):
    if spec:
        origin, _, dest = spec.upper().partition("-")
        route = cfg.route(origin, dest)
        if route is None:
            raise SystemExit(f"config 里没有航线 {spec}")
        return route
    if not cfg.routes:
        raise SystemExit("config 里没有配置航线（routes）。")
    return cfg.routes[0]


def _wd(d) -> str:
    return "周" + "一二三四五六日"[d.weekday()]


def cmd_flex(args: argparse.Namespace) -> int:
    from datetime import date

    from farerecord import repository as repo
    from farerecord.analysis.flex import FlexQuery, search, suggestions

    cfg = load_config(args.config)
    route = _route_or_default(cfg, args.route)
    f = route.flex
    start = date.fromisoformat(args.start) if args.start else f.start
    end = date.fromisoformat(args.end) if args.end else f.end
    if not start or not end:
        print("请用 --start/--end 指定出发窗口，或在 config 里配置 flex.start/end。")
        return 2
    q = FlexQuery(
        start,
        end,
        args.min or f.min_days,
        args.max or f.max_days,
        args.ideal or f.ideal_days,
    )
    conn = db.connect(cfg.database)
    out = repo.latest_oneway(conn, route.origin, route.dest)
    back = repo.latest_oneway(conn, route.dest, route.origin)
    rt = repo.latest_roundtrip(conn, route.origin, route.dest)
    observed = repo.latest_observed_at(conn, route.origin, route.dest)
    conn.close()
    if not out or not back:
        print("还没有数据，先运行 farerecord collect。")
        return 1
    res = search(out, back, q, rt)
    print(f"{route.name}  出发 {start:%m/%d}–{end:%m/%d}  游玩 {q.min_days}–{q.max_days} 天")
    print(f"数据时间：{observed}（UTC） · 价格为去程单程 + 回程单程估价")
    print()
    print(f"{'出发':<10}{'返回':<10}{'天数':>4}{'去程':>9}{'回程':>9}{'合计':>9}")
    for t in res.top(args.top):
        rt_mark = f"  往返实价 ¥{t.rt_price:,.0f}" if t.rt_price else ""
        print(
            f"{t.depart_date:%m/%d} {_wd(t.depart_date)}  "
            f"{t.return_date:%m/%d} {_wd(t.return_date)}  {t.days:>3}"
            f"{t.out_price:>9,.0f}{t.ret_price:>9,.0f}{t.price:>9,.0f}{rt_mark}"
        )
    print()
    for tip in suggestions(res):
        print("· " + tip)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import threading
    import webbrowser

    import uvicorn

    from farerecord.api.app import create_app

    cfg = load_config(args.config)
    url = f"http://{args.host}:{args.port}"
    print(f"航价记已启动：{url}  （Ctrl+C 退出）")
    if args.open:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from farerecord.desktop import DesktopUnavailable, run

    try:
        return run(load_config(args.config))
    except DesktopUnavailable as exc:
        print(exc)
        return 2


def cmd_notify_test(args: argparse.Namespace) -> int:
    from farerecord.alerts import send_test

    cfg = load_config(args.config)
    conn = db.connect(cfg.database)
    ok, n = send_test(conn, cfg)
    conn.close()
    if n.name == "console":
        print("没有可用的推送通道：桌面通知已关闭，微信密钥也没有配置（见 .env.example）。")
        return 2
    print(f"已发送到 {n.label}" if ok else "发送失败，查看上面的错误信息。")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="farerecord", description="航价记：携程机票价格记录")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="配置文件路径（默认 config.yaml）")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init-db", help="创建/迁移数据库")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("collect", help="抓取一次所有关注航线")
    s.add_argument("--no-alerts", action="store_true", help="只抓取，不发提醒")
    s.add_argument(
        "--if-due",
        action="store_true",
        help="距离上次抓取不到一个间隔（collector.interval_minutes）就跳过，定时任务用",
    )
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("flex", help="弹性行程：找窗口内最便宜的去回组合")
    s.add_argument("--route", help="航线，如 CHI-SHA（默认第一条）")
    s.add_argument("--start", help="出发窗口开始 YYYY-MM-DD")
    s.add_argument("--end", help="出发窗口结束 YYYY-MM-DD")
    s.add_argument("--min", type=int, help="最短天数")
    s.add_argument("--max", type=int, help="最长天数")
    s.add_argument("--ideal", type=int, help="理想天数")
    s.add_argument("--top", type=int, default=10, help="显示前 N 个组合")
    s.set_defaults(func=cmd_flex)

    s = sub.add_parser("notify-test", help="发送一条测试提醒")
    s.set_defaults(func=cmd_notify_test)

    s = sub.add_parser("app", help="打开桌面窗口（开着时按间隔自动抓取）")
    s.set_defaults(func=cmd_app)

    s = sub.add_parser("serve", help="启动网页看板（浏览器里看）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    s.set_defaults(func=cmd_serve)
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
