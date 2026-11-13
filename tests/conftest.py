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


@pytest.fixture(autouse=True)
def toasts(monkeypatch):
    """桌面通知不真的弹出来，只记下内容。"""
    shown: list[str] = []
    monkeypatch.setattr("farerecord.alerts.notifiers.run_toast", shown.append)
    return shown


@pytest.fixture(autouse=True)
def clean_push_env(monkeypatch):
    """推送口令的环境变量：每个测试从"没配置"开始，测试里写进去的结束后还原。"""
    for key in ("WXPUSHER_SPT", "WXPUSHER_APP_TOKEN", "WXPUSHER_UID", "PUSHPLUS_TOKEN"):
        monkeypatch.setenv(key, "x")  # 先 set 再 del，monkeypatch 才会记住原始状态
        monkeypatch.delenv(key)
