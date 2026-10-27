# -*- coding: utf-8 -*-
"""`.env` 支持：密钥放 .env（不入库），config.json 里用 ${VAR} 引用。

- `load_env_file()`：解析 KEY=VALUE（支持 # 注释、export 前缀、引号），
  写进 os.environ——**已存在的环境变量优先**（不覆盖）；
- `resolve_placeholders(cfg)`：config.json 里 `"${VAR}"` 的值换成环境变量；
  引用了但没设的变量原样保留 + 记 warning（不静默置空，避免把 token 抹成空串）。
- 敏感键（含 key/token/secret/password）若在 config.json 里是**明文**（非 ${…}），
  启动时记一条 warning，提示搬到 .env。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger("gantry.envfile")

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SENSITIVE = re.compile(r"(key|token|secret|password)", re.I)


def load_env_file(path: str | Path = ".env", root: Path | None = None) -> int:
    """把 .env 读进 os.environ（不覆盖已存在的）。返回加载的条数。"""
    p = Path(path)
    if not p.is_absolute() and root is not None:
        p = root / p
    if not p.is_file():
        return 0
    n = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        # 去掉成对的引号
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        # 行内注释：KEY=value # comment（不在引号里才算）
        if "#" in val and not (val.startswith(("'", '"'))):
            val = val.split("#", 1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = val
            n += 1
    return n


def resolve_placeholders(node):
    """递归把字符串值里的 ${VAR} 换成环境变量；未设置的保留原样并记 warning。"""
    if isinstance(node, dict):
        return {k: resolve_placeholders(v) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve_placeholders(v) for v in node]
    if isinstance(node, str) and "${" in node:
        def _sub(m: re.Match) -> str:
            name = m.group(1)
            if name in os.environ:
                return os.environ[name]
            log.warning("config.json 引用了未设置的环境变量 ${%s}（原样保留）", name)
            return m.group(0)
        return _PLACEHOLDER.sub(_sub, node)
    return node


def warn_plaintext_secrets(node, prefix: str = "") -> list[str]:
    """明文敏感字段清单（建议搬到 .env 后用 ${VAR} 引用）。返回命中的键路径。"""
    hits = []
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else k
            hits += warn_plaintext_secrets(v, path)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits += warn_plaintext_secrets(v, f"{prefix}[{i}]")
    elif isinstance(node, str) and node and "${" not in node and _SENSITIVE.search(prefix):
        hits.append(prefix)
    return hits
