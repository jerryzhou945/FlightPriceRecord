"""到价提醒（price_watch）、界面设置（setting）、桌面通知。"""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from farerecord import db, settings
from farerecord import repository as repo
from farerecord.alerts import recent_log, run_after_collect, send_test
from farerecord.alerts.notifiers import (
    ConsoleNotifier,
    DesktopNotifier,
    MultiNotifier,
    NotifyError,
    build_notifier,
    plain_lines,
    toast_xml,
)
from farerecord.api.app import create_app
from farerecord.config import parse_config
from farerecord.models import OnewayQuote
from tests.conftest import seed_run
from tests.test_collector import FakeSource

D, R = date(2026, 12, 30), date(2027, 1, 29)
CST = timezone(timedelta(hours=8))
NOON = datetime(2026, 11, 10, 12, 0, tzinfo=CST)


def cfg(tmp_path=None, **alerts):
    data = {
        "routes": [
            {
                "name": "芝加哥 ⇄ 上海",
                "origin": "CHI",
                "dest": "SHA",
                "flex": {"start": "2026-12-01", "end": "2026-12-31"},
            }
        ],
        "alerts": {"new_low": False, "drop_percent": 0, "fail_streak": 0, **alerts},
        "collector": {"request_interval_seconds": 0, "retention_days": 0},
    }
    if tmp_path:
        data["database"] = str(tmp_path / "t.db")
    return parse_config(data)


def seed(conn, t: str, out_d: float, ret_r: float) -> None:
    seed_run(conn, t, {D: out_d}, {R: ret_r})


# ---------- 到价提醒 ----------


def test_watch_fires_once_per_price_and_again_when_cheaper(conn) -> None:
    wid = repo.add_watch(conn, "CHI", "SHA", D, R, 7600)
    n = ConsoleNotifier()
    seed(conn, "2026-11-09T00:00:00+00:00", 4000, 4000)
    assert run_after_collect(conn, cfg(), notifier=n, now=NOON) == []  # 8000 > 7600
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    sent = run_after_collect(conn, cfg(), notifier=n, now=NOON)
    assert len(sent) == 1 and "到达提醒价 ¥7,571" in sent[0].title
    assert "目标价 ¥7,600" in sent[0].content
    assert run_after_collect(conn, cfg(), notifier=n, now=NOON) == []  # 同价不重复
    seed(conn, "2026-11-10T01:00:00+00:00", 3800, 3725)
    assert len(run_after_collect(conn, cfg(), notifier=n, now=NOON)) == 1  # 更低了再提醒
    assert recent_log(conn)[0]["dedupe_key"] == f"{wid}:7525"
    assert recent_log(conn)[0]["rule_label"] == "到达提醒价"


def test_watch_skips_disabled_departed_and_missing_legs(conn) -> None:
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    off = repo.add_watch(conn, "CHI", "SHA", D, R, 9000)
    repo.update_watch(conn, off, enabled=False)
    repo.add_watch(conn, "CHI", "SHA", D, R + timedelta(days=1), 9000)  # 回程没有报价
    n = ConsoleNotifier()
    assert run_after_collect(conn, cfg(), notifier=n, now=NOON) == []
    repo.update_watch(conn, off, enabled=True)
    after_departure = datetime(2026, 12, 31, 12, 0, tzinfo=CST)
    assert run_after_collect(conn, cfg(), notifier=n, now=after_departure) == []
    assert len(run_after_collect(conn, cfg(), notifier=n, now=NOON)) == 1


def test_watch_message_names_airlines(conn) -> None:
    t = "2026-11-10T00:00:00+00:00"
    run_id = repo.start_run(conn, "test", t)
    repo.save_oneway(conn, run_id, [OnewayQuote("CHI", "SHA", D, 3846, "CX")], t)
    repo.save_oneway(conn, run_id, [OnewayQuote("SHA", "CHI", R, 3725, "KE")], t)
    repo.finish_run(conn, run_id, "ok", 2, finished_at=t)
    repo.add_watch(conn, "CHI", "SHA", D, R, 8000)
    sent = run_after_collect(conn, cfg(), notifier=ConsoleNotifier(), now=NOON)
    assert "国泰航空 CX" in sent[0].content and "大韩航空 KE" in sent[0].content


# ---------- 界面设置覆盖 config.yaml ----------


def test_settings_override_config(conn) -> None:
    base = cfg(quiet_hours="23:00-08:00")
    assert settings.effective_config(conn, base) is base  # 没存过设置就是原配置
    settings.save(conn, "alerts.new_low", True)
    settings.save(conn, "alerts.quiet_hours", "")
    settings.save(conn, "alerts.desktop", False)
    settings.save(conn, settings.TARGET_PREFIX + "CHI-SHA", 6500)
    eff = settings.effective_config(conn, base)
    assert eff.alerts.new_low is True and eff.alerts.quiet_hours is None
    assert eff.alerts.desktop is False and eff.routes[0].target_price == 6500
    assert base.alerts.new_low is False and base.routes[0].target_price is None  # 原配置不变


def test_saved_target_price_triggers_alert(conn) -> None:
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    n = ConsoleNotifier()
    assert run_after_collect(conn, cfg(), notifier=n, now=NOON) == []
    settings.save(conn, settings.TARGET_PREFIX + "CHI-SHA", 7600)
    sent = run_after_collect(conn, cfg(), notifier=n, now=NOON)
    assert len(sent) == 1 and "低于目标价" in sent[0].title


# ---------- 桌面通知 ----------


def test_toast_xml_escapes_and_links() -> None:
    content = (
        "### 标题\n\n- 去程 12/30 <CX> & 国泰\n- 回程 01/29\n[去携程核价](https://x.test/a?b=1&c=2)"
    )
    assert plain_lines(content) == ["去程 12/30 <CX> & 国泰", "回程 01/29"]
    xml = toast_xml("历史新低 ¥7,571", content, "https://x.test/a?b=1&c=2")
    assert 'launch="https://x.test/a?b=1&amp;c=2"' in xml and 'activationType="protocol"' in xml
    assert "<text>去程 12/30 &lt;CX&gt; &amp; 国泰</text>" in xml
    assert "launch" not in toast_xml("t", "c")


def test_desktop_notifier_uses_runner_and_reports_failure(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    shown = []
    DesktopNotifier(runner=shown.append).send("标题", "### 标题\n\n- 一行", None)
    assert len(shown) == 1 and "<text>一行</text>" in shown[0]

    def boom(xml):
        raise OSError("no powershell")

    with pytest.raises(NotifyError):
        DesktopNotifier(runner=boom).send("t", "c")
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(NotifyError):
        DesktopNotifier(runner=shown.append).send("t", "c")


def test_build_notifier_combines_channels(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.delenv("WXPUSHER_APP_TOKEN", raising=False)
    assert build_notifier(cfg(desktop=True).alerts).name == "desktop"
    assert build_notifier(cfg(desktop=False).alerts).name == "console"
    assert build_notifier(cfg(desktop=True, channel="wxpusher").alerts).name == "desktop"
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "AT_x")
    monkeypatch.setenv("WXPUSHER_UID", "UID_1")
    both = build_notifier(cfg(desktop=True, channel="wxpusher").alerts)
    assert both.name == "desktop+wxpusher" and both.label == "桌面通知 + WxPusher 微信"
    assert build_notifier(cfg(desktop=False, channel="wxpusher").alerts).name == "wxpusher"
    monkeypatch.setattr("sys.platform", "linux")
    assert build_notifier(cfg(desktop=True).alerts).name == "console"


def test_multi_notifier_succeeds_if_any_channel_delivers() -> None:
    class Bad(ConsoleNotifier):
        name, label = "bad", "坏通道"

        def send(self, *a, **k):
            raise NotifyError("x")

    good = ConsoleNotifier()
    MultiNotifier([Bad(), good]).send("t", "c")
    assert len(good.sent) == 1
    with pytest.raises(NotifyError):
        MultiNotifier([Bad()]).send("t", "c")


def test_send_test_goes_through_desktop_toast(conn, toasts, monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    ok, n = send_test(conn, cfg(), now=NOON)
    assert ok and n.name == "desktop" and "航价记测试提醒" in toasts[0]
    assert recent_log(conn)[0]["rule"] == "test"


# ---------- API ----------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    c = cfg(tmp_path)
    conn = db.connect(c.database)
    seed(conn, "2026-11-10T00:00:00+00:00", 3846, 3725)
    conn.close()
    return TestClient(create_app(c, source_factory=lambda _: FakeSource(), web_dir=None))


def test_watch_crud_api(client) -> None:
    body = {"origin": "chi", "dest": "sha", "depart": str(D), "ret": str(R), "target_price": 7600}
    wid = client.post("/api/watches", json=body).json()["id"]
    # 同一个组合再建一次 = 更新目标价，不产生重复提醒
    assert client.post("/api/watches", json={**body, "target_price": 7000}).json()["id"] == wid
    w = client.get("/api/alerts").json()["watches"]
    assert len(w) == 1 and w[0]["target_price"] == 7000 and w[0]["current"] == 7571
    assert w[0]["hit"] is False and w[0]["days"] == 30 and "round-chi-sha" in w[0]["ctrip_url"]
    assert client.patch(f"/api/watches/{wid}", json={"target_price": 8000}).status_code == 200
    assert client.patch(f"/api/watches/{wid}", json={"enabled": False}).status_code == 200
    w = client.get("/api/alerts").json()["watches"][0]
    assert w["hit"] is True and w["enabled"] is False
    assert client.delete(f"/api/watches/{wid}").status_code == 204
    assert client.delete(f"/api/watches/{wid}").status_code == 404
    assert client.patch(f"/api/watches/{wid}", json={"enabled": True}).status_code == 404
    bad = client.post("/api/watches", json={**body, "ret": str(D)})
    assert bad.status_code == 400


def test_alert_settings_api(client) -> None:
    s = client.get("/api/alerts").json()
    assert s["settings"]["desktop"] is True and s["settings"]["target_price"] is None
    assert s["desktop_supported"] is True and s["channel_label"] == "桌面通知"
    r = client.put(
        "/api/alerts/settings",
        json={"new_low": True, "drop_percent": 8, "quiet_hours": "22:30-07:00"},
    )
    assert r.json()["new_low"] is True and r.json()["drop_percent"] == 8
    assert r.json()["quiet_hours"] == "22:30-07:00"
    r = client.put(
        "/api/alerts/settings", json={"target_price": 7000, "origin": "CHI", "dest": "SHA"}
    )
    assert r.json()["target_price"] == 7000
    assert client.get("/api/routes").json()[0]["target_price"] == 7000
    r = client.put(
        "/api/alerts/settings", json={"target_price": None, "origin": "CHI", "dest": "SHA"}
    )
    assert r.json()["target_price"] is None and r.json()["new_low"] is True  # 其他设置不受影响
    assert client.put("/api/alerts/settings", json={"quiet_hours": "bad"}).status_code == 400
    missing = client.put(
        "/api/alerts/settings", json={"target_price": 1, "origin": "AAA", "dest": "BBB"}
    )
    assert missing.status_code == 404


def test_alert_test_endpoint(client, toasts) -> None:
    r = client.post("/api/alerts/test")
    assert r.status_code == 200 and r.json()["channel_label"] == "桌面通知" and len(toasts) == 1
    client.put("/api/alerts/settings", json={"desktop": False})
    assert client.post("/api/alerts/test").status_code == 400


def test_collect_over_api_sends_desktop_toast(client, toasts) -> None:
    from farerecord.collector import today_cst

    dep = today_cst() + timedelta(days=1)  # FakeSource 只给今天起 3 天的价格
    body = {"origin": "CHI", "dest": "SHA", "target_price": 9000}
    client.post("/api/watches", json={**body, "depart": str(dep), "ret": str(dep + timedelta(1))})
    assert client.post("/api/collect").json()["status"] == "ok"
    assert len(toasts) == 1 and "到达提醒价" in toasts[0]
