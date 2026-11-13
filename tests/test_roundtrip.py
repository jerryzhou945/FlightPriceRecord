"""往返预估：校准比例的来源、对组合/走势/提醒的影响。"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from farerecord import db
from farerecord import repository as repo
from farerecord.alerts import run_after_collect
from farerecord.alerts.notifiers import ConsoleNotifier
from farerecord.analysis import calibration
from farerecord.analysis.calibration import Calibration, ratio_from_quotes
from farerecord.analysis.flex import FlexQuery, search
from farerecord.analysis.history import combo_history, flex_low_history
from farerecord.api.app import create_app
from farerecord.collector import collect
from farerecord.config import parse_config
from farerecord.models import OnewayQuote, RoundtripQuote, Trip
from tests.conftest import daterange, seed_run
from tests.test_collector import TODAY, FakeSource

D, R = date(2026, 12, 30), date(2027, 1, 29)
CST = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=CST)
T1, T2 = "2026-11-10T00:00:00+00:00", "2026-11-10T01:00:00+00:00"


def cfg(**route):
    return parse_config(
        {
            "routes": [
                {
                    "origin": "CHI",
                    "dest": "SHA",
                    "flex": {"start": "2026-12-01", "end": "2026-12-31"},
                    **route,
                }
            ],
            "alerts": {"new_low": False, "drop_percent": 0, "fail_streak": 0, "desktop": False},
        }
    )


def seed_rt(conn, run_id: int, t: str, ratio: float, n: int = 10, stay: int = 12) -> None:
    """给这次抓取补上 n 个短途往返缓存，真实往返价 = 两张单程之和 × ratio。"""
    start = date(2027, 3, 1)
    days = daterange(start, n)
    repo.save_oneway(conn, run_id, [OnewayQuote("CHI", "SHA", d, 4000) for d in days], t)
    back = [OnewayQuote("SHA", "CHI", d + timedelta(days=stay), 4000) for d in days]
    repo.save_oneway(conn, run_id, back, t)
    repo.save_roundtrip(
        conn,
        run_id,
        [RoundtripQuote("CHI", "SHA", d, d + timedelta(days=stay), 8000 * ratio) for d in days],
        t,
    )


# ---------- Trip ----------


def test_trip_estimate_and_best_price() -> None:
    t = Trip(D, R, 4487, 4052, ratio=0.85)
    assert t.price == 8539 and t.estimate == 7258 and t.best_price == 7258
    assert t.raw_price == 8539  # 去重用，不含校准
    # 有真实往返价时用真实的；两张单程更便宜就用单程
    assert Trip(D, R, 4000, 4000, rt_price=6000, ratio=0.85).best_price == 6000
    assert Trip(D, R, 4000, 4000, rt_price=9000, ratio=0.85).best_price == 8000
    assert Trip(D, R, 4000, 4000).best_price == 8000  # 没有校准时就是两张单程


def test_search_applies_ratio_to_every_trip() -> None:
    out = {D: 4000.0, D - timedelta(days=1): 4100.0}
    back = {R: 4000.0}
    res = search(out, back, FlexQuery(date(2026, 12, 1), date(2026, 12, 31), 25, 35), ratio=0.9)
    assert res.best.depart_date == D and res.best.best_price == 7200
    assert {t.ratio for t in res.trips} == {0.9}


# ---------- 校准比例 ----------


def test_ratio_from_quotes_uses_long_stays_and_needs_enough_samples() -> None:
    start = date(2027, 3, 1)
    out = {d: 4000.0 for d in daterange(start, 12)}
    back = {d: 4000.0 for d in daterange(start, 40)}
    rt = {(d, d + timedelta(days=12)): 6800.0 for d in daterange(start, 9)}
    rt.update({(d, d + timedelta(days=3)): 12000.0 for d in daterange(start, 9)})  # 短停留不算
    assert ratio_from_quotes(out, back, rt) == (0.85, 9)
    few = {k: v for k, v in list(rt.items())[:5]}
    assert ratio_from_quotes(out, back, few) is None
    assert ratio_from_quotes({}, back, rt) is None


def test_current_calibration_prefers_manual_then_cache_then_none(conn) -> None:
    assert calibration.current(conn, "CHI", "SHA") == Calibration(1.0, "none", 0)
    run_id = seed_run(conn, T1, {D: 4487}, {R: 4052})
    assert calibration.current(conn, "CHI", "SHA").source == "none"  # 没有往返缓存
    seed_rt(conn, run_id, T1, 0.85)
    assert calibration.record(conn, run_id, "CHI", "SHA") == pytest.approx(0.85)
    cal = calibration.current(conn, "CHI", "SHA")
    assert cal.source == "cache" and cal.ratio == pytest.approx(0.85) and cal.samples == 10
    assert cal.apply(8539) == 7258
    # 用户核过真实往返价 7132（两张单程 8539）→ 用它校准
    repo.add_manual_quote(conn, "CHI", "SHA", D, R, 7132, observed_at=T2)
    now = datetime.fromisoformat(T2) + timedelta(hours=5)
    manual = calibration.current(conn, "CHI", "SHA", now)
    assert manual.source == "manual" and manual.ratio == pytest.approx(7132 / 8539)
    # 三天后手动核价过期，回到携程缓存算出的比例
    later = calibration.current(conn, "CHI", "SHA", now + timedelta(days=4))
    assert later.source == "cache"


def test_calibration_is_clamped_and_uses_median_of_recent_runs(conn) -> None:
    for i, ratio in enumerate((0.80, 0.86, 0.90)):
        t = f"2026-11-10T0{i}:00:00+00:00"
        run_id = seed_run(conn, t, {D: 4000}, {R: 4000})
        seed_rt(conn, run_id, t, ratio)
        calibration.record(conn, run_id, "CHI", "SHA")
    assert calibration.current(conn, "CHI", "SHA").ratio == pytest.approx(0.86)
    # 往返比两张单程还贵的航线：不往上调，两张单程永远买得到
    run_id = seed_run(conn, "2026-11-11T00:00:00+00:00", {D: 4000}, {R: 4000})
    conn.execute("DELETE FROM rt_calibration")
    seed_rt(conn, run_id, "2026-11-11T00:00:00+00:00", 1.3)
    calibration.record(conn, run_id, "CHI", "SHA")
    assert calibration.current(conn, "CHI", "SHA").ratio == 1.0


def test_collect_records_calibration(conn) -> None:
    class RtSource(FakeSource):
        def fetch_oneway(self, origin, dest, start):
            return [OnewayQuote(origin, dest, start + timedelta(days=i), 4000) for i in range(60)]

        def fetch_roundtrip_cache(self, origin, dest, depart, ret):
            return [
                RoundtripQuote(origin, dest, d, d + timedelta(days=12), 6800)
                for d in daterange(depart, 10)
            ]

    res = collect(conn, cfg(), RtSource(), today=TODAY, now=lambda: T1, sleep=lambda s: None)
    assert res.status == "ok"
    assert repo.recent_calibrations(conn, "CHI", "SHA", 5) == [(0.85, 10)]


# ---------- 走势 ----------


def test_histories_scale_by_ratio_and_match_search(conn) -> None:
    out = {x: 5000.0 for x in daterange(date(2026, 12, 1), 31)}
    back = {x: 5000.0 for x in daterange(date(2026, 12, 20), 60)}
    seed_run(conn, T1, {**out, D: 4000}, {**back, R: 4000})
    seed_run(conn, T2, {**out, D: 3846}, {**back, R: 3725})
    seed_run(conn, "2026-11-10T02:00:00+00:00", out, {})  # 只有去程的抓取不产生点
    q = FlexQuery(date(2026, 12, 1), date(2026, 12, 31), 25, 35, 30)
    points = flex_low_history(conn, "CHI", "SHA", q, ratio=0.9)
    assert [(p.observed_at, p.best.best_price) for p in points] == [(T1, 7200), (T2, 6814)]
    assert (points[-1].best.depart_date, points[-1].best.return_date) == (D, R)
    assert combo_history(conn, "CHI", "SHA", D, R, 0.9) == [(T1, 7200), (T2, 6814)]
    # 和逐次调用 search 的结果一致（包括同价时的取舍）
    for run_id, _ in repo.runs_with_route(conn, "CHI", "SHA")[:2]:
        expect = search(
            repo.oneway_at_run(conn, run_id, "CHI", "SHA"),
            repo.oneway_at_run(conn, run_id, "SHA", "CHI"),
            q,
        ).best
        got = flex_low_history(conn, "CHI", "SHA", q)[run_id - 1].best
        assert (got.depart_date, got.return_date, got.price) == (
            expect.depart_date,
            expect.return_date,
            expect.price,
        )


def test_flex_low_history_tie_prefers_ideal_days(conn) -> None:
    out = {x: 5000.0 for x in daterange(date(2026, 12, 1), 5)}
    back = {x: 5000.0 for x in daterange(date(2026, 12, 20), 60)}
    seed_run(conn, T1, out, back)
    q = FlexQuery(date(2026, 12, 1), date(2026, 12, 5), 25, 35, 30)
    best = flex_low_history(conn, "CHI", "SHA", q)[0].best
    assert (best.depart_date, best.days) == (date(2026, 12, 1), 30)


# ---------- 提醒 ----------


def test_target_alert_uses_estimate_and_is_not_repeated_when_ratio_drifts(conn) -> None:
    run_id = seed_run(conn, T1, {D: 4487}, {R: 4052})
    n = ConsoleNotifier()
    config = cfg(target_price=7500)
    assert run_after_collect(conn, config, notifier=n, now=NOON) == []  # 两张单程 8539 > 7500
    seed_rt(conn, run_id, T1, 0.85)
    calibration.record(conn, run_id, "CHI", "SHA")
    sent = run_after_collect(conn, config, notifier=n, now=NOON)
    assert len(sent) == 1 and "低于目标价 ¥7,258" in sent[0].title
    assert "往返预估 ¥7,258，分开买两张单程 ¥8,539" in sent[0].content
    # 校准比例变了一点、真实单程价没变：不重复提醒
    conn.execute("UPDATE rt_calibration SET ratio = 0.84")
    conn.commit()
    assert run_after_collect(conn, config, notifier=n, now=NOON) == []


# ---------- API ----------


def test_api_reports_estimate_sum_and_calibration(tmp_path) -> None:
    config = parse_config(
        {
            "database": str(tmp_path / "t.db"),
            "routes": [
                {
                    "origin": "CHI",
                    "dest": "SHA",
                    "flex": {"start": "2026-12-01", "end": "2026-12-31"},
                }
            ],
        }
    )
    conn = db.connect(config.database)
    run_id = seed_run(conn, T1, {D: 4487}, {R: 4052})
    seed_rt(conn, run_id, T1, 0.85)
    calibration.record(conn, run_id, "CHI", "SHA")
    conn.close()
    client = TestClient(create_app(config, web_dir=None))
    params = {"origin": "CHI", "dest": "SHA", "start": "2026-12-01", "end": "2026-12-31"}
    flex = client.get("/api/flex", params={**params, "min": 25, "max": 35}).json()
    assert flex["calibration"] == {"ratio": 0.85, "source": "cache", "samples": 10}
    best = flex["best"]
    assert (best["price"], best["oneway_sum"], best["best_price"]) == (7258, 8539, 7258)
    assert flex["low_history"][0]["price"] == 7258 and flex["cells"][0][2] == 7258
    h = client.get("/api/history", params={**params, "depart": str(D), "ret": str(R)}).json()
    assert h["series"] == [[T1, 7258]] and h["legs"]["sum"] == 8539
    assert h["calibration"]["source"] == "cache"
    # 录入真实往返价后，校准改用它
    body = {"origin": "CHI", "dest": "SHA", "depart": str(D), "ret": str(R), "price": 7132}
    assert client.post("/api/manual-quote", json=body).status_code == 201
    h = client.get("/api/history", params={**params, "depart": str(D), "ret": str(R)}).json()
    assert h["calibration"]["source"] == "manual" and h["series"][0][1] == 7132
