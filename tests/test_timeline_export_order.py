# -*- coding: utf-8 -*-
"""导出时间线的数组顺序必须"自证"，不能让人猜。

用户实测困惑："219 那个就是新的，所以我不知道这个算正序还是倒序？反正最新的
时间线在文件的最前面。"——实测（时间戳逐条核对）三个数组**都是正序（旧→新）**，
最新一条在**末尾**：

    messages: 12:07:59 再同步一下… → … → 18:35:16 停止      （16 条，升序）
    steps   : step 1 → step 377                              （升序）
    files   : id 306 → id 353                                （升序）

历史上这份导出里 `steps`/`messages` 的方向还让人误判过（我本人也读错过一次），
所以现在把 `order` 与 `latest` 写进文件本体：拿到 JSON 不用再猜。

本文件守：
1. 导出 payload 带 `order`（说明三个数组都是升序、最新在末尾）；
2. 带 `latest`（最新一步的 step/status/tool 与最新消息时间/角色、会话状态）；
3. 带 `counts`;
4. 导出前对三个数组做显式排序（不依赖接口返回顺序）。
"""
import json
import re
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
APP = (BASE / "webui" / "app.js").read_text(encoding="utf-8")


class ExportOrderingTests(unittest.TestCase):
    def _export_body(self) -> str:
        i = APP.index("async function exportTimeline")
        j = APP.index("/* ── 页面内通知（toast）", i)
        return APP[i:j]

    def test_payload_declares_order(self):
        body = self._export_body()
        self.assertIn("order:", body, "导出必须自带顺序说明")
        self.assertIn("ascending", body)
        self.assertIn("末尾", body, "要明确写清最新的一条在哪一端")

    def test_payload_declares_latest_and_counts(self):
        body = self._export_body()
        self.assertIn("latest:", body, "要给出最新一步/最新消息，便于一眼看现状")
        self.assertIn("counts:", body)

    def test_arrays_are_sorted_explicitly(self):
        body = self._export_body()
        # 三个数组都要显式排序，不能直接信接口顺序
        self.assertIn("[...(d.steps||[])].sort(", body)
        self.assertIn("[...(d.messages||[])].sort(", body)
        self.assertIn("[...(d.files||[])].sort(", body)
        # 步骤按 step 号，消息/文件按 id（单调递增）
        self.assertIn("(a.step??0)-(b.step??0)", body)
        self.assertIn("(a.id??0)-(b.id??0)", body)

    def test_latest_uses_last_element(self):
        body = self._export_body()
        self.assertIn("_steps[_steps.length-1]", body)
        self.assertIn("_msgs[_msgs.length-1]", body)


class RealExportShapeTests(unittest.TestCase):
    """用真实接口数据验证排序结果（不依赖前端在浏览器里跑）。"""

    def test_sorted_payload_matches_timestamps(self):
        p = BASE / "for debug" / "cbp-task-mveq9t5g-timeline-2026-10-19-10-35-24.json"
        if not p.is_file():
            self.skipTest("没有可用的历史导出样本")
        d = json.loads(p.read_text(encoding="utf-8"))
        msgs = sorted(d.get("messages") or [], key=lambda m: m.get("id") or 0)
        created = [m.get("created") for m in msgs if m.get("created")]
        self.assertEqual(created, sorted(created), "messages 升序后时间必须单调递增")
        steps = sorted(d.get("steps") or [], key=lambda s: s.get("step") or 0)
        nums = [s.get("step") for s in steps]
        self.assertEqual(nums, sorted(nums), "steps 升序后 step 号必须单调递增")
        # 最新一步就是最后一条
        self.assertEqual(steps[-1].get("step"), max(nums))


if __name__ == "__main__":
    unittest.main()
