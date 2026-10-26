# -*- coding: utf-8 -*-
"""贴图（截图附件）链路的三处修复。

用户："最近的版本我贴图后都识别失败，你看看我放 for debug 的 step timeline 内容"。

## 查到的证据
- 时间线里**没有任何**图片/上传相关步骤：附件投递只写在服务端日志里，
  界面上看不到 → "识别失败"最难查的地方就在这；
- `messages` 里附件确实记录了（`8952ad8a3fdb_0.png` 等），前端粘贴也正常
  （实测：派发 paste 后缩略图 1 张、`IMG_STORES.follow` 有 1 项）；
  所以断点不在"贴进浏览器"，而在"投递给模型"。

## 三个真缺陷
1. **回合缓存键不含附件**：`sha1(send_prompt)` 只看文字。同一句"看看这张图"配
   新截图时会命中上一次的缓存答案，模型根本没看到新图 → 表现为"识别失败"。
2. **API 提供商分支静默丢弃附件**：provider 走 HTTP，没有可附加图片的输入框，
   代码直接忽略 `attach_paths`,模型只看到"看看这张图"这句话。
3. **上传只等 2 秒**：大截图上传+解析没完成就把消息发出去，Copilot 收到的是
   没有附件的纯文本。

## 修法
- `turn_cache_key(prompt, attach_paths)`:键 = 提示词哈希 + 附件内容哈希；
- `warn_provider_cannot_attach(sid, model, attach_paths)`:记一条
  `attach_image / error` 步骤 + `runtime_update` 提示"切到 Copilot 模型"；
- 浏览器分支：把每张图的附加结果写成 `attach_image` 步骤（ok/warning/error）,
  失败不再只躺在日志里；
- `attach_file_to_copilot`:2 秒后再轮询最多 10 秒等附件缩略图出现。
"""
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
# 页面自动化层已拆到 gantry/page.py（bridge.py 第三批解耦）
BRIDGE = ((BASE / "gantry" / "bridge.py").read_text(encoding="utf-8")
          + (BASE / "gantry" / "page.py").read_text(encoding="utf-8")
          + (BASE / "gantry" / "agentrun.py").read_text(encoding="utf-8"))


class CacheKeyTests(unittest.TestCase):
    def setUp(self):
        from gantry import bridge
        self.bridge = bridge
        self.tmp = Path(tempfile.mkdtemp())

    def _img(self, name, data: bytes) -> Path:
        p = self.tmp / name
        p.write_bytes(data)
        return p

    def test_same_prompt_same_attach_is_stable(self):
        a = self._img("a.png", b"AAA")
        k1 = self.bridge.turn_cache_key("看看这张图", [a])
        k2 = self.bridge.turn_cache_key("看看这张图", [a])
        self.assertEqual(k1, k2)

    def test_same_prompt_different_image_differs(self):
        """核心回归：同一句话 + 换一张图，键必须不同（否则命中旧答案）。"""
        a = self._img("a.png", b"AAA")
        b = self._img("b.png", b"BBB")
        ka = self.bridge.turn_cache_key("看看这张图", [a])
        kb = self.bridge.turn_cache_key("看看这张图", [b])
        self.assertNotEqual(ka, kb, "换了图片必须换缓存键，否则模型看不到新图")

    def test_attachment_key_differs_from_plain(self):
        a = self._img("a.png", b"AAA")
        self.assertNotEqual(self.bridge.turn_cache_key("hi"),
                            self.bridge.turn_cache_key("hi", [a]))

    def test_prompt_still_matters(self):
        a = self._img("a.png", b"AAA")
        self.assertNotEqual(self.bridge.turn_cache_key("x", [a]),
                            self.bridge.turn_cache_key("y", [a]))


class ProviderDropTests(unittest.TestCase):
    """provider 分支收到图片必须留下痕迹（步骤 + 提示），不能静默丢弃。"""

    def test_records_step_and_warns(self):
        from gantry import bridge
        tmp = Path(tempfile.mkdtemp())
        saved_sessions = bridge.AGENT_SESSIONS
        try:
            from gantry.session_store import SessionStore
            bridge.AGENT_SESSIONS = SessionStore(tmp / "agent.db")
            bridge.AGENT_SESSIONS.create("s1", "贴图测试", str(tmp), "deepseek-v4")
            img = tmp / "shot.png"
            img.write_bytes(b"PNG")
            bridge.warn_provider_cannot_attach("s1", "deepseek-v4-flash", [img])
            steps = bridge.AGENT_SESSIONS.steps("s1") or []
            self.assertTrue(steps, "必须记录一条步骤，否则界面上完全看不到")
            step = steps[-1]
            self.assertEqual(step.get("tool"), "attach_image")
            self.assertEqual(step.get("status"), "error")
            blob = str(step.get("result") or "")
            self.assertIn("shot.png", blob)
            self.assertIn("Copilot", blob, "要给出可操作建议：切到 Copilot 模型")
        finally:
            bridge.AGENT_SESSIONS = saved_sessions

    def test_empty_attach_list_is_noop(self):
        from gantry import bridge
        tmp = Path(tempfile.mkdtemp())
        saved = bridge.AGENT_SESSIONS
        try:
            from gantry.session_store import SessionStore
            bridge.AGENT_SESSIONS = SessionStore(tmp / "agent.db")
            bridge.AGENT_SESSIONS.create("s1", "x", str(tmp), "deepseek-v4")
            bridge.warn_provider_cannot_attach("s1", "deepseek-v4-flash", [])
            self.assertEqual(bridge.AGENT_SESSIONS.steps("s1") or [], [],
                             "没有附件时不应产生步骤")
        finally:
            bridge.AGENT_SESSIONS = saved


class UploadWaitTests(unittest.TestCase):
    def test_waits_longer_than_two_seconds(self):
        i = BRIDGE.index("await file_input.first.set_input_files")
        seg = BRIDGE[i:i + 1200]
        self.assertIn("_chip_sels", seg, "要轮询附件缩略图，而不能只固定等 2 秒")
        self.assertIn("10.0", seg, "最长等待应显著大于 2 秒（大截图上传慢）")


class AttachStepVisibilityTests(unittest.TestCase):
    def test_browser_branch_records_attach_step(self):
        self.assertIn('"tool": "attach_image"', BRIDGE,
                      "浏览器分支也要把附加结果写进时间线")
        i = BRIDGE.index("_ok_att")
        seg = BRIDGE[i:i + 1600]
        self.assertIn("add_step", seg)
        self.assertIn("attached", seg)
        self.assertIn("failed", seg)


if __name__ == "__main__":
    unittest.main()
