"""本地 span 记录：把回合/工具调用写成 OTel GenAI 语义约定字段的 JSONL。

为什么不用 opentelemetry-sdk：本项目刻意零外部依赖、单机运行；先用**同名属性**
写本地 JSONL，UI 与 scripts 直接可读；将来要接 Langfuse/Phoenix/OTLP 时字段已对齐，
只需再加一个 exporter，不必改埋点。

字段取自 OTel GenAI 语义约定（部分）：
- gen_ai.operation.name : chat | invoke_agent | execute_tool | ...
- gen_ai.provider.name  : copilot-web | deepseek | openai | ...
- gen_ai.request.model
- gen_ai.usage.input_tokens / output_tokens / cache_read.input_tokens
- gen_ai.tool.name / gen_ai.tool.call.id
再加本项目私有属性（统一 gantry. 前缀）：gantry.session_id / gantry.step / gantry.conversation_index
注意：**不要把 trace_id 当作会话 id**（OTel 明确禁止此误用）——会话 id 另放
gantry.session_id，trace/span id 只用于链路追踪。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any


def _hex(n: int) -> str:
    return uuid.uuid4().hex[:n]


class SpanRecorder:
    """极简 span 记录器：append-only JSONL + 行数上限（超限从头截断重写）。"""

    def __init__(self, path: Path, enabled: bool = True, max_lines: int = 20000):
        self.path = Path(path)
        self.enabled = bool(enabled)
        self.max_lines = max(100, int(max_lines))
        self._count = 0
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.is_file():
                    with self.path.open("r", encoding="utf-8", errors="replace") as f:
                        self._count = sum(1 for _ in f)
            except Exception:
                pass

    # 兼容 contextvars 风格的当前 span（并发安全：用 task 局部变量而非全局）
    def write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._count += 1
            if self._count > self.max_lines:
                self._rotate()
        except Exception:
            pass   # 埋点失败绝不影响主流程

    def _rotate(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            keep = lines[-self.max_lines // 2:]
            self.path.write_text("\n".join(keep) + "\n", encoding="utf-8", newline="\n")
            self._count = len(keep)
        except Exception:
            pass

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        try:
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return []
        out = []
        for line in lines[-max(1, min(limit, 2000)):]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out


class Span:
    """一次操作（模型回合 / 工具调用）的记录单元；用 async with 或 with 包裹。"""

    def __init__(self, recorder: SpanRecorder, name: str, operation: str,
                 trace_id: str | None = None, parent_id: str | None = None,
                 **attrs: Any):
        self.rec = recorder
        self.name = name
        self.attrs: dict[str, Any] = {"gen_ai.operation.name": operation}
        self.attrs.update({k: v for k, v in attrs.items() if v is not None})
        self.span_id = _hex(16)
        self.trace_id = trace_id or _hex(32)
        self.parent_id = parent_id
        self.start = time.time()
        self.status = "ok"
        self.error: str | None = None

    def set(self, **attrs: Any) -> "Span":
        self.attrs.update({k: v for k, v in attrs.items() if v is not None})
        return self

    def fail(self, exc: BaseException | str) -> "Span":
        self.status = "error"
        self.error = str(exc)[:500]
        return self

    def _emit(self) -> None:
        rec = {
            "name": self.name,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_id,
            "start_time": round(self.start, 3),
            "end_time": round(time.time(), 3),
            "duration_ms": round((time.time() - self.start) * 1000, 1),
            "status": self.status,
            "attributes": self.attrs,
        }
        if self.error:
            rec["error"] = self.error
        self.rec.write(rec)

    def __enter__(self) -> "Span":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.fail(exc)
        self._emit()
        return False

    async def __aenter__(self) -> "Span":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.fail(exc)
        self._emit()
        return False


# 进程级默认实例（bridge 启动时注入真实路径与配置）
RECORDER: SpanRecorder = SpanRecorder(Path(os.devnull), enabled=False)


def set_recorder(recorder: SpanRecorder) -> None:
    global RECORDER
    RECORDER = recorder


def span(name: str, operation: str, **attrs: Any) -> Span:
    return Span(RECORDER, name, operation, **attrs)
