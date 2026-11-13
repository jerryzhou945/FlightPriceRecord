from datetime import date, timedelta

from farerecord import repository as repo
from farerecord.analysis.advice import (
    advise,
    cheaper_nearby,
    find_jumps,
    last_year_outlook,
    peak_season,
)
from farerecord.models import OnewayQuote
from tests.conftest import seed_run

DEP, RET = date(2026, 12, 28), date(2027, 1, 20)


def ts(day: int, hour: int = 0) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:00:00+00:00"


def test_peak_season() -> None:
    assert peak_season(date(2026, 12, 28)) == "圣诞元旦假期"
    assert peak_season(date(2027, 1, 3)) == "圣诞元旦假期"
    assert peak_season(date(2027, 2, 1)) == "春节前后"  # 2027 春节 2/6
    assert peak_season(date(2027, 1, 20)) is None
    assert peak_season(date(2027, 7, 15)) == "暑假"
    assert peak_season(date(2027, 10, 10)) is None


def test_find_jumps_ignores_jitter() -> None:
    series = [(ts(11, 0), 7125.0), (ts(11, 1), 7130.0), (ts(12, 3), 7325.0), (ts(12, 4), 7125.0)]
    jumps = find_jumps(series, DEP)
    assert [(j.before, j.after) for j in jumps] == [(7130, 7325), (7325, 7125)]
    assert jumps[0].days_left == 46 and jumps[0].change == 195


def test_buy_when_at_low_in_peak_tail() -> None:
    series = [(ts(11, 23), 7125.0), (ts(12, 1), 7130.0), (ts(12, 3), 7125.0)]
    a = advise(date(2026, 11, 12), DEP, RET, series, "real")
    assert a.verdict == "buy"
    assert a.peak == "圣诞元旦假期" and a.deadline == date(2026, 11, 16)
    assert a.days_left == 46 and a.low == 7125 and a.current == 7125
    assert a.target == 6910  # 比现在低 3%
    assert any("最低价" in r for r in a.reasons)
    assert a.fences[0] == date(2026, 11, 16) and a.fences[-1] == date(2026, 12, 21)


def test_watch_after_jump_before_deadline() -> None:
    series = [(ts(11, 23), 7125.0), (ts(12, 3), 7325.0)]
    a = advise(date(2026, 11, 12), DEP, RET, series, "real")
    assert a.verdict == "watch"
    assert a.target == 7110  # min(最低价 7125, 现价 × 97%) 取整到 10 元
    assert "11/16" in a.headline
    assert any("跳涨过 1 次" in r for r in a.reasons)


def test_buy_after_deadline_and_last_minute() -> None:
    series = [(ts(1), 7000.0), (ts(2), 7300.0)]
    assert advise(date(2026, 11, 20), DEP, RET, series, "real").verdict == "buy"
    late = advise(DEP - timedelta(days=10), DEP, RET, series, "real")
    assert late.verdict == "buy" and "太近" in late.headline


def test_wait_when_far_and_off_peak() -> None:
    dep, ret = date(2027, 10, 10), date(2027, 10, 30)
    a = advise(date(2027, 5, 1), dep, ret, [(ts(1), 6000.0), (ts(2), 6100.0)], "estimate")
    assert a.verdict == "wait" and a.peak is None and a.deadline == dep - timedelta(days=28)


def test_no_data() -> None:
    a = advise(date(2026, 11, 12), DEP, RET, [], "estimate")
    assert a.verdict == "watch" and a.current is None and a.target is None


def q(o: str, d: str, day: date, price: float, airline: str | None = None) -> OnewayQuote:
    return OnewayQuote(o, d, day, price, airline)


def test_cheaper_nearby_scales_by_current_trip() -> None:
    out = {
        DEP + timedelta(days=i): q("CHI", "SHA", DEP + timedelta(days=i), 4500)
        for i in range(-3, 4)
    }
    back = {
        RET + timedelta(days=i): q("SHA", "CHI", RET + timedelta(days=i), 4000)
        for i in range(-3, 4)
    }
    out[DEP + timedelta(days=2)] = q("CHI", "SHA", DEP + timedelta(days=2), 3800, "CX")
    out[DEP + timedelta(days=3)] = q("CHI", "SHA", DEP + timedelta(days=3), 3800, "CX")
    back[RET - timedelta(days=1)] = q("SHA", "CHI", RET - timedelta(days=1), 3990)
    alts = cheaper_nearby(out, back, DEP, RET, 7225.0, date(2026, 11, 12))
    # 系数 = 7225 / 8500 = 0.85；12/30 + 1/20 = 7800 × 0.85 = 6630
    assert [(a.depart, a.ret) for a in alts] == [
        (date(2026, 12, 30), date(2027, 1, 19)),
        (date(2026, 12, 31), date(2027, 1, 19)),
    ]
    assert alts[0].expected == round(7790 * 0.85) and alts[0].out_quote.airline == "CX"
    # 只差几十块的不算
    assert all(a.saving >= 100 for a in alts)


def test_cheaper_nearby_needs_both_legs() -> None:
    assert cheaper_nearby({}, {}, DEP, RET, 7000.0, date(2026, 11, 12)) == []


def test_last_year_outlook() -> None:
    rows = [
        # 去年 12/27 出发的去程：出发前 46 天 4000，出发前 21 天 4600
        {"depart_date": "2025-12-27", "observed_date": "2025-11-11", "low": 4000.0},
        {"depart_date": "2025-12-27", "observed_date": "2025-12-06", "low": 4600.0},
        # 去年 12/31 出发的只有一个点，不够
        {"depart_date": "2025-12-31", "observed_date": "2025-11-15", "low": 3900.0},
    ]
    ly = last_year_outlook(rows, DEP, 46)
    assert ly is not None and ly.depart == date(2025, 12, 27)
    assert ly.later_days_left == 21 and round(ly.change_pct, 2) == 0.15
    reasons = advise(date(2026, 11, 12), DEP, RET, [(ts(12), 7125.0)], "real", last_year=ly).reasons
    assert any("去年同期" in r and "涨了 15%" in r for r in reasons)
    assert last_year_outlook(rows[2:], DEP, 46) is None


def test_daily_archive_survives_pruning(conn) -> None:
    seed_run(conn, "2026-11-01T02:00:00+00:00", {DEP: 4500.0}, {RET: 4000.0})
    seed_run(conn, "2026-11-01T10:00:00+00:00", {DEP: 4300.0}, {RET: 4100.0})
    seed_run(conn, "2026-11-01T17:00:00+00:00", {DEP: 4400.0}, {RET: 4000.0})  # 北京时间 11/2
    repo.add_real_quote(conn, "CHI", "SHA", DEP, RET, 7125, "土耳其航空", ts(11, 23))
    repo.add_real_quote(conn, "CHI", "SHA", DEP, RET, 7325, "土耳其航空", ts(12, 3))
    repo.prune_runs(conn, "2026-12-01T00:00:00+00:00")
    repo.prune_real_quotes(conn, "2026-12-01T00:00:00+00:00")
    rows = repo.daily_prices(conn, "oneway", "CHI", "SHA", DEP, DEP)
    assert [(r["observed_date"], r["low"], r["high"], r["n"]) for r in rows] == [
        ("2026-11-01", 4300, 4500, 2),
        ("2026-11-02", 4400, 4400, 1),
    ]
    real = repo.daily_prices(conn, "real", "CHI", "SHA", DEP, DEP, RET)
    assert [(r["observed_date"], r["low"], r["high"]) for r in real] == [("2026-11-12", 7125, 7325)]
