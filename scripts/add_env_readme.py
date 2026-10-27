# -*- coding: utf-8 -*-
"""给三语 README 各加一节 .env 说明（README.md 是英文了，zh-CN/TW 各用对应语言）。"""
from pathlib import Path

SECTIONS = {
    "README.md": (
        "## Configuration", """## Secrets and environment variables (.env)

- **Never commit secrets in `config.json`** — it was tracked by git historically (now untracked).
- Put keys in `.env` (git-ignored) and reference them from `config.json` as `"${VAR}"`;
- Precedence: **system environment variables > .env > literals in config.json**
  (existing system variables are never overwritten by .env);
- Template: `.env.example`. If `config.json` still has a plaintext secret, startup logs a warning.
"""),
    "README.zh-CN.md": (
        "## 配置说明（config.json）", """## 密钥与环境变量（.env）

- **不要把密钥写进 `config.json` 再提交**——它历史上被 git 跟踪过（现已脱钩）。
- 新建 `.env`（已入 `.gitignore`）放密钥；`config.json` 里用 `"${VAR}"` 引用；
- 读取优先级：**系统环境变量 > .env > config.json 字面量**（系统已有的不会被 .env 覆盖）；
- 模板见 `.env.example`；启动时若 config.json 里仍有明文敏感字段，日志会提醒。
"""),
    "README.zh-TW.md": (
        "## 設定說明（config.json）", """## 金鑰與環境變數（.env）

- **不要把金鑰寫進 `config.json` 再提交**——它歷史上被 git 追蹤過（現已脫鉤）。
- 新建 `.env`（已入 `.gitignore`）放金鑰；`config.json` 裡用 `"${VAR}"` 引用；
- 讀取優先級：**系統環境變數 > .env > config.json 字面量**（系統已有的不會被 .env 覆蓋）；
- 範本見 `.env.example`；啟動時若 config.json 裡仍有明文敏感欄位，日誌會提醒。
"""),
}

for f, (anchor, body) in SECTIONS.items():
    p = Path(f)
    s = p.read_text(encoding="utf-8")
    if body.splitlines()[0] in s:
        print("已有:", f)
        continue
    if anchor in s:
        s = s.replace(anchor, body + "\n" + anchor, 1)
        p.write_text(s, encoding="utf-8", newline="\n")
        print("已加:", f)
    else:
        # 找不到锚点就贴到文件末尾前
        print(f"⚠ {f} 找不到锚点 {anchor!r}，先看章节名")
        import re
        for m in re.finditer(r"^## .+$", s, re.M):
            print("   ", m.group(0)[:60])
