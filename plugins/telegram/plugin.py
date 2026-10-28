"""telegram 插件：用 Telegram 机器人操作 Gantry（手机端下任务、看进度、收回答）。

**为什么是长轮询**：webhook 要公网地址；getUpdates 长轮询不需要，本机就能用
（@BotFather 建 bot 拿 token）。

**安全**：机器人是公开的——没进白名单的 chat 一律静默忽略（`allowed_chat_ids`）。
token 从 `.env`（`TELEGRAM_BOT_TOKEN`）或插件配置读；白名单同理
（`TELEGRAM_ALLOWED_CHAT_IDS`，逗号分隔）。

**会话映射**：一个 chat 对应一个会话 sid（`/new` 强制开新会话）；普通文字 =
继续该会话（agent 模式跑）；完成时把回答整段发回，进行中按节流发进度。

**命令**：/new · /status · /sessions · /todo · /cancel · /pause · /resume ·
/model [id] · /answer 回答 · /help
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

API = "https://api.telegram.org/bot{token}"

_RUNS: dict[str, asyncio.Task] = {}
_POLL_TASK: asyncio.Task | None = None
_SID_SEQ = 0  # 同一秒开多个新会话时防重号（实测：两次 /new 撞同一个 ts）


def _cfg(ctx, key: str, default: Any = None) -> Any:
    try:
        v = (ctx.config or {}).get(key)
    except Exception:
        v = None
    return default if v in (None, "") else v


def _env(name: str) -> str:
    """读**不带前缀**的环境变量（.env 里就是 TELEGRAM_BOT_TOKEN 这种全名）。

    别用 brand.env()——它是给 GANTRY_/CBP_ 前缀变量用的，
    拿它读全名会永远拿到空（实测踩过）。
    """
    return (os.environ.get(name) or "").strip()


def _token(ctx) -> str:
    return str(_cfg(ctx, "token") or _env("TELEGRAM_BOT_TOKEN")).strip()


def _allowed(ctx) -> set[str]:
    raw = _cfg(ctx, "allowed_chat_ids")
    if not raw:
        raw = _env("TELEGRAM_ALLOWED_CHAT_IDS")
    if isinstance(raw, str):
        return {x.strip() for x in raw.split(",") if x.strip()}
    return {str(x).strip() for x in (raw or []) if str(x).strip()}


def _chats(ctx) -> dict:
    st = ctx.state.get("chats")
    if not isinstance(st, dict):
        st = {}
        ctx.state["chats"] = st
    return st


def _sid_for(ctx, chat_id: str, new: bool = False) -> str:
    global _SID_SEQ
    st = _chats(ctx)
    if new or chat_id not in st:
        _SID_SEQ += 1
        st[chat_id] = {"sid": f"tg-{chat_id}-{int(time.time())}-{_SID_SEQ}",
                       "model": _cfg(ctx, "default_model", "copilot-auto")}
        try:
            ctx.save_state(chats=st)
        except Exception:
            pass
    return st[chat_id]["sid"]


def _model_for(ctx, chat_id: str) -> str:
    return str((_chats(ctx).get(chat_id) or {}).get("model")
               or _cfg(ctx, "default_model", "copilot-auto"))


async def _tg(ctx, method: str, **params) -> Any:
    url = API.format(token=_token(ctx)) + "/" + method
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as c:
        r = await c.post(url, json=params)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} 失败: {data}")
        return data.get("result")


async def _send(ctx, chat_id: str, text: str) -> None:
    text = (text or "").strip() or "（空）"
    for i in range(0, len(text), 3800):
        try:
            await _tg(ctx, "sendMessage", chat_id=chat_id, text=text[i:i + 3800])
        except Exception as exc:
            ctx.log(f"发送失败 {chat_id}: {exc}")


def _fmt_steps(steps: list) -> str:
    if not steps:
        return "0 步"
    last = steps[-1] or {}
    tool = last.get("tool") or last.get("status") or "step"
    mark = {"ok": "✓", "error": "✗", "invalid_action": "✗"}.get(last.get("status"), "·")
    return f"{len(steps)} 步（{mark} {tool}）"


async def _run_task(ctx, chat_id: str, text: str) -> None:
    from gantry import bridge as b
    sid = _sid_for(ctx, chat_id)
    model = _model_for(ctx, chat_id)
    if sid in _RUNS and not _RUNS[sid].done():
        await _send(ctx, chat_id, "上一个任务还在跑。先 /cancel 或等它结束。")
        return
    await _send(ctx, chat_id, f"收到，开始（{model}）…")

    async def _work() -> str:
        req = b.AgentRequest(
            model=model,
            messages=[b.Message(role="user", content=text)],
            stream=False,
            session_id=sid,
            workspace=None,
            agent_mode=bool(_cfg(ctx, "agent_mode", True)),
        )
        result = await b.run_agent(req)
        return (result.content or "").strip() or "（无输出）"

    async def _watch() -> None:
        every = max(3.0, float(_cfg(ctx, "progress_every_seconds", 10)))
        task = _RUNS.get(sid)
        while task and not task.done():
            try:
                steps = b.AGENT_SESSIONS.steps(sid)
                rt = b.RUNTIME.get(sid) or {}
                phase = rt.get("phase") or ""
                await _send(ctx, chat_id, f"进行中… {_fmt_steps(steps)}" +
                            (f"（{phase}）" if phase else ""))
            except Exception:
                pass
            await asyncio.sleep(every)

    _RUNS[sid] = asyncio.create_task(_work())
    watcher = asyncio.create_task(_watch())
    try:
        answer = await _RUNS[sid]
    except asyncio.CancelledError:
        answer = "已取消。"
    except Exception as exc:
        answer = f"任务失败: {exc}"
    finally:
        watcher.cancel()
        _RUNS.pop(sid, None)
    await _send(ctx, chat_id, answer)


async def _cmd(ctx, chat_id: str, text: str) -> bool:
    """处理 /xxx 命令；返回 True 表示已处理。"""
    from gantry import bridge as b
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("start", "help"):
        await _send(ctx, chat_id,
                    "Gantry bot。/new 任务 开始；直接发文字继续当前会话；"
                    "/status /todo /cancel /pause /resume /model [id] /answer 回答。")
        return True
    if cmd == "new":
        if not arg:
            await _send(ctx, chat_id, "用法：/new 任务内容")
            return True
        _sid_for(ctx, chat_id, new=True)
        await _run_task(ctx, chat_id, arg)
        return True
    if cmd == "status":
        sid = _sid_for(ctx, chat_id)
        sess = b.AGENT_SESSIONS.get(sid) or {}
        steps = b.AGENT_SESSIONS.steps(sid)
        rt = b.RUNTIME.get(sid) or {}
        running = sid in _RUNS and not _RUNS[sid].done()
        await _send(ctx, chat_id,
                    f"会话 {sid}\n模型 {_model_for(ctx, chat_id)} · "
                    f"{'运行中' if running else '空闲'} · {len(steps)} 步\n"
                    f"状态 {sess.get('state', '?')} / 阶段 {rt.get('phase', '-')}")
        return True
    if cmd == "sessions":
        rows = b.AGENT_SESSIONS.list()[:8]
        lines = [f"{s.get('id')}  {s.get('state') or ''}" for s in rows]
        await _send(ctx, chat_id, "最近会话：\n" + ("\n".join(lines) or "（无）"))
        return True
    if cmd == "todo":
        sid = _sid_for(ctx, chat_id)
        todos = (b.AGENT_SESSIONS.get(sid) or {}).get("todos") or []
        lines = [f"{'✓' if t.get('done') else '·'} {t.get('content', '')}" for t in todos]
        await _send(ctx, chat_id, "待办：\n" + ("\n".join(lines) or "（无）"))
        return True
    if cmd == "cancel":
        sid = _sid_for(ctx, chat_id)
        t = _RUNS.get(sid)
        if t and not t.done():
            t.cancel()
        try:
            b.AGENT_SESSIONS.patch(sid, state="cancelled", cancel_requested=True)
        except Exception:
            pass
        await _send(ctx, chat_id, "已取消。")
        return True
    if cmd == "pause":
        sid = _sid_for(ctx, chat_id)
        try:
            b.AGENT_SESSIONS.patch(sid, state="paused")
            await _send(ctx, chat_id, "已暂停。")
        except Exception as exc:
            await _send(ctx, chat_id, f"失败: {exc}")
        return True
    if cmd == "resume":
        sid = _sid_for(ctx, chat_id)
        try:
            ok = await b.resume_session_task(sid)
            await _send(ctx, chat_id, "已恢复。" if ok else "没有可恢复的任务。")
        except Exception as exc:
            await _send(ctx, chat_id, f"失败: {exc}")
        return True
    if cmd == "model":
        if not arg:
            await _send(ctx, chat_id, f"当前模型：{_model_for(ctx, chat_id)}\n用法：/model <id>")
            return True
        # 先建 sid 再取 chat 引用：setdefault 先塞空 dict 会让 _sid_for 拿到没 sid 的壳（实测 KeyError）
        sid = _sid_for(ctx, chat_id)
        st = _chats(ctx)
        chat = st[chat_id]
        chat["model"] = arg
        chat["sid"] = sid
        try:
            ctx.save_state(chats=st)
        except Exception:
            pass
        await _send(ctx, chat_id, f"已切换模型：{arg}")
        return True
    if cmd == "answer":
        if not arg:
            await _send(ctx, chat_id, "用法：/answer 你的回答")
            return True
        sid = _sid_for(ctx, chat_id)
        fut = b.EXTRA_ANSWERS.get(sid)
        if fut is None or fut.done():
            await _send(ctx, chat_id, "当前没有等待回答的问题。")
            return True
        fut.set_result(arg)
        try:
            b.runtime_update(sid, question=None)
        except Exception:
            pass
        await _send(ctx, chat_id, "已提交回答。")
        return True
    return False


async def _handle_update(ctx, upd: dict) -> None:
    msg = upd.get("message") or upd.get("edited_message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    if not chat_id:
        return
    allowed = _allowed(ctx)
    if allowed and chat_id not in allowed:
        ctx.log(f"忽略未授权 chat {chat_id}")
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if text.startswith("/") and await _cmd(ctx, chat_id, text):
        return
    await _run_task(ctx, chat_id, text)


async def _poll_loop(ctx) -> None:
    offset = int(ctx.state.get("offset", 0) or 0)
    interval = float(_cfg(ctx, "poll_interval", 2))
    ctx.log("telegram：开始长轮询")
    while True:
        try:
            token = _token(ctx)
            if not token:
                await asyncio.sleep(10)
                continue
            async with httpx.AsyncClient(timeout=httpx.Timeout(75.0, connect=15.0)) as c:
                r = await c.get(API.format(token=token) + "/getUpdates",
                                params={"offset": offset, "timeout": 30})
                r.raise_for_status()
                data = r.json()
            for upd in data.get("result", []):
                offset = max(offset, int(upd.get("update_id", 0)) + 1)
                try:
                    await _handle_update(ctx, upd)
                except Exception as exc:
                    ctx.log(f"处理 update 失败: {exc}")
            if data.get("result"):
                try:
                    ctx.save_state(offset=offset)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.log(f"长轮询异常: {exc}（5s 后重试）")
            await asyncio.sleep(5)
        await asyncio.sleep(interval)


def _status(ctx) -> dict:
    return {
        "enabled": bool(_token(ctx)),
        "allowed_chats": sorted(_allowed(ctx)),
        "known_chats": len(_chats(ctx)),
        "running_tasks": sum(1 for t in _RUNS.values() if not t.done()),
        "offset": ctx.state.get("offset", 0),
    }


def setup(ctx) -> None:
    global _POLL_TASK
    token = _token(ctx)
    if not token:
        ctx.log("telegram：未配置 token（.env 的 TELEGRAM_BOT_TOKEN 或插件配置），插件待命")
    if not _allowed(ctx):
        ctx.log("telegram：未配置 allowed_chat_ids 白名单——任何 chat 都会受理，建议尽快加上")

    ctx.register_api("GET", "api/status", lambda *_: _status(ctx))

    if token and (_POLL_TASK is None or _POLL_TASK.done()):
        # create_task 要有"正在运行"的循环；生产在 lifespan 里有，测试里可能没有——
        # 用 get_event_loop 兜底（任务先挂着，循环跑起来自然会执行）。
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.get_event_loop()
        _POLL_TASK = loop.create_task(_poll_loop(ctx))
    ctx.log("telegram：插件已装载" + ("（待命：无 token）" if not token else ""))


def teardown(ctx) -> None:
    global _POLL_TASK
    if _POLL_TASK and not _POLL_TASK.done():
        _POLL_TASK.cancel()
    _POLL_TASK = None
    for t in _RUNS.values():
        if not t.done():
            t.cancel()
    _RUNS.clear()
    ctx.log("telegram：插件已卸载")
