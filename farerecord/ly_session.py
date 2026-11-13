"""记住同程的登录状态，让桌面窗口重新打开后不用再登录。

同程网页版的登录用的是"会话 Cookie"：浏览器（WebView2）一关就清掉，所以每次重开航价记
都要重新登录。这里做的和浏览器的"记住我"一样：

- 每次成功读到同程价格后，把当时 www.ly.com 页面上的 Cookie 存到本机的
  data/ly_session.json（data/ 不进仓库；日志里不打印 Cookie 的值）
- 下次打开同程结果页之前，先把这些 Cookie 放回浏览器，再打开页面

只是保留用户自己登录得到的状态，不伪造、不破解任何东西。如果同程那边的登录已经过期，
页面照样会跳到登录页，首页提示"同程需要重新登录"。关闭同程监测时删除这个文件。
"""

from __future__ import annotations

import contextlib
import json
import logging
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DOMAIN_SUFFIX = "ly.com"
FILE_NAME = "ly_session.json"  # 放在数据库同一个目录（data/）


def path_for(database: str | Path) -> Path:
    return Path(database).parent / FILE_NAME


def records_from(cookies: list[SimpleCookie]) -> list[dict[str, Any]]:
    """pywebview 的 window.get_cookies() 结果 → 可存成 JSON 的记录（只留同程的）。"""
    out = []
    for jar in cookies:
        for name, m in jar.items():
            domain = (m["domain"] or "").strip()
            if not domain.lstrip(".").endswith(DOMAIN_SUFFIX):
                continue
            out.append(
                {
                    "name": name,
                    "value": m.value,
                    "domain": domain,
                    "path": m["path"] or "/",
                    "secure": bool(m["secure"]),
                    "httponly": bool(m["httponly"]),
                }
            )
    return out


def save(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("name") and "value" in r]


def forget(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def save_from_window(window: Any, path: Path) -> int:
    """从读价窗口里取同程的 Cookie 存下来，返回存了几条。失败就算了，不影响读价。"""
    try:
        records = records_from(window.get_cookies() or [])
    except Exception:  # noqa: BLE001
        log.warning("没能取到同程的登录状态", exc_info=True)
        return 0
    save(path, records)
    return len(records)


def restore_into_window(window: Any, path: Path) -> int:
    """把存下的同程 Cookie 放回浏览器（WebView2），要在打开同程页面之前调用。返回放回几条。"""
    records = load(path)
    if not records:
        return 0
    try:
        from System import Func, Type  # pythonnet，只在 Windows 桌面端有

        def add() -> None:
            manager = window.native.browser.webview.CoreWebView2.CookieManager
            for r in records:
                cookie = manager.CreateCookie(r["name"], r["value"], r["domain"], r["path"])
                cookie.IsSecure = bool(r.get("secure"))
                cookie.IsHttpOnly = bool(r.get("httponly"))
                manager.AddOrUpdateCookie(cookie)

        # WebView2 的 Cookie 只能在界面线程里操作
        window.native.Invoke(Func[Type](add))
    except Exception:  # noqa: BLE001
        log.warning("没能恢复同程的登录状态", exc_info=True)
        return 0
    return len(records)
