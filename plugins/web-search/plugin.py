"""web-search 插件：联网搜索（可插拔后端）。

**为什么做成插件**：联网搜索是"现代 bot"（Grok / Perplexity / ChatGPT）的核心能力，
但它的实现方式因环境而异（免 key 的 DDG、需要 key 的 Brave/Tavily/Serper）。做成插件
后可以独立升级、按需启用，不污染核心工具面——正好验证 CBP 插件系统的贡献点。

后端（config.backend，默认 auto）：
- `auto`    按 BRAVE_API_KEY → TAVILY_API_KEY → SERPER_API_KEY → ddg 的顺序选
- `ddg`     DuckDuckGo HTML（免 key；返回 10 条左右）
- `brave`   Brave Search API（BRAVE_API_KEY）
- `tavily`  Tavily Search API（TAVILY_API_KEY）
- `serper`  Serper (Google) API（SERPER_API_KEY）

工具：`web_search(query, max_results=6)` → {backend, results:[{title,url,snippet}]}
接口：`GET /v1/plugins/web-search/api/search?q=...&n=6`
"""

import html as _html
import json
import os
import re
import urllib.parse

import httpx

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")


def _env(name):
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


def _decode_ddg(href):
    """DDG 的结果是 //duckduckgo.com/l/?uddg=<encoded> 跳转，取出真实 URL。"""
    if "duckduckgo.com/l/" in href:
        try:
            q = urllib.parse.urlparse(href).query
            real = urllib.parse.parse_qs(q).get("uddg")
            if real:
                return real[0]
        except Exception:
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def _clean(text):
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", text or ""))).strip()


async def _search_ddg(query, n):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
        body = r.text
    out = []
    # 结果块：<a class="result__a" href="...">标题</a> + <a class="result__snippet">摘要</a>
    for m in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        href, title = m.group(1), _clean(m.group(2))
        if not href or not title:
            continue
        out.append({"title": title, "url": _decode_ddg(href), "snippet": ""})
        if len(out) >= n:
            break
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
    for i, s in enumerate(snippets[:len(out)]):
        out[i]["snippet"] = _clean(s)[:300]
    return out


async def _search_brave(query, n):
    key = _env("BRAVE_API_KEY")
    if not key:
        raise RuntimeError("BRAVE_API_KEY 未设置")
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get("https://api.search.brave.com/res/v1/web/search",
                        params={"q": query, "count": n},
                        headers={"X-Subscription-Token": key, "Accept": "application/json"})
        r.raise_for_status()
        data = r.json()
    return [{"title": x.get("title") or "", "url": x.get("url") or "",
             "snippet": _clean(x.get("description") or "")[:300]}
            for x in ((data.get("web") or {}).get("results") or [])[:n]]


async def _search_tavily(query, n):
    key = _env("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY 未设置")
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post("https://api.tavily.com/search",
                         json={"api_key": key, "query": query, "max_results": n})
        r.raise_for_status()
        data = r.json()
    return [{"title": x.get("title") or "", "url": x.get("url") or "",
             "snippet": _clean(x.get("content") or "")[:300]}
            for x in (data.get("results") or [])[:n]]


async def _search_serper(query, n):
    key = _env("SERPER_API_KEY")
    if not key:
        raise RuntimeError("SERPER_API_KEY 未设置")
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post("https://google.serper.dev/search",
                         headers={"X-API-KEY": key}, json={"q": query, "num": n})
        r.raise_for_status()
        data = r.json()
    return [{"title": x.get("title") or "", "url": x.get("link") or "",
             "snippet": _clean(x.get("snippet") or "")[:300]}
            for x in (data.get("organic") or [])[:n]]


_BACKENDS = {"ddg": _search_ddg, "brave": _search_brave,
             "tavily": _search_tavily, "serper": _search_serper}


async def web_search(query: str, max_results: int = 6, backend: str = "") -> dict:
    """联网搜索：返回 {backend, results:[{title,url,snippet}]}。"""
    query = str(query or "").strip()
    if not query:
        raise RuntimeError("query 不能为空")
    n = max(1, min(int(max_results or 6), 20))
    want = str(backend or "").strip().lower()
    order = [want] if want and want != "auto" else []
    if not order:
        order = [k for k, env in (("brave", "BRAVE_API_KEY"), ("tavily", "TAVILY_API_KEY"),
                                  ("serper", "SERPER_API_KEY")) if _env(env)]
        order.append("ddg")
    errors = []
    for name in order:
        fn = _BACKENDS.get(name)
        if fn is None:
            errors.append(f"{name}: 未知后端")
            continue
        try:
            results = await fn(query, n)
            if results:
                return {"query": query, "backend": name, "count": len(results),
                        "results": results}
            errors.append(f"{name}: 没有结果")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"query": query, "backend": order[0] if order else "", "count": 0,
            "results": [], "errors": errors}


def setup(ctx):
    """插件入口：注册工具与 HTTP 接口。"""

    async def _tool(query: str, max_results: int = 6) -> dict:
        return await web_search(query, max_results, str(ctx.config.get("backend") or "auto"))

    ctx.register_tool(
        "web_search", _tool,
        "联网搜索：返回标题/链接/摘要（免 key 用 DuckDuckGo；配置 BRAVE/TAVILY/SERPER_API_KEY 自动优选）",
        {"query": "搜索词", "max_results": "返回条数，默认 6"})

    async def _api(request):
        q = request.query_params.get("q") or request.query_params.get("query") or ""
        n = request.query_params.get("n") or ctx.config.get("max_results") or 6
        return await web_search(q, int(n), str(ctx.config.get("backend") or "auto"))

    ctx.register_api("GET", "api/search", _api)
    ctx.log("web-search 已就绪（可用后端: ddg + 已配置的 API）")


def teardown(ctx):
    ctx.log("web-search 已卸载")
