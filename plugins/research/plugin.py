"""research 插件：深度检索（把"搜 → 抓 → 归结"的机械部分做扎实）。

Grok / Perplexity 的"深研"本质是：多角度检索 → 抓正文 → 去重 → 让模型综合并带来源。
本插件只做**可复现的机械部分**（多角度搜索 + 并行抓取正文 + 证据包），综合交给
Agent 的模型——这样既省 token 又不牺牲质量。

工具：`deep_research(query, angles=3, per_angle=4, fetch_top=3)`
→ {queries, sources:[{title,url,snippet,excerpt,chars}], fetched, errors}

接口：`GET /v1/plugins/research/api/query?q=...`
"""

import asyncio
import html as _html
import re
import urllib.parse

import httpx

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# 多角度后缀：先广后尖，覆盖"官方 / 最新 / 对比 / 实践"四类意图
_ANGLE_SUFFIXES = ["官方 文档", "最新 2026", "对比 评测", "实践 教程", "问题 解决"]

_BLOCK_TAGS = re.compile(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>")


def _clean(text):
    t = _BLOCK_TAGS.sub(" ", text or "")
    t = re.sub(r"(?i)<br\s*/?>", "\n", t)
    t = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article)>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = _html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def _decode_ddg(href):
    if "duckduckgo.com/l/" in href:
        try:
            real = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
            if real:
                return real[0]
        except Exception:
            pass
    return "https:" + href if href.startswith("//") else href


async def _search_one(client, query, n):
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        r = await client.get(url)
        r.raise_for_status()
        body = r.text
    except Exception as exc:
        return [], f"搜索失败: {exc}"
    out = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                         body, re.S):
        href, title = m.group(1), _clean(m.group(2))
        if href and title:
            out.append({"title": title[:200], "url": _decode_ddg(href), "snippet": ""})
        if len(out) >= n:
            break
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
    for i, s in enumerate(snippets[:len(out)]):
        out[i]["snippet"] = _clean(s)[:300]
    return out, ""


async def _fetch_excerpt(client, url, cap):
    try:
        r = await client.get(url, follow_redirects=True)
        if r.status_code >= 400:
            return "", f"HTTP {r.status_code}"
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return "", f"非文本: {ctype}"
        return _clean(r.text)[:cap], ""
    except Exception as exc:
        return "", str(exc)[:120]


async def deep_research(query, angles=3, per_angle=4, fetch_top=3, max_excerpt=1800):
    query = str(query or "").strip()
    if not query:
        raise RuntimeError("query 不能为空")
    angles = max(1, min(int(angles or 3), 5))
    per_angle = max(1, min(int(per_angle or 4), 8))
    fetch_top = max(0, min(int(fetch_top or 3), 8))
    cap = max(400, min(int(max_excerpt or 1800), 4000))

    queries = [query] + [f"{query} {s}" for s in _ANGLE_SUFFIXES[:angles - 1]]
    errors = []
    sources = []
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as client:
        # 1) 多角度并行搜索
        results = await asyncio.gather(*[_search_one(client, q, per_angle) for q in queries])
        seen = set()
        for q, (items, err) in zip(queries, results):
            if err:
                errors.append(f"{q}: {err}")
            for it in items:
                u = it.get("url") or ""
                if not u or u in seen:
                    continue
                seen.add(u)
                sources.append(it)
        # 2) 抓前 N 篇正文（并行）
        top = [s for s in sources if s.get("url")][:fetch_top]
        excerpts = await asyncio.gather(*[_fetch_excerpt(client, s["url"], cap) for s in top])
        for s, (text, err) in zip(top, excerpts):
            s["excerpt"] = text
            s["chars"] = len(text)
            if err:
                errors.append(f"{s['url']}: {err}")
    return {"query": query, "queries": queries, "sources": sources[:20],
            "fetched": sum(1 for s in sources if s.get("excerpt")),
            "errors": errors[:8]}


def setup(ctx):
    cfg = ctx.config or {}

    async def _tool(query: str, angles: int = 0, per_angle: int = 0, fetch_top: int = 0):
        return await deep_research(
            query,
            angles or cfg.get("angles", 3),
            per_angle or cfg.get("per_angle", 4),
            fetch_top or cfg.get("fetch_top", 3),
            cfg.get("max_excerpt", 1800))

    ctx.register_tool(
        "deep_research", _tool,
        "深度检索：多角度并行搜索并抓取正文，返回证据包（标题/链接/摘要/正文节选）供你综合",
        {"query": "研究问题", "angles": "搜索角度数，默认 3",
         "per_angle": "每角度结果数，默认 4", "fetch_top": "抓取正文的条数，默认 3"})

    async def _api(request):
        q = request.query_params.get("q") or ""
        return await deep_research(q, cfg.get("angles", 3), cfg.get("per_angle", 4),
                                   cfg.get("fetch_top", 3), cfg.get("max_excerpt", 1800))

    ctx.register_api("GET", "api/query", _api)
    ctx.log("research 已就绪")
