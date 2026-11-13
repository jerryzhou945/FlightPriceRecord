"""固定行程与携程往返订票页的真实往返价：解析、到点判断、记录、接口、对提醒和校准的影响。"""

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from farerecord import db, realprice
from farerecord import repository as repo
from farerecord.alerts.rules import watch_trip
from farerecord.analysis import calibration
from farerecord.api.app import create_app
from farerecord.collector import prune_old_runs
from farerecord.config import parse_config
from farerecord.realprice import Pinned
from farerecord.sources.ctrip_page import (
    PageBlocked,
    PageEmpty,
    is_blocked,
    parse_snapshot,
    read_roundtrip,
    round_url,
    wait_for_quotes,
)
from tests.conftest import seed_run

D, R = date(2026, 12, 28), date(2027, 1, 20)
URL = round_url("CHI", "SHA", D, R)
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
PIN = Pinned("CHI", "SHA", D, R)


def snap(**over):
    base = {
        "url": URL,
        "items": [[7352, "土耳其航空"], [7139, "土耳其航空"], [8627, "国泰航空"]],
        "tabs": [[7139, "2026-12-28", "2027-01-20"]],
        "captcha": False,
    }
    return {**base, **over}


def reader_of(result):
    """假的页面读取器：返回固定内容（或抛出给定的异常），并记下被要求打开的网址。"""
    calls: list[str] = []

    def read(url: str, marker: str):
        calls.append(url)
        if isinstance(result, Exception):
            raise result
        return result

    read.calls = calls
    return read


def cfg(tmp_path=None, **collector):
    data = {
        "routes": [{"origin": "CHI", "dest": "SHA"}],
        "collector": {"request_interval_seconds": 0, **collector},
        "alerts": {"new_low": False, "drop_percent": 0, "fail_streak": 0, "desktop": False},
    }
    if tmp_path is not None:
        data["database"] = str(tmp_path / "t.db")
    return parse_config(data)


# ---------- 读页面 ----------


def test_round_url_carries_both_dates() -> None:
    assert "round-chi-sha?depdate=2026-12-28_2027-01-20" in URL


def test_parse_takes_page_lowest_and_its_airline() -> None:
    q = parse_snapshot(snap(), D, R)
    assert (q.price, q.airline) == (7139, "土耳其航空")


def test_parse_prefers_date_strip_over_rendered_rows() -> None:
    # 列表只渲染了前几行（按直飞优先排），整页最低价在日期条上
    s = snap(items=[[9800, "美国联合航空"]], tabs=[[7139, "2026-12-28", "2027-01-20"]])
    q = parse_snapshot(s, D, R)
    assert q.price == 7139 and q.airline is None  # 最低价那一行没渲染出来，航司不乱猜


def test_parse_ignores_strip_for_other_dates_and_absurd_prices() -> None:
    s = snap(tabs=[[5000, "2026-12-29", "2027-01-21"]], items=[[3, "x"], [7139, "土耳其航空"]])
    assert parse_snapshot(s, D, R).price == 7139


def test_parse_reports_blocked_and_empty() -> None:
    with pytest.raises(PageBlocked):
        parse_snapshot(snap(url="https://passport.ctrip.com/user/login", items=[]), D, R)
    with pytest.raises(PageBlocked):
        parse_snapshot(snap(captcha=True, items=[]), D, R)
    with pytest.raises(PageEmpty):
        parse_snapshot(snap(items=[], tabs=[]), D, R)
    with pytest.raises(PageEmpty):  # 携程把日期改掉了
        parse_snapshot(snap(url=URL.replace("2026-12-28", "2026-12-30")), D, R)
    assert not is_blocked(snap(captcha=True))  # 页面上有报价时，验证码组件只是预加载


def test_wait_for_quotes_waits_for_the_search_to_finish() -> None:
    import json

    early = snap(items=[[7133, "土耳其航空"]], tabs=[[7133, "2026-12-28", "2027-01-20"]])
    final = snap(items=[[7125, "土耳其航空"]], tabs=[[7125, "2026-12-28", "2027-01-20"]])
    frames = [
        "",  # 页面还没好
        json.dumps({"url": "about:blank", "items": [[1, "旧页面"]]}),  # 上一个页面还没换掉
        json.dumps(snap(items=[], tabs=[], loading=True)),
        json.dumps({**early, "loading": True}),  # 先出一批价格，还在搜
        json.dumps({**early, "loading": True}),  # 价格没变，但进度条还在走 → 不算数
        json.dumps(final),  # 搜完了，价格变成最终的
        json.dumps(final),  # 再读一次确认没再变
        json.dumps(snap(items=[[1, "不该读到这里"]])),
    ]
    clock = iter(range(100))
    got = wait_for_quotes(
        lambda js: frames.pop(0),
        "depdate=2026-12-28_2027-01-20",
        sleep=lambda s: None,
        clock=lambda: next(clock),
    )
    assert parse_snapshot(got, D, R).price == 7125 and len(frames) == 1


def test_wait_for_quotes_settles_by_time_without_a_progress_bar() -> None:
    import json

    # 页面改版、认不出进度条：报价出现后至少等 settle 秒，且价格不再变
    ticks = iter(range(0, 1000, 2))
    reads: list[int] = []

    def evaluate(js: str) -> str:
        reads.append(1)
        price = 7133 if len(reads) < 4 else 7125
        return json.dumps(snap(items=[[price, "土耳其航空"]], tabs=[]))

    got = wait_for_quotes(
        evaluate, "depdate=", settle=14, sleep=lambda s: None, clock=lambda: next(ticks)
    )
    assert got["items"][0][0] == 7125 and len(reads) >= 5


def test_wait_for_quotes_stops_when_blocked_or_timed_out() -> None:
    import json

    blocked = json.dumps({"url": "https://passport.ctrip.com/login", "items": []})
    assert is_blocked(wait_for_quotes(lambda js: blocked, "x", sleep=lambda s: None))
    clock = iter(range(0, 1000, 10))
    empty = wait_for_quotes(
        lambda js: 1 / 0, "x", timeout=30, sleep=lambda s: None, clock=lambda: next(clock)
    )
    assert empty == {}


def test_read_roundtrip_opens_the_right_page() -> None:
    read = reader_of(snap())
    assert read_roundtrip(read, "CHI", "SHA", D, R).price == 7139
    assert read.calls == [URL]


# ---------- 固定行程、到点判断、记录 ----------


def test_pin_and_unpin(conn) -> None:
    assert realprice.get_pinned(conn, "CHI", "SHA") is None
    realprice.set_pinned(conn, "CHI", "SHA", D, R)
    assert realprice.get_pinned(conn, "CHI", "SHA") == PIN
    assert realprice.pinned_trips(conn, cfg(), date(2026, 11, 11)) == [PIN]
    assert realprice.pinned_trips(conn, cfg(), date(2026, 12, 29)) == []  # 已出发，不再监测
    realprice.set_pinned(conn, "CHI", "SHA", None, None)
    assert realprice.get_pinned(conn, "CHI", "SHA") is None


def test_check_records_price_and_state(conn) -> None:
    state = realprice.check(conn, PIN, reader_of(snap()), NOW)
    assert state["status"] == "ok" and state["price"] == 7139
    q = repo.latest_real_quote(conn, "CHI", "SHA", D, R)
    assert q["price"] == 7139 and q["airline"] == "土耳其航空"
    assert q["observed_at"] == "2026-11-11T12:00:00+00:00"
    assert realprice.check_state(conn, PIN)["status"] == "ok"


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (snap(url="https://passport.ctrip.com/login", items=[]), "blocked"),
        (snap(items=[], tabs=[]), "empty"),
        (RuntimeError("窗口没开起来"), "error"),
    ],
)
def test_check_failures_are_recorded_not_silent(conn, result, status) -> None:
    state = realprice.check(conn, PIN, reader_of(result), NOW)
    assert state["status"] == status and state["message"]
    assert repo.latest_real_quote(conn, "CHI", "SHA", D, R) is None
    assert realprice.check_state(conn, PIN)["status"] == status


def test_due_respects_interval_and_backs_off_when_blocked(conn) -> None:
    half_hour = timedelta(minutes=30)
    assert realprice.is_due(conn, PIN, half_hour, NOW)  # 从没核过
    realprice.check(conn, PIN, reader_of(snap()), NOW)
    assert not realprice.is_due(conn, PIN, half_hour, NOW + timedelta(minutes=20))
    assert realprice.is_due(conn, PIN, half_hour, NOW + timedelta(minutes=29))
    # 换了日期：马上核新的
    assert realprice.is_due(conn, Pinned("CHI", "SHA", D, R + timedelta(days=1)), half_hour, NOW)
    # 被要求登录/验证：隔 6 小时才再试
    realprice.check(conn, PIN, reader_of(snap(captcha=True, items=[])), NOW)
    assert not realprice.is_due(conn, PIN, half_hour, NOW + timedelta(hours=5))
    assert realprice.is_due(conn, PIN, half_hour, NOW + timedelta(hours=6))


def test_check_interval_follows_collector_with_a_floor() -> None:
    assert realprice.check_interval(cfg(interval_minutes=15)) == timedelta(minutes=15)
    assert realprice.check_interval(cfg()) == timedelta(minutes=30)  # 默认跟抓取间隔一致
    assert realprice.check_interval(cfg(interval_minutes=120)) == timedelta(minutes=120)


def test_run_due_checks_only_when_due(conn) -> None:
    c = cfg()
    read = reader_of(snap())
    assert realprice.run_due(conn, c, read, now=NOW) == []  # 没有固定行程
    realprice.set_pinned(conn, "CHI", "SHA", D, R)
    assert realprice.any_due(conn, c, NOW)
    assert [r["status"] for r in realprice.run_due(conn, c, read, now=NOW)] == ["ok"]
    assert realprice.run_due(conn, c, read, now=NOW + timedelta(minutes=5)) == []
    assert len(realprice.run_due(conn, c, read, now=NOW + timedelta(minutes=5), force=True)) == 1
    assert len(read.calls) == 2


def test_monitor_tick_uses_its_own_connection(tmp_path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(c.database)
    realprice.set_pinned(conn, "CHI", "SHA", D, R)
    monitor = realprice.RealPriceMonitor(c, reader_of(snap()))
    assert [r["status"] for r in monitor.tick()] == ["ok"]
    assert monitor.tick() == [] and not monitor.busy  # 刚核过，不重复
    assert repo.latest_real_quote(conn, "CHI", "SHA", D, R)["price"] == 7139
    conn.close()


def test_old_real_quotes_are_pruned(conn) -> None:
    repo.add_real_quote(conn, "CHI", "SHA", D, R, 7000, None, "2026-09-01T00:00:00+00:00")
    repo.add_real_quote(conn, "CHI", "SHA", D, R, 7139, None, "2026-11-11T00:00:00+00:00")
    prune_old_runs(conn, 30, NOW)
    assert [q["price"] for q in repo.real_quotes(conn, "CHI", "SHA", D, R)] == [7139]


# ---------- 对提醒和校准的影响 ----------


def test_watch_uses_fresh_real_price(conn) -> None:
    seed_run(conn, "2026-11-11T11:00:00+00:00", {D: 4300}, {R: 4239})
    repo.add_watch(conn, "CHI", "SHA", D, R, 7200)
    watch = repo.list_watches(conn)[0]
    assert watch_trip(conn, watch).best_price == 8539  # 没有实价：两张单程（没有校准数据）
    fresh = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    repo.add_real_quote(conn, "CHI", "SHA", D, R, 7139, "土耳其航空", fresh)
    trip = watch_trip(conn, watch)
    assert trip.rt_price == 7139 and trip.best_price == 7139


def test_stale_real_price_is_not_current(conn) -> None:
    old = (NOW - timedelta(hours=4)).isoformat()
    repo.add_real_quote(conn, "CHI", "SHA", D, R, 7139, None, old)
    assert realprice.fresh_real_price(conn, "CHI", "SHA", D, R, NOW) is None
    assert realprice.fresh_real_price(conn, "CHI", "SHA", D, R, NOW - timedelta(hours=2)) == 7139


def test_page_quotes_do_not_move_the_estimate_of_other_trips(conn) -> None:
    seed_run(conn, "2026-11-11T11:00:00+00:00", {D: 4300}, {R: 4239})
    repo.add_real_quote(conn, "CHI", "SHA", D, R, 7139, None, "2026-11-11T12:00:00+00:00")
    assert calibration.current(conn, "CHI", "SHA", NOW).source == "none"


# ---------- 接口 ----------


class FakeMonitor:
    busy = False

    def __init__(self) -> None:
        self.kicks = 0

    def kick(self) -> None:
        self.kicks += 1


@pytest.fixture
def client(tmp_path):
    c = cfg(tmp_path)
    app = create_app(c, web_dir=None, env_file=tmp_path / ".env")
    with TestClient(app) as tc:
        tc.cfg = c
        yield tc


def pin_body(depart: date, ret: date | None) -> dict:
    return {
        "origin": "CHI",
        "dest": "SHA",
        "depart": depart.isoformat() if depart else None,
        "ret": ret.isoformat() if ret else None,
    }


def test_api_pin_roundtrip(client) -> None:
    depart = date.today() + timedelta(days=40)
    ret = depart + timedelta(days=23)
    monitor = client.app.state.real_monitor = FakeMonitor()
    assert client.get("/api/routes").json()[0]["pinned"] is None
    r = client.put("/api/pinned", json=pin_body(depart, ret))
    assert r.status_code == 200 and r.json()["pinned"]["depart"] == depart.isoformat()
    assert monitor.kicks == 1  # 固定后马上去核价
    assert client.get("/api/routes").json()[0]["pinned"]["return"] == ret.isoformat()
    assert client.put("/api/pinned", json=pin_body(None, None)).json() == {"pinned": None}
    assert client.get("/api/routes").json()[0]["pinned"] is None


def test_api_pin_validation(client) -> None:
    today = date.today()
    far = today + timedelta(days=40)
    assert client.put("/api/pinned", json=pin_body(far, far)).status_code == 400
    assert (
        client.put("/api/pinned", json=pin_body(far, far + timedelta(days=200))).status_code == 400
    )
    past = today - timedelta(days=1)
    assert client.put("/api/pinned", json=pin_body(past, far)).status_code == 400
    other = {**pin_body(far, far + timedelta(days=5)), "dest": "BJS"}
    assert client.put("/api/pinned", json=other).status_code == 404


def test_api_history_shows_real_prices(client) -> None:
    depart = date.today() + timedelta(days=40)
    ret = depart + timedelta(days=23)
    conn = db.connect(client.cfg.database)
    realprice.set_pinned(conn, "CHI", "SHA", depart, ret)
    pinned = realprice.get_pinned(conn, "CHI", "SHA")
    realprice.check(
        conn, pinned, reader_of(snap(url=round_url("CHI", "SHA", depart, ret), tabs=[]))
    )
    conn.close()
    params = {"origin": "CHI", "dest": "SHA", "depart": depart.isoformat(), "ret": ret.isoformat()}
    real = client.get("/api/history", params=params).json()["real"]
    assert real["pinned"] and real["fresh"] and real["check"]["status"] == "ok"
    assert real["series"][0][1:] == [7139, "土耳其航空"]
    assert real["stats"]["current"] == 7139
    assert real["airline"]["code"] == "TK" and real["airline"]["short"] == "土航"
    # 别的日期没有实价，也不算固定
    params["ret"] = (ret + timedelta(days=1)).isoformat()
    other = client.get("/api/history", params=params).json()["real"]
    assert not other["pinned"] and other["series"] == [] and other["check"] is None


def test_api_real_check_needs_the_desktop_monitor(client) -> None:
    assert client.get("/api/status").json()["real_monitor"] is False
    assert client.post("/api/real-check").status_code == 409
    monitor = client.app.state.real_monitor = FakeMonitor()
    assert client.post("/api/real-check").status_code == 202 and monitor.kicks == 1
    assert client.get("/api/status").json()["real_monitor"] is True


def test_parse_reads_seat_hint_and_next_level() -> None:
    # 实测页面：土航最低一档没提示余票，全日空那行写着"剩1张"
    s = snap(
        items=[
            [7259, "土耳其航空", None],
            [7259, "土耳其航空", None],
            [7333, "土耳其航空", None],
            [9018, "全日空航空", 1],
        ],
        tabs=[[7259, "2026-12-28", "2027-01-20"]],
    )
    q = parse_snapshot(s, D, R)
    assert (q.price, q.seats_left, q.next_price) == (7259, None, 7333)
    rows = [[7125, "土耳其航空", 2], [7125, "土耳其航空", 0], [7325, "土耳其航空", None]]
    tight = snap(items=rows, tabs=[])  # 0 张这种读数不可信，忽略
    q = parse_snapshot(tight, D, R)
    assert (q.price, q.airline, q.seats_left, q.next_price) == (7125, "土耳其航空", 2, 7325)
    # 最低价那一行没渲染出来：余票未知，下一档取页面上更高的价
    q = parse_snapshot(snap(items=[[9800, "美国联合航空", 3]]), D, R)
    assert (q.price, q.seats_left, q.next_price) == (7139, None, 9800)


def test_check_saves_seats(tmp_path) -> None:
    from farerecord import db, realprice
    from farerecord import repository as repo

    conn = db.connect(tmp_path / "t.db")
    s = snap(items=[[7125, "土耳其航空", 2], [7325, "土耳其航空", None]], tabs=[])
    realprice.check(conn, PIN, reader_of(s), NOW)
    q = repo.latest_real_quote(conn, "CHI", "SHA", D, R)
    assert (q["price"], q["seats_left"], q["next_price"]) == (7125, 2, 7325)
