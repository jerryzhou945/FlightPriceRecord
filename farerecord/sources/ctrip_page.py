"""携程往返订票页：读页面上显示的真实往返含税价。

低价日历接口给不了长途行程的真实往返价（见 docs/architecture.md），而订票页上就写着。
做法和人自己去看是一样的：在一个真实的浏览器窗口（桌面端用的 WebView2）里打开订票页，
等页面自己加载完，再读出页面上已经显示出来的价格。

边界（和 CLAUDE.md 的抓取规则一致）：
- 不直接调用携程的搜索接口，不构造或破解任何签名，不改 User-Agent 和设备指纹，不用代理
- 页面要求登录或出现验证码时立刻放弃并如实报告（PageBlocked），之后隔很久才再试，不做任何绕过
- 只查用户固定监测的那一个行程，频率不超过每 30 分钟一次（见 farerecord/realprice.py）
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from farerecord.sources.base import SourceError

WINDOW_TITLE = "航价记 · 携程核价"
OFFSCREEN = -30000  # 窗口放在屏幕外：页面要"可见"才会加载，但不该打扰用户
PRICE_RANGE = (200, 300_000)  # 读到这个范围以外的数字，多半是读错了

# 读页面上已经渲染出来的内容：每个航班行的价格和航司、日期条上当前日期的最低价。
EXTRACT_JS = r"""
(function () {
  var items = [];
  [].slice.call(document.querySelectorAll('.flight-item')).forEach(function (el) {
    var p = el.querySelector('.flight-price');
    var m = p && p.innerText.match(/[¥￥]\s*([\d,]+)/);
    if (!m) return;
    var a = el.querySelector('.airline-name');
    items.push([+m[1].replace(/,/g, ''), a ? a.innerText.trim() : '']);
  });
  var tabs = [];
  [].slice.call(document.querySelectorAll('.calendar-lowprice li.tab')).forEach(function (li) {
    var m = (li.getAttribute('u_remark') || '')
      .match(/price:(\d+),date:(\d{4}-\d\d-\d\d),date2:(\d{4}-\d\d-\d\d)/);
    if (m) tabs.push([+m[1], m[2], m[3]]);
  });
  return JSON.stringify({
    url: location.href,
    items: items,
    tabs: tabs,
    captcha: !!document.querySelector('[class*=captcha],[id*=captcha],[class*=verify-]')
  });
})()
"""


class PageBlocked(SourceError):
    """携程要求登录或验证。不绕过，交给用户自己去看。"""


class PageEmpty(SourceError):
    """页面打开了，但没有读到这个行程的报价。"""


@dataclass(frozen=True)
class PageQuote:
    price: float
    airline: str | None = None  # 最低价那一行的航司名（页面上的中文名）


def round_url(origin: str, dest: str, depart: date, ret: date) -> str:
    return (
        f"https://flights.ctrip.com/online/list/round-{origin.lower()}-{dest.lower()}"
        f"?depdate={depart}_{ret}&cabin=y_s&adult=1&child=0&infant=0"
    )


def _dates_marker(depart: date, ret: date) -> str:
    return f"depdate={depart}_{ret}"


def is_blocked(snap: dict[str, Any]) -> bool:
    """被带去登录页，或者页面上只有验证码、没有报价。"""
    url = str(snap.get("url") or "")
    if "passport." in url or "/login" in url:
        return True
    return bool(snap.get("captcha")) and not snap.get("items")


def parse_snapshot(snap: dict[str, Any], depart: date, ret: date) -> PageQuote:
    """从 EXTRACT_JS 的结果里取这个行程的最低往返价。"""
    if is_blocked(snap):
        raise PageBlocked("携程要求登录或验证，已暂停自动核价")
    if _dates_marker(depart, ret) not in str(snap.get("url") or ""):
        raise PageEmpty("携程没有按这两个日期显示结果")
    items = [
        (float(p), str(a or "").strip())
        for p, a in snap.get("items") or []
        if PRICE_RANGE[0] <= float(p) <= PRICE_RANGE[1]
    ]
    if not items:
        raise PageEmpty("页面上没有读到报价（没加载完，或这两天没有航班）")
    # 列表默认按"直飞优先"排，而且只渲染前几行，最便宜的不一定在里面；
    # 日期条上当前日期的"低"价才是整页的最低价，有就以它为准。
    strip = [
        float(p)
        for p, d1, d2 in snap.get("tabs") or []
        if (d1, d2) == (depart.isoformat(), ret.isoformat())
        and PRICE_RANGE[0] <= float(p) <= PRICE_RANGE[1]
    ]
    price = min(strip) if strip else min(p for p, _ in items)
    airline = next((a for p, a in sorted(items) if p == price and a), None)
    return PageQuote(price, airline)


def _keep_out_of_taskbar(window: Any) -> None:
    """后台核价的窗口不出现在任务栏和 Alt+Tab 里。失败就算了，不影响读价格。"""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        hwnd = ctypes.c_void_p(window.native.Handle.ToInt64())
        user32 = ctypes.windll.user32
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        style = user32.GetWindowLongPtrW(hwnd, -20)  # GWL_EXSTYLE
        user32.ShowWindow(hwnd, 0)  # 任务栏只在窗口重新显示时才更新
        # 加 WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE，去掉 WS_EX_APPWINDOW
        user32.SetWindowLongPtrW(hwnd, -20, (style | 0x80 | 0x08000000) & ~0x40000)
        user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE


def wait_for_quotes(
    evaluate: Callable[[str], Any],
    marker: str,
    *,
    timeout: float = 45.0,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """反复读页面，直到报价出现、被拦住或超时。返回最后一次读到的内容。

    evaluate：在页面里执行一段脚本并返回结果；marker：目标页面网址里应有的日期片段，
    用来跳过"上一个页面还没换掉"的瞬间。
    """
    deadline = clock() + timeout
    snap: dict[str, Any] = {}
    seen = False
    while clock() < deadline:
        sleep(poll)
        try:
            raw = evaluate(EXTRACT_JS)
            current = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 — 页面正在跳转时执行脚本会失败，下一轮再读
            continue
        url = str(current.get("url") or "")
        if is_blocked(current):
            return current
        if marker not in url:
            continue
        snap = current
        if snap.get("items"):
            if snap.get("tabs") or seen:
                break
            seen = True  # 报价出来了，再等一轮让日期条上的最低价也出来
    return snap


class WebviewReader:
    """用 pywebview 开一个屏幕外的窗口来读页面。桌面端窗口开着时用（webview 已经启动）。"""

    def __init__(self, webview: Any, timeout: float = 45.0) -> None:
        self.webview = webview
        self.timeout = timeout

    def __call__(self, url: str, marker: str) -> dict[str, Any]:
        window = self.webview.create_window(
            WINDOW_TITLE,
            url,
            x=OFFSCREEN,
            y=OFFSCREEN,
            width=1280,
            height=900,
            focus=False,
        )
        window.events.shown += lambda: _keep_out_of_taskbar(window)
        try:
            return wait_for_quotes(window.evaluate_js, marker, timeout=self.timeout)
        finally:
            with contextlib.suppress(Exception):
                window.destroy()


def read_roundtrip(
    reader: Callable[[str, str], dict[str, Any]], origin: str, dest: str, depart: date, ret: date
) -> PageQuote:
    """打开这个行程的携程往返页并读出最低往返价。reader 可注入，测试不访问真实网络。"""
    snap = reader(round_url(origin, dest, depart, ret), _dates_marker(depart, ret))
    return parse_snapshot(snap, depart, ret)


def standalone_session(
    webview: Any,
    storage: Path,
    work: Callable[[Callable[[str, str], dict[str, Any]]], None],
    *,
    hard_timeout: float = 180.0,
) -> None:
    """没有桌面窗口时（任务计划里）：临时开一个屏幕外的浏览器窗口，让 work(reader) 用它读页面。

    pywebview 的事件循环要占用主线程，所以这个函数会一直等到 work 结束、窗口关掉才返回。
    万一浏览器卡死，hard_timeout 秒后直接结束进程——任务计划不能被一个卡住的进程一直占着。
    """
    window = webview.create_window(
        WINDOW_TITLE,
        html="<!doctype html><title>航价记</title>",
        x=OFFSCREEN,
        y=OFFSCREEN,
        width=1280,
        height=900,
        focus=False,
    )
    window.events.shown += lambda: _keep_out_of_taskbar(window)

    def reader(url: str, marker: str) -> dict[str, Any]:
        window.load_url(url)
        return wait_for_quotes(window.evaluate_js, marker)

    def run() -> None:
        try:
            work(reader)
        finally:
            with contextlib.suppress(Exception):
                window.destroy()

    watchdog = threading.Timer(hard_timeout, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    try:
        webview.start(run, private_mode=False, storage_path=str(storage))
    finally:
        watchdog.cancel()
