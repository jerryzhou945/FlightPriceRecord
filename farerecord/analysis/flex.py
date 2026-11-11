"""弹性行程搜索：在出发窗口 × 游玩天数范围里找最便宜的去回组合。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from farerecord.models import Trip


@dataclass(frozen=True)
class FlexQuery:
    start: date
    end: date
    min_days: int
    max_days: int
    ideal_days: int | None = None

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("出发窗口开始日期晚于结束日期")
        if self.min_days < 1 or self.min_days > self.max_days:
            raise ValueError("游玩天数范围不正确")
        if (self.end - self.start).days > 92 or self.max_days - self.min_days > 60:
            raise ValueError("范围太大：出发窗口最多 92 天，天数跨度最多 60 天")

    @property
    def depart_dates(self) -> list[date]:
        n = (self.end - self.start).days
        return [self.start + timedelta(days=i) for i in range(n + 1)]

    @property
    def stays(self) -> list[int]:
        return list(range(self.min_days, self.max_days + 1))


@dataclass
class FlexResult:
    query: FlexQuery
    trips: list[Trip]
    best: Trip | None
    best_ideal: Trip | None
    by_days: dict[int, Trip] = field(default_factory=dict)
    by_depart: dict[date, Trip] = field(default_factory=dict)

    @property
    def ideal_saving(self) -> float:
        """正好玩理想天数的最低价，比全局最低价贵多少（≥ 0）。"""
        if not self.best or not self.best_ideal:
            return 0.0
        return self.best_ideal.best_price - self.best.best_price

    def top(self, n: int = 10) -> list[Trip]:
        return sorted(self.trips, key=_sort_key)[:n]


def _sort_key(t: Trip) -> tuple[float, int, date]:
    return (t.best_price, abs(t.days), t.depart_date)


def _min(a: Trip | None, b: Trip) -> Trip:
    return b if a is None or _sort_key(b) < _sort_key(a) else a


def search(
    out: dict[date, float],
    back: dict[date, float],
    query: FlexQuery,
    rt: dict[tuple[date, date], float] | None = None,
) -> FlexResult:
    """out：去程每天单程价；back：回程每天单程价；rt：往返缓存（可选）。"""
    rt = rt or {}
    trips: list[Trip] = []
    best = best_ideal = None
    by_days: dict[int, Trip] = {}
    by_depart: dict[date, Trip] = {}
    for d in query.depart_dates:
        o = out.get(d)
        if not o:
            continue
        for s in query.stays:
            r = d + timedelta(days=s)
            b = back.get(r)
            if not b:
                continue
            t = Trip(d, r, o, b, rt.get((d, r)))
            trips.append(t)
            best = _min(best, t)
            if s == query.ideal_days:
                best_ideal = _min(best_ideal, t)
            by_days[s] = _min(by_days.get(s), t)
            by_depart[d] = _min(by_depart.get(d), t)
    return FlexResult(query, trips, best, best_ideal, by_days, by_depart)


def suggestions(result: FlexResult) -> list[str]:
    """生成“按价格微调日期”的中文建议。"""
    tips: list[str] = []
    best, q = result.best, result.query
    if not best:
        return ["当前窗口内没有可用报价，试试放宽出发窗口或天数范围。"]
    tips.append(
        f"最便宜：{best.depart_date:%m/%d} 出发 → {best.return_date:%m/%d} 返回，"
        f"{best.days} 天，约 ¥{best.best_price:,.0f}。"
    )
    if q.ideal_days and result.best_ideal:
        bi = result.best_ideal
        if result.ideal_saving > 0:
            tips.append(
                f"正好玩 {q.ideal_days} 天最低 ¥{bi.best_price:,.0f}"
                f"（{bi.depart_date:%m/%d} → {bi.return_date:%m/%d}），"
                f"改成 {best.days} 天可以省 ¥{result.ideal_saving:,.0f}。"
            )
        else:
            tips.append(f"正好玩 {q.ideal_days} 天就是最便宜的选择。")
    elif q.ideal_days:
        tips.append(f"{q.ideal_days} 天没有可用报价。")
    if result.by_depart:
        prices = sorted(t.best_price for t in result.by_depart.values())
        expensive = [
            d for d, t in result.by_depart.items() if t.best_price >= prices[int(len(prices) * 0.8)]
        ]
        if len(prices) >= 5 and expensive:
            first, last = min(expensive), max(expensive)
            tips.append(f"尽量避开 {first:%m/%d}–{last:%m/%d} 之间出发，这段时间普遍更贵。")
    return tips
