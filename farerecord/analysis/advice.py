"""买还是等：给一个去回组合出购买建议。

携程不公开历史价格，也没有能免费拿到往年机票价格的公开数据源，所以这里没有"训练出来的
预测模型"，而是一套透明、可解释的经验规则，配合本机自己攒下的数据：

1. 离出发还有多少天。长途国际航线常见的规律是：出发前两三个月价格较平稳，进入出发前
   六周左右开始一级一级往上跳；出发前 21 / 14 / 7 天是很多票价规则的提前购票门槛，
   低价舱位到这些时间点往往会关掉。
2. 是否旺季（圣诞元旦、春节前后、暑假）。旺季的低价舱位卖得更早，建议的最晚购买日提前。
3. 监测以来的价格位置：现在是不是最低、有没有回落的迹象、发生过几次跳涨。
4. 往年同期：每日留档（daily_price）攒满一年后，可以看去年同一时期、离出发同样天数时，
   价格后来是涨是跌。第一年没有这部分。
5. 附近日期：按最新的单程日历推算，前后挪几天能不能便宜不少。
6. 余票：携程往返页在座位很少时会显示"剩 N 张"，同时页面上能看到下一档价格。
   某一档快卖完时，价格多半会跳到下一档——这是最直接的涨价信号。

规则的阈值都放在常量里，结论只是参考，不保证价格走势。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from farerecord.models import OnewayQuote

# 春节（农历正月初一）
SPRING_FESTIVAL = {
    2026: date(2026, 2, 17),
    2027: date(2027, 2, 6),
    2028: date(2028, 1, 26),
    2029: date(2029, 2, 13),
    2030: date(2030, 2, 3),
}

LEAD_PEAK = 42  # 旺季：出发前这么多天之前买
LEAD_NORMAL = 28  # 平季
LAST_MINUTE = 21  # 不到这么多天就别再等了
WAIT_OK = 90  # 平季、离出发还有这么多天以上，可以先观察
FENCES = (42, 28, 21, 14, 7)  # 常见的涨价节点：出发前 N 天
NEAR_LOW = 1.01  # 不高于最低价的 1% 算"处在低点"
JUMP_PCT = 0.015  # 相邻两次读数涨/跌超过 1.5% 且超过 JUMP_YUAN 才算一次跳变
JUMP_YUAN = 80
NEARBY_DAYS = 3  # 附近日期：出发、返回各前后挪这么多天
MIN_SAVING_PCT = 0.015
MIN_SAVING_YUAN = 100
FEW_SEATS = 3  # 携程显示"剩 N 张"且 N 不超过这个数，就当作这一档快卖完了


def peak_season(d: date) -> str | None:
    """d 落在哪个出行旺季；不在旺季返回 None。"""
    if (d.month, d.day) >= (12, 15) or (d.month, d.day) <= (1, 5):
        return "圣诞元旦假期"
    for y in (d.year, d.year + 1):
        sf = SPRING_FESTIVAL.get(y)
        if sf and sf - timedelta(days=14) <= d <= sf + timedelta(days=7):
            return "春节前后"
    if (6, 10) <= (d.month, d.day) <= (8, 31):
        return "暑假"
    return None


@dataclass(frozen=True)
class Jump:
    at: str  # UTC ISO
    before: float
    after: float
    days_left: int  # 跳变时离出发几天

    @property
    def change(self) -> float:
        return self.after - self.before


def find_jumps(series: list[tuple[str, float]], depart: date) -> list[Jump]:
    """相邻两次读数之间明显的涨跌（时间升序）。"""
    out = []
    for (_, a), (t, b) in zip(series, series[1:], strict=False):
        if abs(b - a) >= max(a * JUMP_PCT, JUMP_YUAN):
            days = (depart - datetime.fromisoformat(t).date()).days
            out.append(Jump(t, a, b, days))
    return out


@dataclass(frozen=True)
class Alternative:
    depart: date
    ret: date
    oneway_sum: float
    expected: float  # 按现在这个组合的"实价 ÷ 单程之和"折算出的往返价
    saving: float
    out_quote: OnewayQuote
    ret_quote: OnewayQuote


def cheaper_nearby(
    out: dict[date, OnewayQuote],
    back: dict[date, OnewayQuote],
    depart: date,
    ret: date,
    current: float,
    today: date,
    *,
    spread: int = NEARBY_DAYS,
    top: int = 3,
) -> list[Alternative]:
    """出发、返回各前后挪 spread 天，按单程日历推算明显更便宜的组合。

    折算方式：现在这个组合的价格 ÷ 它的两张单程之和 = 系数，别的组合 = 单程之和 × 系数。
    假设附近日期的往返票和单程的比例差不多——只是线索，要到携程核实。
    """
    o0, r0 = out.get(depart), back.get(ret)
    if not o0 or not r0 or current <= 0:
        return []
    factor = current / (o0.price + r0.price)
    need = max(current * MIN_SAVING_PCT, MIN_SAVING_YUAN)
    found = []
    for i in range(-spread, spread + 1):
        for j in range(-spread, spread + 1):
            d, r = depart + timedelta(days=i), ret + timedelta(days=j)
            if (i, j) == (0, 0) or d <= today or r <= d:
                continue
            oq, rq = out.get(d), back.get(r)
            if not oq or not rq:
                continue
            total = oq.price + rq.price
            expected = float(round(total * factor))
            if current - expected >= need:
                found.append(Alternative(d, r, total, expected, current - expected, oq, rq))
    found.sort(key=lambda a: (a.expected, abs((a.depart - depart).days) + abs((a.ret - ret).days)))
    picked: list[Alternative] = []
    for a in found:  # 每个出发日只留最便宜的一个，给出不同的选择
        if all(a.depart != b.depart for b in picked):
            picked.append(a)
    return picked[:top]


@dataclass(frozen=True)
class LastYear:
    """去年同期：出发日相近的去程单程，从"离出发同样天数"到出发前 LAST_MINUTE 天的变化。"""

    depart: date
    days_left: int
    price_then: float
    price_later: float
    later_days_left: int

    @property
    def change_pct(self) -> float:
        return (self.price_later - self.price_then) / self.price_then


def last_year_outlook(rows: list[dict], depart: date, days_left: int) -> LastYear | None:
    """rows：daily_price 里去年出发日相近（±3 天）的单程留档。数据不够返回 None。"""
    target = depart - timedelta(days=365)
    later = max(min(days_left - 14, LAST_MINUTE), 1)
    best: LastYear | None = None
    by_dep: dict[str, dict[int, float]] = {}
    for r in rows:
        dep = date.fromisoformat(r["depart_date"])
        left = (dep - date.fromisoformat(r["observed_date"])).days
        by_dep.setdefault(r["depart_date"], {})[left] = r["low"]

    def near(lows: dict[int, float], left: int) -> float | None:
        k = min(lows, key=lambda k: abs(k - left))
        return lows[k] if abs(k - left) <= 3 else None

    for dep_s, lows in by_dep.items():
        dep = date.fromisoformat(dep_s)
        then, after = near(lows, days_left), near(lows, later)
        if then is None or after is None:
            continue
        cand = LastYear(dep, days_left, then, after, later)
        if best is None or abs((dep - target).days) < abs((best.depart - target).days):
            best = cand
    return best


@dataclass(frozen=True)
class Advice:
    verdict: str  # buy：建议现在买 | watch：设好提醒、到最晚日期前买 | wait：可以再等等
    headline: str
    reasons: list[str]
    days_left: int
    peak: str | None
    deadline: date  # 建议的最晚购买日
    target: float | None  # 值得等的价位（到价提醒用）
    current: float | None
    low: float | None
    low_at: str | None
    high: float | None
    basis: str  # real：携程往返实价 | estimate：往返预估
    jumps: list[Jump] = field(default_factory=list)
    fences: list[date] = field(default_factory=list)  # 今后的涨价节点
    alternatives: list[Alternative] = field(default_factory=list)
    last_year: LastYear | None = None
    seats_left: int | None = None  # 现价这一档携程提示的"剩 N 张"
    next_price: float | None = None  # 页面上的下一档价格


def _yuan(v: float) -> str:
    return f"¥{round(v):,}"


def _md(d: date) -> str:
    return f"{d.month}/{d.day}"


def advise(
    today: date,
    depart: date,
    ret: date,
    series: list[tuple[str, float]],
    basis: str,
    *,
    alternatives: list[Alternative] | None = None,
    last_year: LastYear | None = None,
    seats_left: int | None = None,
    next_price: float | None = None,
    elsewhere: list[tuple[str, float]] | None = None,
) -> Advice:
    """series：这个组合按时间升序的 (UTC ISO, 价格)，实价优先，没有就用往返预估。"""
    days_left = (depart - today).days
    peak = peak_season(depart) or peak_season(ret)
    lead = LEAD_PEAK if peak else LEAD_NORMAL
    deadline = depart - timedelta(days=lead)
    fences = [depart - timedelta(days=n) for n in FENCES if depart - timedelta(days=n) > today]
    alternatives = alternatives or []

    current = series[-1][1] if series else None
    low_t, low = min(series, key=lambda x: (x[1], x[0])) if series else (None, None)
    high = max(p for _, p in series) if series else None
    jumps = find_jumps(series, depart)
    near_low = current is not None and low is not None and current <= low * NEAR_LOW
    ups = [j for j in jumps if j.change > 0]

    reasons: list[str] = []
    phase = (
        "离出发不到 3 周，低价舱位基本已经关了"
        if days_left < LAST_MINUTE
        else "离出发 3–6 周，是价格最容易一级级往上跳的阶段"
        if days_left < 42
        else "离出发 6–9 周，处在低价窗口的尾段"
        if days_left < 63
        else "离出发 2–3 个月，通常是长途国际机票比较划算的时段"
        if days_left < WAIT_OK
        else "离出发还有 3 个月以上，价格一般还会有起伏"
    )
    reasons.append(f"还有 {days_left} 天出发：{phase}。")
    if peak:
        reasons.append(f"行程碰上{peak}，旺季的便宜舱位卖得早，建议 {_md(deadline)} 前买好。")
    if current is not None and low is not None and len(series) > 1:
        src = "携程往返实价" if basis == "real" else "往返预估"
        if near_low:
            reasons.append(f"现在的{src} {_yuan(current)} 就是监测以来的最低价。")
        else:
            gap = _yuan(current - low)
            reasons.append(f"现在 {_yuan(current)}，比监测以来的最低价 {_yuan(low)} 高 {gap}。")
    if ups:
        j = ups[-1]
        reasons.append(
            f"监测期间跳涨过 {len(ups)} 次，最近一次在出发前 {j.days_left} 天，"
            f"{_yuan(j.before)} → {_yuan(j.after)}。"
        )
    few = seats_left is not None and seats_left <= FEW_SEATS
    nxt = f"，卖完大概会涨到下一档 {_yuan(next_price)}" if next_price and current else ""
    if seats_left is not None:
        reasons.append(f"携程显示这个价只剩 {seats_left} 张{nxt}。")
    elif next_price and current and next_price > current:
        step = _yuan(next_price - current)
        reasons.append(
            f"携程没有提示余票紧张；页面上下一档是 {_yuan(next_price)}（贵 {step}），"
            "这一档卖完多半会跳到那里。"
        )
    for site, price in elsewhere or []:  # 别的平台同一行程的实价
        if current and price < current:
            reasons.append(
                f"{site}同一行程现在 {_yuan(price)}，比携程便宜 {_yuan(current - price)}。"
            )
        elif current and price > current:
            reasons.append(
                f"{site}同一行程现在 {_yuan(price)}，比携程贵 {_yuan(price - current)}。"
            )
    if last_year:
        pct = last_year.change_pct * 100
        trend = "基本没变"
        if abs(pct) > 0.5:
            trend = f"涨了 {pct:.0f}%" if pct > 0 else f"跌了 {-pct:.0f}%"
        reasons.append(
            f"去年同期（{_md(last_year.depart)} 出发的去程），从出发前 {last_year.days_left} 天到"
            f"出发前 {last_year.later_days_left} 天{trend}。"
        )
    if alternatives:
        a = alternatives[0]
        reasons.append(
            f"挪到 {_md(a.depart)} 去、{_md(a.ret)} 回，推算约 {_yuan(a.expected)}，"
            f"能省约 {_yuan(a.saving)}（要到携程核实）。"
        )

    # 值得等的价位：现价高于监测到的最低价时，就等回到那个实际出现过的低点（向上取整到 10 元，
    # 读到最低价本身也能触发提醒）；现价已经是最低价时，才往下看 3%。
    target = None
    if current is not None:
        if low is not None and not near_low:
            target = float(math.ceil(low / 10) * 10)
        else:
            target = float(round(current * 0.97 / 10) * 10)

    if current is None:
        verdict, headline = "watch", "还没有这个组合的价格，抓到数据后再给建议"
    elif days_left < LAST_MINUTE:
        verdict, headline = "buy", "建议尽快买：离出发太近，很少再降价"
    elif today >= deadline:
        verdict, headline = "buy", f"建议现在买：已经过了建议的最晚购买日 {_md(deadline)}"
    elif few:
        verdict, headline = "buy", f"建议现在买：这个价携程只剩 {seats_left} 张，卖完就涨"
    elif near_low and days_left < 63:
        verdict, headline = "buy", "建议现在买：正处在监测以来的低点，后面上涨的可能更大"
    elif days_left >= WAIT_OK and not peak:
        verdict, headline = "wait", "可以再等等：离出发还早，设好到价提醒即可"
    else:
        verdict = "watch"
        headline = f"可以等一等：设好 {_yuan(target or 0)} 的到价提醒，最晚 {_md(deadline)} 前买"
    if verdict == "buy" and current is not None and days_left >= LAST_MINUTE and target:
        reasons.append(
            f"想等更低价，可以设 {_yuan(target)} 的到价提醒，但不建议拖过 {_md(deadline)}"
            if today < deadline
            else f"想等更低价，可以设 {_yuan(target)} 的到价提醒，但每多等一天都可能更贵"
        )
    return Advice(
        verdict=verdict,
        headline=headline,
        reasons=reasons,
        days_left=days_left,
        peak=peak,
        deadline=deadline,
        target=target,
        current=current,
        low=low,
        low_at=low_t,
        high=high,
        basis=basis,
        jumps=jumps,
        fences=fences,
        alternatives=alternatives,
        last_year=last_year,
        seats_left=seats_left,
        next_price=next_price,
    )


# ---------- 平台对比：同一行程在携程、同程的实价，哪家更稳妥 ----------

BIG_GAP_YUAN = 200  # 差价至少这么多（或 3%）才值得换平台
BIG_GAP_PCT = 0.03
SMALL_GAP_YUAN = 100  # 差价不到这么多，就用常用的平台


@dataclass(frozen=True)
class PlatformView:
    site: str
    price: float
    observed_at: str
    low_24h: float
    high_24h: float
    jumps_24h: int  # 过去 24 小时相邻读数的明显涨跌次数（见 find_jumps）
    fresh: bool  # 最近一次读数还新（3 小时内）


@dataclass(frozen=True)
class PlatformPick:
    pick: str
    reason: str
    checklist: list[str]
    rows: list[PlatformView]


def platform_view(
    site: str, series: list[tuple[str, float]], now: datetime, depart: date, fresh_hours: float = 3
) -> PlatformView | None:
    """series：按时间升序的 (UTC ISO, 价格)。"""
    if not series:
        return None
    t_last, price = series[-1]
    day = [(t, p) for t, p in series if now - datetime.fromisoformat(t) <= timedelta(hours=24)]
    day = day or series[-1:]
    prices = [p for _, p in day]
    age = now - datetime.fromisoformat(t_last)
    return PlatformView(
        site=site,
        price=price,
        observed_at=t_last,
        low_24h=min(prices),
        high_24h=max(prices),
        jumps_24h=len(find_jumps(day, depart)),
        fresh=age <= timedelta(hours=fresh_hours),
    )


def pick_platform(
    ctrip: PlatformView | None, other: PlatformView | None, usual: str = "携程"
) -> PlatformPick | None:
    """两家都有价格时给出推荐。看两点：差价够不够大、便宜的那家价格稳不稳。

    两家都是大平台，真正的区别在出票的代理商和退改规则——列表页上看不到，
    所以推荐便宜那家时，一定附上下单前要核对的清单。
    """
    if ctrip is None or other is None:
        return None
    rows = [ctrip, other]
    check = [
        "进到付款前最后一页，确认总价没有变",
        "看清退改签规则和行李额（便宜的产品常常退改更严）",
        "出票后用票号到航空公司官网的“管理预订”里核实已出票",
    ]
    if not other.fresh:
        return PlatformPick(usual, f"{other.site}的价格超过 3 小时没更新，以携程为准。", [], rows)
    gap = ctrip.price - other.price
    big = max(BIG_GAP_YUAN, ctrip.price * BIG_GAP_PCT)
    if gap >= big:
        return PlatformPick(
            other.site,
            f"{other.site}便宜 {_yuan(gap)}，差价够大，值得在{other.site}买。",
            check,
            rows,
        )
    if gap >= SMALL_GAP_YUAN:
        if other.jumps_24h <= ctrip.jumps_24h:
            return PlatformPick(
                other.site,
                f"{other.site}便宜 {_yuan(gap)}，过去 24 小时价格也不比携程跳得多，"
                f"可以选{other.site}。",
                check,
                rows,
            )
        return PlatformPick(
            usual,
            f"{other.site}只便宜 {_yuan(gap)}，但过去 24 小时跳动 {other.jumps_24h} 次"
            f"（携程 {ctrip.jumps_24h} 次），价格不稳，携程更稳妥。",
            [],
            rows,
        )
    if gap > 0:
        why = f"{other.site}只便宜 {_yuan(gap)}，差价很小"
    elif gap == 0:
        why = "两家一样价"
    else:
        why = f"携程反而便宜 {_yuan(-gap)}"
    return PlatformPick(usual, f"{why}，选你常用的携程，售后更省心。", [], rows)
