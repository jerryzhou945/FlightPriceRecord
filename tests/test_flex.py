from datetime import date

import pytest

from farerecord.analysis.flex import FlexQuery, search, suggestions
from tests.conftest import daterange

DEC1 = date(2026, 12, 1)


def prices(start: date, n: int, base: float, cheap: dict[date, float] | None = None):
    out = {d: base for d in daterange(start, n)}
    out.update(cheap or {})
    return out


def test_search_finds_cheapest_combo() -> None:
    out = prices(DEC1, 31, 5000, {date(2026, 12, 30): 3846})
    back = prices(date(2026, 12, 20), 60, 5000, {date(2027, 1, 29): 3725})
    q = FlexQuery(DEC1, date(2026, 12, 31), 25, 35, 30)
    res = search(out, back, q)
    assert len(res.trips) == 31 * 11
    assert (res.best.depart_date, res.best.return_date, res.best.price) == (
        date(2026, 12, 30),
        date(2027, 1, 29),
        7571,
    )
    assert res.best_ideal == res.best and res.ideal_saving == 0
    assert res.top(3)[0] == res.best
    assert res.by_days[30] == res.best


def test_ideal_saving_and_suggestions() -> None:
    out = prices(DEC1, 31, 5000, {DEC1: 3000})
    back = prices(date(2026, 12, 20), 60, 5000, {date(2026, 12, 27): 3000})
    q = FlexQuery(DEC1, date(2026, 12, 31), 25, 35, 30)
    res = search(out, back, q)
    assert res.best.days != 30
    assert res.best.price == 6000 and res.best.days == 26
    assert res.best_ideal.price == 8000 and res.ideal_saving == 2000
    text = " ".join(suggestions(res))
    assert "省 ¥2,000" in text


def test_ties_prefer_ideal_days() -> None:
    out = {DEC1: 4000, date(2026, 12, 2): 4000}
    back = {date(2026, 12, 31): 4000}
    res = search(out, back, FlexQuery(DEC1, date(2026, 12, 2), 25, 35, 29))
    assert res.best.days == 29 and res.top(1)[0].days == 29


def test_missing_dates_and_rt_cache() -> None:
    out = {DEC1: 4000}
    back = {date(2026, 12, 8): 4000}
    q = FlexQuery(DEC1, date(2026, 12, 3), 5, 9)
    res = search(out, back, q, rt={(DEC1, date(2026, 12, 8)): 6500})
    assert len(res.trips) == 1
    assert res.best.rt_price == 6500 and res.best.best_price == 6500
    assert res.best_ideal is None


def test_empty_result_suggestion() -> None:
    res = search({}, {}, FlexQuery(DEC1, DEC1, 3, 5))
    assert res.best is None
    assert "没有可用报价" in suggestions(res)[0]


@pytest.mark.parametrize(
    "args",
    [
        (date(2026, 12, 31), DEC1, 25, 35),
        (DEC1, date(2026, 12, 31), 30, 25),
        (DEC1, date(2027, 6, 1), 25, 35),
        (DEC1, date(2026, 12, 31), 0, 5),
    ],
)
def test_query_validation(args) -> None:
    with pytest.raises(ValueError):
        FlexQuery(*args)
