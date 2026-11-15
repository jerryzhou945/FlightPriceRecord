import json
from datetime import date, datetime, time, timedelta, timezone

import httpx
import pytest

from farerecord.alerts import recent_log, run_after_collect
from farerecord.alerts.notifiers import (
    ConsoleNotifier,
    NotifyError,
    PushPlusNotifier,
    WxPusherNotifier,
    make_notifier,
)
from farerecord.alerts.rules import in_quiet_hours
from farerecord.config import parse_config
from tests.conftest import daterange, seed_run

CHI_TZ = timezone(timedelta(hours=-6))
D, R = date(2026, 12, 30), date(2027, 1, 29)
OUT = {x: 5000.0 for x in daterange(date(2026, 12, 1), 31)}
BACK = {x: 5000.0 for x in daterange(date(2026, 12, 20), 60)}


def cfg(**alerts):
    return parse_config(
        {
            "routes": [
                {
                    "name": "芝加哥 ⇄ 上海",
                    "origin": "CHI",
                    "dest": "SHA",
                    "target_price": alerts.pop("target", None),
                    "flex": {
                        "start": "2026-12-01",
                        "end": "2026-12-31",
                        "min_days": 25,
                        "max_days": 35,
                        "ideal_days": 30,
                    },
                }
            ],
            "alerts": {"channel": "console", "quiet_hours": "", "drop_percent": 0, **alerts},
        }
    )


def at(hour: int, day: int = 10) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=CHI_TZ)


def seed(conn, t: str, out_d: float, ret_r: float) -> None:
    seed_run(conn, t, {**OUT, D: out_d}, {**BACK, R: ret_r})


def test_new_low_sends_once(conn) -> None:
    seed(conn, "2026-11-09T00:00:00+00:00", 4000, 4000)
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    c, n = cfg(), ConsoleNotifier()
    sent = run_after_collect(conn, c, notifier=n, now=at(12))
    assert len(sent) == 1 and "历史新低 ¥7,571" in sent[0].title
    assert "12/30" in sent[0].content and "¥3,846" in sent[0].content
    assert sent[0].url and "round-chi-sha" in sent[0].url
    # 同样的价格不再重复推送
    assert run_after_collect(conn, c, notifier=n, now=at(13)) == []
    log = recent_log(conn)
    assert log[0]["rule"] == "new_low" and log[0]["status"] == "sent"


def test_target_and_drop_combined(conn) -> None:
    seed(conn, "2026-11-08T00:00:00+00:00", 4500, 4500)  # 9000
    seed(conn, "2026-11-10T00:00:00+00:00", 3600, 3300)  # 6900
    c = cfg(target=7000, new_low=False, drop_percent=5)
    sent = run_after_collect(conn, c, notifier=ConsoleNotifier(), now=at(12))
    assert len(sent) == 1
    assert sent[0].title.startswith("低于目标价")
    assert "大幅降价" in sent[0].content and "24 小时前 ¥9,000" in sent[0].content
    assert {a.rule for a in sent[0].alerts} == {"target", "drop"}


def test_quiet_hours_defer_then_send(conn) -> None:
    seed(conn, "2026-11-09T00:00:00+00:00", 4000, 4000)
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    c = cfg(quiet_hours="23:00-08:00")
    n = ConsoleNotifier()
    assert run_after_collect(conn, c, notifier=n, now=at(2)) == []
    assert recent_log(conn)[0]["status"] == "deferred" and not n.sent
    assert len(run_after_collect(conn, c, notifier=n, now=at(9))) == 1


def test_no_alert_without_data_or_window(conn) -> None:
    assert run_after_collect(conn, cfg(), notifier=ConsoleNotifier(), now=at(12)) == []
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    # 只有一次记录：不算历史新低
    assert run_after_collect(conn, cfg(), notifier=ConsoleNotifier(), now=at(12)) == []


def test_fail_streak_alert_once(conn) -> None:
    for h in (0, 6):
        seed_run(conn, f"2026-11-10T{h:02d}:00:00+00:00", {}, {}, status="failed")
    c = cfg(fail_streak=2, new_low=False)
    sent = run_after_collect(conn, c, notifier=ConsoleNotifier(), now=at(12))
    assert len(sent) == 1 and "连续 2 次抓取失败" in sent[0].title
    seed_run(conn, "2026-11-10T12:00:00+00:00", {}, {}, status="failed")
    assert run_after_collect(conn, c, notifier=ConsoleNotifier(), now=at(13)) == []


def test_failed_push_is_logged_and_retried(conn) -> None:
    class Broken(ConsoleNotifier):
        name = "broken"

        def send(self, *a, **k):
            raise NotifyError("down")

    seed(conn, "2026-11-09T00:00:00+00:00", 4000, 4000)
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    assert run_after_collect(conn, cfg(), notifier=Broken(), now=at(12)) == []
    assert recent_log(conn)[0]["status"] == "failed"
    assert len(run_after_collect(conn, cfg(), notifier=ConsoleNotifier(), now=at(13))) == 1


@pytest.mark.parametrize(
    ("now", "expected"),
    [(time(23, 30), True), (time(7, 59), True), (time(8, 0), False), (time(12, 0), False)],
)
def test_in_quiet_hours(now, expected) -> None:
    dt = datetime.combine(date(2026, 11, 10), now)
    assert in_quiet_hours(dt, (time(23, 0), time(8, 0))) is expected
    assert in_quiet_hours(dt, None) is False


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_wxpusher_payload_and_errors() -> None:
    seen = {}

    def ok(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"code": 1000, "msg": "处理成功", "success": True})

    WxPusherNotifier("AT_x", ["UID_1"], _client(ok)).send("标题", "**内容**", "https://x")
    assert seen["contentType"] == 3 and seen["uids"] == ["UID_1"] and seen["url"] == "https://x"

    bad = WxPusherNotifier(
        "AT_x",
        ["UID_1"],
        _client(lambda r: httpx.Response(200, json={"code": 1001, "msg": "token 错误"})),
    )
    with pytest.raises(NotifyError, match="token 错误"):
        bad.send("t", "c")
    with pytest.raises(NotifyError):
        WxPusherNotifier(None, []).send("t", "c")


def test_pushplus_payload() -> None:
    seen = {}

    def ok(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"code": 200, "msg": "请求成功"})

    PushPlusNotifier("tok", _client(ok)).send("标题", "内容", "https://x")
    assert seen["template"] == "markdown" and "https://x" in seen["content"]
    with pytest.raises(NotifyError):
        PushPlusNotifier(
            "tok", _client(lambda r: httpx.Response(200, json={"code": 900, "msg": "x"}))
        ).send("t", "c")


def test_make_notifier(monkeypatch) -> None:
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "AT_x")
    monkeypatch.setenv("WXPUSHER_UID", "UID_1, UID_2")
    n = make_notifier("wxpusher")
    assert n.configured and n.uids == ["UID_1", "UID_2"]
    monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
    assert not make_notifier("pushplus").configured
    assert make_notifier("whatever").name == "console"
