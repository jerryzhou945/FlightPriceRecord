"""记住同程登录状态：只存同程的 Cookie、存取、关闭监测时删除、读价窗口先放回再打开页面。"""

from http.cookies import SimpleCookie

from farerecord import ly_session
from farerecord.sources.ctrip_page import WebviewReader


def jar(name: str, value: str, domain: str, **attrs) -> SimpleCookie:
    c = SimpleCookie()
    c[name] = value
    c[name]["domain"] = domain
    c[name]["path"] = attrs.get("path", "/")
    if attrs.get("secure"):
        c[name]["secure"] = True
    if attrs.get("httponly"):
        c[name]["httponly"] = True
    return c


def test_records_keep_only_ly(tmp_path) -> None:
    cookies = [
        jar("sid", "abc", ".ly.com", secure=True, httponly=True),
        jar("u", "1", "www.ly.com"),
        jar("other", "x", ".example.com"),
    ]
    recs = ly_session.records_from(cookies)
    assert [(r["name"], r["domain"], r["secure"], r["httponly"]) for r in recs] == [
        ("sid", ".ly.com", True, True),
        ("u", "www.ly.com", False, False),
    ]
    path = ly_session.path_for(tmp_path / "farerecord.db")
    assert path == tmp_path / "ly_session.json"
    ly_session.save(path, recs)
    assert ly_session.load(path) == recs
    ly_session.forget(path)
    assert not path.exists() and ly_session.load(path) == []
    ly_session.forget(path)  # 没有文件也不报错
    ly_session.save(path, [])  # 没东西不写文件
    assert not path.exists()


class FakeEvent:
    def wait(self, timeout=None):
        return True

    def __iadd__(self, fn):
        return self


class FakeWindow:
    def __init__(self, log, page):
        self.log, self.page = log, page
        self.events = type("E", (), {"shown": FakeEvent(), "loaded": FakeEvent()})()

    def load_url(self, url):
        self.log.append(("load", url))

    def evaluate_js(self, script):
        return self.page

    def get_cookies(self):
        self.log.append(("get_cookies",))
        return [jar("sid", "new", ".ly.com")]

    def destroy(self):
        self.log.append(("destroy",))


class FakeWebview:
    def __init__(self, page):
        self.log: list = []
        self.page = page

    def create_window(self, title, url=None, html=None, **kw):
        self.log.append(("create", url, bool(html)))
        return FakeWindow(self.log, self.page)


def test_reader_restores_then_saves_for_ly(tmp_path, monkeypatch) -> None:
    import json

    path = tmp_path / "ly_session.json"
    ly_session.save(path, [{"name": "sid", "value": "old", "domain": ".ly.com", "path": "/"}])
    restored = []
    monkeypatch.setattr(ly_session, "restore_into_window", lambda w, p: restored.append(p) or 1)
    url = "https://www.ly.com/iflight/book1.html?para=CHI*SHA*2026-12-28*2027-01-20*RT"
    page = json.dumps({"url": url, "items": [[6968, "土耳其航空", None]], "tabs": []})
    wv = FakeWebview(page)
    reader = WebviewReader(wv, timeout=1, ly_session=path)
    monkeypatch.setattr("farerecord.sources.ctrip_page.time.sleep", lambda s: None)
    snap = reader(url, "2026-12-28*2027-01-20", "script")
    assert snap["items"][0][0] == 6968
    assert wv.log[0] == ("create", None, True)  # 先开空白页
    assert restored == [path] and ("load", url) in wv.log  # 放回登录状态后才打开结果页
    assert ly_session.load(path)[0]["value"] == "new"  # 读成功后存下最新的


def test_reader_leaves_ctrip_alone(tmp_path, monkeypatch) -> None:
    import json

    url = "https://flights.ctrip.com/online/list/round-chi-sha?depdate=2026-12-28_2027-01-20"
    page = json.dumps({"url": url, "items": [[7259, "土耳其航空", None]], "tabs": []})
    wv = FakeWebview(page)
    monkeypatch.setattr("farerecord.sources.ctrip_page.time.sleep", lambda s: None)
    WebviewReader(wv, timeout=1, ly_session=tmp_path / "s.json")(url, "depdate=2026-12-28")
    assert wv.log[0] == ("create", url, False)
    assert ("get_cookies",) not in wv.log and not (tmp_path / "s.json").exists()


def test_turning_ly_off_forgets_session(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from farerecord.api.app import create_app
    from farerecord.config import parse_config

    cfg = parse_config(
        {"database": str(tmp_path / "t.db"), "routes": [{"origin": "CHI", "dest": "SHA"}]}
    )
    path = ly_session.path_for(cfg.database)
    ly_session.save(path, [{"name": "sid", "value": "v", "domain": ".ly.com", "path": "/"}])
    client = TestClient(create_app(cfg, web_dir=None))
    assert client.delete("/api/ly").status_code == 204
    assert not path.exists()
