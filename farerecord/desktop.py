"""桌面端：一个独立窗口（WebView2），不需要开浏览器。

窗口里显示的还是 web/ 这套界面，后面是同一个 FastAPI 服务，只是跑在本机的随机端口上、
只监听回环地址。窗口开着的时候按 collector.interval_minutes 自动抓取。

启动：`farerecord app`，或双击 scripts\\install-shortcut.ps1 生成的桌面快捷方式。
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import socket
import sys
import threading
import time
from pathlib import Path

from farerecord.config import ROOT, AppConfig, load_config

log = logging.getLogger(__name__)

TITLE = "航价记"
APP_ID = "FareRecord.Desktop"
ICON = ROOT / "web" / "icon.ico"  # scripts/make_icon.py 生成
WINDOW_BG = "#0a0c1b"  # 和 web/styles.css 的 --bg 一致，加载时不闪白
CAPTION_COLOR = 0x00220F0D  # 标题栏颜色 #0d0f22，Windows 的 COLORREF 是 0x00BBGGRR


class DesktopUnavailable(RuntimeError):
    """缺少桌面窗口需要的组件。"""


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def import_webview():
    try:
        import webview
    except ImportError as exc:
        raise DesktopUnavailable(
            '桌面窗口需要 pywebview：请运行 .venv\\Scripts\\pip install -e ".[desktop]"'
        ) from exc
    return webview


def start_server(cfg: AppConfig, port: int, timeout: float = 15.0):
    """在后台线程启动本机服务和定时抓取，等服务就绪后返回 (server, scheduler, app)。"""
    import uvicorn

    from farerecord.api.app import create_app
    from farerecord.scheduler import CollectScheduler

    app = create_app(cfg)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    threading.Thread(target=server.run, name="farerecord-server", daemon=True).start()
    deadline = time.monotonic() + timeout
    while not server.started:
        if time.monotonic() > deadline:
            raise DesktopUnavailable("本机服务启动超时")
        time.sleep(0.05)
    scheduler = CollectScheduler(cfg, app.state.collect_lock)
    scheduler.start()
    return server, scheduler, app


def set_app_identity() -> None:
    """让任务栏把它当成独立的应用（用窗口自己的图标），而不是归到 Python 名下。"""
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


def set_window_icon(hwnd: int) -> None:
    """标题栏和任务栏用航价记的图标（否则是 python.exe 的）。"""
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    # (WM_SETICON 的大小图标, 对应的系统图标尺寸)：小图标 SM_CXSMICON=49，大图标 SM_CXICON=11
    for which, metric in ((0, 49), (1, 11)):
        size = user32.GetSystemMetrics(metric)
        handle = user32.LoadImageW(None, str(ICON), 1, size, size, 0x10)  # IMAGE_ICON, 从文件加载
        if handle:
            user32.SendMessageW(hwnd, 0x80, which, handle)  # WM_SETICON


def style_window(window) -> None:
    """标题栏改成和界面一致的深色并换上图标（Windows 11；旧系统上静默跳过）。"""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        hwnd = window.native.Handle.ToInt64()
        dwm = ctypes.windll.dwmapi
        for attribute, value in ((20, 1), (35, CAPTION_COLOR)):  # 深色模式、标题栏颜色
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attribute, ctypes.byref(v), ctypes.sizeof(v)
            )
        if ICON.is_file():
            set_window_icon(hwnd)


def open_ly_login(webview, monitor) -> None:
    """弹出同程登录页。用户自己输入账号密码；关掉窗口后马上去读一次同程价格。

    和后台读价的窗口共用同一个 WebView2 数据目录，所以这里登录后，后台窗口也是登录状态。
    """
    from farerecord.sources.ly_page import LOGIN_URL

    window = webview.create_window(
        "登录同程（登录成功后关掉这个窗口）", LOGIN_URL, width=520, height=760
    )
    window.events.closed += monitor.kick


def run(cfg: AppConfig | None = None) -> int:
    cfg = cfg or load_config()
    webview = import_webview()
    set_app_identity()
    port = free_port()
    from farerecord.realprice import RealPriceMonitor
    from farerecord.sources.ctrip_page import WebviewReader

    server, scheduler, app = start_server(cfg, port)
    # 固定监测的行程：后台开一个屏幕外的窗口读携程往返订票页上的真实往返价
    monitor = RealPriceMonitor(cfg, WebviewReader(webview))
    app.state.real_monitor = monitor
    app.state.ly_login = lambda: open_ly_login(webview, monitor)
    storage = Path(cfg.database).parent / "webview"  # 记住主题等界面偏好
    try:
        window = webview.create_window(
            TITLE,
            f"http://127.0.0.1:{port}/",
            width=1320,
            height=900,
            min_size=(980, 640),
            background_color=WINDOW_BG,
            text_select=True,
        )
        window.events.shown += lambda: style_window(window)
        webview.start(monitor.start, private_mode=False, storage_path=str(storage))
    finally:
        monitor.stop()
        scheduler.stop()
        server.should_exit = True
    return 0


def main() -> int:
    """pythonw -m farerecord.desktop 的入口（没有控制台窗口）。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run()
    except Exception as exc:  # 没有控制台，出错要让用户看得见
        log.exception("桌面端启动失败")
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, f"航价记启动失败：\n{exc}", TITLE, 0x10)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
