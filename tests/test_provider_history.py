"""提供商对话历史的配对/结构：system 首位、user/assistant 严格交替、裁剪后仍合法。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry import bridge as B


def roles(hist):
    return [m.get("role") for m in hist]


class ProviderHistoryTests(unittest.TestCase):
    def setUp(self):
        self.proto = B._PROVIDER_PROTOCOL

    def test_repair_drops_leading_assistant_but_keeps_consecutive_users(self):
        hist = [{"role": "system", "content": self.proto},
                {"role": "assistant", "content": "不该打头"},
                {"role": "user", "content": "u1"},
                {"role": "user", "content": "u2"},
                {"role": "assistant", "content": "a2"}]
        fixed = B._repair_provider_history(hist)
        self.assertEqual(fixed, 1, "只应丢弃打头的 assistant；连续 user 是合法的，不该被算作修正")
        # 打头的 assistant 被丢弃；连续 user 合法（服务端顺次理解），故保留两条
        self.assertEqual(roles(hist), ["system", "user", "user", "assistant"])
        self.assertEqual(hist[1]["content"], "u1", "锚点（首条 user）必须原样保留")
        self.assertEqual(hist[2]["content"], "u2", "最近一条指令也要保留")

    def test_repair_inserts_missing_system(self):
        hist = [{"role": "user", "content": "u"}]
        B._repair_provider_history(hist)
        self.assertEqual(roles(hist)[0], "system")
        self.assertEqual(hist[0]["content"], self.proto)

    def test_valid_history_untouched(self):
        hist = [{"role": "system", "content": self.proto},
                {"role": "user", "content": "u1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "u2"}]
        before = list(hist)
        self.assertEqual(B._repair_provider_history(hist), 0)
        self.assertEqual(hist, before, "合法序列不应被改动（否则每回合都在丢上下文）")

    def test_trim_keeps_pairing_and_protected_head(self):
        """重度裁剪后仍必须：system 在首位、首条任务消息保住、角色交替。"""
        proto, first = self.proto, "TASK-AND-TOOLS" + "x" * 500
        hist = [{"role": "system", "content": proto}, {"role": "user", "content": first}]
        for i in range(40):
            hist.append({"role": "user", "content": "u%d" % i + "y" * 200})
            hist.append({"role": "assistant", "content": "a%d" % i + "z" * 200})
        B._trim_provider_history(hist, max_chars=5000)
        B._repair_provider_history(hist)
        self.assertEqual(roles(hist)[0], "system")
        self.assertEqual(hist[1]["content"], first, "首条（含工具清单）必须保留")
        r = roles(hist)[1:]
        self.assertEqual(r[0], "user")
        for prev, cur in zip(r, r[1:]):
            self.assertFalse(prev == cur == "assistant", "不得出现连续 assistant")
        self.assertNotIn("assistant", r[:1], "不得以 assistant 打头")
        self.assertLess(sum(len(m["content"]) for m in hist), 5000 + 1200)

    def test_trim_survives_all_assistant_tail(self):
        """极端：只剩 system+首条+一条 assistant 时，裁剪要停下且不破坏结构。"""
        hist = [{"role": "system", "content": self.proto},
                {"role": "user", "content": "first"},
                {"role": "user", "content": "u" * 9000},
                {"role": "assistant", "content": "a" * 9000}]
        B._trim_provider_history(hist, max_chars=100)
        B._repair_provider_history(hist)
        self.assertEqual(roles(hist)[0], "system")
        B._trim_provider_history(hist, max_chars=100)   # 幂等：再裁一次不应崩
        self.assertGreaterEqual(len(hist), 2)


if __name__ == "__main__":
    unittest.main()
