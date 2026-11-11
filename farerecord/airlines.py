"""航司识别：携程低价日历本身不返回航司，但支持按航司筛选（selectionType 9）。

做法：拿到某方向"全部航司"的每日最低价后，再按航司逐个查同一份日历；
哪家航司的每日最低价和总体最低价对得上，这一天的最低价就记在它名下。
两份日历各有缓存，价格可能差几块钱，所以允许一个很小的误差（OnewayQuote.airline_exact）。

实测有不少日期的最低价对不上任何单一航司（多家航司联程，或携程自己的特价组合），
这时记下已查航司里最便宜的一家和它的价格，界面上显示为"最接近的航司"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from farerecord.models import OnewayQuote

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


def airline_name(code: str | None) -> str | None:
    """航司二字码 → 中文名；不认识的代码原样返回。"""
    if not code:
        return None
    return NAMES.get(code.upper(), code.upper())


def airline_json(quote: OnewayQuote | None) -> dict[str, Any] | None:
    """接口里的航司信息。exact=False 表示最低价对不上单一航司，这是最接近的一家和它的起价。"""
    if quote is None or not quote.airline:
        return None
    exact = quote.airline_exact
    return {
        "code": quote.airline,
        "name": airline_name(quote.airline),
        "exact": exact,
        "price": None if exact else quote.airline_price,
    }


def airline_text(quote: OnewayQuote | None) -> str:
    """提醒消息里的航司说明；没有信息返回空字符串。"""
    if quote is None or not quote.airline:
        return ""
    name = f"{airline_name(quote.airline)} {quote.airline}"
    if quote.airline_exact:
        return name
    return f"参考航司 {name} ¥{quote.airline_price:,.0f} 起"


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
