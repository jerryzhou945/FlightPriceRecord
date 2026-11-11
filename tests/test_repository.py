from datetime import date

from farerecord import repository as repo
from farerecord.models import RoundtripQuote
from tests.conftest import seed_run

D1, D2, R1 = date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 29)


def test_latest_oneway_uses_newest_run(conn) -> None:
    seed_run(conn, "2026-11-10T00:00:00+00:00", {D1: 4000, D2: 4100}, {R1: 3900})
    seed_run(conn, "2026-11-10T06:00:00+00:00", {D1: 3846}, {R1: 3725})
    assert repo.latest_oneway(conn, "CHI", "SHA") == {D1: 3846}
    assert repo.latest_oneway(conn, "SHA", "CHI") == {R1: 3725}
    assert repo.latest_oneway(conn, "CHI", "PEK") == {}
    assert repo.latest_observed_at(conn, "CHI", "SHA") == "2026-11-10T06:00:00+00:00"


def test_series(conn) -> None:
    seed_run(conn, "2026-11-10T00:00:00+00:00", {D1: 4000}, {R1: 3900})
    seed_run(conn, "2026-11-10T06:00:00+00:00", {D1: 3846}, {})
    seed_run(conn, "2026-11-10T12:00:00+00:00", {D1: 3800}, {R1: 3700})
    assert repo.oneway_series(conn, "CHI", "SHA", D1) == [
        ("2026-11-10T00:00:00+00:00", 4000),
        ("2026-11-10T06:00:00+00:00", 3846),
        ("2026-11-10T12:00:00+00:00", 3800),
    ]
    # 第二次抓取缺回程，组合历史里跳过
    assert repo.combo_series(conn, "CHI", "SHA", D1, R1) == [
        ("2026-11-10T00:00:00+00:00", 4000, 3900),
        ("2026-11-10T12:00:00+00:00", 3800, 3700),
    ]
    assert [t for _, t in repo.runs_with_route(conn, "SHA", "CHI")] == [
        "2026-11-10T00:00:00+00:00",
        "2026-11-10T12:00:00+00:00",
    ]


def test_fail_streak(conn) -> None:
    seed_run(conn, "2026-11-10T00:00:00+00:00", {D1: 1}, {}, status="ok")
    assert repo.fail_streak(conn) == 0
    seed_run(conn, "2026-11-10T06:00:00+00:00", {}, {}, status="failed")
    seed_run(conn, "2026-11-10T12:00:00+00:00", {}, {}, status="failed")
    assert repo.fail_streak(conn) == 2
    assert len(repo.recent_runs(conn, 2)) == 2


def test_roundtrip_and_manual(conn) -> None:
    run_id = repo.start_run(conn, "test")
    repo.save_roundtrip(
        conn, run_id, [RoundtripQuote("CHI", "SHA", D1, date(2027, 1, 5), 6000)], "t"
    )
    assert repo.latest_roundtrip(conn, "CHI", "SHA") == {(D1, date(2027, 1, 5)): 6000}
    repo.add_manual_quote(conn, "CHI", "SHA", D1, R1, 7100, "官网")
    rows = repo.manual_quotes(conn, "CHI", "SHA", D1, R1)
    assert rows[0]["price"] == 7100 and rows[0]["note"] == "官网"
