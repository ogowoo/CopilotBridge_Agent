# -*- coding: utf-8 -*-
"""托盘图标改用**字母 G**（用户："图片用 G 字吧"），并输出一张预览图便于核对。

设计：圆角方底（品牌绿 #0b7a5a）+ 白色粗体 G，与界面上那个「G」品牌块一致。
小尺寸（16/20/24）单独按像素网格画，避免缩放出锯齿——直接缩小大图会让 G 的开口糊掉。
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "assets" / "gantry.ico"
PREVIEW = BASE / "data" / "ui-audit" / "tray-icon.png"

BG = (11, 122, 90, 255)       # #0b7a5a（与页面 .nc-head 的品牌块同色）
FG = (255, 255, 255, 255)

# Windows 自带字体，按"粗、字腔大"排序；都没有就用 PIL 默认位图字体（也能看）
FONT_CANDIDATES = ("segoeuib.ttf", "seguibl.ttf", "arialbd.ttf", "arial.ttf")


def _font(px: int):
    for name in FONT_CANDIDATES:
        for root in (Path("C:/Windows/Fonts"),):
            p = root / name
            if p.is_file():
                try:
                    return ImageFont.truetype(str(p), px)
                except Exception:               # noqa: BLE001
                    continue
    return ImageFont.load_default()


def draw(size: int) -> Image.Image:
    """画一张 size×size 的图标：圆角底 + 居中的 G。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = max(2, round(size * 0.22))
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)

    # 字号按高度给足（G 是"满字"字形，约 78% 高度看起来最饱满）
    # 小尺寸要更满一点，否则 16px 里 G 显得又小又细
    px = max(7, round(size * (0.92 if size <= 24 else 0.78)))
    font = _font(px)
    # 用实际包围盒居中：不同字体的 G 左右留白不同，靠 anchor 容易偏
    left, top, right, bottom = d.textbbox((0, 0), "G", font=font)
    w, h = right - left, bottom - top
    d.text(((size - w) / 2 - left, (size - h) / 2 - top), "G", font=font, fill=FG)
    return img


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 20, 24, 32, 48, 64, 128, 256]
    imgs = [draw(n) for n in sizes]
    imgs[-1].save(OUT, format="ICO", sizes=[(n, n) for n in sizes])
    print(f"已生成 {OUT}（{OUT.stat().st_size} 字节，{len(sizes)} 个尺寸）")

    # 预览：小尺寸按 1x 贴上去（像素级），再加一张 16px 放大 6 倍看清晰度
    PREVIEW.parent.mkdir(parents=True, exist_ok=True)
    pad, canvas_h = 12, 280
    widths = sum(i.width + pad for i in imgs) + pad + 16 * 6 + pad
    sheet = Image.new("RGBA", (widths, canvas_h), (245, 247, 250, 255))
    x = pad
    for im in imgs:
        sheet.alpha_composite(im, (x, pad))
        x += im.width + pad
    big = draw(16).resize((16 * 6, 16 * 6), Image.NEAREST)
    sheet.alpha_composite(big, (x, pad))
    sheet.save(PREVIEW)
    print(f"预览已保存：{PREVIEW}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
