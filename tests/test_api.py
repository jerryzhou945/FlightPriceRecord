from datetime import date

import pytest
from fastapi.testclient import TestClient

from farerecord import db
from farerecord.api.app import create_app, ctrip_url
from farerecord.config import parse_config
from tests.conftest import daterange, seed_run
from tests.test_collector import FakeSource

D, R = date(2026, 12, 30), date(2027, 1, 29)


@pytest.fixture
def setup(tmp_path):
    cfg = parse_config(
        {
            "database": str(tmp_path / "t.db"),
            "collector": {"request_interval_seconds": 0},
            "routes": [
                {
                    "name": "芝加哥 ⇄ 上海",
                    "origin": "CHI",
                    "dest": "SHA",
                    "target_price": 7000,
                    "flex": {"start": "2026-12-01", "end": "2026-12-31"},
                }
            ],
        }
    )
    conn = db.connect(cfg.database)
    out = {x: 5000.0 for x in daterange(date(2026, 12, 1), 31)}
    back = {x: 5000.0 for x in daterange(date(2026, 12, 20), 60)}
    seed_run(conn, "2026-11-09T00:00:00+00:00", {**out, D: 4000}, {**back, R: 4000})
    seed_run(conn, "2026-11-10T00:00:00+00:00", {**out, D: 3846}, {**back, R: 3725})
    conn.close()
    app = create_app(cfg, source_factory=lambda c: FakeSource(), web_dir=None)
    return TestClient(app), cfg


def test_routes(setup) -> None:
    client, _ = setup
    r = client.get("/api/routes").json()
    assert r[0]["key"] == "CHI-SHA" and r[0]["flex"]["start"] == "2026-12-01"


def test_calendar(setup) -> None:
    client, _ = setup
    r = client.get("/api/calendar", params={"origin": "chi", "dest": "sha", "month": "2026-12"})
    body = r.json()
    assert r.status_code == 200 and len(body["days"]) == 31
    assert body["low"]["date"] == "2026-12-30" and body["low"]["tier"] == 0
    assert (
        client.get(
            "/api/calendar", params={"origin": "C", "dest": "SHA", "month": "2026-12"}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/calendar", params={"origin": "CHI", "dest": "SHA", "month": "12"}
        ).status_code
        == 422
    )


def test_flex(setup) -> None:
    client, _ = setup
    params = {
        "origin": "CHI",
        "dest": "SHA",
        "start": "2026-12-01",
        "end": "2026-12-31",
        "min": 25,
        "max": 35,
        "ideal": 30,
    }
    body = client.get("/api/flex", params=params).json()
    assert body["best"]["depart"] == "2026-12-30" and body["best"]["price"] == 7571
    assert body["best"]["days"] == 30
    assert body["count"] == len(body["cells"]) == 31 * 11
    assert [h["price"] for h in body["low_history"]] == [8000, 7571]
    assert body["low_stats"]["is_new_low"] is True
    assert body["suggestions"]
    assert body["leg_lows"]["out"] == {"date": "2026-12-30", "price": 3846}
    assert body["leg_lows"]["ret"] == {"date": "2027-01-29", "price": 3725}
    bad = client.get("/api/flex", params={**params, "start": "2027-01-01"})
    assert bad.status_code == 400


def test_history_and_manual(setup) -> None:
    client, _ = setup
    params = {"origin": "CHI", "dest": "SHA", "depart": "2026-12-30", "ret": "2027-01-29"}
    body = client.get("/api/history", params=params).json()
    assert [p for _, p in body["series"]] == [8000, 7571]
    assert body["stats"]["change_24h"] == -429
    one = client.get(
        "/api/history", params={"origin": "CHI", "dest": "SHA", "depart": "2026-12-30"}
    ).json()
    assert [p for _, p in one["series"]] == [4000, 3846]

    r = client.post(
        "/api/manual-quote",
        json={
            "origin": "CHI",
            "dest": "SHA",
            "depart": "2026-12-30",
            "ret": "2027-01-29",
            "price": 7172,
            "note": "携程往返",
        },
    )
    assert r.status_code == 201
    body = client.get("/api/history", params=params).json()
    assert body["manual"][0]["price"] == 7172
    bad = client.post(
        "/api/manual-quote",
        json={
            "origin": "CHI",
            "dest": "SHA",
            "depart": "2027-01-29",
            "ret": "2026-12-30",
            "price": 1,
        },
    )
    assert bad.status_code == 400


def test_status_and_collect(setup) -> None:
    client, _ = setup
    s = client.get("/api/status").json()
    assert s["n_runs"] == 2 and s["fail_streak"] == 0 and s["last_ok_at"]
    r = client.post("/api/collect").json()
    assert r["status"] == "ok" and r["n_quotes"] > 0
    assert client.get("/api/status").json()["n_runs"] == 3


def test_ctrip_url() -> None:
    assert ctrip_url("CHI", "SHA", D, R).startswith(
        "https://flights.ctrip.com/online/list/round-chi-sha?depdate=2026-12-30_2027-01-29"
    )
    assert "oneway-sha-chi" in ctrip_url("SHA", "CHI", R)


def test_alerts_endpoint(setup) -> None:
    client, _ = setup
    body = client.get("/api/alerts", params={"origin": "CHI", "dest": "SHA"}).json()
    assert body["channel"] == "console" and body["configured"] is False
    rules = {r["key"]: r for r in body["rules"]}
    assert rules["target"]["enabled"] and "7,000" in rules["target"]["title"]
    assert body["log"] == []
