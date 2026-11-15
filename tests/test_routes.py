from datetime import date

from farerecord import db, realprice, settings
from farerecord import repository as repo
from farerecord.cities import city_name, route_name
from farerecord.config import parse_config
from tests.test_api import setup  # noqa: F401  （复用 API 测试的临时库和客户端）


def make_cfg():
    return parse_config(
        {
            "routes": [
                {
                    "origin": "CHI",
                    "dest": "SHA",
                    "flex": {"start": "2026-12-01", "end": "2026-12-31", "min_days": 20},
                }
            ]
        }
    )


def test_city_names() -> None:
    assert city_name("nyc") == "纽约" and city_name("XYZ") == "XYZ"
    assert route_name("NYC", "SHA") == "纽约 ⇄ 上海"


def test_extra_routes_merge_into_config(conn) -> None:
    cfg = make_cfg()
    route = settings.add_route(conn, cfg, "nyc", "sha")
    assert route.key == "NYC-SHA" and route.name == "纽约 ⇄ 上海"
    assert route.flex.start == date(2026, 12, 1) and route.flex.min_days == 20  # 沿用第一条航线
    eff = settings.effective_config(conn, cfg)
    assert [r.key for r in eff.routes] == ["CHI-SHA", "NYC-SHA"]
    assert cfg.routes[0].key == "CHI-SHA" and len(cfg.routes) == 1  # 不改 config.yaml 的对象

    # 界面上的目标价对新航线同样生效
    settings.save(conn, settings.TARGET_PREFIX + "NYC-SHA", 6500)
    assert settings.effective_config(conn, cfg).route("NYC", "SHA").target_price == 6500


def test_add_route_rejects_duplicates_and_limit(conn) -> None:
    cfg = make_cfg()
    for code in ("NYC", "LAX", "SFO", "BOS", "SEA"):  # 加上配置里的 CHI 共 6 条
        settings.add_route(conn, cfg, code, "SHA")
    for o, d, msg in (
        ("CHI", "SHA", "已经在监测"),
        ("NYC", "SHA", "已经在监测"),
        ("DEN", "SHA", "最多"),
    ):
        try:
            settings.add_route(conn, cfg, o, d)
        except ValueError as exc:
            assert msg in str(exc)
        else:
            raise AssertionError(f"{o}-{d} 应该被拒绝")


def test_remove_route(conn) -> None:
    cfg = make_cfg()
    settings.add_route(conn, cfg, "NYC", "SHA")
    assert settings.remove_route(conn, "nyc", "sha")
    assert not settings.remove_route(conn, "NYC", "SHA")
    assert [r.key for r in settings.effective_config(conn, cfg).routes] == ["CHI-SHA"]


def test_pinned_trips_include_extra_routes(conn) -> None:
    cfg = make_cfg()
    settings.add_route(conn, cfg, "NYC", "SHA")
    realprice.set_pinned(conn, "NYC", "SHA", date(2026, 12, 28), date(2027, 1, 20))
    trips = realprice.pinned_trips(conn, cfg, date(2026, 11, 12))
    assert [(p.origin, p.depart) for p in trips] == [("NYC", date(2026, 12, 28))]


def test_routes_api(setup) -> None:  # noqa: F811
    client, cfg = setup
    cities = client.get("/api/cities").json()
    assert cities[0]["group"] == "北美" and {"code": "NYC", "name": "纽约"} in cities[0]["cities"]

    assert client.post("/api/routes", json={"origin": "nyc", "dest": "sha"}).status_code == 201
    routes = client.get("/api/routes").json()
    assert [(r["key"], r["custom"]) for r in routes] == [("CHI-SHA", False), ("NYC-SHA", True)]
    assert routes[1]["name"] == "纽约 ⇄ 上海" and routes[1]["flex"]["start"] == "2026-12-01"

    assert client.post("/api/routes", json={"origin": "NYC", "dest": "SHA"}).status_code == 409
    assert client.post("/api/routes", json={"origin": "SHA", "dest": "SHA"}).status_code == 400
    assert client.post("/api/routes", json={"origin": "N1C", "dest": "SHA"}).status_code == 400

    # 新航线可以固定行程、设目标价、设到价提醒
    pin = {"origin": "NYC", "dest": "SHA", "depart": "2026-12-28", "ret": "2027-01-20"}
    assert client.put("/api/pinned", json=pin).status_code == 200
    body = {"target_price": 6500, "origin": "NYC", "dest": "SHA"}
    assert client.put("/api/alerts/settings", json=body).status_code == 200
    watch = {"origin": "NYC", "dest": "SHA", "depart": "2026-12-28", "ret": "2027-01-20"}
    assert client.post("/api/watches", json={**watch, "target_price": 6000}).status_code == 201

    # 删除：config.yaml 里的删不了；界面加的删掉后取消固定、停用提醒
    assert client.delete("/api/routes/CHI/SHA").status_code == 400
    assert client.delete("/api/routes/NYC/SHA").status_code == 204
    assert client.delete("/api/routes/NYC/SHA").status_code == 404
    assert [r["key"] for r in client.get("/api/routes").json()] == ["CHI-SHA"]
    conn = db.connect(cfg.database)
    assert realprice.get_pinned(conn, "NYC", "SHA") is None
    assert [w.enabled for w in repo.list_watches(conn) if w.origin == "NYC"] == [False]
    conn.close()


def test_collect_fetches_extra_routes(conn) -> None:
    from farerecord.collector import collect_and_alert
    from tests.test_collector import FakeSource

    cfg = make_cfg()
    cfg.collector.request_interval_seconds = 0
    settings.add_route(conn, cfg, "NYC", "SHA")
    source = FakeSource()
    result = collect_and_alert(conn, cfg, lambda c: source, alerts=False)
    assert set(result.per_route) == {"CHI-SHA", "NYC-SHA"}
    assert ("NYC", "SHA", "ow") in source.calls and ("SHA", "NYC", "ow") in source.calls


def test_city_list_codes() -> None:
    from farerecord.cities import CITY_GROUPS, CITY_NAMES

    codes = [c for _, cities in CITY_GROUPS for c, _ in cities]
    assert len(codes) == len(set(codes)) and all(len(c) == 3 and c.isupper() for c in codes)
    # 2026-11-15 逐个查过携程低价日历：西安要用 SIA，XIY 查不到
    assert "SIA" in CITY_NAMES and "XIY" not in CITY_NAMES
    assert len(codes) >= 40
