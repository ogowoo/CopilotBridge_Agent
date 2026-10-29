# -*- coding: utf-8 -*-
"""截屏右下角通知区，确认托盘图标真的出现了（只看截图，不依赖进程名）。"""
import ctypes
import sys
from pathlib import Path

out = Path("data/ui-audit/tray-screen.png")
out.parent.mkdir(parents=True, exist_ok=True)

try:
    from PIL import ImageGrab
except Exception as exc:                                  # noqa: BLE001
    print("PIL 不可用:", exc)
    sys.exit(1)

ctypes.windll.user32.SetProcessDPIAware()
shot = ImageGrab.grab()
print("屏幕尺寸:", shot.size)
# 通知区在右下角：取右下 620x140
w, h = shot.size
crop = shot.crop((max(0, w - 620), max(0, h - 140), w, h))
crop = crop.resize((crop.width * 2, crop.height * 2))     # 放大 2 倍便于看清
crop.save(out)
print("已保存:", out)
