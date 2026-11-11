"""推送通道：桌面通知（Windows）、WxPusher、PushPlus（微信）、控制台（测试用）。

密钥只从环境变量读取，不写日志。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol
from xml.sax.saxutils import escape, quoteattr

import httpx

if TYPE_CHECKING:
    from farerecord.config import AlertConfig

log = logging.getLogger(__name__)


class NotifyError(Exception):
    pass


class Notifier(Protocol):
    name: str
    label: str

    @property
    def configured(self) -> bool: ...

    def send(self, title: str, content: str, url: str | None = None) -> None:
        """content 为 Markdown；失败抛 NotifyError。"""
        ...


class ConsoleNotifier:
    name = "console"
    label = "控制台（未推送到微信）"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    @property
    def configured(self) -> bool:
        return True

    def send(self, title: str, content: str, url: str | None = None) -> None:
        self.sent.append((title, content, url))
        print(f"[提醒] {title}\n{content}" + (f"\n{url}" if url else ""))


# ---------- 桌面通知 ----------

# 用系统自带的 Windows PowerShell 弹出通知，不需要额外安装东西。
# 内容通过环境变量传入，不拼进命令行。
_TOAST_SCRIPT = (
    "$ErrorActionPreference = 'Stop';"
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
    " ContentType = WindowsRuntime] | Out-Null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument,"
    " ContentType = WindowsRuntime] | Out-Null;"
    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
    "$xml.LoadXml($env:FARERECORD_TOAST_XML);"
    "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml;"
    "[Windows.UI.Notifications.ToastNotificationManager]::"
    "CreateToastNotifier($env:FARERECORD_TOAST_APP).Show($toast)"
)
# 通知必须挂在一个已注册的应用名下；用 PowerShell 自己的，系统里一定存在
_TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def plain_lines(markdown: str, limit: int = 4) -> list[str]:
    """把提醒的 Markdown 正文压成几行纯文本，给桌面通知用。"""
    lines = []
    for raw in markdown.splitlines()[1:]:  # 第一行是标题，通知里已经有了
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", "", raw)  # 链接
        line = re.sub(r"^\s*(#+|[-*])\s*", "", line).replace("`", "").strip()
        if line:
            lines.append(line)
    return lines[:limit]


def toast_xml(title: str, content: str, url: str | None = None) -> str:
    texts = "".join(f"<text>{escape(t)}</text>" for t in [title, *plain_lines(content)][:3])
    launch = f' activationType="protocol" launch={quoteattr(url)}' if url else ""
    return (
        f"<toast{launch}><visual><binding template='ToastGeneric'>{texts}"
        "<text placement='attribution'>航价记</text></binding></visual></toast>"
    )


def run_toast(xml: str) -> None:
    """真正弹出通知。测试里会被替换掉。"""
    env = {**os.environ, "FARERECORD_TOAST_XML": xml, "FARERECORD_TOAST_APP": _TOAST_APP_ID}
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TOAST_SCRIPT],
        env=env,
        check=True,
        capture_output=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


class DesktopNotifier:
    """Windows 右下角的系统通知；点击通知打开携程核价页。"""

    name = "desktop"
    label = "桌面通知"

    def __init__(self, runner: Callable[[str], None] | None = None) -> None:
        self._runner = runner

    @property
    def configured(self) -> bool:
        return sys.platform == "win32"

    def send(self, title: str, content: str, url: str | None = None) -> None:
        if not self.configured:
            raise NotifyError("桌面通知目前只支持 Windows")
        try:
            (self._runner or run_toast)(toast_xml(title, content, url))
        except (OSError, subprocess.SubprocessError) as exc:
            raise NotifyError(f"桌面通知发送失败：{exc.__class__.__name__}") from exc


# ---------- 微信 ----------


class WxPusherNotifier:
    """https://wxpusher.zjiecode.com ：创建应用拿 APP_TOKEN，关注应用拿 UID。"""

    name = "wxpusher"
    label = "WxPusher 微信"
    URL = "https://wxpusher.zjiecode.com/api/send/message"

    def __init__(
        self, app_token: str | None, uids: list[str], client: httpx.Client | None = None
    ) -> None:
        self.app_token = app_token
        self.uids = [u for u in uids if u]
        self._client = client or httpx.Client(timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self.app_token and self.uids)

    def send(self, title: str, content: str, url: str | None = None) -> None:
        if not self.configured:
            raise NotifyError("没有配置 WXPUSHER_APP_TOKEN / WXPUSHER_UID")
        payload = {
            "appToken": self.app_token,
            "content": content,
            "summary": title[:95],
            "contentType": 3,  # Markdown
            "uids": self.uids,
        }
        if url:
            payload["url"] = url
        try:
            resp = self._client.post(self.URL, json=payload)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NotifyError(f"WxPusher 请求失败：{exc.__class__.__name__}") from exc
        if not data.get("success") and data.get("code") != 1000:
            raise NotifyError(f"WxPusher 返回错误：{data.get('msg')}")


class PushPlusNotifier:
    """https://www.pushplus.plus ：登录后复制 token。"""

    name = "pushplus"
    label = "PushPlus 微信"
    URL = "https://www.pushplus.plus/send"

    def __init__(self, token: str | None, client: httpx.Client | None = None) -> None:
        self.token = token
        self._client = client or httpx.Client(timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def send(self, title: str, content: str, url: str | None = None) -> None:
        if not self.configured:
            raise NotifyError("没有配置 PUSHPLUS_TOKEN")
        body = content + (f"\n\n[去携程核价]({url})" if url and url not in content else "")
        payload = {"token": self.token, "title": title, "content": body, "template": "markdown"}
        try:
            resp = self._client.post(self.URL, json=payload)
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NotifyError(f"PushPlus 请求失败：{exc.__class__.__name__}") from exc
        if data.get("code") != 200:
            raise NotifyError(f"PushPlus 返回错误：{data.get('msg')}")


class MultiNotifier:
    """同时发到多个通道。只要有一个发成功就算成功。"""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self.notifiers = notifiers
        self.name = "+".join(n.name for n in notifiers)
        self.label = " + ".join(n.label for n in notifiers)

    @property
    def configured(self) -> bool:
        return any(n.configured for n in self.notifiers)

    def send(self, title: str, content: str, url: str | None = None) -> None:
        errors = []
        delivered = 0
        for n in self.notifiers:
            if not n.configured:
                continue
            try:
                n.send(title, content, url)
                delivered += 1
            except NotifyError as exc:
                log.warning("%s 发送失败：%s", n.label, exc)
                errors.append(f"{n.label}：{exc}")
        if not delivered:
            raise NotifyError("；".join(errors) or "没有可用的推送通道")


def make_notifier(channel: str) -> Notifier:
    """微信通道（或控制台）。"""
    channel = (channel or "console").lower()
    if channel == "wxpusher":
        uids = os.environ.get("WXPUSHER_UID", "").replace(";", ",").split(",")
        return WxPusherNotifier(os.environ.get("WXPUSHER_APP_TOKEN"), [u.strip() for u in uids])
    if channel == "pushplus":
        return PushPlusNotifier(os.environ.get("PUSHPLUS_TOKEN"))
    if channel == "desktop":
        return DesktopNotifier()
    return ConsoleNotifier()


def build_notifier(alerts: AlertConfig) -> Notifier:
    """按提醒设置组合通道：桌面通知（desktop 开关）+ 微信通道（channel）。"""
    parts: list[Notifier] = []
    if alerts.desktop and sys.platform == "win32":
        parts.append(DesktopNotifier())
    wechat = make_notifier(alerts.channel)
    if wechat.name not in ("console", "desktop") and wechat.configured:
        parts.append(wechat)
    if not parts:
        return ConsoleNotifier()
    return parts[0] if len(parts) == 1 else MultiNotifier(parts)
