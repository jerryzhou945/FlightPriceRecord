"""航司识别：按航司筛选的日历请求、对价逻辑、入库与 API 字段。"""

import json
from datetime import date, timedelta
from pathlib import Path

import httpx

from farerecord import db
from farerecord import repository as repo
from farerecord.airlines import DEFAULT_AIRLINES, airline_name
from farerecord.alerts.rules import Alert, build_price_message
from farerecord.collector import attribute_airlines
from farerecord.config import parse_config
from farerecord.models import OnewayQuote, Trip
from farerecord.sources.base import SourceError
from farerecord.sources.ctrip_calendar import CtripCalendarSource, build_payload
from tests.test_collector import TODAY, FakeSource, run

FIXTURES = Path(__file__).parent / "fixtures"
D0 = TODAY + timedelta(days=1)


def _attr(quotes, previous, by_code, max_lookups=8, candidates=("CX", "KE", "UA")):
    asked = []

    def lookup(code):
        asked.append(code)
        return by_code.get(code, {})

    out = attribute_airlines(
        [OnewayQuote("CHI", "SHA", d, p) for d, p in quotes.items()],
        {d: OnewayQuote("CHI", "SHA", d, *fields) for d, fields in previous.items()},
        (TODAY, TODAY + timedelta(days=30)),
        list(candidates),
        max_lookups,
        lookup,
    )
    return {q.depart_date: q.airline for q in out}, asked


def test_attribute_matches_lowest_price_with_tolerance() -> None:
    d1, d2, d3 = (D0 + timedelta(days=i) for i in range(3))
    got, asked = _attr(
        {d1: 4000, d2: 5000, d3: 6000},
        {},
        {"CX": {d1: 4000, d2: 5600}, "KE": {d2: 5030, d3: 6500}},
    )
    # d2 在容差内；d3 只识别到高于最低价的参考航司。
    assert got == {d1: "CX", d2: "KE", d3: "KE"}
    assert asked == ["CX", "KE", "UA"]


def test_attribute_carries_over_unchanged_prices_without_requests() -> None:
    d1, d2 = D0, D0 + timedelta(days=1)
    got, asked = _attr({d1: 4000, d2: 5000}, {d1: (4000, "KE"), d2: (5000, "")}, {})
    assert got == {d1: "KE", d2: ""} and asked == []


def test_attribute_rechecks_changed_price_best_airline_first() -> None:
    d1, d2 = D0, D0 + timedelta(days=1)
    got, asked = _attr(
        {d1: 3900, d2: 5000}, {d1: (4000, "KE"), d2: (5000, "KE")}, {"KE": {d1: 3900}}
    )
    # 上次出最低价最多的航司先查，对上就停
    assert got == {d1: "KE", d2: "KE"} and asked == ["KE"]


def test_attribute_respects_lookup_cap_and_failure() -> None:
    got, asked = _attr({D0: 4000}, {}, {"UA": {D0: 4000}}, max_lookups=2)
    assert asked == ["CX", "KE"] and got == {D0: ""}
    got, asked = _attr({D0: 4000}, {}, {"CX": None, "KE": {D0: 4000}})
    assert asked == ["CX"] and got == {D0: None}  # 查询失败：停止，留到下次再试
    got, asked = _attr({D0: 4000}, {}, {"CX": {D0: 4500}, "KE": None})
    assert asked == ["CX", "KE"] and got == {D0: None}


def test_attribute_only_requires_dates_in_range() -> None:
    far = TODAY + timedelta(days=200)
    got, asked = _attr({far: 4000}, {}, {"CX": {far: 4000}})
    assert asked == [] and got == {far: None}


class AirlineSource(FakeSource):
    def fetch_oneway_by_airline(self, origin, dest, start, airline):
        self.calls.append((origin, dest, airline))
        self._maybe_fail((origin, dest, airline))
        if airline != "KE":
            return []
        return [
            OnewayQuote(origin, dest, start + timedelta(days=i), 4001 + i, "KE") for i in (0, 1)
        ]


def _cfg(airlines):
    return parse_config(
        {"routes": [{"origin": "CHI", "dest": "SHA"}], "collector": {"airlines": airlines}}
    )


def test_collect_records_airlines_and_reuses_them(conn) -> None:
    src = AirlineSource()
    res, _ = run(conn, src, _cfg(["CX", "KE"]))
    assert res.status == "ok"
    details = repo.latest_oneway_detail(conn, "CHI", "SHA")
    assert {d: (q.price, q.airline) for d, q in details.items()} == {
        TODAY: (4001, "KE"),
        TODAY + timedelta(days=1): (4002, "KE"),
        TODAY + timedelta(days=2): (4003, ""),
    }
    assert {d: q.airline for d, q in repo.latest_airlines(conn, "SHA", "CHI").items()} == {
        TODAY: "KE",
        TODAY + timedelta(days=1): "KE",
    }
    assert [c[2] for c in src.calls] == ["ow", "CX", "KE", "ow", "CX", "KE", "rt"]
    # 第二次抓取价格没变：不再发航司查询
    src.calls.clear()
    run(conn, src, _cfg(["CX", "KE"]))
    assert [c[2] for c in src.calls] == ["ow", "ow", "rt"]
    cached = repo.latest_airlines(conn, "CHI", "SHA")[TODAY]
    assert (cached.airline, cached.airline_price) == ("KE", 4001)


def test_collect_airline_lookup_failure_is_partial(conn) -> None:
    src = AirlineSource(fail={("CHI", "SHA", "CX"): [SourceError("blocked")]})
    res, _ = run(conn, src, _cfg(["CX"]))
    assert res.status == "partial" and "航司 CX" in res.errors[0]
    assert repo.latest_oneway(conn, "CHI", "SHA")
    assert not repo.latest_airlines(conn, "CHI", "SHA")


def test_collect_without_airlines_makes_no_extra_requests(conn) -> None:
    src = AirlineSource()
    run(conn, src, _cfg([]))
    assert [c[2] for c in src.calls] == ["ow", "ow", "rt"]


def test_airlines_config() -> None:
    c = parse_config({"routes": [], "collector": {"airlines": "cx, ke，CX,bad1"}}).collector
    assert c.airlines == ["CX", "KE"]
    assert parse_config({"routes": []}).collector.airlines == DEFAULT_AIRLINES
    assert airline_name("cx") == "国泰航空" and airline_name("XX") == "XX"
    assert airline_name("") is None


def test_airline_filter_payload_and_fetch() -> None:
    p = build_payload("chi", "sha", date(2026, 11, 10), airline="cx")
    assert p["calendarSelections"][-1] == {"selectionType": 9, "selectionContent": ["CX"]}
    assert len(build_payload("CHI", "SHA", date(2026, 11, 10))["calendarSelections"]) == 2
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        fixture = (FIXTURES / "ctrip_oneway_chi_sha.json").read_text(encoding="utf-8")
        return httpx.Response(200, json=json.loads(fixture))

    src = CtripCalendarSource(client=httpx.Client(transport=httpx.MockTransport(handler)))
    quotes = src.fetch_oneway_by_airline("CHI", "SHA", date(2026, 11, 10), "cx")
    assert seen["body"]["calendarSelections"][-1]["selectionContent"] == ["CX"]
    assert quotes and all(q.airline == "CX" for q in quotes)


def test_migrate_v1_database_keeps_quotes() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db._MIGRATIONS[1])
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO fetch_run (source, started_at) VALUES ('t', '2026-11-10T00:00:00')")
    conn.execute("INSERT INTO oneway_quote VALUES (1, 'CHI', 'SHA', '2026-12-30', 3846, 't')")
    assert db.migrate(conn) == db.SCHEMA_VERSION
    row = conn.execute("SELECT price, airline FROM oneway_quote").fetchone()
    assert row["price"] == 3846 and row["airline"] is None


def test_price_message_names_airlines() -> None:
    route = parse_config({"routes": [{"origin": "CHI", "dest": "SHA"}]}).routes[0]
    trip = Trip(date(2026, 12, 30), date(2027, 1, 29), 3846, 3725)
    outbound = OnewayQuote("CHI", "SHA", trip.depart_date, 3846, "CX")
    msg = build_price_message(route, trip, [Alert("new_low", "k", 7571)], {}, (outbound, None))
    assert "¥3,846 · 国泰航空 CX" in msg.content
    assert "¥3,725\n" in msg.content  # 回程没识别出航司就不写


def test_migrate_v3_retries_unknown_airline_and_keeps_known_match() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for version in (1, 2, 3):
        conn.executescript(db._MIGRATIONS[version])
    conn.execute("PRAGMA user_version = 3")
    run_id = repo.start_run(conn, "legacy")
    conn.executemany(
        "INSERT INTO oneway_quote VALUES (?, 'CHI', 'SHA', ?, 4000, 't', ?)",
        [(run_id, "2026-12-01", ""), (run_id, "2026-12-02", "KE")],
    )
    db.migrate(conn)
    detail = repo.latest_oneway_detail(conn, "CHI", "SHA")
    missing, known = detail[date(2026, 12, 1)], detail[date(2026, 12, 2)]
    assert missing.price == known.price == 4000
    assert missing.airline is None
    assert known.airline == "KE" and known.airline_exact
    conn.close()
