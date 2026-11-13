"""同程往返结果页：解析、和携程分开记录、只在桌面端且开启后才读、提醒取两边的低价。"""

from datetime import UTC, date, datetime, timedelta

import pytest

from farerecord import db, realprice, settings
from farerecord import repository as repo
from farerecord.config import parse_config
from farerecord.realprice import Pinned
from farerecord.sources import ly_page
from farerecord.sources.ctrip_page import PageBlocked, PageEmpty

D, R = date(2026, 12, 28), date(2027, 1, 20)
NOW = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)
PIN = Pinned("CHI", "SHA", D, R)
URL = "https://www.ly.com/iflight/book1.html?para=CHI*SHA*2026-12-28*2027-01-20*RT*1_0_0*Y|S|C|F"


def page(**over):
    """实测同程结果页（2026-11-12）上读出来的内容，只留前几行。"""
    base = {
        "url": URL,
        "items": [
            [6968, "土耳其航空", None],
            [7146, "土耳其航空", None],
            [7160, "土耳其航空", None],
            [7735, "土耳其航空", 6],
            [8366, "全日空", 2],
        ],
        "tabs": [[6968, "2026-12-28", "2027-01-20"]],
        "loading": False,
        "captcha": False,
    }
    return {**base, **over}


def reader_of(result):
    calls: list[tuple[str, str, str]] = []

    def read(url: str, marker: str, script: str = ""):
        calls.append((url, marker, script))
        if isinstance(result, Exception):
            raise result
        return result

    read.calls = calls
    return read


def cfg(tmp_path):
    return parse_config(
        {
            "database": str(tmp_path / "t.db"),
            "routes": [{"origin": "CHI", "dest": "SHA"}],
            "alerts": {"new_low": False, "drop_percent": 0, "fail_streak": 0, "desktop": False},
        }
    )


def test_round_url_and_marker() -> None:
    assert ly_page.round_url("chi", "sha", D, R) == URL
    assert ly_page.dates_marker(D, R) == "2026-12-28*2027-01-20"


def test_read_roundtrip_parses_page() -> None:
    read = reader_of(page())
    q = ly_page.read_roundtrip(read, "CHI", "SHA", D, R)
    assert (q.price, q.airline, q.seats_left, q.next_price) == (6968, "土耳其航空", None, 7146)
    url, marker, script = read.calls[0]
    assert url == URL and marker == "2026-12-28*2027-01-20" and ".airways-title" in script


def test_read_roundtrip_login_and_empty() -> None:
    login = page(url="https://passport.ly.com/?pageurl=xxx", items=[], tabs=[])
    with pytest.raises(PageBlocked, match="同程需要重新登录"):
        ly_page.read_roundtrip(reader_of(login), "CHI", "SHA", D, R)
    with pytest.raises(PageEmpty, match="同程"):
        ly_page.read_roundtrip(reader_of(page(items=[], tabs=[])), "CHI", "SHA", D, R)


def test_check_keeps_sources_apart(tmp_path) -> None:
    conn = db.connect(tmp_path / "t.db")
    ctrip = {
        "url": "https://flights.ctrip.com/online/list/round-chi-sha?depdate=2026-12-28_2027-01-20",
        "items": [[7259, "土耳其航空", None], [7336, "土耳其航空", None]],
        "tabs": [],
    }
    realprice.check(conn, PIN, reader_of(ctrip), NOW)
    state = realprice.check(conn, PIN, reader_of(page()), NOW, source="ly")
    assert state["status"] == "ok" and state["price"] == 6968
    assert repo.latest_real_quote(conn, "CHI", "SHA", D, R)["price"] == 7259
    assert repo.latest_real_quote(conn, "CHI", "SHA", D, R, "ly")["price"] == 6968
    assert [q["price"] for q in repo.real_quotes(conn, "CHI", "SHA", D, R, "ly")] == [6968]
    assert realprice.check_state(conn, PIN, "ly")["price"] == 6968
    assert realprice.check_state(conn, PIN)["price"] == 7259
    # 提醒用两边里更便宜、还新鲜的那个
    assert realprice.fresh_real_price(conn, "CHI", "SHA", D, R, NOW) == 6968
    later = NOW + realprice.FRESH + timedelta(minutes=1)
    assert realprice.fresh_real_price(conn, "CHI", "SHA", D, R, later) is None
    # 每日留档分开记
    assert repo.daily_prices(conn, "real_ly", "CHI", "SHA", D, D, R)[0]["low"] == 6968


def test_ly_only_on_desktop_after_enabled(tmp_path) -> None:
    conn = db.connect(tmp_path / "t.db")
    assert realprice.enabled_sources(conn, desktop=True) == ("ctrip",)
    settings.save(conn, realprice.LY_ENABLED_KEY, True)
    assert realprice.enabled_sources(conn, desktop=True) == ("ctrip", "ly")
    assert realprice.enabled_sources(conn, desktop=False) == ("ctrip",)  # 任务计划没有登录状态


def test_run_due_reads_both_and_blocked_ly_backs_off(tmp_path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(c.database)
    realprice.set_pinned(conn, "CHI", "SHA", D, R)
    login = page(url="https://passport.ly.com/", items=[], tabs=[])

    def read(url: str, marker: str, script: str = ""):
        if "ly.com" in url:
            return login
        return {"url": url, "items": [[7259, "土耳其航空", None]], "tabs": []}

    now = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)
    got = realprice.run_due(conn, c, read, now=now, alerts=False, sources=("ctrip", "ly"))
    assert sorted((r["source"], r["status"]) for r in got) == [("ctrip", "ok"), ("ly", "blocked")]
    # 30 分钟后携程到点了，同程要等 6 小时
    later = now + timedelta(minutes=30)
    got = realprice.run_due(conn, c, read, now=later, alerts=False, sources=("ctrip", "ly"))
    assert [r["source"] for r in got] == ["ctrip"]


def test_ly_api(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from farerecord.api.app import create_app

    c = cfg(tmp_path)
    conn = db.connect(c.database)
    realprice.set_pinned(conn, "CHI", "SHA", D, R)
    now = datetime.now(UTC).replace(microsecond=0)
    repo.add_real_quote(conn, "CHI", "SHA", D, R, 7259, "土耳其航空", now.isoformat())
    repo.add_real_quote(
        conn, "CHI", "SHA", D, R, 6968, "土耳其航空", now.isoformat(), next_price=7146, source="ly"
    )
    settings.save(conn, "real_check_ly:CHI-SHA", {"status": "blocked", "at": now.isoformat()})
    conn.close()
    app = create_app(c, web_dir=None)
    client = TestClient(app)
    params = {"origin": "CHI", "dest": "SHA", "depart": "2026-12-28", "ret": "2027-01-20"}

    h = client.get("/api/history", params=params).json()
    assert h["real"]["stats"]["current"] == 7259
    ly = h["ly"]
    assert ly["stats"]["current"] == 6968 and ly["next_price"] == 7146 and ly["fresh"]
    assert ly["enabled"] is False and "ly.com/iflight" in ly["url"]

    a = client.get("/api/advice", params=params).json()
    assert any("同程同一行程现在 ¥6,968，比携程便宜 ¥291" in r for r in a["reasons"])

    assert client.post("/api/ly/login").status_code == 409  # 没有桌面端
    opened = []
    app.state.ly_login = lambda: opened.append(True)
    assert client.post("/api/ly/login").status_code == 202 and opened == [True]
    s = client.get("/api/status").json()
    assert s["ly_enabled"] and s["ly_login"]
    conn = db.connect(c.database)
    assert realprice.check_state(conn, PIN, "ly") is None  # 登录后不用再等 6 小时
    conn.close()
    assert client.delete("/api/ly").status_code == 204
    assert client.get("/api/status").json()["ly_enabled"] is False


def test_blocked_message_tells_login_from_captcha() -> None:
    captcha = page(items=[], tabs=[], captcha=True)
    with pytest.raises(PageBlocked, match="同程出现了验证"):
        ly_page.read_roundtrip(reader_of(captcha), "CHI", "SHA", D, R)
