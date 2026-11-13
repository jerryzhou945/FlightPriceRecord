"""同程旅行国际机票往返结果页：读页面上显示的往返含税价和余票提示。

同程网页版查国际机票要先登录。用户在航价记的窗口里自己登录一次（登录状态存在本机
WebView2 的数据目录里），之后桌面端按抓取间隔打开结果页、读页面上已经显示的价格。

边界（和 CLAUDE.md 一致）：
- 只读页面上已经渲染出来的内容，不调用页面背后的搜索接口，不构造签名，不改设备指纹
- 只在桌面端读（任务计划里的后台窗口没有登录状态，不读）；登录失效或出现验证码时
  立刻停下并在首页提示，不做任何绕过
- 只查用户固定监测的行程，间隔跟抓取间隔走，最快每 15 分钟一次
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

from farerecord.sources.ctrip_page import PageQuote, parse_snapshot

SITE = "同程"
LOGIN_URL = "https://passport.ly.com/"

# 和携程那份脚本返回一样的结构，这样等待、解析的代码可以共用：
# items：[价格, 航司, "余 N 张"的 N 或 null]；tabs：日期条上当前这两天的最低价。
EXTRACT_JS = r"""
(function () {
  var items = [];
  [].slice.call(document.querySelectorAll('.flight-list .flight-item')).forEach(function (el) {
    var p = el.querySelector('.flight-price .price-info');
    var m = p && p.innerText.match(/[¥￥]\s*([\d,]+)/);
    if (!m) return;
    var a = el.querySelector('.airways-title');
    var s = el.querySelector('.left-num');
    var n = s && s.innerText.match(/(\d+)\s*张/);
    items.push([+m[1].replace(/,/g, ''), a ? a.innerText.trim() : '', n ? +n[1] : null]);
  });
  var tabs = [];
  var para = decodeURIComponent(location.search).match(/(\d{4}-\d\d-\d\d)\*(\d{4}-\d\d-\d\d)/);
  var on = document.querySelector('.if-calendar_tab-item.checked');
  var t = on && on.innerText.match(/[¥￥]\s*([\d,]+)/);
  if (para && t) tabs.push([+t[1].replace(/,/g, ''), para[1], para[2]]);
  return JSON.stringify({
    url: decodeURIComponent(location.href),
    items: items,
    tabs: tabs,
    loading: false,
    captcha: !!document.querySelector(
      '[class*=captcha],[id*=captcha],[class*=verify-],[class*=slider-]')
  });
})()
"""


def round_url(origin: str, dest: str, depart: date, ret: date) -> str:
    return (
        "https://www.ly.com/iflight/book1.html"
        f"?para={origin.upper()}*{dest.upper()}*{depart}*{ret}*RT*1_0_0*Y|S|C|F"
    )


def dates_marker(depart: date, ret: date) -> str:
    return f"{depart}*{ret}"


def read_roundtrip(
    reader: Callable[..., dict[str, Any]], origin: str, dest: str, depart: date, ret: date
) -> PageQuote:
    """打开同程往返结果页并读出最低往返价。reader 可注入，测试不访问真实网络。"""
    marker = dates_marker(depart, ret)
    snap = reader(round_url(origin, dest, depart, ret), marker, EXTRACT_JS)
    return parse_snapshot(snap, depart, ret, site=SITE, marker=marker)
