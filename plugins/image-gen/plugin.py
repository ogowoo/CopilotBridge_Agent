"""image-gen 插件：文生图（Grok / ChatGPT 那类"画一张"能力）。

后端（config.backend，默认 auto）：
- `auto`     按 DASHSCOPE_API_KEY → OPENAI_API_KEY 选择
- `dashscope` 通义万相（异步任务：创建 → 轮询 → 下载）
- `openai`    OpenAI Images（gpt-image-1 / dall-e-3，b64 返回）

生成结果落盘到工作区 `generated/`，返回相对路径，可直接在 WebUI 里预览/下载。

工具：`image_generate(prompt, size="1024*1024", n=1)`
接口：`POST /v1/plugins/image-gen/api/generate`（JSON: {prompt, size, n}）
"""

import asyncio
import base64
import time
import uuid

import httpx

_DASHSCOPE_CREATE = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
_DASHSCOPE_TASK = "https://dashscope.aliyuncs.com/api/v1/tasks/{}"
_OPENAI_IMAGES = "https://api.openai.com/v1/images/generations"


def _env(name):
    import os
    v = os.environ.get(name)
    if v:
        return v.strip()
    try:
        import winreg
        for hive, sub in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                          (winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
            try:
                with winreg.OpenKey(hive, sub) as key:
                    v, _ = winreg.QueryValueEx(key, name)
                    if v:
                        return str(v).strip()
            except OSError:
                continue
    except ImportError:
        pass
    return ""


def _pick_backend(want):
    want = str(want or "auto").lower()
    if want in ("dashscope", "openai"):
        return want
    if _env("DASHSCOPE_API_KEY"):
        return "dashscope"
    if _env("OPENAI_API_KEY"):
        return "openai"
    return "dashscope"     # 都没有时给出明确错误


async def _dashscope(prompt, size, n, model):
    key = _env("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
               "X-DashScope-Async": "enable"}
    body = {"model": model or "wanx2.1-t2i-turbo",
            "input": {"prompt": prompt},
            "parameters": {"size": size.replace("x", "*"), "n": int(n)}}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(_DASHSCOPE_CREATE, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
        task_id = ((data.get("output") or {}).get("task_id"))
        if not task_id:
            raise RuntimeError(f"创建任务失败: {str(data)[:200]}")
        deadline = time.time() + 120
        while time.time() < deadline:
            await asyncio.sleep(3)
            t = await c.get(_DASHSCOPE_TASK.format(task_id), headers=headers)
            t.raise_for_status()
            out = (t.json().get("output") or {})
            st = out.get("task_status")
            if st == "SUCCEEDED":
                return [x.get("url") for x in (out.get("results") or []) if x.get("url")]
            if st in ("FAILED", "CANCELED"):
                raise RuntimeError(f"任务失败: {str(out)[:300]}")
        raise RuntimeError("生图超时（>120s）")


async def _openai(prompt, size, n, model):
    key = _env("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY 未设置")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {"model": model or "gpt-image-1", "prompt": prompt,
            "size": size.replace("*", "x"), "n": int(n)}
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(_OPENAI_IMAGES, headers=headers, json=body)
        r.raise_for_status()
        items = (r.json().get("data") or [])
    urls = []
    for it in items:
        if it.get("url"):
            urls.append(it["url"])
        elif it.get("b64_json"):
            urls.append("b64:" + it["b64_json"])
    return urls


async def generate(ctx, prompt, size="1024*1024", n=1, backend=""):
    prompt = str(prompt or "").strip()
    if not prompt:
        raise RuntimeError("prompt 不能为空")
    be = _pick_backend(backend or ctx.config.get("backend"))
    model = str(ctx.config.get("model") or "")
    urls = await (_dashscope if be == "dashscope" else _openai)(prompt, size, n, model)
    if not urls:
        raise RuntimeError("后端没有返回图片")
    files = []
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
        for i, u in enumerate(urls[:4]):
            name = f"generated/{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}-{i}.png"
            try:
                if u.startswith("b64:"):
                    data = base64.b64decode(u[4:])
                else:
                    r = await c.get(u)
                    r.raise_for_status()
                    data = r.content
                files.append(ctx.save_file(name, data))
            except Exception as exc:
                files.append(f"保存失败: {exc}")
    return {"backend": be, "prompt": prompt, "size": size,
            "files": files, "urls": [u for u in urls if not u.startswith("b64:")]}


def setup(ctx):
    async def _tool(prompt: str, size: str = "", n: int = 1):
        try:
            return await generate(ctx, prompt, size or ctx.config.get("size", "1024*1024"), n)
        except Exception as exc:
            return {"error": str(exc)[:400], "backend": _pick_backend(ctx.config.get("backend"))}

    ctx.register_tool(
        "image_generate", _tool,
        "文生图：按提示词生成图片并保存到工作区 generated/（返回相对路径）",
        {"prompt": "画面描述", "size": "尺寸，默认 1024*1024", "n": "张数，默认 1"})

    async def _api(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            return await generate(ctx, body.get("prompt") or "", body.get("size") or
                                  ctx.config.get("size", "1024*1024"), body.get("n") or 1)
        except Exception as exc:
            return {"error": str(exc)[:400], "backend": _pick_backend(ctx.config.get("backend"))}

    ctx.register_api("POST", "api/generate", _api)
    ctx.log("image-gen 已就绪")
