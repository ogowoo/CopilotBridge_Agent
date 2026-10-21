# -*- coding: utf-8 -*-
"""从 webui/i18n.js 的 S2T_CHARS 生成 cbp/s2t_data.py（简→繁对照）。

后端生成提示词的繁体版本时要用同一张表，否则同一个词在界面与提示词里写法不一致。
"""
from __future__ import annotations

import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parent.parent
JS = BASE / "webui" / "i18n.js"
OUT = BASE / "cbp" / "s2t_data.py"


def main() -> int:
    src = JS.read_text(encoding="utf-8")
    i = src.index("const S2T_CHARS = (() => {")
    j = src.index("})();", i)
    pairs = re.findall(r'\["(.)","(.)"\]', src[i:j])
    if not pairs:
        print("没抽到对照表，检查格式是否变了")
        return 1
    lines = [
        "# -*- coding: utf-8 -*-",
        '"""简→繁字符对照（**由 webui/i18n.js 的 S2T_CHARS 生成**,勿手改）。',
        "",
        "为什么复用前端那张表：UI 的繁体就是用它转的，后端提示词若另写一份，两边用字会",
        "不一致。重新生成：python scripts/gen_s2t.py",
        '"""',
        "from __future__ import annotations",
        "",
        "S2T: dict[str, str] = {",
    ]
    lines += [f'    "{a}": "{b}",' for a, b in pairs]
    lines.append("}")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"写入 {OUT.name}: {len(pairs)} 对")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
