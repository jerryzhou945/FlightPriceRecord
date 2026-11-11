from datetime import date, timedelta

import pytest

from farerecord import db
from farerecord import repository as repo
from farerecord.models import OnewayQuote


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def seed_run(conn, observed_at: str, out: dict, back: dict, status: str = "ok") -> int:
    """写入一次抓取：out/back 为 {date: price}，CHI→SHA 与 SHA→CHI。"""
    run_id = repo.start_run(conn, "test", observed_at)
    n = repo.save_oneway(
        conn, run_id, [OnewayQuote("CHI", "SHA", d, p) for d, p in out.items()], observed_at
    )
    n += repo.save_oneway(
        conn, run_id, [OnewayQuote("SHA", "CHI", d, p) for d, p in back.items()], observed_at
    )
    repo.finish_run(conn, run_id, status, n, finished_at=observed_at)
    return run_id


def daterange(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]
