"""记忆层接线 + 语义测试。"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gantry.memory import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_mem_"))
        self.mem = MemoryStore(self.tmp / "memory.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_dedupe_and_stats(self):
        a = self.mem.remember("global", "用户偏好用中文回答", kind="preference")
        b = self.mem.remember("global", "  用户偏好用中文回答  ")   # 归一化后相同
        self.assertTrue(a["created"])
        self.assertTrue(b["updated"])
        self.assertEqual(a["id"], b["id"])
        st = self.mem.stats(["global"])
        self.assertEqual(st["total"], 1)
        self.assertEqual(st["by_kind"], {"preference": 1})

    def test_scope_isolation(self):
        self.mem.remember("global", "全局记忆一")
        self.mem.remember("ws:abc", "工作区专有记忆")
        self.assertEqual([m["text"] for m in self.mem.list(["global"])], ["全局记忆一"])
        self.assertEqual([m["text"] for m in self.mem.list(["global", "ws:abc"])].__len__(), 2)
        # 别的会话/工作区看不到
        self.assertEqual(self.mem.list(["ws:other"]), [])

    def test_search_scores_and_touches(self):
        self.mem.remember("global", "这个仓库跑单测大约 15 秒")
        self.mem.remember("global", "部署用 python start.py 启动服务")
        hits = self.mem.search("单测 要多久", ["global"], limit=5)
        self.assertTrue(hits, "应命中相关记忆")
        self.assertIn("15 秒", hits[0]["text"])
        again = self.mem.list(["global"], query="单测")
        self.assertEqual(again[0]["use_count"], 1, "被检索命中应累计使用次数")

    def test_pinned_always_returned(self):
        self.mem.remember("global", "永远记住：生产环境禁止 allow_delete",
                          kind="convention", pinned=True)
        self.mem.remember("global", "无关记忆：今天天气不错")
        hits = self.mem.search("完全无关的查询词", ["global"], limit=5)
        self.assertTrue(any(m["pinned"] for m in hits), "pinned 记忆应始终返回")

    def test_validation_and_forget(self):
        with self.assertRaises(ValueError):
            self.mem.remember("global", "   ")
        with self.assertRaises(ValueError):
            self.mem.remember("global", "x" * 2001)
        mid = self.mem.remember("global", "待删除的记忆")["id"]
        self.assertFalse(self.mem.forget(mid, scopes=["ws:other"]), "跨作用域不得删除")
        self.assertTrue(self.mem.forget(mid, scopes=["global"]))
        self.assertEqual(self.mem.stats(["global"])["total"], 0)

    def test_pin_toggle(self):
        mid = self.mem.remember('global', '重要硬约定')['id']
        self.assertTrue(self.mem.set_pinned(mid, True, scopes=['global']))
        self.assertTrue(self.mem.list(['global'])[0]['pinned'])
        self.assertTrue(self.mem.set_pinned(mid, False, scopes=['global']))
        self.assertFalse(self.mem.list(['global'])[0]['pinned'])
        self.assertFalse(self.mem.set_pinned(mid, True, scopes=['ws:other']), '跨作用域不得置顶')

    def test_cjk_bigram_selectivity(self):
        """中文按二元组匹配：查询里的"一下"不得命中"买一送一"这类无关记忆。"""
        self.mem.remember("global", "星巴克周三买一送一")
        self.mem.remember("global", "本机服务端口是 8765")
        hits = self.mem.search("帮我看一下 8765 端口配置", ["global"], limit=5)
        texts = [m["text"] for m in hits]
        self.assertTrue(any("8765" in t for t in texts))
        self.assertFalse(any("星巴克" in t for t in texts), "无关记忆被误召回")

    def test_trim_keeps_pinned_and_used(self):
        self.mem.MAX_PER_SCOPE = 5
        pinned = self.mem.remember("global", "固定记忆", pinned=True)["id"]
        for i in range(8):
            self.mem.remember("global", f"普通记忆 {i}")
        rows = self.mem.list(["global"], limit=50)
        self.assertLessEqual(len(rows), 5)
        self.assertIn(pinned, [r["id"] for r in rows], "淘汰应优先保留 pinned")


if __name__ == "__main__":
    unittest.main()
