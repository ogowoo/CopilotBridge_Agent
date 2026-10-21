# -*- coding: utf-8 -*-
"""修：并发创建窗口时 index 会重复（都从 len+1 算）。改成单调递增计数器。"""
import subprocess
from pathlib import Path

p = Path("cbp/window_pool.py")
s = p.read_text(encoding="utf-8")

s = s.replace('''        self._windows: list[Window] = []''',
              '''        self._windows: list[Window] = []
        self._next_index = 1        # 单调递增（并发创建时 len+1 会撞号）''', 1)
old = '''    async def _create(self) -> Window:
        index = len(self._windows) + 1
        page = await self._factory(index)'''
assert old in s, "_create 锚点"
new = '''    async def _create(self) -> Window:
        index = self._next_index
        self._next_index += 1
        page = await self._factory(index)'''
s = s.replace(old, new, 1)
p.write_text(s, encoding="utf-8", newline="\n")
print("window_pool: 窗口序号改为单调递增")

r = subprocess.run(["python", "-m", "py_compile", "cbp/window_pool.py"], capture_output=True, text=True)
print("编译:", "OK" if r.returncode == 0 else (r.stdout + r.stderr)[-200:])
