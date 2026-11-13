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
    """一个去回组合。price 为估价（去程单程 + 回程单程），rt_price 为真实往返价（如有）。"""

    depart_date: date
    return_date: date
    out_price: float
    ret_price: float
    rt_price: float | None = None

    @property
    def days(self) -> int:
        return (self.return_date - self.depart_date).days

    @property
    def price(self) -> float:
        return self.out_price + self.ret_price

    @property
    def best_price(self) -> float:
        """估价和真实往返价里更低的那个。"""
        return min(self.price, self.rt_price) if self.rt_price else self.price


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
