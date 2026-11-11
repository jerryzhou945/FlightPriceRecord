from datetime import date, timedelta

from farerecord import repository as repo
from farerecord.collector import collect
from farerecord.config import parse_config
from farerecord.models import OnewayQuote, RoundtripQuote
from farerecord.sources.base import RetryableSourceError, SourceError

TODAY = date(2026, 11, 10)


class FakeSource:
    name = "fake"

    def __init__(self, fail: dict | None = None) -> None:
        self.fail = fail or {}  # (origin, dest, kind) -> list of exceptions to raise in order
        self.calls: list[tuple] = []

    def _maybe_fail(self, key):
        errs = self.fail.get(key)
        if errs:
            raise errs.pop(0)

    def fetch_oneway(self, origin, dest, start):
        self.calls.append((origin, dest, "ow"))
        self._maybe_fail((origin, dest, "ow"))
        days = [start - timedelta(days=1)] + [start + timedelta(days=i) for i in range(3)]
        return [OnewayQuote(origin, dest, d, 4000 + i) for i, d in enumerate(days)]

    def fetch_roundtrip_cache(self, origin, dest, depart, ret):
        self.calls.append((origin, dest, "rt"))
        self._maybe_fail((origin, dest, "rt"))
        return [RoundtripQuote(origin, dest, depart, ret, 6000)]


def cfg(routes=(("CHI", "SHA"),)):
    return parse_config({"routes": [{"origin": o, "dest": d} for o, d in routes]})


def run(conn, source, config=None):
    sleeps = []
    res = collect(
        conn,
        config or cfg(),
        source,
        today=TODAY,
        now=lambda: "2026-11-10T10:00:00+00:00",
        sleep=sleeps.append,
    )
    return res, sleeps


def test_collect_ok_saves_both_directions(conn) -> None:
    res, sleeps = run(conn, FakeSource())
    assert res.status == "ok" and not res.errors
    # 昨天的价格被过滤：每方向 3 条 + 往返 1 条
    assert res.n_quotes == 7
    assert set(repo.latest_oneway(conn, "SHA", "CHI")) == {
        TODAY + timedelta(days=i) for i in range(3)
    }
    assert repo.latest_roundtrip(conn, "CHI", "SHA")
    assert len(sleeps) == 2  # 三次请求之间间隔两次
    assert repo.recent_runs(conn, 1)[0]["status"] == "ok"


def test_retryable_error_retries_once(conn) -> None:
    src = FakeSource({("CHI", "SHA", "ow"): [RetryableSourceError("429")]})
    res, sleeps = run(conn, src)
    assert res.status == "ok"
    assert src.calls.count(("CHI", "SHA", "ow")) == 2


def test_partial_when_one_call_fails(conn) -> None:
    src = FakeSource({("SHA", "CHI", "ow"): [SourceError("格式变化")]})
    res, _ = run(conn, src)
    assert res.status == "partial"
    assert "SHA→CHI" in res.errors[0]
    assert repo.latest_oneway(conn, "SHA", "CHI") == {}
    assert repo.recent_runs(conn, 1)[0]["error"]


def test_failed_when_nothing_saved_and_other_routes_continue(conn) -> None:
    boom = [SourceError("x")]
    src = FakeSource(
        {
            ("CHI", "SHA", "ow"): list(boom),
            ("SHA", "CHI", "ow"): list(boom),
            ("CHI", "SHA", "rt"): list(boom),
        }
    )
    res, _ = run(conn, src)
    assert res.status == "failed" and len(res.errors) == 3

    src2 = FakeSource({("CHI", "SHA", "ow"): [SourceError("x")]})
    res2, _ = run(conn, src2, cfg((("CHI", "SHA"), ("CHI", "BJS"))))
    assert res2.status == "partial"
    assert res2.per_route["CHI-BJS"] == 7


def test_is_due_respects_interval(conn) -> None:
    from datetime import UTC, datetime

    from farerecord.collector import is_due, next_due_at

    assert is_due(conn, 60) and next_due_at(conn, 60) is None  # 从没抓过
    repo.start_run(conn, "fake", "2026-11-10T10:00:00+00:00")
    at = lambda h, m: datetime(2026, 11, 10, h, m, tzinfo=UTC)  # noqa: E731
    assert next_due_at(conn, 60) == at(11, 0)
    assert not is_due(conn, 60, at(10, 30))
    assert is_due(conn, 60, at(10, 56))  # 5 分钟以内的提前量算到点
    assert is_due(conn, 60, at(12, 0))


def test_interval_has_a_floor() -> None:
    c = parse_config({"routes": [], "collector": {"interval_minutes": 5}})
    assert c.collector.interval_minutes == 30
    assert parse_config({"routes": []}).collector.interval_minutes == 60
