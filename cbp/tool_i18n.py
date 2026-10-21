# -*- coding: utf-8 -*-
"""工具清单的多语言层：描述与参数提示按提示词语言输出。

工具清单是整段提示词里最后一块中文（英文模式下约占 13% 中的一半以上）。
它虽然属于结构化内容，但模型是**读这些描述**来决定用哪个工具的，语言不一致会
增加误用概率，所以一起翻。

- `DESC_EN`: 25 个工具的英文描述（与 `cbp/tools.py`、`cbp/tools_extra.py` 的中文
  原文逐条对应；工具名、参数名、文件后缀一律保持原样）;
- `ARG_EN`: 参数提示，键为 `工具名.参数名`;
- `localize_items(items, lang)`: `zh` 原样返回；`en` 换译本；`tw` 按字转换
  （与界面同一张简繁表）。缺失译文时**保留中文原文**——宁可见中文，也不要丢信息。
"""
from __future__ import annotations

from typing import Any

from .prompt_lang import LANGS, normalize, to_tw

DESC_EN: dict[str, str] = {
    "list_directory": "List entries of a workspace directory",
    "find_files": "Find files by glob pattern",
    "read_text": ("Read UTF-8 text; reads the whole file by default (up to max_read_chars). "
                  "For huge files page by characters with offset/limit, or read by line with "
                  "line_start/line_count (returns line numbers, handy for editing a few lines)"),
    "write_text": ("Overwrite a UTF-8 plain-text file. Documents (.md/.txt/.log) may be written "
                   "whole; code files only up to 1500 characters (use b64 chunks for more)"),
    "append_text": ("Append UTF-8 plain text. Use it to continue documents in chunks; code files "
                    "only up to 1500 characters (use append_text_b64 for more)"),
    "write_text_b64": ("Base64-decode and overwrite as UTF-8; required for code, JSON, HTML and "
                       "Markdown"),
    "append_text_b64": ("Base64-decode and append as UTF-8; required for code, JSON, HTML and "
                        "Markdown"),
    "replace_text": ("Replace a short exact snippet in an existing file; prefer this when editing "
                     "code"),
    "apply_patch": ("Apply several edits in one call (atomic: nothing is written if any hunk "
                    "fails). Accepts a patch or an edits list; use it for four or more small "
                    "edits to save turns"),
    "convert_indentation": ("Convert **leading** indentation between tabs and spaces in one shot "
                            "(only line prefixes; strings and inner text untouched). Stop fixing "
                            "indentation chunk by chunk with replace_text"),
    "indent_lines": ("Add or remove **leading spaces** on a line range (positive delta = indent, "
                     "negative = outdent). Use this for indentation instead of stuffing spaces "
                     "into replace_text"),
    "replace_lines": ("Replace a whole line range by line number (1-based, inclusive) keeping the "
                      "file's newline style. More robust than replace_text for re-indentation or "
                      "whole-section rewrites; no shell needed"),
    "make_directory": "Create a directory",
    "copy_path": "Copy a file or directory",
    "move_path": "Move a file or directory",
    "validate_python": ("Check Python syntax of a file or directory; run it only after all files "
                        "are complete (a half-written file always fails — that is not a defect)"),
    "run_unittest": "Run unittest discovery; run it only after all files are complete",
    "exit_plan_mode": ("Leave plan mode: submit the plan and lift read-only mode (call it after "
                       "read-only exploration in the planning phase)"),
    "read_excel": "Read the contents of Excel worksheets",
    "write_excel": "Write 2-D row data into Excel (creates the file if missing)",
    "read_docx": "Extract paragraphs and table text from a Word document",
    "write_docx": "Create or append paragraphs in a Word document",
    "read_pptx": "Extract slide text and speaker notes from a presentation",
    "read_pdf": "Extract per-page text from a PDF",
    "tool_list": ("List every available tool and its group (the prompt only lists tools relevant "
                  "to the current task; others can still be called directly)"),
}

# 参数提示：键为 "工具.参数"
ARG_EN: dict[str, str] = {
    "list_directory.path": "Relative path, defaults to .",
    "find_files.pattern": "e.g. **/*.log",
    "read_text.path": "Relative path",
    "read_text.offset": "Character offset, default 0",
    "read_text.limit": "Max characters returned this time, default 120000",
    "read_text.line_start": "Read by line: first line, 1-based (ignores offset when set)",
    "read_text.line_count": "Read by line: how many lines, defaults to end of file",
    "read_text.reveal_whitespace": "Show spaces as · and tabs as → (essential for checking indentation)",
    "write_text.path": "Relative path",
    "write_text.content": "Text",
    "append_text.path": "Relative path",
    "append_text.content": "Text",
    "write_text_b64.path": "Relative path",
    "write_text_b64.content_b64": "Base64 of the UTF-8 content",
    "append_text_b64.path": "Relative path",
    "append_text_b64.content_b64": "Base64 of the UTF-8 content",
    "replace_text.path": "Relative path",
    "replace_text.old": "Original text",
    "replace_text.new": "New text",
    "replace_text.count": "Number of replacements, default 1",
    "apply_patch.path": "Relative path",
    "apply_patch.patch": ("Patch text: *** Begin Patch / *** Update File: x / @@ hunks; leading "
                          "' '=context '-'=old '+'=new"),
    "apply_patch.edits": "Or an array [{old,new,count?}] (mutually exclusive with patch)",
    "apply_patch.count": "Replacements per edit, default 1 (0 = all)",
    "convert_indentation.path": "Relative path",
    "convert_indentation.mode": "tabs2spaces (default) or spaces2tabs",
    "convert_indentation.width": "Tab width, default 4",
    "convert_indentation.line_start": "Only this range: first line, 1-based (optional)",
    "convert_indentation.line_count": "How many lines (optional)",
    "indent_lines.path": "Relative path",
    "indent_lines.line_start": "First line, 1-based",
    "indent_lines.line_count": "How many lines, default 1",
    "indent_lines.delta": "Spaces to add or remove (positive = indent, negative = outdent)",
    "replace_lines.path": "Relative path",
    "replace_lines.start": "First line number, 1-based",
    "replace_lines.count": "How many lines to replace, default 1",
    "replace_lines.content": ("New content (may span lines; an empty string deletes these lines)"),
    "make_directory.path": "Relative path",
    "copy_path.source": "Relative path",
    "copy_path.destination": "Relative path",
    "move_path.source": "Relative path",
    "move_path.destination": "Relative path",
    "validate_python.path": "Relative path, defaults to .",
    "run_unittest.path": "Test directory, defaults to tests",
    "run_unittest.pattern": "Defaults to test_*.py",
    "exit_plan_mode.plan": "Plan to execute (Markdown)",
    "exit_plan_mode.notes": "Extra notes (optional)",
    "read_excel.path": "Relative path",
    "read_excel.sheet": "Sheet name, defaults to all",
    "read_excel.max_rows": "Max rows per sheet, default 200",
    "write_excel.path": "Relative path",
    "write_excel.sheet": "Sheet name, defaults to the active sheet",
    "write_excel.rows": "2-D array",
    "write_excel.start_cell": "Start cell, default A1",
    "read_docx.path": "Relative path",
    "write_docx.path": "Relative path",
    "write_docx.paragraphs": "Array of strings",
    "write_docx.mode": "append or overwrite, default append",
    "read_pptx.path": "Relative path",
    "read_pdf.path": "Relative path",
}


def localize_items(items: list[dict[str, Any]], lang: str | None = None) -> list[dict[str, Any]]:
    """按语言改写工具清单的 description / arguments 提示（不动 name 与参数名）。"""
    target = normalize(lang)
    if target not in LANGS or target == "zh":
        return items
    out: list[dict[str, Any]] = []
    for item in items:
        name = str(item.get("name") or "")
        desc = str(item.get("description") or "")
        args = dict(item.get("arguments") or {})
        if target == "en":
            new_desc = DESC_EN.get(name, desc)
            new_args = {k: ARG_EN.get(f"{name}.{k}", v) for k, v in args.items()}
        else:                                    # tw
            new_desc = to_tw(desc)
            new_args = {k: to_tw(str(v)) for k, v in args.items()}
        row = dict(item)
        row["description"] = new_desc
        row["arguments"] = new_args
        out.append(row)
    return out

# ── 其余工具（memory / skill / subagent / web / job / 视觉等）的英文描述与参数 ──
DESC_EN.update({
    'outlook_unread': 'List unread messages in the Outlook inbox',
    'outlook_read': 'Read a message body by entry_id',
    'outlook_search': 'Search a folder by subject keyword and date (inbox / sent / outbox / deleted / drafts)',
    'skill_list': 'List available skills (global + project level; same-name project skills override global)',
    'skill_read': 'Read the full SKILL.md of a skill (global skills live outside the workspace; only this tool can read them)',
    'memory_write': 'Record one long-term memory useful across sessions (fact / preference / decision / pitfall). Write only when it really helps later tasks; duplicates are merged on write. scope=global is visible to every task, project (default) only to this workspace',
    'memory_search': 'Search long-term memory by keyword; returns the most relevant entries',
    'memory_forget': 'Delete one outdated or wrong long-term memory entry',
    'skill_create': 'Turn a method that proved itself in this task into a skill (SKILL.md). scope=project writes the workspace skills/ (default); scope=global writes the service install dir',
    'ask_user_question': 'Ask the user mid-task and wait for the answer (use it when a decision or extra information is needed instead of guessing)',
    'create_goal': 'Create a multi-round goal: while incomplete, returning final auto-continues (up to max_rounds); finish with update_goal',
    'get_goal': 'Read the current goal and its progress',
    'grep': 'Regex-search workspace file contents (find_files matches names only); returns file:line:content',
    'job_kill': 'Stop a background job',
    'job_list': 'List background jobs',
    'job_output': 'Read background job output (wait=true blocks until it finishes or times out)',
    'present': 'Declare deliverables (at most 4 files). It only registers them for the UI: the files are already in the local workspace and are not copied or uploaded. Say "generated at <workspace-relative path>", never "downloadable"',
    'pwsh': 'Run a PowerShell command (one-off approval required by default); with run_in_background=true it returns a job_id',
    'ralph': 'Goal iteration: each round advances the goal with a fresh sub-agent until one declares GOAL_COMPLETE or the rounds run out',
    'read_image': 'Read image metadata (format / size / dimensions); with analyze=true Copilot vision describes it',
    'subagent': 'Hand one self-contained task to a sub-agent (separate session and context) to isolate context or parallelise research/analysis/local refactors; returns its final result and step count, not intermediate steps',
    'subagent_fork': 'Fork a sub-agent from this session: it inherits the completed conversation, which suits trying a second route or another implementation. State only the new requirements in the task',
    'subagent_list': 'List sub-agent sessions (optionally filtered by parent) with state and step count',
    'subagent_many': 'Dispatch several independent sub-tasks in one call (API provider models run in parallel up to agent.subagent_parallel; the web Copilot channel runs sequentially; everything returns together)',
    'subagent_send': 'Add one more round to an existing sub-agent session: it remembers the earlier context, so state only the new requirements',
    'todo_write': 'Write the task checklist (full replace, up to 50 items); use it to track long tasks',
    'update_goal': 'Update goal state: complete / blocked / pause / resume / edit',
    'web_fetch': 'Fetch an HTTP(S) URL as plain text (HTML tags stripped; private addresses refused by default)',
    'workflow': 'Orchestrate several sub-agents in phases (sequential within a phase; API provider models can run in parallel)',
    'copilot_attach': 'Upload a file to Copilot web for analysis and return the answer (for images and complex documents local tools cannot parse)',
    'copilot_file': 'Let Copilot generate a file and download it into the workspace (content does not go through chat messages, so output length limits do not apply: one large file per turn; suits code and Office documents)',
})

ARG_EN.update({
    'outlook_unread.max_count': 'Max messages, default 10',
    'outlook_read.entry_id': 'Message entry_id',
    'outlook_search.keyword': 'Subject keyword',
    'outlook_search.folder': 'inbox / sent / outbox / deleted / drafts',
    'outlook_search.since': 'Only messages after this date (YYYY-MM-DD)',
    'outlook_search.max_count': 'Max messages, default 20',
    'skill_read.name': 'Skill name',
    'memory_write.text': 'The statement itself (self-contained, no diary)',
    'memory_write.kind': 'fact / preference / decision / pitfall',
    'memory_write.tags': 'Tag array',
    'memory_write.pinned': 'true = always visible to every turn',
    'memory_write.scope': 'project (default) or global',
    'memory_search.query': 'Keywords',
    'memory_search.limit': 'Max entries, default 10',
    'memory_search.scope': 'project / global / all',
    'memory_forget.id': 'Memory id',
    'skill_create.name': 'Skill name',
    'skill_create.content': 'Full SKILL.md text',
    'skill_create.description': 'One-line description',
    'skill_create.scope': 'project (default) or global',
    'skill_create.overwrite': 'true = overwrite an existing skill of the same name',
    'ask_user_question.question': 'The question',
    'ask_user_question.options': 'Optional candidate answers',
    'create_goal.objective': 'Goal',
    'create_goal.max_rounds': 'Max continuation rounds',
    'grep.pattern': 'Regex',
    'grep.path': 'Directory to search, defaults to .',
    'grep.include': 'Filename filter, e.g. *.py',
    'grep.max_results': 'Max matches, default 200',
    'job_kill.job_id': 'Job id',
    'job_output.job_id': 'Job id',
    'job_output.wait': 'true = wait for completion',
    'job_output.timeout_seconds': 'Wait timeout in seconds',
    'present.files': 'Array of {path, description}',
    'pwsh.command': 'Command line',
    'pwsh.workdir': 'Working directory (optional)',
    'pwsh.timeout_seconds': 'Timeout in seconds',
    'pwsh.run_in_background': 'true = return a job_id immediately',
    'ralph.objective': 'Goal',
    'ralph.max_rounds': 'Max rounds, default 5',
    'ralph.model': 'Model id (optional)',
    'read_image.path': 'Relative path',
    'read_image.question': 'What to ask about the image (with analyze=true)',
    'read_image.analyze': 'true = let Copilot vision describe it',
    'subagent.task': 'Self-contained task text',
    'subagent.description': 'Short label for the session list',
    'subagent.model': 'Model id (optional)',
    'subagent.max_steps': 'Step cap (optional)',
    'subagent_fork.session_id': 'Parent session id (optional)',
    'subagent_fork.description': 'Short label',
    'subagent_fork.keep_messages': 'How many recent messages to inherit',
    'subagent_list.parent_id': 'Filter by parent session id',
    'subagent_list.limit': 'Max entries',
    'subagent_many.tasks': 'Array of self-contained tasks',
    'subagent_many.model': 'Model id (optional)',
    'subagent_many.max_steps': 'Step cap per sub-agent',
    'subagent_many.stop_on_error': 'true = stop the rest on first failure',
    'subagent_send.session_id': 'Sub-agent session id',
    'subagent_send.message': 'New requirements',
    'subagent_send.model': 'Model id (optional)',
    'subagent_send.max_steps': 'Step cap',
    'todo_write.todos': 'Array of {content, status} where status is pending / in_progress / completed',
    'update_goal.action': 'complete / blocked / pause / resume / edit',
    'update_goal.objective': 'New goal (only with action=edit)',
    'web_fetch.url': 'HTTP(S) URL',
    'web_fetch.max_chars': 'Max characters returned',
    'workflow.spec': 'Workflow specification (stages and sub-tasks)',
    'workflow.model': 'Model id for sub-agents (optional)',
    'copilot_attach.path': 'Relative path',
    'copilot_attach.question': 'What to ask Copilot about the file',
    'copilot_file.path': 'Target path in the workspace',
    'copilot_file.instruction': 'What Copilot should generate',
})
