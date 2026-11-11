"""核心数据结构。价格统一为人民币含税总价、1 位成人、经济舱。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class OnewayQuote:
    """某方向某天的单程最低价。airline 为出这个最低价的航司二字码（识别不出为 None）。"""

    origin: str
    dest: str
    depart_date: date
    price: float
    airline: str | None = None


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
