"""携程往返订票页：读页面上显示的真实往返含税价。

低价日历接口给不了长途行程的真实往返价（见 docs/architecture.md），而订票页上就写着。
做法和人自己去看是一样的：在一个真实的浏览器窗口（桌面端用的 WebView2）里打开订票页，
等页面自己加载完，再读出页面上已经显示出来的价格。

边界（和 CLAUDE.md 的抓取规则一致）：
- 不直接调用携程的搜索接口，不构造或破解任何签名，不改 User-Agent 和设备指纹，不用代理
- 页面要求登录或出现验证码时立刻放弃并如实报告（PageBlocked），之后隔很久才再试，不做任何绕过
- 只查用户固定监测的那一个行程，频率不超过每 15 分钟一次（见 farerecord/realprice.py）
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
SETTLE_SECONDS = 14.0  # 认不出搜索进度条时，报价出现后至少再等这么久
PRICE_RANGE = (200, 300_000)  # 读到这个范围以外的数字，多半是读错了

# 读页面上已经渲染出来的内容：每个航班行的价格、航司和余票提示（携程只在座位很少时
# 在价格旁显示"剩 N 张"），以及日期条上当前日期的最低价。
EXTRACT_JS = r"""
(function () {
  var items = [];
  [].slice.call(document.querySelectorAll('.flight-item')).forEach(function (el) {
    var p = el.querySelector('.flight-price');
    var m = p && p.innerText.match(/[¥￥]\s*([\d,]+)/);
    if (!m) return;
    var a = el.querySelector('.airline-name');
    var seat = null;
    [].slice.call(el.querySelectorAll('*')).some(function (x) {
      var s = !x.children.length && x.textContent.match(/剩\s*(\d+)\s*张/);
      if (s) seat = +s[1];
      return !!s;
    });
    items.push([+m[1].replace(/,/g, ''), a ? a.innerText.trim() : '', seat]);
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
    loading: !!document.querySelector('[class*="Progress_process"]'),
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
    # 最低价那一行的"剩 N 张"。携程只在座位很少（实测见过剩 1 张）时才显示，
    # None 表示页面没提示余票紧张（或最低价那一行没渲染出来）。
    seats_left: int | None = None
    # 页面上比最低价高的下一档价格（优先同一家航司）：这一档卖完后大概会涨到这里
    next_price: float | None = None


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


def parse_snapshot(
    snap: dict[str, Any],
    depart: date,
    ret: date,
    *,
    site: str = "携程",
    marker: str | None = None,
) -> PageQuote:
    """从读页面脚本的结果里取这个行程的最低往返价。"""
    if is_blocked(snap):
        url = str(snap.get("url") or "")
        if "passport." in url or "/login" in url:
            raise PageBlocked(f"{site}需要重新登录（被带到了登录页），已暂停自动核价")
        raise PageBlocked(f"{site}出现了验证（验证码或滑块），已暂停自动核价")
    if (marker or _dates_marker(depart, ret)) not in str(snap.get("url") or ""):
        raise PageEmpty(f"{site}没有按这两个日期显示结果")
    items = [
        (float(row[0]), str(row[1] or "").strip(), _seat(row))
        for row in snap.get("items") or []
        if PRICE_RANGE[0] <= float(row[0]) <= PRICE_RANGE[1]
    ]
    if not items:
        raise PageEmpty(f"{site}页面上没有读到报价（没加载完，或这两天没有航班）")
    # 列表默认按"直飞优先"排，而且只渲染前几行，最便宜的不一定在里面；
    # 日期条上当前日期的"低"价才是整页的最低价，有就以它为准。
    strip = [
        float(p)
        for p, d1, d2 in snap.get("tabs") or []
        if (d1, d2) == (depart.isoformat(), ret.isoformat())
        and PRICE_RANGE[0] <= float(p) <= PRICE_RANGE[1]
    ]
    price = min(strip) if strip else min(p for p, _, _ in items)
    airline = next((a for p, a, _ in sorted(items, key=lambda x: x[0]) if p == price and a), None)
    seats = [s for p, _, s in items if p == price and s is not None]
    higher = sorted(p for p, a, _ in items if p > price and (not airline or a == airline))
    higher = higher or sorted(p for p, _, _ in items if p > price)
    return PageQuote(price, airline, min(seats) if seats else None, higher[0] if higher else None)


def _seat(row: list[Any]) -> int | None:
    """旧版脚本的行只有 [价格, 航司]；新版第三项是"剩 N 张"的 N 或 null。"""
    if len(row) < 3 or row[2] in (None, ""):
        return None
    try:
        n = int(row[2])
    except (TypeError, ValueError):
        return None
    return n if 0 < n < 100 else None


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
    script: str = EXTRACT_JS,
    timeout: float = 45.0,
    poll: float = 2.0,
    settle: float = SETTLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """反复读页面，直到报价出齐、被拦住或超时。返回最后一次读到的内容。

    携程是边搜边显示的：先出一批价格，几秒后搜索进度条走完，价格才变成最终的
    （实测先显示 ¥7,133，6 秒后变成 ¥7,125）。所以要等进度条结束，并且连续两次读到的
    价格一样才算数。万一页面改版认不出进度条，就在报价出现后至少再等 settle 秒。

    evaluate：在页面里执行一段脚本并返回结果；marker：目标页面网址里应有的日期片段，
    用来跳过"上一个页面还没换掉"的瞬间。
    """
    deadline = clock() + timeout
    snap: dict[str, Any] = {}
    previous: Any = None
    first_seen: float | None = None
    saw_progress = False
    while clock() < deadline:
        sleep(poll)
        try:
            raw = evaluate(script)
            current = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001 — 页面正在跳转时执行脚本会失败，下一轮再读
            continue
        if is_blocked(current):
            return current
        if marker not in str(current.get("url") or ""):
            continue
        snap = current
        loading = bool(snap.get("loading"))
        saw_progress = saw_progress or loading
        if not snap.get("items"):
            continue
        now = clock()
        first_seen = now if first_seen is None else first_seen
        prices = (snap.get("items"), snap.get("tabs"))
        steady = prices == previous
        previous = prices
        if steady and not loading and (saw_progress or now - first_seen >= settle):
            break
    return snap


class WebviewReader:
    """用 pywebview 开一个屏幕外的窗口来读页面。桌面端窗口开着时用（webview 已经启动）。"""

    def __init__(self, webview: Any, timeout: float = 45.0) -> None:
        self.webview = webview
        self.timeout = timeout

    def __call__(self, url: str, marker: str, script: str = EXTRACT_JS) -> dict[str, Any]:
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
            return wait_for_quotes(window.evaluate_js, marker, script=script, timeout=self.timeout)
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

    def reader(url: str, marker: str, script: str = EXTRACT_JS) -> dict[str, Any]:
        window.load_url(url)
        return wait_for_quotes(window.evaluate_js, marker, script=script)

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
