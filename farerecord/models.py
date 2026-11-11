"""核心数据结构。价格统一为人民币含税总价、1 位成人、经济舱。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class OnewayQuote:
    """某方向某天的单程最低价。

    airline：出这个最低价的航司二字码；对不上单一航司时，是已查航司里最便宜的那家，
    airline_price 是它的价格（会比 price 高）。None = 还没查，"" = 查过但没有航司有报价。
    """

    origin: str
    dest: str
    depart_date: date
    price: float
    airline: str | None = None
    airline_price: float | None = None

    @property
    def airline_exact(self) -> bool:
        """最低价是否就是这家航司的（允许两份日历缓存之间的一点价差）。"""
        if not self.airline:
            return False
        return self.airline_price is None or self.airline_price <= self.price * 1.01


@dataclass(frozen=True, slots=True)
class RoundtripQuote:
    """携程往返日历缓存里的真实往返价（只有短途组合）。"""

    origin: str
    dest: str
    depart_date: date
    return_date: date
    price: float


@dataclass(frozen=True, slots=True)
class Trip:
    """一个去回组合。

    price：去程单程 + 回程单程，分开买两张票一定买得到的价格。
    estimate：往返预估 = price × ratio（往返票通常比两张单程便宜，ratio 见 analysis/calibration）。
    rt_price：携程往返缓存里的真实往返价（只有短途组合才有）。
    """

    depart_date: date
    return_date: date
    out_price: float
    ret_price: float
    rt_price: float | None = None
    ratio: float = 1.0

    @property
    def days(self) -> int:
        return (self.return_date - self.depart_date).days

    @property
    def price(self) -> float:
        return self.out_price + self.ret_price

    @property
    def estimate(self) -> float:
        return float(round(self.price * self.ratio))

    @property
    def raw_price(self) -> float:
        """不含校准的价格：两张单程和真实往返价里更低的。提醒去重用它，不受校准比例变化影响。"""
        return min(self.price, self.rt_price) if self.rt_price else self.price

    @property
    def best_price(self) -> float:
        """界面和提醒用的价格：有真实往返价就用真实的（和两张单程取低），否则用往返预估。"""
        return self.raw_price if self.rt_price else self.estimate


@dataclass(frozen=True, slots=True)
class PriceWatch:
    """到价提醒：某个去回组合的往返估价不高于 target_price 时通知。"""

    id: int
    origin: str
    dest: str
    depart_date: date
    return_date: date
    target_price: float
    enabled: bool = True
