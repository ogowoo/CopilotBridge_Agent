# -*- coding: utf-8 -*-
"""静态审计：包内不能有未定义名（NameError 隐患）。

为什么需要它：这类 bug 语法检查过、导入也过，只有真正跑到那一行才炸；
而很多调用点外面包着 try/except，炸了会被静默吞掉——表现为"功能莫名
不生效"（实测踩过两次：copilot_delete.py 里 CFG 未导入导致删除对账整段
静默失效；body_text 未导入导致"会话已删除"检测永远为假）。

审计方式：AST 遍历，按作用域链（模块 → 外层函数 → 内层函数）判断每个
Load 名字是否可见；闭包变量、推导式变量、except 名、import 别名都算可见。
"""
import ast
import builtins
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 解释器/模块自动注入的名字，不等于代码 bug
ALLOWED_EXTRA = {"__file__", "__name__", "__doc__", "__package__", "__builtins__", "self"}


def _scope_names(fn: ast.AST) -> set:
    names = set()
    for a in (list(getattr(fn.args, "args", [])) + list(getattr(fn.args, "kwonlyargs", []))
              + list(getattr(fn.args, "posonlyargs", []))):
        names.add(a.arg)
    if getattr(fn.args, "vararg", None):
        names.add(fn.args.vararg.arg)
    if getattr(fn.args, "kwarg", None):
        names.add(fn.args.kwarg.arg)
    for n in ast.walk(fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.comprehension):
            for t in ast.walk(n.target):
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
    return names


def _module_names(tree: ast.Module) -> set:
    names = set(ALLOWED_EXTRA)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _scan(fn, enclosing: set, module: set, filename: str, out: list):
    scope = set(enclosing) | _scope_names(fn)
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in scope and n.id not in module and not hasattr(builtins, n.id):
                out.append(f"{filename}:{n.lineno} {fn.name}() -> {n.id}")
    for child in ast.walk(fn):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not fn:
            _scan(child, scope, module, filename, out)


def audit(paths: list[Path]) -> list[str]:
    problems: list[str] = []
    for p in paths:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            problems.append(f"{p.name}: 语法错误 {exc}")
            continue
        module = _module_names(tree)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _scan(node, set(), module, p.name, problems)
    return problems


class UndefinedNameTests(unittest.TestCase):
    def test_package_has_no_undefined_names(self):
        targets = sorted((ROOT / "cbp").glob("*.py"))
        targets += sorted(ROOT.glob("*.py"))
        targets += sorted((ROOT / "scripts").glob("*.py"))
        problems = audit(targets)
        self.assertEqual(problems, [], "发现未定义名（运行时 NameError 隐患）:\n" + "\n".join(problems))

    def test_audit_catches_known_bug_shape(self):
        """自检审计本身有效：构造一个引用未定义名的函数必须被抓到。"""
        import tempfile
        src = "def f():\n    return CFG.get('x')\n"
        tmp = Path(tempfile.mkdtemp(prefix="cbp_audit_")) / "sample.py"
        tmp.write_text(src, encoding="utf-8")
        problems = audit([tmp])
        self.assertTrue(any("CFG" in p for p in problems), problems)

    def test_audit_allows_closures(self):
        """闭包变量（外层函数局部）不应误报。"""
        import tempfile
        src = ("def outer():\n"
               "    value = 1\n"
               "    def inner():\n"
               "        return value + 1\n"
               "    return inner\n")
        tmp = Path(tempfile.mkdtemp(prefix="cbp_audit_")) / "closure.py"
        tmp.write_text(src, encoding="utf-8")
        self.assertEqual(audit([tmp]), [])


if __name__ == "__main__":
    unittest.main()
