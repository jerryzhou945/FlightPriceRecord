import os
from datetime import date, time

from farerecord.config import ROOT, load_config, load_env, parse_config


def test_example_config_loads() -> None:
    cfg = load_config(ROOT / "config.example.yaml")
    r = cfg.routes[0]
    assert (r.origin, r.dest) == ("CHI", "SHA")
    assert r.flex.start == date(2026, 12, 1) and r.flex.ideal_days == 30
    assert r.target_price == 7000
    assert cfg.alerts.quiet_hours == (time(23, 0), time(8, 0))
    assert cfg.database == ROOT / "data" / "farerecord.db"
    assert cfg.route("chi", "sha") is r and cfg.route("SHA", "CHI") is None


def test_parse_defaults(tmp_path) -> None:
    cfg = parse_config({"routes": [{"origin": "chi", "dest": "pek"}]}, root=tmp_path)
    r = cfg.routes[0]
    assert r.name == "chi ⇄ pek" and r.origin == "CHI"
    assert r.flex.start is None and r.target_price is None
    assert cfg.alerts.channel == "console" and cfg.alerts.quiet_hours is None
    assert cfg.database == tmp_path / "data" / "farerecord.db"


def test_load_env_does_not_override(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("# c\nFR_A=1\nFR_B='two'\nFR_EMPTY=\n", encoding="utf-8")
    monkeypatch.setenv("FR_A", "keep")
    monkeypatch.delenv("FR_B", raising=False)
    load_env(tmp_path / ".env")
    assert os.environ["FR_A"] == "keep"
    assert os.environ["FR_B"] == "two"
    assert "FR_EMPTY" not in os.environ
    monkeypatch.delenv("FR_B")
