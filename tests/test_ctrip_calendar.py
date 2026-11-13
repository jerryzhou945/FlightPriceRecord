import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from farerecord.sources.base import RetryableSourceError, SourceError
from farerecord.sources.ctrip_calendar import (
    URL,
    CtripCalendarSource,
    build_payload,
    parse_ctrip_date,
    parse_oneway,
    parse_roundtrip,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def test_parse_ctrip_date() -> None:
    assert parse_ctrip_date("/Date(1794326400000+0800)/") == date(2026, 11, 11)
    assert parse_ctrip_date("/Date(-62135798399433+0800)/") is None
    assert parse_ctrip_date("2026-11-11") is None
    assert parse_ctrip_date(None) is None


def test_parse_oneway_fixture_skips_zero_prices() -> None:
    data = load("ctrip_oneway_chi_sha.json")
    quotes = parse_oneway(data, "chi", "sha")
    raw_nonzero = [r for r in data["priceList"] if r["totalPrice"]]
    assert len(quotes) == len(raw_nonzero) > 0
    assert all(q.origin == "CHI" and q.dest == "SHA" for q in quotes)
    assert all(q.price > 0 for q in quotes)
    assert [q.depart_date for q in quotes] == sorted(q.depart_date for q in quotes)


def test_parse_roundtrip_fixture() -> None:
    quotes = parse_roundtrip(load("ctrip_roundtrip_chi_sha.json"), "CHI", "SHA")
    assert quotes
    assert all(q.return_date > q.depart_date for q in quotes)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"responseStatus": {"Ack": "Failure", "Errors": [{"Message": "x"}]}, "priceList": []},
        {"responseStatus": {"Ack": "Success", "Errors": []}},
    ],
)
def test_parse_rejects_bad_responses(data) -> None:
    with pytest.raises(SourceError):
        parse_oneway(data, "CHI", "SHA")


def test_parse_keeps_min_for_duplicate_dates() -> None:
    ok = {"Ack": "Success", "Errors": []}
    d = "/Date(1794326400000+0800)/"
    data = {
        "responseStatus": ok,
        "priceList": [
            {"departDate": d, "totalPrice": 5000},
            {"departDate": d, "totalPrice": 4000},
            {"departDate": d, "totalPrice": float("nan")},
            {"departDate": d, "totalPrice": True},
        ],
    }
    assert [q.price for q in parse_oneway(data, "CHI", "SHA")] == [4000]


def test_build_payload_flags() -> None:
    one = build_payload("chi", "sha", date(2026, 12, 1))
    assert one["flag"] == 1 and "returnDate" not in one
    assert one["departNewCityCode"] == "CHI" and one["startDate"] == "2026-12-01"
    rt = build_payload("CHI", "SHA", date(2026, 12, 1), date(2026, 12, 8))
    assert rt["flag"] == 5 and rt["returnDate"] == "2026-12-08"


def _source(handler) -> CtripCalendarSource:
    return CtripCalendarSource(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_fetch_oneway_posts_payload() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=load("ctrip_oneway_chi_sha.json"))

    quotes = _source(handler).fetch_oneway("CHI", "SHA", date(2026, 12, 1))
    assert seen["url"] == URL
    assert seen["body"]["flag"] == 1
    assert quotes


@pytest.mark.parametrize(
    ("status", "exc"),
    [(429, RetryableSourceError), (503, RetryableSourceError), (403, SourceError)],
)
def test_fetch_http_errors(status, exc) -> None:
    src = _source(lambda r: httpx.Response(status, text="no"))
    with pytest.raises(exc):
        src.fetch_oneway("CHI", "SHA", date(2026, 12, 1))


def test_fetch_non_json_is_source_error() -> None:
    src = _source(lambda r: httpx.Response(200, text="<html>blocked</html>"))
    with pytest.raises(SourceError) as info:
        src.fetch_oneway("CHI", "SHA", date(2026, 12, 1))
    assert not isinstance(info.value, RetryableSourceError)


def test_fetch_timeout_is_retryable() -> None:
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RetryableSourceError):
        _source(handler).fetch_oneway("CHI", "SHA", date(2026, 12, 1))
