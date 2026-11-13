"""从数据库组装价格历史：单个组合的走势、弹性窗口最低价的走势。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from farerecord import repository as repo
from farerecord.analysis.flex import FlexQuery
from farerecord.models import Trip


def combo_history(
    conn: sqlite3.Connection, origin: str, dest: str, depart: date, ret: date, ratio: float = 1.0
) -> list[tuple[str, float]]:
    """某个去回组合的往返预估历史 (时间, (去程+回程)×校准比例)。"""
    return [
        (t, Trip(depart, ret, o, r, ratio=ratio).estimate)
        for t, o, r in repo.combo_series(conn, origin, dest, depart, ret)
    ]


@dataclass(frozen=True)
class FlexPoint:
    observed_at: str
    best: Trip


def flex_low_history(
    conn: sqlite3.Connection, origin: str, dest: str, query: FlexQuery, ratio: float = 1.0
) -> list[FlexPoint]:
    """每次抓取时，弹性窗口内的最低组合。用于“窗口最低价走势”和历史新低提醒。

    抓取次数多了以后（每 30 分钟一次、保留一个月就有上千次）逐次查库太慢，
    所以两个方向各用一条 SQL 取出窗口内的全部报价，在内存里按抓取分组。
    同价时的取舍和 flex.search 一致：更接近理想天数的优先，再选更早出发的。
    """
    stays = query.stays
    ideal = query.ideal_days
    out_runs = repo.oneway_by_run(conn, origin, dest, query.start, query.end)
    back_runs = repo.oneway_by_run(
        conn,
        dest,
        origin,
        query.start + timedelta(days=stays[0]),
        query.end + timedelta(days=stays[-1]),
    )
    # 每个出发日对应的 (天数, 返回日)，只算一遍
    returns = {d: [(s, d + timedelta(days=s)) for s in stays] for d in query.depart_dates}
    points = []
    for run_id, observed_at in repo.runs_with_route(conn, origin, dest):
        out, back = out_runs.get(run_id), back_runs.get(run_id)
        if not out or not back:
            continue
        best: tuple[float, int, date, date, float, float] | None = None
        for d, o in out.items():
            for s, r in returns.get(d, ()):
                b = back.get(r)
                if not b:
                    continue
                key = (o + b, abs(s - ideal) if ideal else 0, d, r, o, b)
                if best is None or key[:3] < best[:3]:
                    best = key
        if best:
            _, _, d, r, o, b = best
            points.append(FlexPoint(observed_at, Trip(d, r, o, b, ratio=ratio)))
    return points
