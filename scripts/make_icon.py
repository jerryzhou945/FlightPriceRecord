"""生成 web/icon.ico（桌面快捷方式的图标），图案和 web/icon.svg 一致。

只用标准库：自己光栅化（超采样抗锯齿）→ PNG → ICO。改了图标设计后重新运行：
    .venv\\Scripts\\python scripts\\make_icon.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZES = (256, 64, 48, 32, 16)
SS = 4  # 每个像素采样 SS×SS 次
RADIUS = 15 / 64  # 圆角，占边长的比例

# 纸飞机的两个面（64×64 坐标系，和 icon.svg 里的 path 一致）
PLANE_LIGHT = [(50, 14), (12, 29), (25, 34), (29, 48), (36, 38), (45, 44)]
PLANE_SHADE = [(50, 14), (25, 34), (29, 48), (32, 38)]


def inside(poly: list[tuple[float, float]], x: float, y: float) -> bool:
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1], strict=True):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            hit = not hit
    return hit


def in_rounded_square(x: float, y: float) -> bool:
    """x、y 在 0–1 之间。"""
    cx = min(max(x, RADIUS), 1 - RADIUS)
    cy = min(max(y, RADIUS), 1 - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS**2


def mix(a: tuple[float, ...], b: tuple[float, ...], t: float) -> tuple[float, ...]:
    return tuple(p + (q - p) * t for p, q in zip(a, b, strict=True))


def sample(x: float, y: float) -> tuple[float, float, float, float]:
    """一个采样点的颜色（预乘前的 RGB + 透明度），x、y 在 0–1 之间。"""
    if not in_rounded_square(x, y):
        return (0, 0, 0, 0)
    px, py = x * 64, y * 64
    if inside(PLANE_SHADE, px, py):
        return (0xE3, 0xB9, 0xC0, 1)
    if inside(PLANE_LIGHT, px, py):
        return (0xF6, 0xEE, 0xF0, 1)
    color = mix((0x2A, 0x2D, 0x55), (0x0D, 0x0F, 0x22), (x + y) / 2)  # 深蓝紫渐变
    glow = max(0.0, 1 - (((x - 0.82) ** 2 + (y - 0.14) ** 2) ** 0.5) / 0.75) * 0.75  # 右上角暖光
    return (*mix(color, (0xF0, 0xC6, 0xCC), glow), 1)


def render(size: int) -> bytes:
    """返回 size×size 的 RGBA 字节。"""
    out = bytearray()
    n = SS * SS
    for j in range(size):
        for i in range(size):
            r = g = b = a = 0.0
            for sj in range(SS):
                for si in range(SS):
                    cr, cg, cb, ca = sample(
                        (i + (si + 0.5) / SS) / size, (j + (sj + 0.5) / SS) / size
                    )
                    r, g, b, a = r + cr * ca, g + cg * ca, b + cb * ca, a + ca
            out += bytes(
                (round(r / a), round(g / a), round(b / a), round(a / n * 255))
                if a
                else (0, 0, 0, 0)
            )
    return bytes(out)


def png(size: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00" + rgba[y * size * 4 : (y + 1) * size * 4] for y in range(size)
    )  # 每行前面是滤波类型 0
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 位 RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 9))
        + chunk(b"IEND", b"")
    )


def ico(images: dict[int, bytes]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(images))
    offset = len(head) + 16 * len(images)
    entries = b""
    for size, data in images.items():
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset
        )  # 256 写成 0
        offset += len(data)
    return head + entries + b"".join(images.values())


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "web" / "icon.ico"
    target.write_bytes(ico({s: png(s, render(s)) for s in SIZES}))
    print(f"已生成 {target}（{target.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
