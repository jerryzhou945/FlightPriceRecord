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

from farerecord.config import AppConfig, load_config

log = logging.getLogger(__name__)

TITLE = "航价记"
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
    """在后台线程启动本机服务和定时抓取，等服务就绪后返回 (server, scheduler)。"""
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
    return server, scheduler


def style_title_bar(window) -> None:
    """把 Windows 标题栏改成和界面一致的深色（Windows 11；旧系统上静默跳过）。"""
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


def run(cfg: AppConfig | None = None) -> int:
    cfg = cfg or load_config()
    webview = import_webview()
    port = free_port()
    server, scheduler = start_server(cfg, port)
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
        window.events.shown += lambda: style_title_bar(window)
        webview.start(private_mode=False, storage_path=str(storage))
    finally:
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
