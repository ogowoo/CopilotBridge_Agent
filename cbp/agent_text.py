from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class TextMixin:
    @staticmethod
    def estimate_tokens(text: str) -> int:
        text = text or ""
        chinese = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        ascii_text = ''.join(c if ord(c) < 128 else ' ' for c in text)
        words = len(re.findall(r"[A-Za-z0-9_]+", ascii_text))
        punctuation = len(re.findall(r"[^\w\s]", ascii_text))
        code_weight = len(re.findall(r"[{}\[\]():;=<>/\\]", text)) // 2
        base64_weight = sum(len(x) // 3 for x in re.findall(r"[A-Za-z0-9+/]{80,}={0,2}", text))
        return max(1, chinese + words + punctuation // 2 + code_weight + base64_weight)

    def _spill_result(self, tool: str, step_no: int, result: Any) -> dict[str, Any] | None:
        """结果过大时落盘（对应 DSH 的 spill）：完整内容写到工作区 `.spill/` 下，
        上下文里只放"保头保尾"的摘要 + 路径提示，需要全文时用 read_text 读。

        放在**工作区内**是刻意的：沙箱不允许读工作区外的绝对路径，落盘在外部等于
        模型永远读不到；放 `.spill/` 也让用户能自己看、并可用普通的文件工具清理。
        """
        try:
            threshold = int(self.config.get("spill_threshold_chars", 20000))
            if threshold <= 0:
                return None
            text = json.dumps(result, ensure_ascii=False)
            if len(text) < threshold:
                return None
            max_bytes = int(self.config.get("spill_max_bytes", 2_000_000))
            root = Path(getattr(self.tools, "root", "."))
            sid = str(getattr(self, "_session_id", "") or "session").replace("/", "_")[:40]
            out_dir = root / ".spill" / sid
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / f"{step_no}-{tool}.json"
            data = text.encode("utf-8")
            if len(data) > max_bytes:
                data = data[:max_bytes]
            out.write_bytes(data)
            return {"spill_path": out.relative_to(root).as_posix(),
                    "spill_chars": len(text),
                    "hint": f"完整结果已落盘（{len(text)} 字符）：用 read_text 读 "
                            f"{out.relative_to(root).as_posix()}（可配合 offset/limit 分段）"}
        except Exception:
            return None          # 落盘失败绝不能影响任务

    @staticmethod
    def retain_text(text: str, limit: int) -> str:
        """超长文本**保头保尾**（DSH 的 head/tail 保留思路）。

        原来只留头部（`value[:limit] + "[truncated]"`），把**尾部**丢掉了——而工具
        结果的尾部往往正是错误信息与结论（例如 unittest 的失败列表、traceback 末尾）。
        这里保留头 60% / 尾 40%，中间替换为统一省略标记，并给出省略字符数。
        Python 的字符串切片按码点，不会切坏 UTF-8。
        """
        if limit <= 0 or len(text) <= limit:
            return text
        head = max(1, int(limit * 0.6))
        tail = max(1, limit - head)
        removed = len(text) - head - tail
        if removed <= 0:
            return text[:limit]
        return (text[:head]
                + f"\n\n[... 中间 {removed} 字符已省略（保留头尾；错误与结论通常在尾部）...]\n\n"
                + text[-tail:])

    @staticmethod
    def compact_value(value: Any, limit: int = 3000) -> Any:
        if isinstance(value, str):
            return TextMixin.retain_text(value, limit)
        if isinstance(value, list):
            return [TextMixin.compact_value(x, limit) for x in value]
        if isinstance(value, dict):
            return {k: TextMixin.compact_value(v, limit) for k, v in value.items()
                    if k != "content_b64"}  # raw_tail 保留：模型需要看到自己被截断的输出
        return value


