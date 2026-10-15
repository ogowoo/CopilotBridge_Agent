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
import urllib.request
from typing import Any, AsyncIterator

import httpx


def system_proxy() -> str | None:
    """读取本机系统代理（Windows 注册表 / 环境变量）。

    为什么需要它：浏览器走的是 Windows 系统代理（WinINET），而 Python 的 httpx
    默认只读 HTTP_PROXY/HTTPS_PROXY 环境变量、**不看系统代理**。于是出现
    "浏览器能打开 API 站点、本服务却报提供商不可用"这种最容易被误判成"对方挂了"
    的情况。urllib 在 Windows 上会读注册表，正好补上这个差异。
    """
    try:
        proxies = urllib.request.getproxies() or {}
    except Exception:
        return None
    for key in ("https", "http"):
        value = str(proxies.get(key) or "").strip()
        if value:
            return value
    return None

# 结构化输出能力：openai/grok 支持真·json_schema；deepseek/kimi 支持 json_object；
# 未列出的走 auto（先试 json_object，服务不支持时自动去掉并重试一次）。
JSON_MODE_BY_PROVIDER = {
    "openai": "json_schema",
    "deepseek": "json_object",
    "kimi": "json_object",
    "moonshot": "json_object",
    "grok": "json_schema",
    "xai": "json_schema",
}

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
        # 结构化输出模式：auto（默认，先试 json_object）/ json_schema /
        # json_object / off。不支持的服务会在 4xx 时自动降级重试一次。
        self.json_mode = str(cfg.get("json_mode", "auto")).lower()
        # 代理：显式 proxy 优先；否则 use_system_proxy（默认开）时用系统代理。
        # 关掉可强制直连（代理本身故障时用）。
        self.proxy = str(cfg.get("proxy") or "").strip() or None
        self.use_system_proxy = bool(cfg.get("use_system_proxy", True))

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

    def proxy_url(self) -> str | None:
        if self.proxy:
            return self.proxy
        if self.use_system_proxy:
            return system_proxy()
        return None

    def _client(self, use_proxy: bool = True):
        kwargs: dict[str, Any] = {}
        if use_proxy:
            proxy = self.proxy_url()
            if proxy:
                kwargs["proxy"] = proxy
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=httpx.Timeout(self.timeout, connect=30),
            **kwargs,
        )

    def net_error_text(self, exc: Exception | None) -> str:
        """传输层异常的可读描述。

        httpx 的 ReadTimeout/ConnectError 等 str() **常为空串**，只报类名等于什么都没
        说（用户实测：步骤里"服务暂时不可用（第 1 次）："后面是空的，看不出原因）。
        这里补上异常类名、目标地址、是否经代理，并给出可操作的排查提示。
        """
        name = type(exc).__name__ if exc is not None else "NetworkError"
        detail = str(exc).strip() if exc is not None else ""
        via = self.proxy_url()
        where = f"经代理 {via}" if via else "直连"
        return (f"{where} 访问 {self.base_url} 时网络异常（{name}）："
                f"{detail[:200] if detail else '对端无详细消息'}。"
                "若浏览器能打开该站点而这里失败，多半是代理差异——"
                "可在 providers 配置里设 proxy，或设 use_system_proxy=false 强制直连")

    @staticmethod
    def _check_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        body = resp.text[:500]
        if resp.status_code in (408, 409, 425, 429) or resp.status_code >= 500:
            raise ProviderUnavailable(f"HTTP {resp.status_code}: {body}")
        raise ProviderError(f"HTTP {resp.status_code}: {body}")

    def _json_hint(self, schema: dict[str, Any] | None) -> dict[str, Any]:
        """按能力生成 response_format；拿不准就退回 json_object（兼容最广）。"""
        if not schema or self.json_mode == "off":
            return {}
        if self.json_mode in ("json_schema", "schema"):
            return {"response_format": {"type": "json_schema", "json_schema": {
                "name": "agent_action", "strict": False, "schema": schema}}}
        return {"response_format": {"type": "json_object"}}

    async def complete(self, model: str, messages: list[dict[str, str]],
                       response_schema: dict[str, Any] | None = None) -> str:
        self.require_ready()
        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        payload.update(self._json_hint(response_schema))
        # 先按配置（可能含自动探测到的系统代理）请求；传输层失败且代理是**自动探测**
        # 来的，再直连重试一次——避免"系统代理存在但连不上"把可用服务判死。
        attempts = [True]
        if self.proxy_url() and not self.proxy:
            attempts.append(False)
        last_exc: Exception | None = None
        for use_proxy in attempts:
            try:
                async with self._client(use_proxy=use_proxy) as client:
                    resp = await client.post("/chat/completions", json=payload)
                    if resp.status_code >= 400 and "response_format" in payload:
                        # 服务不支持该参数：去掉后重试一次（能力探测，不报错给用户）
                        payload.pop("response_format", None)
                        resp = await client.post("/chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                continue
            self._check_status(resp)
            try:
                return resp.json()["choices"][0]["message"].get("content") or ""
            except Exception as exc:
                raise ProviderError(f"响应格式异常: {exc}") from exc
        raise ProviderUnavailable(self.net_error_text(last_exc)) from last_exc

    async def stream(self, model: str, messages: list[dict[str, str]],
                     response_schema: dict[str, Any] | None = None) -> AsyncIterator[str]:
        """流式产出 content 增量。

        response_schema：与 complete() 同样支持结构化输出（Agent 的 JSON 动作协议
        靠它稳住解析率）。若服务不支持 response_format+stream（4xx），抛
        ProviderError —— 调用方可退回非流式 complete()。
        """
        self.require_ready()
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        body.update(self._json_hint(response_schema))
        try:
            async with self._client() as client:
                async with client.stream("POST", "/chat/completions", json=body) as resp:
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
            raise ProviderUnavailable(self.net_error_text(exc)) from exc


def build_providers(cfg: dict[str, Any]) -> dict[str, ApiProvider]:
    """内置预设与 config.json 的 providers 段合并（后者覆盖/新增）。"""
    merged: dict[str, dict[str, Any]] = {name: dict(preset) for name, preset in PRESETS.items()}
    for name, custom in (cfg.get("providers") or {}).items():
        if isinstance(custom, dict):
            merged.setdefault(name, {}).update(custom)
    out: dict[str, ApiProvider] = {}
    for name, c in merged.items():
        cfg2 = dict(c)
        # 未显式配置时按已知能力给默认值（不支持的会在 4xx 时自动降级重试）
        cfg2.setdefault("json_mode", JSON_MODE_BY_PROVIDER.get(name.lower(), "auto"))
        out[name] = ApiProvider(name, cfg2)
    return out
