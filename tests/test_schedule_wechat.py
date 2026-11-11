"""抓取间隔设置、航司识别间隔、过期数据清理，以及界面里配置微信推送。"""

import json
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from farerecord import db, settings
from farerecord import repository as repo
from farerecord.airlines import airline_json, airline_short
from farerecord.alerts.notifiers import (
    NotifyError,
    WxPusherNotifier,
    WxPusherSimpleNotifier,
    make_notifier,
)
from farerecord.api.app import create_app
from farerecord.collector import collect, prune_old_runs
from farerecord.config import parse_config
from farerecord.envfile import update_env_file
from farerecord.models import OnewayQuote
from farerecord.scheduler import CollectScheduler
from tests.conftest import seed_run
from tests.test_airlines import AirlineSource
from tests.test_collector import TODAY, FakeSource

WECHAT_KEYS = ("WXPUSHER_SPT", "WXPUSHER_APP_TOKEN", "WXPUSHER_UID", "PUSHPLUS_TOKEN")


def cfg(tmp_path, **collector):
    return parse_config(
        {
            "database": str(tmp_path / "t.db"),
            "routes": [{"origin": "CHI", "dest": "SHA"}],
            "collector": {"request_interval_seconds": 0, "airlines": ["CX", "KE"], **collector},
            "alerts": {"desktop": False},
        }
    )


# ---------- 抓取间隔 ----------


def test_interval_setting_overrides_config(tmp_path) -> None:
    c = cfg(tmp_path)
    conn = db.connect(c.database)
    assert settings.effective_config(conn, c).collector.interval_minutes == 30
    settings.save(conn, settings.INTERVAL_KEY, 15)
    assert settings.effective_config(conn, c).collector.interval_minutes == 15
    settings.save(conn, settings.INTERVAL_KEY, 1)  # 存进了不合法的值也不会低于下限
    assert settings.effective_config(conn, c).collector.interval_minutes == 15
    conn.close()


def test_scheduler_follows_interval_saved_in_ui(tmp_path) -> None:
    c = cfg(tmp_path, interval_minutes=60, airlines=[])
    sched = CollectScheduler(c, threading.Lock(), source_factory=lambda _: FakeSource())
    assert sched.tick() is not None
    conn = db.connect(c.database)
    old = (datetime.now(UTC) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
    conn.execute("UPDATE fetch_run SET started_at = ?", (old,))
    conn.commit()
    assert sched.tick() is None  # 60 分钟的间隔还没到
    settings.save(conn, settings.INTERVAL_KEY, 15)
    conn.close()
    assert sched.tick() is not None  # 界面里改成 15 分钟后就到点了


def test_interval_api(tmp_path) -> None:
    client = TestClient(create_app(cfg(tmp_path), web_dir=None))
    s = client.get("/api/status").json()
    assert s["interval_minutes"] == 30 and s["retention_days"] == 30
    assert s["interval_options"] == [15, 30, 60, 120]
    r = client.put("/api/settings/collector", json={"interval_minutes": 15})
    assert r.json() == {"interval_minutes": 15}
    assert client.get("/api/status").json()["interval_minutes"] == 15
    assert client.put("/api/settings/collector", json={"interval_minutes": 5}).status_code == 422


# ---------- 航司识别间隔 ----------


def test_airlines_are_only_refreshed_after_refresh_interval(conn) -> None:
    class Changing(AirlineSource):
        price = 4000

        def fetch_oneway(self, origin, dest, start):
            self.calls.append((origin, dest, "ow"))
            return [OnewayQuote(origin, dest, start, self.price)]

    c = parse_config(
        {
            "routes": [{"origin": "CHI", "dest": "SHA"}],
            "collector": {"airlines": ["CX", "KE"], "airline_refresh_minutes": 120},
        }
    )
    src = Changing()

    def run(t: str) -> list[str]:
        src.calls.clear()
        collect(conn, c, src, today=TODAY, now=lambda: t, sleep=lambda s: None)
        return [call[2] for call in src.calls]

    assert run("2026-11-10T10:00:00+00:00") == ["ow", "CX", "KE", "ow", "CX", "KE", "rt"]
    src.price = 3900  # 半小时后价格变了，但还没到重新识别航司的时间：不发航司请求
    assert run("2026-11-10T10:30:00+00:00") == ["ow", "ow", "rt"]
    assert repo.latest_oneway_detail(conn, "CHI", "SHA")[TODAY].airline is None  # 待确认
    # 两小时后重新识别，把刚才没查的补上
    assert run("2026-11-10T12:00:00+00:00") == ["ow", "CX", "KE", "ow", "CX", "KE", "rt"]
    assert repo.latest_oneway_detail(conn, "CHI", "SHA")[TODAY].airline is not None


# ---------- 清理 ----------


def test_prune_removes_runs_older_than_retention(conn) -> None:
    from datetime import date

    d = date(2026, 12, 30)
    seed_run(conn, "2026-10-01T00:00:00+00:00", {d: 4000}, {d: 4000})
    seed_run(conn, "2026-11-05T00:00:00+00:00", {d: 3900}, {d: 3900})
    now = datetime(2026, 11, 10, tzinfo=UTC)
    assert prune_old_runs(conn, 0, now) == 0  # 0 = 不清理
    assert prune_old_runs(conn, 30, now) == 1
    assert [r["started_at"] for r in repo.recent_runs(conn)] == ["2026-11-05T00:00:00+00:00"]
    assert conn.execute("SELECT COUNT(*) FROM oneway_quote").fetchone()[0] == 2  # 报价一起删了
    assert repo.oneway_series(conn, "CHI", "SHA", d) == [("2026-11-05T00:00:00+00:00", 3900)]


def test_collect_over_api_prunes_old_runs(tmp_path) -> None:
    from datetime import date

    c = cfg(tmp_path, retention_days=30, airlines=[])
    conn = db.connect(c.database)
    seed_run(conn, "2020-01-01T00:00:00+00:00", {date(2020, 2, 1): 4000}, {date(2020, 3, 1): 4000})
    conn.close()
    client = TestClient(create_app(c, source_factory=lambda _: FakeSource(), web_dir=None))
    assert client.post("/api/collect").json()["status"] == "ok"
    assert client.get("/api/status").json()["n_runs"] == 1


# ---------- 航司简称 ----------


def test_airline_short_names_and_colors() -> None:
    assert airline_short("tk") == "土航" and airline_short("YP") == "Air Premia"
    assert airline_short("XX") == "XX" and airline_short(None) is None
    q = OnewayQuote("CHI", "SHA", TODAY, 4000, "TK", 4806)
    assert airline_json(q) == {
        "code": "TK",
        "name": "土耳其航空",
        "short": "土航",
        "color": "#c81f2f",
        "exact": False,
        "price": 4806,
    }
    assert airline_json(OnewayQuote("CHI", "SHA", TODAY, 4000, "XX"))["color"] == "#6b7190"


# ---------- 微信 ----------


def test_update_env_file_keeps_other_lines(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("WXPUSHER_SPT", raising=False)
    monkeypatch.delenv("PUSHPLUS_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text("# 说明\nPUSHPLUS_TOKEN=old\nOTHER=1\n", encoding="utf-8")
    update_env_file(env, {"WXPUSHER_SPT": "SPT_abc", "PUSHPLUS_TOKEN": None})
    assert (
        env.read_text(encoding="utf-8")
        == "# 说明\nPUSHPLUS_TOKEN=\nOTHER=1\nWXPUSHER_SPT=SPT_abc\n"
    )
    import os

    assert os.environ["WXPUSHER_SPT"] == "SPT_abc" and "PUSHPLUS_TOKEN" not in os.environ
    update_env_file(env, {"WXPUSHER_SPT": "SPT_new"})
    assert env.read_text(encoding="utf-8").count("WXPUSHER_SPT") == 1


def test_wxpusher_simple_push_payload_and_errors() -> None:
    seen = {}

    def ok(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"code": 1000, "success": True})

    client = httpx.Client(transport=httpx.MockTransport(ok))
    WxPusherSimpleNotifier("SPT_abc", client).send("标题", "### 内容", "https://x.test")
    assert seen["url"].endswith("/api/send/message/simple-push")
    assert seen["body"] == {
        "spt": "SPT_abc",
        "content": "### 内容",
        "summary": "标题",
        "contentType": 3,
        "url": "https://x.test",
    }
    bad = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": 1001, "msg": "x"})
        )
    )
    with pytest.raises(NotifyError):
        WxPusherSimpleNotifier("SPT_abc", bad).send("t", "c")
    with pytest.raises(NotifyError):
        WxPusherSimpleNotifier(None).send("t", "c")


def test_make_notifier_prefers_simple_push(monkeypatch) -> None:
    for key in WECHAT_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WXPUSHER_APP_TOKEN", "AT_x")
    monkeypatch.setenv("WXPUSHER_UID", "UID_1")
    assert isinstance(make_notifier("wxpusher"), WxPusherNotifier)
    monkeypatch.setenv("WXPUSHER_SPT", "SPT_abc")
    assert isinstance(make_notifier("wxpusher"), WxPusherSimpleNotifier)


def test_wechat_setup_api(tmp_path, monkeypatch) -> None:
    for key in WECHAT_KEYS:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    client = TestClient(create_app(cfg(tmp_path), web_dir=None, env_file=env))
    w = client.get("/api/alerts").json()["wechat"]
    assert w["provider"] is None and w["configured"] is False and w["can_configure"] is True
    assert w["spt_qr"].startswith("https://wxpusher.zjiecode.com/api/qrcode/")

    bad = client.put("/api/alerts/wechat", json={"provider": "wxpusher_spt", "spt": "hello"})
    assert bad.status_code == 400 and "hello" not in bad.text and not env.exists()

    token = "SPT_" + "a1B2c3D4e5"
    r = client.put("/api/alerts/wechat", json={"provider": "wxpusher_spt", "spt": f" {token} "})
    assert r.json() == {"provider": "wxpusher_spt", "configured": True}
    assert token not in r.text and f"WXPUSHER_SPT={token}" in env.read_text(encoding="utf-8")
    body = client.get("/api/alerts").json()
    assert body["wechat"]["provider"] == "wxpusher_spt" and body["configured"] is True
    assert token not in json.dumps(body)  # 口令不通过接口返回

    r = client.put(
        "/api/alerts/wechat",
        json={"provider": "wxpusher", "app_token": "AT_" + "x" * 10, "uid": "UID_" + "y" * 10},
    )
    assert r.json()["provider"] == "wxpusher"
    assert client.get("/api/alerts").json()["wechat"]["provider"] == "wxpusher"

    r = client.put("/api/alerts/wechat", json={"provider": "pushplus", "token": "p" * 32})
    assert r.json() == {"provider": "pushplus", "configured": True}

    assert client.put("/api/alerts/wechat", json={"provider": "off"}).json()["configured"] is False
    body = client.get("/api/alerts").json()
    assert body["channel"] == "console" and body["wechat"]["provider"] is None
    assert "PUSHPLUS_TOKEN=" + "p" * 32 in env.read_text(encoding="utf-8")  # 关掉不删口令
