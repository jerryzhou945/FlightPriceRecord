"""从数据库组装价格历史：单个组合的走势、弹性窗口最低价的走势。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from farerecord import repository as repo
from farerecord.analysis.flex import FlexQuery, search
from farerecord.models import Trip


def combo_history(
    conn: sqlite3.Connection, origin: str, dest: str, depart: date, ret: date
) -> list[tuple[str, float]]:
    """某个去回组合的估价历史 (时间, 去程+回程)。"""
    return [(t, o + r) for t, o, r in repo.combo_series(conn, origin, dest, depart, ret)]


@dataclass(frozen=True)
class FlexPoint:
    observed_at: str
    best: Trip


def flex_low_history(
    conn: sqlite3.Connection, origin: str, dest: str, query: FlexQuery
) -> list[FlexPoint]:
    """每次抓取时，弹性窗口内的最低组合。用于“窗口最低价走势”和历史新低提醒。"""
    back_runs = {run_id for run_id, _ in repo.runs_with_route(conn, dest, origin)}
    points = []
    for run_id, t in repo.runs_with_route(conn, origin, dest):
        if run_id not in back_runs:
            continue
        out = repo.oneway_at_run(conn, run_id, origin, dest)
        back = repo.oneway_at_run(conn, run_id, dest, origin)
        res = search(out, back, query)
        if res.best:
            points.append(FlexPoint(t, res.best))
    return points
