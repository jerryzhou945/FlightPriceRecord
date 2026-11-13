from datetime import date

from farerecord.analysis.flex import FlexQuery
from farerecord.analysis.history import combo_history, flex_low_history
from farerecord.analysis.stats import compute
from tests.conftest import seed_run


def ts(day: int, hour: int) -> str:
    return f"2026-11-{day:02d}T{hour:02d}:00:00+00:00"


def test_compute_basic() -> None:
    series = [(ts(1, 0), 8000), (ts(5, 0), 7600), (ts(9, 0), 7800), (ts(10, 0), 7400)]
    s = compute(series, window_days=14)
    assert s.current == 7400 and s.low == 7400 and s.high == 8000
    assert s.cheaper_than_pct == 75
    assert s.change_24h == -400
    assert s.is_new_low


def test_compute_window_and_no_24h() -> None:
    series = [(ts(1, 0), 5000), (ts(10, 0), 7000), (ts(10, 6), 7100)]
    s = compute(series, window_days=3)
    assert s.n == 2 and s.low == 7000 and s.low_at == ts(10, 0)
    assert s.change_24h == 2100
    assert not s.is_new_low
    assert compute([(ts(10, 0), 7000), (ts(10, 6), 6900)]).change_24h is None
    assert compute([]) is None
    single = compute([(ts(1, 0), 100)])
    assert single.cheaper_than_pct == 0 and not single.is_new_low


def test_combo_and_flex_low_history(conn) -> None:
    d, r = date(2026, 12, 30), date(2027, 1, 29)
    seed_run(conn, ts(9, 0), {d: 4000}, {r: 4000})
    seed_run(conn, ts(10, 0), {d: 3846}, {r: 3725})
    seed_run(conn, ts(10, 6), {d: 3800}, {})  # 缺回程的抓取被跳过
    assert combo_history(conn, "CHI", "SHA", d, r) == [(ts(9, 0), 8000), (ts(10, 0), 7571)]
    q = FlexQuery(date(2026, 12, 1), date(2026, 12, 31), 25, 35, 30)
    pts = flex_low_history(conn, "CHI", "SHA", q)
    assert [p.best.price for p in pts] == [8000, 7571]
