"""schedule 插件：定时任务（"每天 9 点汇总未读邮件" 这类 bot 能力）。

时间规格（`spec`）支持三种：

- `{"every": "30m"}`       按间隔循环（s / m / h / d）
- `{"daily": "09:00"}`     每天固定时刻（本地时间）
- `{"once": "2025-01-01T09:00:00"}`  一次性

到点后调用 `ctx.run_task()` 在**独立新会话**里跑 Agent 任务（会话 id 形如
`cron-xxxxxxxxxx`），结果写进该会话，可在 WebUI 会话列表里查看。

启停与数据都落在 `data/plugins.json` 的插件状态里，重启服务后继续生效。
"""

import asyncio
import datetime as _dt
import time
import uuid

_TICK = 20.0


def _parse_every(text):
    """'30s' / '10m' / '2h' / '1d' → 秒。"""
    t = str(text or "").strip().lower()
    if not t:
        raise ValueError("every 不能为空")
    unit = t[-1]
    num = float(t[:-1])
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit)
    if mult is None or num <= 0:
        raise ValueError(f"无法解析间隔: {text}（示例: 30s / 10m / 2h / 1d）")
    return num * mult


def _next_run(spec, now=None):
    """根据规格算下一次执行的 unix 时间；非法规格抛 ValueError。"""
    now = now or time.time()
    if "every" in spec:
        return now + _parse_every(spec["every"])
    if "daily" in spec:
        hh, mm = (str(spec["daily"]).strip().split(":") + ["0"])[:2]
        h, m = int(hh), int(mm)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"daily 时间非法: {spec['daily']}")
        base = _dt.datetime.fromtimestamp(now)
        target = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if target.timestamp() <= now:
            target += _dt.timedelta(days=1)
        return target.timestamp()
    if "once" in spec:
        raw = str(spec["once"]).strip()
        try:
            ts = _dt.datetime.fromisoformat(raw).timestamp()
        except ValueError as exc:
            raise ValueError(f"once 需要 ISO 时间（如 2025-01-01T09:00:00）: {raw}") from exc
        return ts
    raise ValueError("spec 需要 every / daily / once 之一")


def _fmt(ts):
    try:
        return _dt.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


class Scheduler:
    def __init__(self, ctx):
        self.ctx = ctx
        self._task = None

    # ── 数据 ──
    def items(self):
        return list(self.ctx.state.get("schedules") or [])

    def save(self, items):
        self.ctx.save_state(schedules=items)

    def find(self, name):
        return next((x for x in self.items() if x.get("name") == name), None)

    # ── 操作 ──
    def add(self, name, spec, task, model="", enabled=True):
        name = str(name or "").strip()
        task = str(task or "").strip()
        if not name or not task:
            raise ValueError("name 与 task 都不能为空")
        if not isinstance(spec, dict):
            raise ValueError("spec 必须是对象，如 {\"every\":\"30m\"} 或 {\"daily\":\"09:00\"}")
        next_run = _next_run(spec)
        items = [x for x in self.items() if x.get("name") != name]
        items.append({"name": name, "spec": spec, "task": task, "model": str(model or ""),
                      "enabled": bool(enabled), "created": time.time(),
                      "next_run": next_run, "last_run": None, "last_result": None,
                      "runs": 0})
        self.save(items)
        return {"name": name, "next_run": _fmt(next_run), "enabled": bool(enabled)}

    def remove(self, name):
        items = [x for x in self.items() if x.get("name") != name]
        self.save(items)
        return {"removed": name, "left": len(items)}

    def list(self):
        return [{**{k: x.get(k) for k in ("name", "spec", "task", "model", "enabled",
                                          "runs", "last_result")},
                 "next_run": _fmt(x.get("next_run")) if x.get("next_run") else None,
                 "last_run": _fmt(x.get("last_run")) if x.get("last_run") else None}
                for x in self.items()]

    async def run_now(self, name=None):
        """立即执行（指定 name 或全部到期项）。"""
        items = self.items()
        targets = [x for x in items if (x.get("name") == name if name else self._due(x))]
        results = []
        for item in targets:
            results.append(await self._fire(item))
        return results

    def _due(self, item):
        if not item.get("enabled", True):
            return False
        nr = item.get("next_run")
        return bool(nr) and float(nr) <= time.time()

    async def _fire(self, item):
        name = item.get("name")
        self.ctx.log(f"定时任务触发: {name}")
        try:
            res = await self.ctx.run_task(item.get("task") or "", item.get("model") or "",
                                          "", "cron")
        except Exception as exc:
            res = {"error": str(exc), "session_id": None, "content": "", "steps": 0}
        items = self.items()
        for x in items:
            if x.get("name") != name:
                continue
            x["last_run"] = time.time()
            x["runs"] = int(x.get("runs") or 0) + 1
            x["last_result"] = (res.get("content") or res.get("error") or "")[:300]
            spec = x.get("spec") or {}
            if "once" in spec:
                x["enabled"] = False
                x["next_run"] = None
            else:
                try:
                    x["next_run"] = _next_run(spec)
                except Exception:
                    x["next_run"] = None
        self.save(items)
        return {"name": name, **res}

    async def loop(self):
        while True:
            try:
                for item in list(self.items()):
                    if self._due(item):
                        await self._fire(item)
            except Exception as exc:
                self.ctx.log(f"调度循环异常（继续）: {exc}")
            await asyncio.sleep(_TICK)


def setup(ctx):
    sched = Scheduler(ctx)
    ctx._scheduler = sched

    async def _add(name: str, spec: dict, task: str, model: str = "", enabled: bool = True):
        return sched.add(name, spec, task, model, enabled)

    async def _list():
        return {"schedules": sched.list()}

    async def _remove(name: str):
        return sched.remove(name)

    async def _run(name: str = ""):
        return {"results": await sched.run_now(name or None)}

    ctx.register_tool("schedule_add", _add,
                      "添加定时任务：spec 支持 {\"every\":\"30m\"}/{\"daily\":\"09:00\"}/{\"once\":\"ISO时间\"}",
                      {"name": "任务名", "spec": "时间规格对象", "task": "到点执行的完整任务描述",
                       "model": "使用的模型（默认跟随配置）", "enabled": "是否启用，默认 true"})
    ctx.register_tool("schedule_list", _list, "列出定时任务与下次执行时间", {})
    ctx.register_tool("schedule_remove", _remove, "删除定时任务", {"name": "任务名"})
    ctx.register_tool("schedule_run", _run, "立即执行定时任务（不传 name 则执行所有到期项）",
                      {"name": "任务名（可选）"})

    async def _api_list(request):
        return {"schedules": sched.list()}

    async def _api_run(request):
        # 名字可以从 query 或 JSON body 传（WebUI/脚本都方便）
        name = request.query_params.get("name") or ""
        if not name:
            try:
                body = await request.json()
                name = str((body or {}).get("name") or "")
            except Exception:
                name = ""
        return {"results": await sched.run_now(name or None)}

    async def _api_add(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return sched.add(body.get("name") or "", body.get("spec") or {},
                         body.get("task") or "", body.get("model") or "",
                         bool(body.get("enabled", True)))

    async def _api_remove(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        return sched.remove(body.get("name") or "")

    ctx.register_api("GET", "api/list", _api_list)
    ctx.register_api("POST", "api/run", _api_run)
    ctx.register_api("POST", "api/add", _api_add)
    ctx.register_api("POST", "api/remove", _api_remove)

    async def _start():
        await asyncio.sleep(3)
        await sched.loop()

    sched._task = asyncio.ensure_future(_start())
    ctx.log("schedule 已就绪（tick=20s）")


def teardown(ctx):
    task = getattr(getattr(ctx, "_scheduler", None), "_task", None)
    if task is not None:
        try:
            task.cancel()
        except Exception:
            pass
    ctx.log("schedule 已卸载")
