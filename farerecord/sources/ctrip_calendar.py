"""携程 H5 低价日历接口（soa2/15380）。

- 单程模式：一次请求返回某方向未来约 366 天每天的最低含税价
- 往返模式：只返回携程缓存的短途组合（停留 ≤ 14 天），长途组合不在其中
- 航司：响应里的 airLine / flightNo 字段在国际航线上是空的；但请求可以带航司筛选
  （calendarSelections 的 selectionType 9），返回该航司每天的最低价

只使用公开匿名接口，不带会话或设备凭据，不做任何反爬绕过。
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import httpx

from farerecord.models import OnewayQuote, RoundtripQuote
from farerecord.sources.base import RetryableSourceError, SourceError

URL = "https://m.ctrip.com/restapi/soa2/15380/bjjson/FlightIntlAndInlandLowestPriceSearch"
CST = timezone(timedelta(hours=8))

# 公开请求样本里的小程序风格请求头（参考 davidwushi1145/flightAlert），不含任何个人凭据。
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Referer": "https://servicewechat.com/wx0e6ed4f51db9d078/1022/page-frame.html",
    "x-ctx-currency": "CNY",
    "x-ctx-locale": "zh-CN",
    "x-ctx-region": "CN",
    "x-ctx-group": "ctrip",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Version/4.0 Chrome/112.0.5615.136 Mobile Safari/537.36 MicroMessenger/8.0.49 "
        "NetType/WIFI Language/zh_CN miniProgram/wx0e6ed4f51db9d078"
    ),
}

_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")


def build_payload(
    origin: str, dest: str, start: date, ret: date | None = None, airline: str | None = None
) -> dict[str, Any]:
    """airline：航司二字码，只看这家航司的每日最低价（日历页自带的航司筛选）。"""
    payload: dict[str, Any] = {
        "searchType": 2,  # 2 = 国际/地区
        "departNewCityCode": origin.upper(),
        "arriveNewCityCode": dest.upper(),
        "startDate": start.isoformat(),
        "passengerList": [{"passengercount": 1, "passengertype": "Adult"}],
        "grade": 3,  # 经济舱
        "calendarSelections": [
            {"selectionType": 8, "selectionContent": ["3"]},
            {"selectionType": 2, "selectionContent": ["1"]},
        ],
        "flag": (4 if ret else 0) + 1,  # 1 = 国际单程，5 = 国际往返
        "channelName": "MobileH5",
        "lowPriceExtendInfo": [],
    }
    if ret:
        payload["returnDate"] = ret.isoformat()
    if airline:
        payload["calendarSelections"].append(
            {"selectionType": 9, "selectionContent": [airline.upper()]}
        )
    return payload


def parse_ctrip_date(value: Any) -> date | None:
    """解析 "/Date(1794326400000+0800)/"，按北京时间取日期。占位的负时间戳返回 None。"""
    if not isinstance(value, str):
        return None
    m = _DATE_RE.fullmatch(value)
    if not m:
        return None
    ms = int(m[1])
    if ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).astimezone(CST).date()


def _rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise SourceError("携程响应不是 JSON 对象")
    status = data.get("responseStatus") or data.get("ResponseStatus")
    if not isinstance(status, dict) or status.get("Ack") != "Success" or status.get("Errors"):
        raise SourceError(f"携程日历接口返回业务错误：{status!r:.200}")
    rows = data.get("priceList")
    if not isinstance(rows, list):
        raise SourceError("携程响应缺少 priceList")
    return [r for r in rows if isinstance(r, dict)]


def _price(row: dict[str, Any]) -> float | None:
    total = row.get("totalPrice")
    if isinstance(total, bool) or not isinstance(total, int | float):
        return None
    if not math.isfinite(total) or total <= 0:
        return None  # 0 表示当天无报价
    return float(total)


def parse_oneway(data: Any, origin: str, dest: str) -> list[OnewayQuote]:
    quotes: dict[date, float] = {}
    for row in _rows(data):
        d, p = parse_ctrip_date(row.get("departDate")), _price(row)
        if d and p:
            quotes[d] = min(p, quotes.get(d, p))
    return [OnewayQuote(origin.upper(), dest.upper(), d, p) for d, p in sorted(quotes.items())]


def parse_roundtrip(data: Any, origin: str, dest: str) -> list[RoundtripQuote]:
    quotes: dict[tuple[date, date], float] = {}
    for row in _rows(data):
        d, r, p = (
            parse_ctrip_date(row.get("departDate")),
            parse_ctrip_date(row.get("returnDate")),
            _price(row),
        )
        if d and r and p and r > d:
            quotes[(d, r)] = min(p, quotes.get((d, r), p))
    return [
        RoundtripQuote(origin.upper(), dest.upper(), d, r, p)
        for (d, r), p in sorted(quotes.items())
    ]


class CtripCalendarSource:
    name = "ctrip_calendar"

    def __init__(self, client: httpx.Client | None = None, timeout: float = 25.0) -> None:
        self._client = client or httpx.Client(timeout=timeout, headers=HEADERS)

    def _post(self, payload: dict[str, Any]) -> Any:
        try:
            resp = self._client.post(URL, json=payload, headers=HEADERS)
        except httpx.TimeoutException as exc:
            raise RetryableSourceError("请求携程超时") from exc
        except httpx.HTTPError as exc:
            raise RetryableSourceError(f"网络错误：{exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableSourceError(f"携程暂时不可用（HTTP {resp.status_code}）")
        if resp.status_code != 200:
            raise SourceError(f"携程请求失败（HTTP {resp.status_code}）")
        try:
            return resp.json()
        except ValueError as exc:
            raise SourceError("携程返回的不是 JSON，可能被拦截") from exc

    def fetch_oneway(self, origin: str, dest: str, start: date) -> list[OnewayQuote]:
        return parse_oneway(self._post(build_payload(origin, dest, start)), origin, dest)

    def fetch_oneway_by_airline(
        self, origin: str, dest: str, start: date, airline: str
    ) -> list[OnewayQuote]:
        """某家航司在该方向每天的单程最低价（没有报价的日期不返回）。"""
        data = self._post(build_payload(origin, dest, start, airline=airline))
        return [
            OnewayQuote(q.origin, q.dest, q.depart_date, q.price, airline.upper())
            for q in parse_oneway(data, origin, dest)
        ]

    def fetch_roundtrip_cache(
        self, origin: str, dest: str, depart: date, ret: date
    ) -> list[RoundtripQuote]:
        data = self._post(build_payload(origin, dest, depart, ret))
        return parse_roundtrip(data, origin, dest)

    def close(self) -> None:
        self._client.close()
