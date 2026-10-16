# -*- coding: utf-8 -*-
"""消息附件：粘贴的图片落盘后要能在聊天区回看。

链路：前端粘贴 data URL -> POST /messages 带 attachments -> 落盘 data/uploads ->
消息行记录文件名（JSON 数组）-> GET /v1/uploads/{name} 供图 -> 聊天区缩略图。

约束：
- 消息只记**文件名**，不记绝对路径（服务目录搬家不影响）；
- 供图接口严格校验文件名（禁分隔符与 `..`），且解析后必须落在 data/uploads 内；
- 供图在 /v1/ 下，走统一的 api_token 鉴权中间件。
"""
import base64
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from cbp import bridge as b
from cbp.session_store import SessionStore
from cbp.snapshots import SnapshotStore

# 1x1 红色 PNG
PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c62600000020001e221bc330000000049454e44ae426082")).decode()


class MessageAttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_att_"))
        self.ws = self.tmp / "ws"; self.ws.mkdir()
        self.saved = (b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.BASE)
        b.AGENT_SESSIONS = SessionStore(self.tmp / "agent.db")
        b.SNAPSHOTS = SnapshotStore(self.tmp / "snaps")
        b.AGENT_TOOLS.set_root(self.ws, False)
        b.BASE = self.tmp          # 附件落盘目录也指到临时位置
        b.AGENT_SESSIONS.create("s1", "任务", str(self.ws), "copilot-auto")

    def tearDown(self):
        b.AGENT_SESSIONS, b.SNAPSHOTS, b.AGENT_TOOLS.root, b.BASE = self.saved

    def test_message_roundtrip_with_attachment_names(self):
        """入库记文件名、读出来是文件名列表。"""
        paths = b.save_image_attachments([f"data:image/png;base64,{PNG_1PX}"])
        self.assertEqual(len(paths), 1)
        b.AGENT_SESSIONS.add_message("s1", "user", "看这张图", 0,
                                     attachments=[p.name for p in paths])
        msgs = b.AGENT_SESSIONS.messages("s1")
        self.assertEqual(msgs[0]["attachments"], [paths[0].name])
        # 无附件的消息读出来是空列表而不是 None
        b.AGENT_SESSIONS.add_message("s1", "assistant", "看到了", 0)
        msgs = b.AGENT_SESSIONS.messages("s1")
        self.assertEqual(msgs[1]["attachments"], [])

    def test_migration_keeps_existing_rows(self):
        """老库（无 attachments 列）升级后旧消息仍可读，附件列为空。"""
        # 另建一个"老库"（绕过本类的表结构，手工造无该列的库）
        old_db = self.tmp / "old.db"
        import sqlite3
        con = sqlite3.connect(old_db)
        con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, workspace TEXT,"
                    " model TEXT, state TEXT, created REAL, updated REAL)")
        con.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,"
                    " created REAL, role TEXT, content TEXT, conversation_index INTEGER DEFAULT 0)")
        con.execute("INSERT INTO messages(session_id,created,role,content,conversation_index)"
                    " VALUES('s9',1.0,'user','旧消息',0)")
        con.commit(); con.close()
        store = SessionStore(old_db)   # 打开即迁移
        msgs = store.messages("s9")
        self.assertEqual(msgs[0]["content"], "旧消息")
        self.assertEqual(msgs[0]["attachments"], [])

    def test_upload_endpoint_serves_and_rejects_traversal(self):
        from cbp.api import app
        paths = b.save_image_attachments([f"data:image/png;base64,{PNG_1PX}"])
        self.assertTrue(paths)
        c = TestClient(app)
        r = c.get(f"/v1/uploads/{paths[0].name}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content[:8], b"\x89PNG\r\n\x1a\n")
        # 路径穿越与非法文件名一律拒绝
        self.assertEqual(c.get("/v1/uploads/../config.json").status_code, 404)   # 路由不命中
        self.assertEqual(c.get("/v1/uploads/..%2Fconfig.json").status_code, 404) # 解码后含分隔符，路由不命中
        self.assertEqual(c.get("/v1/uploads/..hidden.png").status_code, 400)     # 含 ..，进业务校验
        self.assertEqual(c.get("/v1/uploads/nope.png").status_code, 404)         # 不存在

    def test_save_caps_and_bad_data_skipped(self):
        """超限与坏数据静默跳过（原有语义），但合法的照常落盘。"""
        items = ["not-a-data-url", f"data:image/png;base64,{PNG_1PX}"]
        paths = b.save_image_attachments(items)
        self.assertEqual(len(paths), 1)


if __name__ == "__main__":
    unittest.main()
