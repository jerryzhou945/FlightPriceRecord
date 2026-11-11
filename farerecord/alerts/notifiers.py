"""推送通道：WxPusher、PushPlus（都推送到微信）、控制台（测试用）。密钥只从环境变量读取。"""

from __future__ import annotations

import os
from typing import Protocol

import httpx


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


def make_notifier(channel: str) -> Notifier:
    channel = (channel or "console").lower()
    if channel == "wxpusher":
        uids = os.environ.get("WXPUSHER_UID", "").replace(";", ",").split(",")
        return WxPusherNotifier(os.environ.get("WXPUSHER_APP_TOKEN"), [u.strip() for u in uids])
    if channel == "pushplus":
        return PushPlusNotifier(os.environ.get("PUSHPLUS_TOKEN"))
    return ConsoleNotifier()
