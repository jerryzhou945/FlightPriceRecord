"""桌面端：定时抓取器、窗口启动前的准备。不真的开窗口。"""

import builtins
import threading
from datetime import UTC, datetime, timedelta

import pytest

from farerecord import db, desktop
from farerecord import repository as repo
from farerecord.cli import main as cli_main
from farerecord.config import parse_config
from farerecord.scheduler import CollectScheduler
from tests.test_collector import FakeSource


def cfg(tmp_path, routes=True):
    return parse_config(
        {
            "database": str(tmp_path / "t.db"),
            "routes": [{"origin": "CHI", "dest": "SHA"}] if routes else [],
            "collector": {"request_interval_seconds": 0},
            "alerts": {"desktop": False},
        }
    )


def test_scheduler_collects_when_due_and_skips_until_next_interval(tmp_path) -> None:
    c = cfg(tmp_path)
    sched = CollectScheduler(c, threading.Lock(), source_factory=lambda _: FakeSource())
    first = sched.tick()
    assert first is not None and first.status == "ok"
    assert sched.tick() is None  # 刚抓过，没到下一个间隔
    conn = db.connect(c.database)
    old = (datetime.now(UTC) - timedelta(minutes=61)).replace(microsecond=0).isoformat()
    conn.execute("UPDATE fetch_run SET started_at = ?", (old,))
    conn.commit()
    assert sched.tick() is not None  # 过了一个间隔，再抓
    assert len(repo.recent_runs(conn)) == 2
    conn.close()


def test_scheduler_skips_while_another_collect_is_running(tmp_path) -> None:
    lock = threading.Lock()
    sched = CollectScheduler(cfg(tmp_path), lock, source_factory=lambda _: FakeSource())
    with lock:  # 用户正好点了"立即抓取"
        assert sched.tick() is None
    assert not lock.locked()
    assert sched.tick() is not None


def test_scheduled_collect_rechecks_after_another_process_starts(tmp_path) -> None:
    c = cfg(tmp_path)
    source = FakeSource()

    def create_source(_):
        # 在第一次检查到点之后，另一个进程已开始抓取。
        other = db.connect(c.database)
        repo.start_run(other, "other")
        other.close()
        return source

    sched = CollectScheduler(c, threading.Lock(), source_factory=create_source)
    assert sched.tick() is None
    assert source.calls == []
    conn = db.connect(c.database)
    assert len(repo.recent_runs(conn)) == 1
    conn.close()


def test_scheduler_does_nothing_without_routes(tmp_path) -> None:
    sched = CollectScheduler(cfg(tmp_path, routes=False), threading.Lock())
    assert sched.tick() is None


def test_free_port_is_usable() -> None:
    assert 1024 < desktop.free_port() < 65536


def test_app_command_explains_missing_webview(monkeypatch, capsys, tmp_path) -> None:
    real_import = builtins.__import__

    def no_webview(name, *args, **kwargs):
        if name == "webview":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_webview)
    with pytest.raises(desktop.DesktopUnavailable):
        desktop.import_webview()
    config = tmp_path / "config.yaml"
    config.write_text("routes: []\n", encoding="utf-8")
    assert cli_main(["-c", str(config), "app"]) == 2
    assert "pywebview" in capsys.readouterr().out
