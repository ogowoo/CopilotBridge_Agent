# -*- coding: utf-8 -*-
"""文件操作必须走 Python 工具，不许拿 PowerShell 改文件。

用户担心"文件操作用的是 PowerShell"。事实核实：文件工具本来就是 pathlib 实现
（11 个工具全部纯 Python，显式 utf-8、保留 CRLF）。PowerShell 只出现在两处：
可选的 `pwsh` 命令工具、以及"选择工作目录"的系统对话框。

但用户的直觉是对的，且**有实测代价**:PowerShell 管道按本机代码页重新编码，
用它改 UTF-8 源码会把中文写成 "é¦–è½®æ…" 这类乱码（本仓库开发过程中真实踩过，
只能整篇重写）。所以本文件钉住两件事：

1. `gantry/tools.py` 里不许出现 PowerShell 调用（文件工具必须保持纯 Python）；
2. 只有**工具面里真的有 pwsh** 时才往提示词里注入"禁止用 pwsh 改文件"的纪律，
   否则不许提这个工具名（提了模型会照着调，实测踩过 copilot_* 的同类坑）。
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = Path(__file__).resolve().parent.parent


class FileToolsArePurePythonTests(unittest.TestCase):
    """用 AST 查**真实调用**，而不是扫文本——注释里提到 PowerShell 不算违规
    （`replace_lines`/`read_text` 的文档就引用了 Get-Content 作为对照）。"""

    SPAWN_ATTRS = {"create_subprocess_exec", "create_subprocess_shell", "run", "Popen",
                   "call", "check_output", "system", "popen"}

    # 文件工具已拆到 gantry/tools_files.py（tools.py 第四批解耦），两处都要查
    SOURCES = ("tools.py", "tools_files.py")

    def _tree(self):
        import ast
        return [ast.parse((BASE / "gantry" / name).read_text(encoding="utf-8"))
                for name in self.SOURCES]

    def test_file_tools_never_spawn_a_process(self):
        import ast
        targets = {"read_text", "write_text", "append_text", "write_text_b64",
                   "append_text_b64", "replace_text", "replace_lines", "list_directory",
                   "find_files", "make_directory", "copy_path", "move_path", "delete_path",
                   "grep"}
        offenders = []
        for tree in self._tree():
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef) or node.name not in targets:
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr in self.SPAWN_ATTRS:
                        offenders.append(f"{node.name} -> .{sub.attr}")
                    if isinstance(sub, ast.Import):
                        for a in sub.names:
                            if a.name.split(".")[0] in ("subprocess",):
                                offenders.append(f"{node.name} -> import {a.name}")
        self.assertEqual(offenders, [], f"文件工具里出现了进程调用: {offenders}")

    def test_no_powershell_program_passed_to_exec(self):
        import ast
        bad = []
        for tree in self._tree():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fname = ""
                if isinstance(node.func, ast.Attribute):
                    fname = node.func.attr
                elif isinstance(node.func, ast.Name):
                    fname = node.func.id
                if fname not in self.SPAWN_ATTRS:
                    continue
                for arg in node.args[:2]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        low = arg.value.lower()
                        if "powershell" in low or low.endswith("pwsh") or "/pwsh" in low:
                            bad.append(arg.value[:60])
        self.assertEqual(bad, [], f"有子进程调用 PowerShell: {bad}")

    def test_file_tools_use_pathlib_with_explicit_utf8(self):
        src = "".join((BASE / "gantry" / name).read_text(encoding="utf-8")
                      for name in self.SOURCES)
        # 写/读必须显式指定编码，否则中文在 GBK 机器上会乱码
        self.assertIn('encoding="utf-8"', src)
        self.assertIn("newline=", src,
                      "读写要带 newline=，避免 CRLF 被静默转换（实测踩过）")


class FileOpsRuleInjectionTests(unittest.TestCase):
    def setUp(self):
        from gantry.tools import ToolRegistry
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_rule_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.saved_root = None
        self.ToolRegistry = ToolRegistry

    def _prompt(self, allow_shell: bool) -> str:
        from gantry.agent import AgentRunner
        from gantry.tools_extra import register_extra_tools
        reg = self.ToolRegistry(self.ws, {"allow_shell": allow_shell})
        register_extra_tools(reg, {"allow_shell": allow_shell,
                                   "tools_extra": {"shell": allow_shell}})
        state = {"state": "ready", "total_steps": 0, "pending": [], "recent_steps": []}
        runner = AgentRunner(reg, None, {"allow_shell": allow_shell, "max_total_steps": 5},
                             state_getter=lambda: state,
                             state_saver=lambda **kw: state.update(kw),
                             step_saver=lambda s: None,
                             file_event_saver=lambda e: None)
        return runner._system_prompt()

    def test_rule_present_when_shell_available(self):
        p = self._prompt(True)
        self.assertIn("文件操作纪律", p)
        self.assertIn("禁止用 pwsh", p)
        self.assertIn("乱码", p, "要写出原因，否则模型会当成洁癖而不是硬约束")
        self.assertIn("git", p, "要说明 pwsh 仍可用于进程类任务")

    def test_no_pwsh_mention_without_shell(self):
        p = self._prompt(False)
        self.assertNotIn("pwsh", p, "工具面里没有 pwsh 就不该提它（提了模型会照着调）")
        self.assertNotIn("文件操作纪律", p)


if __name__ == "__main__":
    unittest.main()
