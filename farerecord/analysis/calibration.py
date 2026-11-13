"""往返预估的校准：往返票通常比"去程单程 + 回程单程"便宜，便宜多少？

携程的往返日历只缓存停留不超过 14 天的真实往返价，长途组合拿不到。但这批真实往返价
可以告诉我们这条航线上"往返票 ÷ 两张单程之和"大约是多少（实测停留 10–14 天时中位数
在 0.85 左右），用这个比例去估长途组合的往返价，比直接把两张单程相加更接近真实。

校准来源的优先级：
1. 用户最近手动核过的真实往返价（最可信）
2. 最近若干次抓取里，携程往返缓存 ÷ 同一次抓取的两张单程之和
3. 都没有时比例为 1，即不校准

比例对所有日期一视同仁，并且用最近多次抓取的中位数，所以它变化很慢——
走势图的起伏只反映真实的单程价变化，不会被校准本身搅动。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median

from farerecord import repository as repo

MIN_STAY_DAYS = 10  # 只用停留较长的往返票来校准，更接近长途行程
MIN_SAMPLES = 8  # 一次抓取里至少这么多个可比的组合才算数
RECENT_RUNS = 48  # 取最近这么多次抓取的中位数，让比例变化得慢
MANUAL_VALID = timedelta(days=3)  # 手动核价在这段时间内用来校准
RATIO_RANGE = (0.6, 1.0)  # 两张单程永远买得到，所以往返预估不会高于它；也不信过低的比例


@dataclass(frozen=True)
class Calibration:
    ratio: float = 1.0
    source: str = "none"  # manual | cache | none
    samples: int = 0

    def apply(self, oneway_sum: float) -> float:
        return float(round(oneway_sum * self.ratio))


def _clamp(ratio: float) -> float:
    return min(max(ratio, RATIO_RANGE[0]), RATIO_RANGE[1])


def ratio_from_quotes(
    out: dict[date, float], back: dict[date, float], rt: dict[tuple[date, date], float]
) -> tuple[float, int] | None:
    """一次抓取里：真实往返价 ÷ 两张单程之和 的中位数，以及用了多少个组合。"""
    ratios = [
        price / (out[d] + back[r])
        for (d, r), price in rt.items()
        if (r - d).days >= MIN_STAY_DAYS and out.get(d) and back.get(r)
    ]
    if len(ratios) < MIN_SAMPLES:
        return None
    return median(ratios), len(ratios)


def record(conn: sqlite3.Connection, run_id: int, origin: str, dest: str) -> float | None:
    """抓取后调用：算出这次抓取的比例并存下来。数据不够返回 None。"""
    result = ratio_from_quotes(
        repo.oneway_at_run(conn, run_id, origin, dest),
        repo.oneway_at_run(conn, run_id, dest, origin),
        repo.roundtrip_at_run(conn, run_id, origin, dest),
    )
    if result is None:
        return None
    repo.save_calibration(conn, run_id, origin, dest, *result)
    return result[0]


def _from_manual(
    conn: sqlite3.Connection, origin: str, dest: str, now: datetime
) -> Calibration | None:
    ratios = []
    for q in repo.recent_manual_quotes(conn, origin, dest, (now - MANUAL_VALID).isoformat()):
        depart, ret = date.fromisoformat(q["depart_date"]), date.fromisoformat(q["return_date"])
        series = repo.combo_series(conn, origin, dest, depart, ret)
        if not series:
            continue
        # 用核价那一刻（或之后最近一次抓取）的单程价来比
        at = [p for p in series if p[0] <= q["observed_at"]] or series[:1]
        _, out_price, ret_price = at[-1]
        ratios.append(q["price"] / (out_price + ret_price))
    if not ratios:
        return None
    return Calibration(_clamp(median(ratios)), "manual", len(ratios))


def current(
    conn: sqlite3.Connection, origin: str, dest: str, now: datetime | None = None
) -> Calibration:
    """这条航线现在该用的往返校准比例。"""
    manual = _from_manual(conn, origin, dest, now or datetime.fromisoformat(repo.utcnow_iso()))
    if manual:
        return manual
    stored = repo.recent_calibrations(conn, origin, dest, RECENT_RUNS)
    if stored:
        return Calibration(_clamp(median(r for r, _ in stored)), "cache", sum(n for _, n in stored))
    # 旧数据库里的抓取还没有存过比例：用最近一次抓取现算
    latest = ratio_from_quotes(
        repo.latest_oneway(conn, origin, dest),
        repo.latest_oneway(conn, dest, origin),
        repo.latest_roundtrip(conn, origin, dest),
    )
    if latest:
        return Calibration(_clamp(latest[0]), "cache", latest[1])
    return Calibration()
