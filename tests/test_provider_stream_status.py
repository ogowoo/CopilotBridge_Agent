# -*- coding: utf-8 -*-
"""提供商流式路径的状态检查：错误响应不能抛 httpx 的"未读流"错误。

真事（同步进来的回归）：`stream()` 在 `client.stream()` 里调用 `_check_status(resp)`，
而它会读 `resp.text`——流式响应未消费时 httpx 直接抛
"Attempted to access streaming response content, without having called `read()`"，
把整个提供商通道的 Agent 回合打成 500（实测 Kimi/DeepSeek 全挂）。

这里用 `httpx.MockTransport` 造一个真实的流式响应来钉住这个契约。
"""
import asyncio
import sys
import unittest
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp.providers import ApiProvider, ProviderError, ProviderUnavailable  # noqa: E402


def _provider_with(handler) -> ApiProvider:
    p = ApiProvider("t", {"api": "openai", "base_url": "https://example.invalid",
                          "api_key": "k", "models": ["m"], "timeout": 5})
    transport = httpx.MockTransport(handler)
    p._client = lambda use_proxy=True: httpx.AsyncClient(  # type: ignore[assignment]
        base_url="https://example.invalid", transport=transport, timeout=5.0)
    return p


class StreamStatusTests(unittest.TestCase):
    def test_rate_limit_is_provider_unavailable_not_runtime_error(self):
        def handler(request):
            return httpx.Response(429, json={"error": {"message": "quota"}})

        p = _provider_with(handler)

        async def run():
            async with p._client() as client:
                async with client.stream("POST", "/chat/completions",
                                         json={"model": "m"}) as resp:
                    await ApiProvider._check_status_stream(resp)

        with self.assertRaises(ProviderUnavailable) as ctx:
            asyncio.run(run())
        self.assertIn("429", str(ctx.exception))
        self.assertIn("quota", str(ctx.exception), "错误体要带出来，便于判断配额/鉴权")

    def test_client_error_is_provider_error(self):
        def handler(request):
            return httpx.Response(401, json={"error": "bad key"})

        p = _provider_with(handler)

        async def run():
            async with p._client() as client:
                async with client.stream("POST", "/chat/completions",
                                         json={"model": "m"}) as resp:
                    await ApiProvider._check_status_stream(resp)

        with self.assertRaises(ProviderError):
            asyncio.run(run())

    def test_success_passes_through(self):
        def handler(request):
            return httpx.Response(200, json={"ok": True})

        p = _provider_with(handler)

        async def run():
            async with p._client() as client:
                async with client.stream("POST", "/chat/completions",
                                         json={"model": "m"}) as resp:
                    await ApiProvider._check_status_stream(resp)
                    return True

        self.assertTrue(asyncio.run(run()))

    def test_non_streaming_check_still_static(self):
        """`_check_status` 必须是静态方法（否则 self 被当成 resp）。"""
        import inspect
        self.assertIsInstance(
            inspect.getattr_static(ApiProvider, "_check_status"), staticmethod)


if __name__ == "__main__":
    unittest.main()
