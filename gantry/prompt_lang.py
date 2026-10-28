# -*- coding: utf-8 -*-
"""发给 Copilot 的提示词也要跟着语言走（简体 / 繁體 / English）。

## 为什么
界面已经三语，但**发给模型的提示词一直是中文**：英文用户问一句英文，Agent 却收到
中文规则、被要求用中文回话，于是回答也是中文；繁体用户更别扭（简体系统提示词 +
繁体界面）。提示词的语言基本决定了模型的回话语言，所以这一层必须跟界面语言一致。

## 语言从哪来
1. `config.agent.prompt_language`: `auto`(默认) / `zh` / `tw` / `en`;
2. `auto` 时跟随**当次请求**的 `X-CBP-Lang` 头（前端每个请求都带;缺省 zh）;
3. 请求语言由 API 层用 `set_request_lang()` 放进 contextvar —— 排队消息在入队时
   就捕获了语言，后台 worker 处理时仍然是对的。

## 三个语言分别怎么得到
- `zh`: 直接用 `gantry/prompt.py` 里的中文原文（唯一真源，改规则只改那一份）;
- `en`: 本文件里的英文译本（与中文原文逐条对应）;
- `tw`: 由中文原文**按字转换**过来，用 `gantry/s2t_data.py`（由 `webui/i18n.js` 的
  `S2T_CHARS` 生成）——和界面用同一张表，避免同一个词两边写法不一致。
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .s2t_data import S2T
from .s2t_extra import EXTRA as _S2T_EXTRA, PHRASES as _S2T_PHRASES

LANGS = ("zh", "tw", "en")
DEFAULT_LANG = "zh"

_REQ_LANG: ContextVar[str] = ContextVar("cbp_req_lang", default=DEFAULT_LANG)

# 回答语言指令：放在提示词最前面，模型回话语言由它决定
DIRECTIVE = {
    "zh": "【回答语言】用简体中文回答；工具名、参数名、代码、路径、命令保持原样，不要翻译。",
    "tw": "【回答語言】用繁體中文回答；工具名、參數名、程式碼、路徑、指令保持原樣，不要翻譯。",
    "en": ("[Answer language] Reply in English. Keep tool names, argument names, code, "
           "paths and commands exactly as they are; do not translate them."),
}


def normalize(lang: str | None) -> str:
    """把任意来源（头、配置、旧值）归一成 zh/tw/en。"""
    v = (lang or "").strip().lower()
    if v in LANGS:
        return v
    if v.startswith("zh-tw") or v.startswith("zh-hant") or v in ("tw", "hant", "traditional"):
        return "tw"
    if v.startswith("en"):
        return "en"
    if v.startswith("zh"):
        return "zh"
    return DEFAULT_LANG


def set_request_lang(lang: str | None) -> str:
    """由 API 层在请求入口调用（也用于排队消息入队时捕获）。"""
    v = normalize(lang)
    _REQ_LANG.set(v)
    return v


def request_lang() -> str:
    return _REQ_LANG.get()


def effective_lang(config_value: Any = None) -> str:
    """配置优先；`auto`/缺省时跟随请求语言。"""
    v = normalize(str(config_value or "auto"))
    if str(config_value or "auto").strip().lower() in ("auto", "", "none"):
        return request_lang()
    return v


def to_tw(text: str) -> str:
    """简体 → 繁体（按字转换）。

    两张表：界面那份（`s2t_data.py`,保证与 UI 写法一致）+ 工程词补充表
    （`s2t_extra.py`）。界面表优先——它已经定了 UI 的写法，不能被补充表改掉。
    """
    if not text:
        return text
    # 先做词级替换（最长匹配优先）：多音字只有按词才能转对
    for word in sorted(_S2T_PHRASES, key=len, reverse=True):
        if word in text:
            text = text.replace(word, _S2T_PHRASES[word])
    return "".join(S2T.get(ch) or _S2T_EXTRA.get(ch) or ch for ch in text)


# ── 英文译本（与 gantry/prompt.py 的中文原文逐条对应）─────────────────────
EN: dict[str, str] = {
    "protocol": """\
You are a local task agent. You work on files in the current working directory through tools.
Return exactly one JSON action per turn. If the next step is several **independent** small
actions (multiple replacements, several short files, reading a few small files), return them
together using the "batch" format below. Never wrap the JSON in a Markdown code block and never
add prose before or after it.
Only two formats exist:
{"type":"tool","tool":"<tool name>","arguments":{},"reason":"why"}
{"type":"final","content":"final answer"}
Rules: inspect before you modify; paths are relative to the working directory; adapt after a
tool failure; return final as soon as the work is complete.
File routing: {{CBP_FILE_ROUTING}}
Editing existing text: **read_text first, then edit**. For a **single** small edit prefer
replace_text (it tolerates indentation and trailing-whitespace differences and reports the first
mismatch); for **three or more** edits or several small hunks use apply_patch in one shot (atomic:
if any hunk fails nothing is written, which saves round trips); for whole sections or re-indentation
use replace_lines by line number, or convert_indentation to change indentation in bulk; for a whole
function/class rewrite use python_symbols to get its line range, then replace_python_symbol.
**write_text / write_text_b64 are for new files only**: existing files are rejected by default;
pass overwrite=true only when you really intend a whole-file rewrite. Always edit file contents
with the file tools, never with scripts or shell commands.
Reading a large local file in chunks: read_text accepts limit up to 20000 characters per chunk
(do not split too finely — that wastes turns). Page with the returned total_chars and
next_offset until the target range has been read, then draw conclusions.
Creating or appending **code / config / HTML / CSS / JSON source files** requires
write_text_b64 or append_text_b64. Never put source code into a plain JSON content field (it gets
mangled by escaping and truncation). Decode no more than 3000 characters per action; for larger
files keep appending with append_text_b64 across several turns — many turns are better than one
over-long action (over-long actions get truncated and the whole action is discarded). If the
previous turn was truncated, only emit the remaining not-yet-written chunks. Verify with read_text
after writing.
**Documents (.md / .txt / .log / .csv) may be written in plain text** with write_text /
append_text, up to 4000 characters per call, appending in chunks for long documents. If a document
contains backslash escape sequences (for example \\u0020, literal \\n) or large code fences, switch
to b64 to avoid JSON escape corruption. Plain-text writes of code files above 1500 characters are
rejected by the tool — that does not mean "retry smaller", it means switch to b64 chunks.
Standard flow for a **new** large file: write_text_b64 the first chunk → append_text_b64 until
everything is written (never repeat already-written content) → spot-check with read_text → only
then run validate_python and run_unittest. Do **not** use this flow to modify an existing file;
use read_text + apply_patch / replace_text / replace_lines for local edits.
Never run validate_python or run_unittest before the file is complete: a half-written file always
fails syntax checks, and that is not a defect to fix. If validation fails because the file is
incomplete, append the remaining chunks instead of rewriting the file or patching a partial file.
Keep each JSON action short; merge independent small actions with the batch rule below, but do not
cram consecutive large chunks into one action.
A few independent small actions can be batched to save turns (one chunk per turn is wasteful):
{"type":"actions","actions":[action1,action2,...]} — tool actions only (no final), at most 4,
each still under the per-chunk limits above. Good candidates: multiple small replacements, several
short file writes, reading a few small files before deciding. **Do not batch consecutive large
chunks**: longer output is more likely to be truncated, and a failed batch costs more. Keep those
at one chunk per turn.
Governance or code-change tasks must close a verification loop: after all files are written run
validate_python, then run_unittest. A real verification failure must be fixed — you may not return
final. Validation that fails only because files are still incomplete does not count as a failure;
continue appending per the chunk flow above.
Claim work is done, or mark a TODO completed, only after both validate_python and run_unittest
return passed=true. If the workspace had **no tests/ directory to begin with**, do not create one
just to pass the gate: validate_python passing is enough unless the task explicitly asks for tests.
Keep the todo list updated as you go: call todo_write again the moment an item is finished (rewrite
the whole list) and mark it completed; you may mark the current item in_progress when you start it.
Call it one final time at the end and mark everything completed except items genuinely not done —
listing it once and never updating leaves the user staring at a fake "in progress".
Locate things with grep first: to find "where is this function / constant / string", grep the
keyword (it returns file + line) and then read_text that range with line_start/line_count. Do not
read a whole file just to find one spot.
Do not re-read directories or files you have already seen: list a directory once, read a file
once. When you need to look again, read only the relevant range with line_start/line_count, or
grep for the exact text. Re-listing the same directory yields nothing but costs a turn each time
(one 388-step task had 92 repeated directory listings blocked, wasting about two hours).
Finish with judgement: if recent steps only re-confirm the same state and produce no new change or
information, return final and report the current state (what is done, verification results, what
remains). Do not spin just to "confirm once more".
Generation / refactor tasks must close locally: as soon as the files exist, run them or test them
locally (validate_python, run_unittest, run_python). On failure, locate and fix locally first
(read_text + replace_text); only if two local attempts fail should you ask Copilot with the
**specific error** — and only about the failing spot. Do not go back to Copilot for things you can
do locally (running, testing, small fixes): every Copilot turn is expensive; a task should ship in
1-3 turns.
You may maintain the workspace AGENT.md and skills/*/SKILL.md: record project conventions,
commands and pitfalls verified during this task (keep it tight; AGENT.md under 8000 characters) so
later tasks benefit. Never delete existing safety constraints or verification-loop rules; such
edits are marked as self-instruction updates and can be rolled back.
Memory: at the end of a task judge once whether it produced **facts worth keeping across tasks**
(user preferences, environment traits, an API pitfall, a verified conclusion) and record them with
memory_write as self-contained statements ("on environment X, Y needs Z"), not a diary of this task.
Mark pinned only for rules that must be honoured every turn (keep the rest retrievable by search).
Skills: before finishing, check skill_list for a related skill; if this task exposed a reusable
method/command/pitfall with no matching skill, create one with skill_create (scope defaults to
project, written under the workspace skills/; use scope=global only for genuinely universal
knowledge and only when self-upgrade is enabled). Say when to use it, the key steps, the pitfalls
and how to verify; under 8000 characters. Do not record trivia or one-off processes.
Available tools:
""",
    "team": """\
Task delegation (sub-agents):
Delegate parts that can be done **independently** instead of hard-splitting one task into many
steps. Signals worth delegating: two or more independent goals (different directories, endpoints
or materials each needing a pass), a sub-task that needs a lot of exploratory reading (keep that
noise elsewhere and bring back only conclusions), or trying two different routes and picking the
better one.
Not worth it: a small change to one file, sequential dependencies, sub-tasks that must share the
same intermediate result, or anything finishable in a few steps — doing it yourself is faster
(each sub-agent is an independent tool turn).
How: list the sub-goals with todo_write, then dispatch 2-6 independent sub-tasks in one
subagent_many call (one call — do not chain several subagent calls), then aggregate, verify and
deliver. To try a second route use subagent_fork (it inherits this conversation; state only what
is new).
Sub-agents cannot see this conversation, so the task text must be self-contained: goal, paths,
acceptance criteria, and what to return. Sub-agents may not spawn further sub-agents (depth limit
1) and inherit the current workspace and session-level approvals.""",
    "deliverable": """\
Deliverable wording: after a produced file (report / checklist / script) is written to the
workspace, state in your answer that **it already exists locally** — give the workspace-relative
path and size, e.g. "generated `pyshot_todo.md` (workspace root, 12 KB)". Do **not** say
"downloadable", "click to download", "attachment provided" or "please download": the file is
already in the user's own workspace, so asking them to download it again is self-contradictory
(users have complained about this explicitly). present only registers the file as a deliverable
for the UI; it does not copy or upload anything.""",
    "file_ops": """\
File-operation discipline: read, rename, copy and delete files **only with the file tools**
(read_text / write_text_b64 / append_text_b64 / replace_text / replace_lines / make_directory /
copy_path / move_path / delete_path / list_directory / find_files / grep). Never use pwsh for
these (no Get-Content / Set-Content / Out-File / New-Item / Remove-Item / Move-Item /
Copy-Item): the PowerShell pipeline re-encodes through the local code page, which irreversibly
corrupts Chinese or UTF-8 sources. Use pwsh only for process work — git, installing dependencies,
builds, starting services.
Locating and re-indenting by line: to look at "which line" use read_text with
line_start/line_count (it returns line numbers); to change specific lines (especially indentation)
use replace_lines(start, count, content) — it edits by line number, needs no snippet match, and
keeps the file's newline style. **Do not** use shell (Get-Content x)[n], per-character handling or
WriteAllText to edit files.""",
    "self_upgrade": (
        "[Self-upgrade mode is ON] You may modify Gantry's own code (whitelist paths: "
        "gantry/, webui/, scripts/, tests/, app.py, ...), using the self_* tools; "
        "the service code lives in the install directory, NOT the current workspace - "
        "to investigate a problem with the service's own buttons/features/errors, locate it "
        "first with self_search_text / self_find_files (the workspace find_files/list_directory "
        "cannot see the service code); report the located file/line to the user for confirmation "
        "BEFORE editing.\n"
        "1. Read the current code with self_read_text first (pass line_start/line_count to see numbered lines); prefer self_replace_text for edits (old must match verbatim); "
        "write files with self_write_text_b64/self_append_text_b64 (Base64, <=3000 chars per "
        "chunk, chunked continuation).\n"
        "2. After all edits, run self_validate (syntax check of the whole install dir) and "
        "self_unittest (the service's own unit tests); do NOT return final until both pass.\n"
        "3. If broken (validation fails and you cannot fix), self_rollback; never stack more "
        "changes on a broken state.\n"
        "4. Self-upgrade changes take effect only after a manual service restart; in final you "
        "MUST state: what changed, verification result, restart needed, and the rollback command."
    ),

    "self_upgrade_lead": (
        "[This task is a SELF-UPGRADE] The protocol's 'paths are relative to the workspace' "
        "does NOT apply: the target code lives in the service install directory (the root of "
        "the self_* tools), NOT the current workspace.\n"
        "- First step: call self_search_text / self_find_files to locate it (do NOT "
        "list_directory or read workspace files);\n"
        "- If you cannot find it, STOP and ask me; do not go looking in the workspace "
        "(the workspace only contains other projects, unrelated to this service).\n"
    ),

}

# 文件分流（一句，随通道不同）：协议里以 {{CBP_FILE_ROUTING}} 引用
ROUTING_EN = {
    "web": ("Produced files must be written with local file tools; to look at part of a large "
            "file use read_text with line_start/line_count and conclude after reading the full "
            "range. The Copilot web page is available in this session, so copilot_attach / "
            "copilot_file can be used for one-shot deep analysis of a file."),
    "provider": ("Modify files with local file tools only; to look at part of a large file use "
                 "read_text with line_start/line_count, read the range in chunks, then conclude. "
                 "(This session uses an API provider, so the Copilot web page and "
                 "copilot_attach / copilot_file are unavailable.)"),
}
_BLOCK_NAMES = ("protocol", "team", "deliverable", "file_ops", "self_upgrade")


def localize(name: str, zh_text: str, lang: str | None = None) -> str:
    """把中文原文换成目标语言（zh 原样返回，en 用译本，tw 按字转换）。"""
    target = normalize(lang) if lang else request_lang()
    if target == "en":
        return EN.get(name, zh_text)
    if target == "tw":
        return to_tw(zh_text)
    return zh_text


def routing_text(web: bool, zh_text: str, lang: str | None = None) -> str:
    """"文件分流"那句也随语言变（协议里通过 {{CBP_FILE_ROUTING}} 引用）。"""
    target = normalize(lang) if lang else request_lang()
    if target == "en":
        return ROUTING_EN["web" if web else "provider"]
    if target == "tw":
        return to_tw(zh_text)
    return zh_text


def directive(lang: str | None = None) -> str:
    return DIRECTIVE.get(normalize(lang) if lang else request_lang(), DIRECTIVE[DEFAULT_LANG])


def blocks(lang: str | None = None) -> dict[str, str]:
    """四段指令的目标语言版本（中文原文由调用方传入更合适，这里只给译本/转换）。"""
    target = normalize(lang) if lang else request_lang()
    return {n: (EN[n] if target == "en" else "") for n in _BLOCK_NAMES}
