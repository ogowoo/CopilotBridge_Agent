# -*- coding: utf-8 -*-
"""护栏：新任务首轮必须显式声明"忽略此前对话话题"，且 new_chat 要验证对话为空。

真实事故（task-316f6502774b）：第一轮 prompt 明明是「在工作区 zz-fix-check/ 下建 ok.txt」，
模型却接着浏览器里另一条关于 pyshot 快捷键的 Copilot 对话去读 pyshot/main.py，
连快捷键/菜单的上下文都继承了——新任务被"窜"到上一条对话上。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class NewTaskIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "cbp" / "bridge.py").read_text(encoding="utf-8")

    def test_first_turn_declares_new_task(self):
        self.assertIn("【新任务】忽略此前任何对话的话题与进度", self.src)
        self.assertIn("send_prompt = (marker + \"\\n【新任务】", self.src)

    def test_new_chat_verifies_empty(self):
        """new_chat 必须检查页面真的空了，否则报错而不是继续发。"""
        i = self.src.find("async def new_chat():")
        j = self.src.find("\nasync def ", i + 20)
        body = self.src[i:j]
        self.assertIn("count_user_markers() == 0", body)
        self.assertIn("新对话未创建成功", body)

    def test_guard_raises_copilot_send_failed(self):
        i = self.src.find("async def new_chat():")
        j = self.src.find("\nasync def ", i + 20)
        self.assertIn("CopilotSendFailed(", self.src[i:j])


if __name__ == "__main__":
    unittest.main()
