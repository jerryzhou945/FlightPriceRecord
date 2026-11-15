"""桌面端开着的时候，按间隔自动抓取。

和 Windows 任务计划（`farerecord collect --if-due`）用同一个"到点了吗"的判断，
所以两边同时存在也不会重复抓取：谁先到点谁抓，另一个看到刚抓过就跳过。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from farerecord import db
from farerecord.collector import CollectionNotDue, CollectResult, collect_and_alert, is_due
from farerecord.config import AppConfig
from farerecord.settings import effective_config
from farerecord.sources.base import PriceSource

log = logging.getLogger(__name__)

CHECK_SECONDS = 60.0  # 每隔多久看一眼是否到点


class CollectScheduler:
    def __init__(
        self,
        cfg: AppConfig,
        lock: threading.Lock,
        source_factory: Callable[[AppConfig], PriceSource] | None = None,
        check_seconds: float = CHECK_SECONDS,
    ) -> None:
        self.cfg = cfg
        self.lock = lock  # 和"立即抓取"共用，保证同一时间只有一次抓取
        self.source_factory = source_factory
        self.check_seconds = check_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> CollectResult | None:
        """到点且没有别的抓取在跑，就抓一次。返回抓取结果；没抓返回 None。"""
        if not self.cfg.routes or not self.lock.acquire(blocking=False):
            return None
        try:
            conn = db.connect(self.cfg.database)
            try:
                interval = effective_config(conn, self.cfg).collector.interval_minutes
                if not is_due(conn, interval):
                    return None
                return collect_and_alert(conn, self.cfg, self.source_factory, if_due=True)
            except CollectionNotDue:
                return None
            finally:
                conn.close()
        finally:
            self.lock.release()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.tick()
                if result:
                    log.info("定时抓取完成：%s，%d 条报价", result.status, result.n_quotes)
            except Exception:  # 定时器不能因为一次意外就停掉
                log.exception("定时抓取出错")
            self._stop.wait(self.check_seconds)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="collect-scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
