"""价格序列统计：价格位置（分位）、最低点、24 小时变化、是否历史新低。不做预测。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class PriceStats:
    current: float
    observed_at: str
    low: float
    low_at: str
    high: float
    n: int
    cheaper_than_pct: int  # 当前价比窗口内多少 % 的记录便宜
    change_24h: float | None  # 相比约 24 小时前；数据不足为 None
    is_new_low: bool  # 严格低于此前所有记录
    window_days: int


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def compute(series: list[tuple[str, float]], window_days: int = 14) -> PriceStats | None:
    """series：按时间升序的 (observed_at ISO, price)。"""
    if not series:
        return None
    t_now, current = series[-1]
    now = _ts(t_now)
    window = [(t, p) for t, p in series if _ts(t) >= now - timedelta(days=window_days)]
    prices = [p for _, p in window]
    low_t, low = min(window, key=lambda x: (x[1], x[0]))
    cheaper = round(sum(1 for p in prices if p > current) / len(prices) * 100)

    change = None
    cutoff = now - timedelta(hours=24)
    before = [p for t, p in series[:-1] if _ts(t) <= cutoff]
    if before:
        change = current - before[-1]

    earlier = [p for _, p in series[:-1]]
    return PriceStats(
        current=current,
        observed_at=t_now,
        low=low,
        low_at=low_t,
        high=max(prices),
        n=len(window),
        cheaper_than_pct=cheaper,
        change_24h=change,
        is_new_low=bool(earlier) and current < min(earlier),
        window_days=window_days,
    )
