# -*- coding: utf-8 -*-
"""issue #1 的回归测试：句柄失效时 `_press_box` / `_type_prompt` 必须逐级兜底。

用假对象模拟三种情况：
1. 旧句柄 press 直接抛（元素 detached）→ 重新取句柄后成功；
2. 新句柄也抛 → 退回 `page.keyboard.press`；
3. fill 抛 → 兜底写入成功。
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gantry.api  # noqa: F401,E402  (先导入 api，避免 bridge/page 循环导入)
from gantry import page as pg  # noqa: E402


class _Boom:
    async def press(self, keys, timeout=None):
        raise RuntimeError("Element is not attached to the DOM")

    async def fill(self, text):
        raise RuntimeError("Element is not attached to the DOM")

    async def press_sequentially(self, text, timeout=None):
        raise RuntimeError("Element is not attached to the DOM")

    async def click(self, timeout=None):
        raise RuntimeError("detached")

    async def evaluate(self, *a, **k):
        raise RuntimeError("detached")

    async def focus(self, timeout=None):
        raise RuntimeError("detached")


class _KeyboardLike:
    def __init__(self, rec):
        self._rec = rec

    async def press(self, keys):
        self._rec.append(("press", keys))

    async def type(self, text):
        self._rec.append(("type", text))


class _Page:
    def __init__(self):
        self.calls = []
        self.keyboard = _KeyboardLike(self.calls)


class _Ok:
    def __init__(self):
        self.pressed = []
        self.filled = []
        self.typed = []

    async def press(self, keys, timeout=None):
        self.pressed.append(keys)

    async def fill(self, text):
        self.filled.append(text)

    async def press_sequentially(self, text, timeout=None):
        self.typed.append(text)


class PressFallbackTests(unittest.TestCase):
    def test_repress_on_fresh_handle(self):
        fresh = _Ok()
        with mock.patch.object(pg.b, "input_box", mock.AsyncMock(return_value=fresh)):
            ok = asyncio.run(pg._press_box(_Boom(), "Enter"))
        self.assertTrue(ok)
        self.assertEqual(fresh.pressed, ["Enter"], "应该在新句柄上重按")

    def test_falls_back_to_page_keyboard(self):
        page = _Page()
        with mock.patch.object(pg.b, "input_box", mock.AsyncMock(side_effect=_Boom())), \
             mock.patch.object(pg.b, "page", page, create=True):
            ok = asyncio.run(pg._press_box(_Boom(), "Enter"))
        self.assertTrue(ok)
        self.assertEqual(page.calls, [("press", "Enter")], "最后应退回 page.keyboard.press")

    def test_all_fail_returns_false(self):
        class _KeyboardBoom:
            async def press(self, keys):
                raise RuntimeError("no page")

        class _PageBoom:
            keyboard = _KeyboardBoom()

        with mock.patch.object(pg.b, "input_box", mock.AsyncMock(side_effect=_Boom())), \
             mock.patch.object(pg.b, "page", _PageBoom(), create=True):
            ok = asyncio.run(pg._press_box(_Boom(), "Enter"))
        self.assertFalse(ok)

    def test_type_prompt_falls_back_to_keyboard(self):
        page = _Page()
        with mock.patch.object(pg.b, "input_box", mock.AsyncMock(side_effect=_Boom())), \
             mock.patch.object(pg.b, "page", page, create=True):
            ok = asyncio.run(pg._type_prompt(_Boom(), "hello"))
        self.assertTrue(ok)
        # 先发 Control+A 清空旧内容，最后才是 keyboard.type
        self.assertEqual(page.calls, [("press", "Control+A"), ("type", "hello")],
                         "fill/逐字都失败时应退回 keyboard")


class SendUsesHelpersTests(unittest.TestCase):
    def test_send_does_not_press_stale_handle(self):
        src = (ROOT / "gantry" / "page.py").read_text(encoding="utf-8")
        i = src.index("async def send(prompt, make_new, model_id):")
        body = src[i:i + 4000]
        self.assertIn('await _press_box(box, "Enter")', body, "Enter 必须走 _press_box")
        self.assertIn("await _type_prompt(box, prompt)", body, "填充必须走 _type_prompt")
        self.assertNotIn('await box.press("Enter")', body, "不能再用可能失效的句柄直接 press")


if __name__ == "__main__":
    unittest.main()
