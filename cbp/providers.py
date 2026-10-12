"""API 提供商接入层。

把 OpenAI 兼容的 HTTP API（OpenAI / DeepSeek / Kimi / Grok / xAI 等）接入桥接，
与 Copilot 网页自动化并列作为模型来源：

- 模型 ID 约定 "provider/model"，如 deepseek/deepseek-chat、kimi/kimi-k2-0905-preview；
- /v1/chat/completions 直接透传客户端 messages；
- Agent / 普通对话模式复用本地 Session 的消息记录作为上下文；
- API Key 走环境变量（推荐，不进 git）或 config.json 的 providers 段。

config.json 的 providers 段可覆盖预设或新增任意 OpenAI 兼容提供商：
    "providers": {
      "deepseek": {"models": ["deepseek-chat"]},
      "myapi": {"base_url": "https://example.com/v1", "api_key_env": "MYAPI_KEY",
                "models": ["my-model"], "description": "自建网关"}
    }
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "description": "OpenAI API",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "description": "DeepSeek API",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "api_key_env": "MOONSHOT_API_KEY",
        "models": ["kimi-k2-0905-preview", "moonshot-v1-128k"],
        "description": "Kimi（Moonshot）API",
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "models": ["grok-4", "grok-3"],
        "description": "Grok（xAI）API",
    },
}


class ProviderError(RuntimeError):
    """API 提供商调用失败（非限流类）。"""


class ProviderUnavailable(RuntimeError):
    """限流/5xx/超时等暂时不可用，上层按可重试处理。"""


def split_provider_model(model_id: str) -> tuple[str, str] | None:
    """'deepseek/deepseek-chat' -> ('deepseek', 'deepseek-chat')；非提供商格式返回 None。"""
    if "/" not in (model_id or ""):
        return None
    provider, _, model = model_id.partition("/")
    return (provider, model) if provider and model else None


def _read_env(name: str) -> str:
    """读环境变量；Windows 下进程环境过期时回退读注册表的 User/Machine 级值。

    场景：服务由某个早先启动的宿主进程拉起时，子进程继承的是旧环境块，
    用户刚在系统设置里添加的 KEY 不可见；注册表回退让这类情况也能读到。
    """
    value = os.environ.get(name)
    if value:
        return value
    if os.name == "nt":
        try:
            import winreg
            for hive, sub in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                              (winreg.HKEY_LOCAL_MACHINE,
                               r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
                try:
                    with winreg.OpenKey(hive, sub) as key:
                        value, _ = winreg.QueryValueEx(key, name)
                        if value:
                            # REG_EXPAND_SZ 可能含 %VAR%，strip 去首尾空白
                            return os.path.expandvars(str(value)).strip()
                except OSError:
                    continue
        except ImportError:
            pass
    return ""


class ApiProvider:
    def __init__(self, name: str, cfg: dict[str, Any]):
        self.name = name
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        env_name = str(cfg.get("api_key_env", ""))
        self.api_key = str(cfg.get("api_key") or (_read_env(env_name) if env_name else "") or "")
        self.api_key_env = env_name
        self.models = [str(m) for m in (cfg.get("models") or [])]
        self.description = str(cfg.get("description") or name)
        self.timeout = float(cfg.get("timeout_seconds", 300))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def require_ready(self) -> None:
        if not self.base_url:
            raise ProviderError(f"提供商 {self.name} 缺少 base_url")
        if not self.api_key:
            raise ProviderError(
                f"提供商 {self.name} 未配置 API Key：请设置环境变量 {self.api_key_env or '(未指定)'} "
                f"或在 config.json providers.{self.name}.api_key 中填写"
            )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(self.timeout, connect=30),
        )

    @staticmethod
    def _check_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        body = resp.text[:500]
        if resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500:
            raise ProviderUnavailable(f"HTTP {resp.status_code}: {body}")
        raise ProviderError(f"HTTP {resp.status_code}: {body}")

    async def complete(self, model: str, messages: list[dict[str, str]]) -> str:
        self.require_ready()
        try:
            async with self._client() as client:
                resp = await client.post("/chat/completions", json={
                    "model": model, "messages": messages, "stream": False,
                })
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailable(str(exc)) from exc
        self._check_status(resp)
        try:
            return resp.json()["choices"][0]["message"].get("content") or ""
        except Exception as exc:
            raise ProviderError(f"响应格式异常: {exc}") from exc

    async def stream(self, model: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        """流式产出 content 增量。"""
        self.require_ready()
        try:
            async with self._client() as client:
                async with client.stream("POST", "/chat/completions", json={
                    "model": model, "messages": messages, "stream": True,
                }) as resp:
                    self._check_status(resp)
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            return
                        try:
                            chunk = json.loads(payload)
                        except Exception:
                            continue
                        for choice in chunk.get("choices") or []:
                            delta = (choice.get("delta") or {}).get("content")
                            if delta:
                                yield delta
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderUnavailable(str(exc)) from exc


def build_providers(cfg: dict[str, Any]) -> dict[str, ApiProvider]:
    """内置预设与 config.json 的 providers 段合并（后者覆盖/新增）。"""
    merged: dict[str, dict[str, Any]] = {name: dict(preset) for name, preset in PRESETS.items()}
    for name, custom in (cfg.get("providers") or {}).items():
        if isinstance(custom, dict):
            merged.setdefault(name, {}).update(custom)
    return {name: ApiProvider(name, c) for name, c in merged.items()}
