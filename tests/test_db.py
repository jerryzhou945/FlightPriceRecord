from datetime import date

from farerecord import db
from farerecord.models import Trip


def test_connect_creates_schema(tmp_path) -> None:
    conn = db.connect(tmp_path / "sub" / "t.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"fetch_run", "oneway_quote", "rt_quote", "manual_quote", "alert_log"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_migrate_is_idempotent() -> None:
    conn = db.connect(":memory:")
    assert db.migrate(conn) == db.SCHEMA_VERSION


def test_trip_prices() -> None:
    t = Trip(date(2026, 12, 30), date(2027, 1, 29), 3846, 3725)
    assert t.days == 30
    assert t.price == 7571
    assert t.best_price == 7571
    assert Trip(date(2026, 12, 1), date(2026, 12, 8), 4000, 4000, rt_price=6000).best_price == 6000
