# -*- coding: utf-8 -*-
"""生成托盘图标 assets/gantry.ico（与页面 favicon 同一个"桥"字形）。

调色板跟随界面：深青绿底（#0b7a5a）+ 白色桥拱。输出多尺寸（16/20/24/32/48/64/128/256），
Windows 会按 DPI 自动挑。这个脚本只在需要换图标时手动跑一次，产物入库。
"""
from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "assets" / "gantry.ico"

BG = (11, 122, 90, 255)       # #0b7a5a
FG = (255, 255, 255, 255)


def draw(size: int) -> Image.Image:
    """在 32×32 的坐标系里画（与 favicon 的 SVG 同比例），再放大到目标尺寸。"""
    s = 32
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 圆角底
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=7, fill=BG)
    # 桥面（一条横线）
    w = max(2, round(s * 2.5 / 32))
    d.line([(8, 21), (24, 21)], fill=FG, width=w)
    # 桥拱（半圆 + 两条腿）
    d.arc([11, 11, 21, 21], start=180, end=360, fill=FG, width=w)
    d.line([(11, 16), (11, 21)], fill=FG, width=w)
    d.line([(21, 16), (21, 21)], fill=FG, width=w)
    if size != s:
        img = img.resize((size, size), Image.LANCZOS)
    return img


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 20, 24, 32, 48, 64, 128, 256]
    imgs = [draw(n) for n in sizes]
    imgs[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes])
    print(f"已生成 {OUT}（{OUT.stat().st_size} 字节，{len(sizes)} 个尺寸）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
