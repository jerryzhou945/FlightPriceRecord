"""数据源接口与错误类型。"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from farerecord.models import OnewayQuote, RoundtripQuote


class SourceError(Exception):
    """接口返回业务错误或格式变化，需要人工检查。"""


class RetryableSourceError(SourceError):
    """限流或临时不可用，稍后重试即可。"""


class PriceSource(Protocol):
    name: str

    def fetch_oneway(self, origin: str, dest: str, start: date) -> list[OnewayQuote]:
        """返回某方向从 start 起各天的单程最低价。"""
        ...

    def fetch_roundtrip_cache(
        self, origin: str, dest: str, depart: date, ret: date
    ) -> list[RoundtripQuote]:
        """返回往返日历缓存中的组合（可能为空）。"""
        ...
