# -*- coding: utf-8 -*-
"""schedule 插件：工作区参数必须真正传到 run_task（不能只存不用）。"""
import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
PLUGIN = BASE / "plugins" / "schedule" / "plugin.py"


def load_plugin():
    spec = importlib.util.spec_from_file_location("cbp_plugin_schedule_unit", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeCtx:
    """只实现 schedule 插件用到的接口（state/save_state/run_task/log/register_*）。"""

    def __init__(self):
        self.tools = {}
        self.apis = {}
        self.calls = []
        self._state = {}

    def log(self, msg):        # noqa: D401 - 测试替身
        pass

    def register_tool(self, name, fn, desc="", params=None):
        self.tools[name] = fn

    def register_api(self, method, path, handler):
        self.apis[(method, path)] = handler

    @property
    def state(self):
        return self._state

    def save_state(self, **kw):
        self._state.update(kw)

    async def run_task(self, task, model="", workspace="", prefix="plugin"):
        self.calls.append({"task": task, "model": model, "workspace": workspace})
        return {"session_id": "cron-test", "content": "", "steps": 0, "error": None}


class ScheduleWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_plugin()
        self.ctx = FakeCtx()
        self.mod.setup(self.ctx)

    def tearDown(self):
        task = getattr(getattr(self.ctx, "_scheduler", None), "_task", None)
        if task is not None:
            task.cancel()

    def test_workspace_stored_and_used(self):
        async def scenario():
            await self.ctx.tools["schedule_add"](
                name="a", spec={"every": "1d"}, task="t",
                model="m", enabled=True, workspace="/tmp/target-ws")
            listed = (await self.ctx.tools["schedule_list"]())["schedules"]
            self.assertEqual(listed[0]["workspace"], "/tmp/target-ws")
            await self.ctx.tools["schedule_run"](name="a")

        asyncio.run(scenario())
        self.assertEqual(len(self.ctx.calls), 1)
        self.assertEqual(self.ctx.calls[0]["workspace"], "/tmp/target-ws",
                         "工作区必须传给 run_task，而不是只存进列表")

    def test_empty_workspace_stays_empty(self):
        async def scenario():
            await self.ctx.tools["schedule_add"](name="b", spec={"every": "1d"}, task="t")
            await self.ctx.tools["schedule_run"](name="b")

        asyncio.run(scenario())
        self.assertEqual(self.ctx.calls[0]["workspace"], "")


if __name__ == "__main__":
    unittest.main()
