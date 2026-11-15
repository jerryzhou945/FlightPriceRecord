"""界面里可选的城市（携程用的城市三字码，即 IATA 城市代码）。

列表只是方便选择：不在列表里的城市也可以直接填三字码。
"""

from __future__ import annotations

CITY_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "北美",
        [
            ("CHI", "芝加哥"),
            ("NYC", "纽约"),
            ("LAX", "洛杉矶"),
            ("SFO", "旧金山"),
            ("SEA", "西雅图"),
            ("BOS", "波士顿"),
            ("WAS", "华盛顿"),
            ("DTT", "底特律"),
            ("MSP", "明尼阿波利斯"),
            ("HOU", "休斯顿"),
            ("DFW", "达拉斯"),
            ("ATL", "亚特兰大"),
            ("PHL", "费城"),
            ("DEN", "丹佛"),
            ("LAS", "拉斯维加斯"),
            ("MIA", "迈阿密"),
            ("SAN", "圣地亚哥"),
            ("PHX", "凤凰城"),
            ("YTO", "多伦多"),
            ("YVR", "温哥华"),
            ("YMQ", "蒙特利尔"),
        ],
    ),
    (
        "中国",
        [
            ("SHA", "上海"),
            ("BJS", "北京"),
            ("CAN", "广州"),
            ("SZX", "深圳"),
            ("HGH", "杭州"),
            ("NKG", "南京"),
            ("CTU", "成都"),
            ("CKG", "重庆"),
            ("WUH", "武汉"),
            ("XMN", "厦门"),
            ("TAO", "青岛"),
            ("SIA", "西安"),  # 携程用 SIA（咸阳机场是 XIY，查不到）
            ("TSN", "天津"),
            ("SHE", "沈阳"),
            ("DLC", "大连"),
            ("CSX", "长沙"),
            ("KMG", "昆明"),
            ("CGO", "郑州"),
            ("FOC", "福州"),
            ("HRB", "哈尔滨"),
            ("HKG", "香港"),
            ("MFM", "澳门"),
            ("TPE", "台北"),
        ],
    ),
]

CITY_NAMES: dict[str, str] = {code: name for _, cities in CITY_GROUPS for code, name in cities}


def city_name(code: str) -> str:
    return CITY_NAMES.get(code.upper(), code.upper())


def route_name(origin: str, dest: str) -> str:
    return f"{city_name(origin)} ⇄ {city_name(dest)}"
