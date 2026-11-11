"""航司识别：携程低价日历本身不返回航司，但支持按航司筛选（selectionType 9）。

做法：拿到某方向"全部航司"的每日最低价后，再按航司逐个查同一份日历；
哪家航司的每日最低价和总体最低价对得上，这一天的最低价就记在它名下。
两份日历各有缓存，价格可能差几块钱，所以允许一个很小的误差。
"""

from __future__ import annotations

from datetime import date

# 默认候选航司，按"在中美/亚洲长航线上经常出最低价"的经验排序；可在 config.yaml 里改。
DEFAULT_AIRLINES = [
    "CX",
    "KE",
    "PR",
    "TK",
    "NH",
    "DL",
    "UA",
    "AA",
    "AC",
    "BR",
    "JL",
    "MU",
    "CA",
    "HU",
    "OZ",
    "CZ",
    "EK",
    "QR",
]

NAMES = {
    "3U": "四川航空",
    "9C": "春秋航空",
    "AA": "美国航空",
    "AC": "加拿大航空",
    "AF": "法国航空",
    "AS": "阿拉斯加航空",
    "AY": "芬兰航空",
    "BA": "英国航空",
    "BR": "长荣航空",
    "CA": "中国国航",
    "CI": "中华航空",
    "CX": "国泰航空",
    "CZ": "南方航空",
    "DL": "达美航空",
    "EK": "阿联酋航空",
    "ET": "埃塞俄比亚航空",
    "EY": "阿提哈德航空",
    "FM": "上海航空",
    "HO": "吉祥航空",
    "HU": "海南航空",
    "HX": "香港航空",
    "JL": "日本航空",
    "JX": "星宇航空",
    "KE": "大韩航空",
    "KL": "荷兰皇家航空",
    "LH": "汉莎航空",
    "MF": "厦门航空",
    "MH": "马来西亚航空",
    "MU": "东方航空",
    "NH": "全日空",
    "NZ": "新西兰航空",
    "OZ": "韩亚航空",
    "PR": "菲律宾航空",
    "QF": "澳洲航空",
    "QR": "卡塔尔航空",
    "SC": "山东航空",
    "SQ": "新加坡航空",
    "TG": "泰国国际航空",
    "TK": "土耳其航空",
    "UA": "美联航",
    "VN": "越南航空",
    "WS": "西捷航空",
    "YP": "Air Premia",
    "ZG": "ZIPAIR",
    "ZH": "深圳航空",
}

PRICE_TOLERANCE = 0.01  # 航司日历价 ≤ 总体最低价 × (1 + 1%) 视为同一个报价


def airline_name(code: str | None) -> str | None:
    """航司二字码 → 中文名；不认识的代码原样返回。"""
    if not code:
        return None
    return NAMES.get(code.upper(), code.upper())


def airline_json(code: str | None) -> dict[str, str] | None:
    return {"code": code, "name": airline_name(code) or code} if code else None


def normalize_codes(codes: object) -> list[str]:
    """配置里的航司列表 → 去重、大写的二字码列表。"""
    if codes is None:
        return list(DEFAULT_AIRLINES)
    if isinstance(codes, str):
        codes = codes.replace("，", ",").split(",")
    out: list[str] = []
    for c in codes or []:  # type: ignore[union-attr]
        c = str(c).strip().upper()
        if len(c) == 2 and c.isalnum() and c not in out:
            out.append(c)
    return out


def matching_dates(
    lowest: dict[date, float], by_airline: dict[date, float], tolerance: float = PRICE_TOLERANCE
) -> set[date]:
    """这家航司的每日最低价里，哪些天就是（或低于）总体最低价。"""
    return {
        d
        for d, p in by_airline.items()
        if d in lowest and p > 0 and p <= lowest[d] * (1 + tolerance)
    }
