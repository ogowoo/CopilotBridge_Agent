# -*- coding: utf-8 -*-
"""回归护栏：import 模块不得改动会话状态（今天最大的事故）。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cbp.session_store import SessionStore  # noqa: E402


class ImportSideEffectTests(unittest.TestCase):
    def test_bridge_import_does_not_call_recover(self):
        """`cbp/bridge.py` 模块级不能再调 recover_interrupted（会误杀在跑的任务）。"""
        src = (ROOT / "cbp" / "bridge.py").read_text(encoding="utf-8")
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped.startswith("AGENT_SESSIONS."):
                continue
            self.assertNotIn("recover_interrupted", stripped,
                             "模块级的 AGENT_SESSIONS.recover_interrupted() 会污染任何 import")

    def test_recover_runs_in_lifespan(self):
        src = ((ROOT / "cbp" / "api.py").read_text(encoding="utf-8") + "".join((ROOT / "cbp" / n).read_text(encoding="utf-8") for n in ("routes_core.py", "routes_config.py", "routes_agents.py", "routes_sessions.py")))
        i = src.find("async def lifespan(")
        j = src.find("\n@app", i) if i > 0 else -1
        body = src[i:j if j > 0 else i + 4000]
        self.assertIn("recover_interrupted", body, "服务启动时要恢复中断会话")

    def test_recover_semantics_unchanged(self):
        store = SessionStore(Path(tempfile.mkdtemp(prefix="cbp_rec_")) / "a.db")
        store.create("r1", "跑着的", str(Path(tempfile.gettempdir())), "copilot-auto")
        store.patch("r1", state="running")
        store.recover_interrupted()
        st = store.get("r1")
        self.assertEqual(st["state"], "paused")
        self.assertIn("服务中断", st["last_error"] or "")


if __name__ == "__main__":
    unittest.main()
