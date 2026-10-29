# Gantry

> **Language**: **English** · [简体中文](README.zh-CN.md) · [繁體中文](README.zh-TW.md)

> **Rename notice**: This project was formerly **CopilotBridge Agent** and has been renamed to **Gantry** since 11.0.0.
> The Python package `cbp` → `gantry`; environment variables `CBP_*`, request headers `X-CBP-*`, and the Copilot session marker
> `[CBP-AGENT]` remain compatible (write the new `[GANTRY-*]`, both sets are accepted on read), so old sessions and old scripts are unaffected.

> **Works without Copilot**: the browser starts on demand — service startup never touches the browser, and the WebUI plus provider models (DeepSeek/Kimi/OpenAI, etc.) are usable immediately; the browser is only launched when Copilot is actually needed, and a status with a "Retry browser" button is shown at the top of the UI. **Note**: in lazy mode, the first Copilot task after a restart must wait for the browser to launch and for SSO (can take several minutes on enterprise tenants); that waiting time does not count toward the task timeout. Provider models are unaffected.

> **Version**: the `main` branch is currently **11.0.0-preview.30** (the multi-window parallel preview line has been merged in).
> The old stable line is archived at tag `v10.2.1-archive` / branch `archive/10.2.1-stable`.
> For versioning rules, release process, and multi-machine collaboration conventions, see [docs/VERSIONING.md](docs/VERSIONING.md);
> for changes in each release, see [CHANGELOG.md](CHANGELOG.md).

A bridge that wraps the **Microsoft 365 Copilot web app** as a local service: it exposes an **OpenAI-compatible Chat API** and ships with a built-in **long-running task Agent** that can read and write local files and execute a validation loop. Model calls do not go through any official API — Playwright automates your local Edge browser and directly uses your already-logged-in Copilot web session.

```
Client ──HTTP──> FastAPI (app.py) ──Playwright──> Edge persistent Profile ──> m365.cloud.microsoft/chat
                     │
                     ├─ /v1/chat/completions      Normal chat (OpenAI protocol, streaming supported)
                     ├─ /v1/agent/completions     Agent mode (tool calls + long tasks)
                     ├─ /v1/agent/sessions/*      Session management / pause / resume / delete
                     └─ /ui                       Built-in web console
```

> **Just want to get it running fast**: see the [one-page quickstart](docs/QUICKSTART.md) (install → login → start → first task),
> or just save a card image: [简体](docs/quickstart-zh.png) ｜ [繁體](docs/quickstart-zh-TW.png) ｜ [English](docs/quickstart-en.png) (regenerate with `python scripts/make_quickstart_card.py`).
> One step deeper: see **Quick Start** below (4 commands) → **Operations Manual** (how to use the WebUI, how to write AGENT.md/skills, how to roll back, how to self-upgrade, troubleshooting).

## Features

- **OpenAI compatible**: `/v1/chat/completions` supports stream / non-stream and works with any OpenAI SDK client; `/v1/models` lists available models. The model list is based on a built-in table (Auto / Quick response / Think deeper / GPT / GPT-5.6 / Sol / Think, extensible via config), and **at startup it automatically opens the Copilot model menu to enumerate options (including second-level submenus under parent items like GPT)** — newly appearing models are merged in automatically (IDs like `copilot-gpt-5-7`), and submenus are also expanded automatically when selecting a model; you can also rescan anytime with `POST /v1/models/refresh`.
- **API provider integration**: besides Copilot web automation, you can plug in any OpenAI-compatible HTTP API directly (built-in presets for OpenAI / DeepSeek / Kimi / Grok, customizable). Model IDs look like `deepseek/deepseek-chat`, API keys come from environment variables; usable for **both normal chat and Agent mode (including the tool loop)** — the JSON action protocol is purely prompt-driven and provider-agnostic.
- **Agent mode**: each Copilot turn returns one JSON action (a tool call or the final answer); tools are executed locally and results are sent back, looping until the task is done. **Batch actions supported**: one reply can carry multiple tool actions (`{"type":"actions","actions":[...]}` or a bare action array), executed in order and billed step by step — writing several small files or making several small replacements no longer costs one Copilot turn per edit, saving many turns; `final` is not allowed inside a batch (completion must be returned separately so the validation loop cannot be bypassed), the action count cap is controlled by `max_actions_per_turn`, and if a batch is truncated by the output limit, the already-complete leading actions are still executed.
- **15+ workspace tools**: directory/file read and write (Base64 writes to prevent JSON escaping corruption), exact replace, copy/move/delete, `validate_python`, `run_unittest`, `run_python` — all confined to the workspace root.
- **Copilot generates files directly (`copilot_file`)**: have Copilot produce the result as a **downloadable file**, and the bridge downloads it into the workspace — the content **never goes through a chat message**, so it completely bypasses Copilot's per-message output limit: a large file arrives in a single turn, no more b64 chunking. Supports `blob:`/`data:` links and "Download" buttons (clicked and captured via the browser download event); before writing to disk the original image is still snapshotted (rollback-able), and `.py/.json/.html` writes still go through the validation loop. Toggle: `agent.allow_copilot_file` (default on). When no download entry is found, `last_answer.html` is saved for troubleshooting, with a hint to fall back to chunked writes.
- **Two-level project context (global + project)**: both `AGENT.md` and `skills/` have two scopes — **global** lives in the service install directory (applies to all workspaces), **project** lives in the workspace root; the two are merged on injection, project-level wins for same-named skills/conventions, and identical files are auto-deduplicated. Global skills live outside the workspace and are read with the `skill_read` tool (`skill_list` lists them). Each `AGENT.md` is truncated at 8000 characters by default (`agent_md_max_chars`); SKILL.md supports YAML frontmatter (`name`/`description`), falling back to "first level-1 heading + first paragraph". This repo itself ships a global [AGENT.md](AGENT.md) and example skills.
- **Service self-upgrade (optional, off by default)**: with `agent.allow_self_upgrade: true`, the Agent can use `self_read_text`/`self_write_text_b64`/`self_append_text_b64`/`self_replace_text` to modify the **service's own code** directly (even when the workspace is another project), with a whitelist limited to `gantry/`, `webui/`, `scripts/`, `docs/`, `tests/` and a few top-level files — `config.json`/`data/`/`EdgeProfile/` are forbidden; after writing, both `self_validate` + `self_unittest` must pass (an early final is blocked by the `validation_gate`), and a `self_rollback` tool is provided to roll back the self-upgrade.
- **Change snapshots and two-level rollback (self-upgrade failback)**: **before** a write executes, the target's original image is stored in `data/snapshots/<sid>/` (workspace) and `data/snapshots/_self_<sid>/` (service code); the earliest state of each path is kept **and versions are counted per Copilot turn (one complete = one version)**, so you can roll a whole task back to "before the task started", or roll back per turn. Entry points: WebUI "More ▾ → Roll back task changes… / Roll back service self-upgrade…", `POST /v1/agent/sessions/{sid}/rollback` (optionally with `{version}`), `POST /v1/agent/self/rollback?last=true`, plus the **offline channel for when the service/WebUI won't start**: `python scripts/rollback.py [--scope self] --last --yes [--rev N]` (if the `app.py` import fails, guidance is printed automatically). Self-upgrade writes drop a `data/.self_upgrade_pending` marker that is archived automatically after a service restart.
- **M365 file and software integration**:
  - **Office document tools**: `read_excel`/`write_excel` (openpyxl), `read_docx`/`write_docx` (python-docx), `read_pptx` (python-pptx), `read_pdf` (pypdf) — the Agent can analyze/modify xlsx, docx, pptx, pdf directly;
  - **Copilot file upload**: the `copilot_attach` tool attaches a workspace file to the Copilot web attachment box and retrieves the analysis result, for files that can't be parsed locally such as images/scans/complex layouts;
  - **Outlook integration**: `outlook_unread`/`outlook_read`/`outlook_search`/`outlook_send`. **Defaults to OWA web automation** (reuses the bridge browser and SSO session; New Outlook has no COM, EWS is disabled by Microsoft from 2026, Graph needs admin consent — the web app is currently the only viable path); machines with classic Outlook installed automatically keep the COM fallback. Reading is on by default, sending is controlled separately by `allow_outlook_send`. In OWA mode, `outlook_read` locates messages by subject keywords (there is no entry_id concept).
- **Long-task resilience**:
  - A unique marker `[GANTRY-TURN:...]` is injected every turn; the answer is only read after delivery is confirmed, eliminating stale answers from the previous turn;
  - Three failure classes are strictly distinguished — **rate limiting** (in-place exponential backoff retry), **conversation length limit reached** (summarize progress, switch to a new session and resend), **malformed answer** (tolerant parsing + continue in the same session after compacting context);
  - **Session governance model**: M365 Copilot's conversation length limit is undisclosed and changes dynamically; local compaction only shortens future prompts, **it does not shorten the remote conversation**, so governance relies on a combined approach — continuous local compaction + conservative thresholds to proactively switch sessions + page-limit detection as a passive fallback + checkpoint handoff (summary/modified files/validation results carried into the new session);
  - **Idempotency protection**: on session-switch handoff or turn resend, a non-idempotent operation (append/move/copy) identical to the last successful action is skipped, avoiding duplicate file modifications;
  - **Measured calibration**: at the end of each Copilot session, measured data (turn count / estimated tokens / character count / end reason / model / URL) is recorded into the `conv_stats` table, queryable via the session detail API, for continuously calibrating local thresholds;
  - Forced validation loop after code changes: completion may only be claimed when both `validate_python` and `run_unittest` pass.
- **Session persistence**: SQLite (WAL) stores the step timeline, file-change hashes, and conversation messages; after a service restart, work can continue from a checkpoint.
- **Web console**: `/ui` provides task launch (button + dialog), live progress (four tabs: execution status / current answer / sent content / continue conversation), a full-width step timeline (JSON exportable), pause/resume/cancel, session archiving, and remote Copilot session deletion (with same-session markers to prevent accidental deletion plus a reconciliation final check). See the [Operations Manual](#1-webui-怎么用).

## Quick Start

```bash
git clone https://github.com/ogowoo/gantry.git
cd gantry
python install.py
```


Requirements: Windows + Microsoft Edge + Python 3.11+ (on other platforms, change `browser_channel` to `chrome` or omit it to use the bundled Chromium).

```powershell
# 1. Install dependencies (pip + Playwright browsers)
python install.py

# 2. Copy the config and edit as needed
copy config.example.json config.json

# 3. Log in to Copilot (opens Edge; press Enter when login is complete)
python scripts/login.py

# 4. Start the service (default http://127.0.0.1:8765)
python start.py
```

After startup, open `http://127.0.0.1:8765/ui` to use the console, or visit `/docs` for the API documentation.

### Multiple accounts: switch to an account with an M365 subscription

The login state is saved in `EdgeProfile/`, and the Windows account picker (WAM) often **automatically suggests the personal account currently signed into the system**, causing the bridge to use the wrong account. Ways to switch (pick one):

1. **Switch directly in the automation window** (when the service runs in windowed mode): in the automated Edge Copilot page, click the avatar at the top right and "Sign out", then sign in with the subscription account — the login state is saved automatically, no service restart needed;
2. **Re-run the login wizard** (stop the service first): `python scripts/login.py`; when the account picker pops up, be sure to click "Use another account" and enter the subscription account;
3. **Start fresh when the login state is messed up**: `python scripts/login.py --reset` deletes EdgeProfile and logs in from scratch.

### Normal chat (OpenAI SDK)

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
resp = client.chat.completions.create(
    model="copilot-auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### API providers (DeepSeek / Kimi / OpenAI / Grok …)

Model IDs use the `provider/model` format, e.g. `deepseek/deepseek-chat`, `kimi/kimi-k2-0905-preview`, `openai/gpt-4o`, `grok/grok-4`. **API keys are configured via environment variables** (not written into the config file, not committed to git):

```powershell
$env:DEEPSEEK_API_KEY="sk-..."   # or MOONSHOT_API_KEY / OPENAI_API_KEY / XAI_API_KEY
```

Models without a configured key are marked "(API Key not configured)" in `/v1/models`.

> Want to use the **DeepSeek web app** as a model too (free/fallback): see [three integration approaches and the shipped plugin](docs/PROVIDER-deepseek-web.md) — just run a reverse proxy and enable the `deepseek-web` plugin; model IDs look like `dsweb/deepseek-reasoner`. To define any custom OpenAI-compatible provider (including a self-hosted gateway):

```json
"providers": {
  "myapi": {
    "base_url": "https://example.com/v1",
    "api_key_env": "MYAPI_KEY",
    "models": ["my-model"],
    "description": "自建网关"
  }
}
```

Provider models are available at every entry point: OpenAI-compatible chat (streaming pass-through), WebUI new tasks (normal chat / Agent execution), and session follow-ups. Normal chat uses the session's message history as context; Agent mode reuses the same JSON action protocol and tool loop. Provider rate-limiting/5xx automatically follows the same exponential-backoff retry as Copilot.

**Proxy (important)**: browser access to APIs goes through the **Windows system proxy**, while Python's httpx by default only reads the `HTTP_PROXY`/`HTTPS_PROXY` environment variables and **ignores the system proxy** — this causes a fake failure where "the browser can open it but the service reports the provider unavailable". Provider requests now **follow the system proxy by default** (read from the registry), and if a transport-layer failure occurs and the proxy was auto-detected, it **retries once with a direct connection**. You can also configure explicitly:

```json
"providers": {
  "deepseek": { "proxy": "http://127.0.0.1:10809" },   // explicit proxy
  "kimi":     { "use_system_proxy": false }            // force direct connection (for proxy failures)
}
```

**One-command connectivity self-check** (prints the real results of both the "via proxy" and "direct" paths):

```powershell
python scripts/provider_selftest.py                    # all configured providers
python scripts/provider_selftest.py --provider deepseek
```

### Agent tasks

```bash
curl -X POST http://127.0.0.1:8765/v1/agent/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "copilot-auto",
    "messages": [{"role": "user", "content": "分析工作区项目并生成 TODO 清单"}],
    "workspace": "C:/path/to/project"
  }'
```

## Secrets and environment variables (.env)

- **Never commit secrets in `config.json`** — it was tracked by git historically (now untracked).
- Put keys in `.env` (git-ignored) and reference them from `config.json` as `"${VAR}"`;
- Precedence: **system environment variables > .env > literals in config.json**
  (existing system variables are never overwritten by .env);
- Template: `.env.example`. If `config.json` still has a plaintext secret, startup logs a warning.

## Configuration (config.json)

### Top level

| Field | Default | Description |
|---|---|---|
| `copilot_url` | `https://m365.cloud.microsoft/chat/` | Copilot entry URL |
| `browser_channel` | `msedge` | Playwright browser channel (`msedge` / `chrome` / omit for bundled Chromium) |
| `headless` | `false` | Headless mode; automatically falls back to windowed mode if startup fails |
| `reply_timeout_seconds` | `180` | **No-progress** timeout: a timeout is only declared when the reply has had no new text for this long |
| `reply_hard_timeout_seconds` | `1200` | Hard per-turn cap (fallback to prevent waiting forever) |
| `stable_seconds` | `3` | How long the reply text must stay stable to be considered finished |
| `poll_interval_seconds` | `0.35` | Page polling interval |
| `diagnostics` / `diagnostic_html` | `true` / `false` | Save a `last_page.txt` (or `.html`) snapshot on errors |
| `default_model` | `copilot-auto` | Default model ID |
| `server.host` / `server.port` | `127.0.0.1` / `8765` | Listen address. **Binding a non-loopback address without setting api_token makes the service refuse to start** |
| `api_token` | empty | Once set, all `/v1/*` endpoints require `Authorization: Bearer <token>` (or `?token=`) |
| `models` | `{}` | Extend models: `{"copilot-custom": {"ui_names": ["Custom"], "description": "..."}}` |
| `markers` | see below | Copilot page accessibility-text anchors; append wording here for other UI languages |

`markers` values are **merged into** the built-in default list (not overriding it); any hit takes effect.
The built-in defaults already cover **English + Chinese + Japanese** (`Copilot said:` / `You said:` / `Sources` /
`出典` / `あなたの発言:` / `Copilot の回答:` etc.); button/overlay wording
(`stop` / `new_chat` / `send_aria` / `dismiss`) can also be appended here:

```json
"markers": {
  "assistant_turn": ["Copilot said:", "Copilot の回答:"],
  "user_turn": ["You said:", "あなたの発言:"],
  "answer_end": ["\nSources\n", "\nMessage Copilot\n", "\n出典\n"],
  "sources": ["Sources", "出典"],
  "ignored_lines": ["Copy", "复制", "更多", "コピー", "その他"],
  "stop": ["Stop generating", "停止生成", "生成を停止"],
  "new_chat": ["New chat", "新建聊天", "新しいチャット"],
  "send_aria": ["Send", "发送", "送信"],
  "dismiss": ["Accept", "接受", "同意する"]
}
```

- `assistant_turn` / `user_turn`: turn-delimiter text, used to locate the latest reply and count user turns — **when reading times out because the UI is in a language other than English/Chinese, append anchors for that language here first**;
- `answer_end`: end markers of the reply body (sources section, input prompts, etc.);
- `sources`: a section title whose appearance in the reply is treated as generation complete;
- `ignored_lines`: leftover button text stripped from the reply;
- `stop` / `new_chat` / `send_aria` / `dismiss`: wording anchors for stop-generating, new-chat, send button, and popup dismissal (on Japanese systems, the measured "can send but never receive a reply" issue is exactly this anchor group not matching).

> The default wording for other languages such as Japanese is **built in based on common phrasing**; if your test tenant uses different wording,
> just append via `markers` — no code changes needed.

### agent section

| Field | Default | Description |
|---|---|---|
| `enabled` | `true` | When disabled, the Agent endpoints return 503 |
| `workspace` | `workspace` | Tool root directory (relative to the project directory) |
| `workspace_roots` | `[]` | Workspace allowed-roots whitelist (array of absolute paths); when non-empty, switching workspaces is restricted to the whitelist, preventing the sandbox from being moved wholesale |
| `session_store` | `data/agent.db` | SQLite session database path |
| `max_total_steps` | `1000` | Total step cap per task (hard ceiling 10000) |
| `max_steps_per_conversation` | `80` | Step cap within a single Copilot conversation (triggers a session switch) |
| `max_conversation_rollovers` | `25` | Maximum number of Copilot session switches |
| `context_token_budget` | `80000` | Estimated context budget (a local estimate; **does not represent the real M365 limit**) |
| `compact_at_tokens` | `35000` | Compact history into a summary once reached |
| `rollover_at_tokens` | `55000` | Switch to a new session once reached (requires `auto_rollover`); must satisfy `0 < compact < rollover ≤ budget` |
| `keep_recent_steps` | `8` | Number of recent steps kept during compaction/session switch |
| `max_summary_tokens` | `5000` | Estimated cap for the long-term summary |
| `max_context_chars` | `240000` | Character-count fallback threshold |
| `auto_rollover` | `true` | Recommended on: local compaction does not shorten the remote conversation, so proactive session switching at conservative thresholds is the main line of defense, with page-limit detection as a passive fallback |
| `min_steps_before_rollover` | `20` | Minimum step threshold before a proactive session switch |
| `auto_compact` | `true` | Automatically compact context when thresholds are reached; with `false`, only manual compaction remains |
| `auto_resume` | `true` | Automatically resume sessions interrupted (not user-paused) by a service restart |
| `session_timeout_seconds` | `86400` | `ready`/`waiting` sessions inactive longer than this are marked `expired` |
| `completed_session_retention_days` | `30` | Finished (completed/cancelled/failed/expired) sessions are auto-archived after this many days |
| `max_sessions` | `100` | Cap on unarchived sessions; beyond it, archiving starts from the least recently updated |
| `max_parse_failures` | `5` | Force context compaction after this many consecutive parse failures |
| `max_actions_per_turn` | `4` | Maximum actions executed per reply (batch protocol, hard ceiling 12); raising it saves turns but makes truncation by Copilot's output limit more likely |
| `allow_copilot_file` | `true` | Whether to enable `copilot_file` (let Copilot generate files directly and download them into the workspace; content bypasses chat messages and the output limit). **Note**: `copilot_file` and `copilot_attach` depend on the Copilot web session, so they are **automatically disabled** in sessions using an API provider model (e.g. `deepseek/…`) — neither injected nor executed, avoiding wasted turns |
| `copilot_file_model` | `""` | The specific model used when `copilot_file` generates files. When the session model is `Auto`, Auto usually only chats and never produces a download entry (measured); setting this (e.g. `copilot-gpt-5-6-sol-think-deeper`) switches automatically; leave empty to keep the session model and get an explicit reason on failure || `max_service_unavailable` | `30` | Cap on rate-limit backoff retries; beyond it, the task enters a safe pause |
| `service_retry_base_seconds` / `service_retry_max_seconds` | `30` / `600` | Start/cap of the rate-limit exponential backoff |
| `service_retry_window_seconds` | `3660` | **Copilot web** rate-limit window: roughly how long to wait from the first over-limit until usable again (measured ~1 hour, plus a 1-minute margin); the **centered countdown card** is anchored to "first over-limit + this window". **API providers do not use this window** (their 429/5xx/network glitches usually recover in seconds to minutes, so applying 1 hour would mislead); instead, "next retry" is shown per the current backoff; the card title shows "Copilot rate-limited/unavailable" or "API service rate-limited/unavailable" depending on the current model channel |
| `max_read_chars` / `max_write_chars` | `120000` / `200000` | Per-call read/write caps for tools |
| `allow_delete` | `false` | Whether to enable the `delete_path` tool |
| `allow_python` | `false` | Whether to enable the `run_python` tool (**can execute arbitrary code — enable with caution**) |
| `allow_office` | `true` | Whether to enable the Office document tools (Excel/Word/PPT/PDF) |
| `allow_memory_write` | `true` | Whether to allow the Agent to deposit **long-term memory** with `memory_write` (cross-session facts/preferences/conclusions, automatically retrieved and injected by relevance; `memory_forget` deletes) |
| `browser_startup` | `lazy` | Browser startup policy: `lazy`=start on demand (WebUI and provider models work without Copilot); `eager`=warm up at service startup |
| `allow_skill_write` | `true` | Whether to allow `skill_create` to deposit skills (project-level writes to the workspace `skills/`; global-level additionally requires `allow_self_upgrade`) |
| `allow_outlook` | `false` | Whether to enable Outlook read tools (unread/body/search) |
| `allow_outlook_send` | `false` | Whether to enable `outlook_send` (sends real emails — **enable with caution**) |
| `outlook_url` | `https://outlook.office.com/mail/` | OWA automation entry (used when `allow_outlook` is on) |
| `python_timeout_seconds` | `20` | `run_python` timeout |
| `snapshot_retention` | `50` | How many sessions' change snapshots to keep; the oldest are cleaned up beyond this |
| `agent_md_max_chars` | `8000` | Injection cap per `AGENT.md` (global and project counted separately) |
| `allow_self_upgrade` | `false` | Whether to allow the Agent to modify the **service's own code** (`self_*` tools — **high privilege; understand the risks before enabling**) |
| `self_upgrade_paths` | see `config.example.json` | Path whitelist reachable by self-upgrade (relative to the install directory) |

## Command Line (headless)

```bash
pip install -e .                    # afterwards the gantry command is available (or python -m gantry.cli)

gantry serve --port 8765               # start the service (equivalent to python start.py)
gantry status                          # health check
gantry run "List the dead links in docs" --model kimi-coding/k3
gantry run "Refactor X" --plan             # plan mode: read-only exploration → stops after producing a plan
gantry run "Analyze this repo" --sandbox read-only --workspace C:/proj
gantry sessions --limit 10             # session list
gantry tools                           # 62 tools
gantry plugins list|enable|disable|reload [name]
gantry mcp list                        # MCP server status
gantry mcp add --name fs --command npx --arg -y --arg @modelcontextprotocol/server-filesystem --arg C:/data
gantry sandbox read-only --persist     # switch the sandbox level and write it back to config.json
```

- Shared parameters: `--url` (default `http://127.0.0.1:8765`, or `CBP_URL`), `--token` (or `CBP_TOKEN`)
- The CLI **does not start a browser in-process**: it calls the HTTP interface of the same service and sees the same facts as the WebUI
- `--json` prints the raw response for easy script consumption; `--quiet` suppresses the step summary
- When the service is unreachable it returns a non-zero exit code and tells you to run `gantry serve` first

## API Overview

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check (includes busy status) |
| GET | `/v1/models` | Model list |
| POST | `/v1/models/refresh` | Re-enumerate the Copilot model menu and merge in new models |
| POST | `/v1/chat/completions` | OpenAI-compatible chat |
| POST | `/v1/agent/completions` | Start/continue an Agent task |
| GET | `/v1/agent/sessions` | Session list |
| GET | `/v1/agent/sessions/{sid}` | Session details (steps / file events / messages) |
| GET | `/v1/agent/sessions/{sid}/runtime` | Live runtime status (polling endpoint for when SSE is unavailable) |
| GET | `/v1/uploads/{name}` | Serve "pasted attachment" images (filename under `data/uploads`; strict filename validation, separators and `..` rejected; protected by api_token, frontend img tags use `?token=`) |
| POST | `/v1/shutdown` | **Terminate the service** (the WebUI ⏻ button). Responds before shutting down; graceful shutdown (uvicorn `should_exit`), falls back to SIGINT if the server object is unavailable, with a hard-exit backstop after a grace period; idempotent |
| GET | `/v1/agent/sessions/{sid}/events` | **SSE real-time push** (`text/event-stream`): first sends a runtime snapshot, then pushes `runtime`/`queue`/`messages`/`session` events; sends a heartbeat after 15 s without events; auto-unsubscribes when the client disconnects. `api_token` may be passed via `?token=` |
| POST | `/v1/agent/sessions/{sid}/messages` | Append a message to a session (Agent or plain chat mode); **returns immediately on enqueue** with `queued/position`, processed in order in the background (the next round starts only after the current one finishes) |
| POST | `/v1/agent/sessions/{sid}/inject` | **Force-inject an instruction** (DSH Ctrl+J style): written into `pending` and **immediately interrupts the current turn** — provider sessions cancel the underlying HTTP request (stateless; cancelled means "this round never happened"); Copilot web sessions **only abort the wait if the answer has not started generating** (once generation has started, let it finish to avoid losing the answer / misalignment). Then continues per the new instruction; when the session is idle it automatically becomes a queued follow-up |
| GET | `/v1/agent/sessions/{sid}/queue` | View the session's queue (waiting items + the one being processed, the latter marked `processing`; the UI only shows waiting items) |
| POST | `/v1/agent/sessions/{sid}/queue/{item_id}` | Edit the content / model of a queued message (kept in sync with message history) |
| DELETE | `/v1/agent/sessions/{sid}/queue/{item_id}` | Delete a queued message (kept in sync with message history) |
| POST | `/v1/agent/sessions/{sid}/actions/{pause\|resume\|cancel\|compact\|rollover\|archive}` | Session control |
| POST | `/v1/agent/sessions/{sid}/activate` | Switch the automation browser to that session's page |
| DELETE | `/v1/agent/sessions/{sid}?delete_remote=true&force_local=true` | Delete a session (optionally also delete the Copilot web session) |
| POST | `/v1/agent/sessions/{sid}/model` | Switch the model used by this session (persisted; takes effect on the next turn; still used after a service restart) |
| GET | `/v1/agent/sessions/{sid}/snapshots` | Snapshot list of the task's changes (rollback-able paths/types + per-turn version list `versions`) |
| GET | `/v1/agent/sessions/{sid}/prompt` | View the session's prompts: `initial_prompt` (full first-turn content, incl. system prompt / tool list / project conventions) and `last_prompt`; for old sessions where it was never persisted, approximately rebuilt from the current workspace with `rebuilt=true` |
| POST | `/v1/agent/sessions/{sid}/rollback` | Roll back the workspace changes of this task; optional body `{version:N}` undoes the changes made after turn N (returns 409 while running) |
| GET | `/v1/agent/self/status` | Self-upgrade status (enabled or not, whitelist, pending-restart flag, snapshot scope) |
| GET | `/v1/agent/self/snapshots?sid=` | Snapshot list of self-upgrade changes |
| POST | `/v1/agent/self/rollback?sid=&last=` | Roll back the service's own code (`last=true` takes the most recent one) |
| GET/POST | `/v1/agent/workspace` | Query / switch the tool workspace |
| GET/POST | `/v1/agent/browser` | Query / switch browser window display (headless ↔ windowed; restarts the browser context while keeping login state, `persist=true` writes back to config.json) |
| GET | `/v1/agent/browse?path=` | One-level directory listing (subdirectories only), for the working-directory picker; without `path` it starts from the current workspace |
| POST | `/v1/agent/browse/mkdir` | Create a subdirectory in the picker (single-level names only; separators / `..` rejected) |
| POST | `/v1/agent/pick-directory` | System folder dialog (native). **Note**: a process spawned by the service may inherit a non-interactive window station, making the dialog invisible — times out with a 400 after 15 s; the WebUI only uses in-app browsing |
| POST | `/v1/agent/deep-research` | One-click deep research: `{query, model, angles}` → evidence pack + one direct synthesis → report with sources (saved as a `research-*` session + a workspace md file) |
| GET | `/v1/plugins` | Plugin list (loaded/enabled state, permissions, contribution points) |
| POST | `/v1/plugins/{name}/{action}` | Plugin operations: `enable` / `disable` / `reload` |
| ANY | `/v1/plugins/{plugin}/api/{path}` | Plugin custom endpoints (e.g. `web-search`'s `api/search`) |
| GET | `/ui` | Web admin console |

## Operation Manual

- `sandbox_mode`: `read-only` / `workspace-write` (default) / `danger-full-access`.
  Under `read-only`, all mutating tools (file writes / command execution / office writes / sending email…) are rejected before execution;
  `danger-full-access` allows writing outside the workspace, but the **service install directory stays protected** (unless `allow_self_upgrade` is enabled).
- At runtime you can query / switch it via `GET/POST /v1/agent/sandbox` (`persist:true` writes back to config.json).

### 1. How to use the WebUI

Open `http://127.0.0.1:8765/ui` (a hard refresh with `Ctrl+F5` is recommended to make sure the latest frontend is loaded).

| Location | Action |
|---|---|
| Sidebar top **＋ New task** | **Opens the new-session editor** (no longer a modal): large input box + model + working directory (📁 opens the directory picker, defaults to the workspace) + **Agent run / Plain chat** + "Start"; `Enter` sends, `Shift+Enter` inserts a newline |
| **`/` commands** in the input box | Typing `/` opens the command palette (↑↓ to select, Tab/Enter to complete, Esc to close): `/research question` (deep research), `/goal goal` (goal mode), `/plan task` (plan mode), `/agent` (switch to Agent mode), `/chat` (switch to plain chat), `/help` (list commands). With arguments, Enter executes immediately; without arguments it completes and waits for input |
| Workspace groups under **SESSIONS** in the sidebar | Sessions are grouped by **workspace** (group header: collapse arrow + folder icon + name + session count); click the header to collapse/expand (state stored in localStorage); the **＋** on the right of the header = create a session in that workspace; hover the workspace name to see the full path |
| Grouping of sub-agent sessions | Child sessions are **collapsed under their parent session** (`▸` on the left of the parent row; expanding shows them indented, state stored in localStorage); deleting a parent session **cascades** to its sub-agents, leaving no orphans |
| Sidebar **SESSIONS** | Session list (leading source marker: ⏱ scheduled task / 🔎 deep research / 子 sub-agent; status dot: green=running, yellow=waiting/paused, red=failed, blue=completed); `Filter…` filters by title/ID/**workspace**; icon row: **＋ New session**, **📁 New workspace**, 📖 user manual, ▣ toggle the automation browser window, ◐ cycle the theme (**follows the system by default**; cycle: system → dark → amber → jade → light → eye-care beige → mint, 3 dark / 3 light; the choice persists across refreshes and automatically follows system theme changes), etc. |
| Slim handle on the sidebar's left edge | Collapse/expand the whole sidebar (the session list fills the width) |
| Top action bar | Pause / Resume / Compact context / **Deep research** (multi-angle search + body fetching → report with sources, new session) / Continue / Switch to page (jumps the automation Edge to this session) / Open session (in your own browser) / Cancel task / More ▾ (plugin management, archive, roll back task changes, roll back service self-upgrade, delete record, delete together with the Copilot session) |
| **Model selector** on the right of the top bar | Shows **the current session's own model** and can switch it directly; persisted to that session, **takes effect on the next turn and is kept after service restarts**. Options come from the available models of `/v1/models`; if the session's original model is no longer in the menu, it is marked "(not found in menu)" |
| ID chip in the top bar | Shows the current session ID; click to copy (for verification before deletion) |
| KPI bar | Status, total steps (incl. sub-agents), current session steps, sessions (which Copilot conversation, counted the same way as the sidebar), token estimate (incl. sub-agents, with a budget progress bar) |
| Goal / Plan bar | In plan mode shows the plan status; with a goal shows `🎯 Goal (round r/max · status)`, with todos shows `☑ Plan done/total`; the whole bar is hidden when there is no goal/plan |
| Live panel (four tabs) | **Current execution status** (phase / wait details / counters), **Current answer** (the model's latest answer; structured actions are auto-converted to a JSON view, plain answers are rendered as Markdown; **grows character by character** when the provider streams; a green dot appears on the tab when there are updates), **Recently sent content** (the tail of the raw text sent to the model), **Continue conversation** (see below) |
| Message edit / delete | Hovering any persisted message in the conversation shows ✎ / 🗑: edit changes the content, delete removes the record. **Honest semantics**: only the local history is changed; content already sent to web Copilot cannot be recalled; **provider sessions** sync the in-memory conversation history (`_PROVIDER_AGENT_HISTORY` accumulates incrementally rather than being rebuilt each turn — without syncing, the model would still see the old text on the next turn), and the sync result is stated in the notice (`history_synced`); if the entry has already been pruned from memory it says so honestly instead of pretending success |
| 🔍 Cross-session search | The sidebar search icon opens the panel (click again / Esc / auto-collapses after jumping); it searches **the conversation bodies of all sessions** (backend `GET /v1/search`; LIKE wildcards are escaped — searching `100%`/`_` is literal); results list session title, role, time and the matched snippet; **clicking a result jumps** to that session and scrolls that message into the middle of the view with a brief highlight. Archived sessions do not participate in search |
| 🏷 Session tags & grouping | Hovering a session card shows 🏷: set a tag (leave empty to clear). The sidebar list **groups by tag** (tagged sessions first, untagged ones collected into a last group); the filter box matches title/ID/status/tag at the same time. Tags are a purely local organization device (`POST /v1/agent/sessions/{sid}/tag`) and do not participate in Agent logic |
| Timeline filters | Two dropdowns in the timeline header: **by tool** (options are automatically collected from the tools that appear in the current steps) and **by status** (success only / failed & blocked only / events only); counts are shown as `m/n items (filtered)`. With no filter it still renders incrementally (expansion state is preserved); the whole table is rebuilt only when the filter changes |
| Streaming bubble in the conversation area | Besides the "Current answer" panel, provider streaming output also **shows a temporary "generating" bubble in the conversation area** (dashed border + temporary label, sourced from the `answer_preview` SSE pushes), automatically replaced by the real message once the final message is persisted. It is deliberately styled differently from final messages to avoid mistaking a half-streamed JSON action for the final answer |
| ⬇ Export conversation Markdown | "Export MD" in the chat bar exports the current conversation as `.md` (header with session ID, model, export time, message count + sections by role + timestamps + attachment references), handy for pasting elsewhere or archiving |
| Tool result dual view | Each timeline step's "View result" offers **Rendered / JSON** views: the rendered view lays short scalars out as key-value rows, and renders long text and objects with **exactly the same code-block component as the conversation** (the language is inferred from the file extension in the arguments or from the tool name, e.g. `gantry/demo.py` → Python; `validate_python` → Python), so it has the same language label, syntax highlighting, line numbers, folding, copy and "…" menu; the JSON view keeps the original structured browsing |
| 🌐 Multi-language (Simplified / Traditional / English) | The sidebar language button cycles (`简 → 繁 → EN`); the choice is stored in localStorage and persists across refreshes; switching immediately repaints the session list, conversation, timeline and all panels. **Coverage**: all static UI + all dynamic copy (toasts, dialogs, panels, memory/skills, rollback & delete details, demo data, code-block and timeline labels). **Implementation trade-offs**: the Chinese source text is used as the key (`tr('取消任务')`), and missing translations fall back to the source — the UI can never go blank because of a mismatched key; English is handwritten, while **Traditional Chinese is auto-converted via a simplified→traditional phrase + character table** overlaid with a Taiwan-usage override table (記憶體/檔案/設定/匯出/停機/強行/手冊…). Only display copy is translated — **strings inside decision logic are never translated**; **HTML attributes generated by JS are translated at the DOM layer** (`translateAttrs()`, without touching the source — wrapping attributes in template/concatenated strings with `tr()` is extremely error-prone and broke three times in practice). Backend and Agent-protocol copy stays in Chinese; the notes of major APIs carry a `note_code`, which the frontend localizes accordingly |
| ⚡ Sub-agent parallel fan-out | **True parallelism for API provider models**: `subagent_many` / `POST /v1/agent/fanout` run 2–6 mutually independent subtasks concurrently under a cap (`agent.subagent_parallel`, default 4); results are merged in order, and a single failure does not affect the rest; **the web Copilot channel parallelizes by the number of automation windows** (`agent.copilot_windows`, 1–4): each sub-agent owns a tab and the parent task owns one, so the concurrency cap is "windows − 1"; with 1 window it stays sequential (same as before). The prerequisite for parallelism was turning "current session / whether web tools are allowed / sub-agent recursion depth / page" from global variables into **task-level context** (contextvars), otherwise concurrent sub-agents would overwrite each other |
| Conversation reading experience | Message cards (avatar / role name / timestamp / hover-to-copy); assistant messages are **visualized** by content shape: JSON action → **tool card** (tool name + reason + argument JSON view; `content_b64` shown as "[Base64 content, N chars]"), batch actions → one card with multiple tools, `{"type":"final"}` → body rendered as **Markdown** and marked "Final answer", other JSON → JSON view, and ```json code blocks also use the JSON view; JSON that cannot be parsed is clearly marked "JSON parse failed (raw text)" instead of pretending nothing happened |
| Markdown code blocks | Aligned with Copilot's look and feel: left-side **line numbers** (stay fixed during horizontal scrolling), **language display name** (PowerShell/Python/…), **soft wrap** (wraps by default, can be turned off in "…"), copy icon and the "**…**" menu (copy code / **download as file** (extension based on language) / line-number toggle / wrap toggle / fold). **Clicking a line number selects the whole line** (multiple lines possible), and the code bar then shows "**Copy selected N lines**" (the copied text excludes line numbers). **Syntax highlighting for 11 languages** (python/js/ts/json/bash/powershell/sql/yaml/html/css + ```diff/patch: additions in green, deletions in red, @@ hunk headers in blue); folds beyond 22 lines; the preference is remembered |
| Images & attachments | Markdown `![alt](url)` renders as an image (**https/http and data:image only**; svg and `javascript:` are not allowed), click to zoom; broken links show an "image failed to load" placeholder instead of a broken-image icon; **pasted image attachments** show a thumbnail under the message, click to zoom (the message line records the filename; served via `GET /v1/uploads/{name}` under unified auth); the **session list** shows a 📎 badge for sessions with attachments |
| Other reading aids | Long answers over 1800 characters are folded; **🔍 in-conversation search** (highlights matches, Enter/Shift+Enter to jump; never matches the line-number column); newly arrived messages fade in (respects `prefers-reduced-motion`). All hand-written with zero dependencies, no CDN (works offline) |
| ⏻ Terminate service / exit | Available both at the power icon on the sidebar's top-right (**reachable in any state**) and in the session's "More" menu; clicking pops a **second confirmation** (explaining that running tasks will be interrupted and that state is persisted and resumable). The backend `POST /v1/shutdown` **responds before shutting down** (otherwise the frontend would see a connection reset that looks like a failure), uses uvicorn **graceful shutdown** (`should_exit`, exit code 0), falls back to SIGINT if the server object is unavailable, and always has an "8-second grace period then hard exit" backstop — the button must really be able to stop the service. After shutdown the page stops all polling and shows a "Service terminated" banner |
| Provider streaming output | When `stream_provider_agent` (default `true`) is on, provider turns go streaming: increments are written into `answer_preview` as they arrive and **pushed to the UI in real time via SSE** (see below) — genuinely character-by-character growth. **Streaming still carries `response_schema`** (the parse rate of the structured-action protocol is not sacrificed); it falls back to non-streaming only when **not a single increment was received** (resending after content was received would cause double billing / two answers) |
| SSE real-time push | The WebUI connects `EventSource` to `GET /v1/agent/sessions/{sid}/events`, and the server **pushes** runtime snapshots, queue changes, new messages and session-list changes — normally no polling is needed. On connect it first sends a current snapshot; a heartbeat (`: ping`) every 15 idle seconds prevents middleboxes from cutting the connection; on disconnect it auto-unsubscribes (leaving no memory behind) and **falls back to polling** (keeps 350 ms fast polling + reconnect with backoff). `api_token` goes via `?token=` (EventSource cannot carry custom headers; the auth middleware already supports it) |
| 📄 Initial prompt | A button in the panel header shows the **complete prompt actually sent to the model on the first turn** (system prompt + tool list + project conventions + verification-loop rules + task) — the first place to look when investigating "why did the Agent do that". The first-turn content is persisted (`initial_prompt`) and is no longer overwritten by later turns; for **old sessions** where it was never persisted, it is **approximately rebuilt** from the current workspace and marked in the title |
| 🧾 Task completion card | After a task finishes (completed/failed/cancelled), the bottom of the conversation shows "N files changed this run +X −Y": per-file paths with line-level additions/deletions (aligned with DSH's *Edited N files*), larger changes first; more than 6 files are folded into "All N files". Clicking a row **shows the unified diff** (reuses the viewer overlay, copyable); deliverables registered via `present` are listed as chips. The data comes from **pre-write snapshot originals** (no git dependency); binary files (xlsx/docx/images) only have their paths listed. APIs: `GET /v1/agent/sessions/{sid}/changes`, `GET …/changes/diff?path=` (only paths recorded in snapshots are allowed — arbitrary file reads are prevented) |
| Message queue | Messages sent in "Continue conversation" **return immediately on enqueue** without blocking the UI; sending a message while the task is running queues it and shows "Queued (N ahead)", and replies are flushed into the chat area automatically once processed. The chat page queue bar lists only **waiting** messages (the one being processed is not shown; watch the runtime status panel for progress), and allows **per-item edit/delete** (✎ / ×, kept in sync with message history). The runtime status panel shows "Queue: N pending". Pasting images (screenshots) to send along with a message is supported |
| ⚡ Force-inject (**Ctrl+J**) | While a task is running, use **Ctrl+J** (or the "⚡ Force-inject" button in the chat area) to insert an instruction and **immediately interrupt the current turn**: provider sessions directly cancel that HTTP request (stateless; cancelled means "this round never happened"); Copilot web sessions **only stop waiting if the answer has not started generating** (once generation has started, let it finish to avoid losing the answer, and to avoid the "abandoned answer" being misaligned with the reply to the new instruction). The injection mode is selectable (remembers the last choice): **Obey immediately** (`queue=step`: enters the next-step queue, appends to the current execution, and **preemptively abandons the remaining actions mid-batch**) / **Replan the whole turn** (`queue=turn`: the current turn is voided and the turn is replanned per the new instruction). Injections are written into message history and leave `user_inject`/`user_interrupt`/`step_preempted` steps in the timeline; when the session is idle it automatically becomes a queued follow-up |
| New topic generates files directly | On the **first turn** of the Copilot web channel a suggestion is attached: when a task needs to produce new files, prefer a single `copilot_file(path, instruction)` to have Copilot generate and download it — the file content never passes through chat messages and is not bound by the per-message output cap (a large file arrives in one turn; no `write_text_b64` chunking and no truncation). First turn only, and only on the Copilot channel (provider sessions do not have this tool; suggesting it would just burn turns); it is **not written into the system prompt** (that must stay byte-stable to hit the prompt cache). Also: `copilot_file` has been moved into the core write tools and is **exempt from keyword pruning** — it used to be in the copilot group (keywords = images/screenshots/attachments), so any "generate a file" task would prune it away, the model could not see the tool, and this path was effectively blocked |
| Step timeline | Each step carries time and status; "View result" expands into the **Rendered / JSON dual view** (see above); "⬇ Export" at the top-right downloads the complete timeline JSON (the first choice for troubleshooting) |
| PATCH / DELETE | `/v1/agent/sessions/{sid}/messages/{mid}` | Edit / delete a message (only changes the local history; provider sessions sync the in-memory context and return `history_synced`) |
| GET | `/v1/search?q=&limit=` | Cross-session search of conversation bodies (LIKE wildcards escaped; archived sessions excluded; limit capped at 100) |
| POST | `/v1/agent/sessions/{sid}/tag` | Set a session tag (`{"tag": "..."}`; empty string clears it; used for grouping the list by tag) |

### Long-term memory (cross-session)

Besides AGENT.md and skills, the Agent also has a set of **long-term memory**: scattered facts
verified in past tasks are accumulated and automatically retrieved and injected for later related
tasks (keyword + Chinese bigram matching, zero dependencies).

- Writing: the Agent calls `memory_write` itself when wrapping up; you can also ask it to record something anytime during a task.
- Retrieval: `memory_search`; only the **most relevant few entries** are injected into the prompt (not the full set); on conflicts, the current facts win.
- Deletion: `memory_forget`, or in the WebUI / via the API `DELETE /v1/agent/memory/{id}`.
- Scope: `global` (all tasks) and `project` (current workspace only), with no leakage between them.
- Management: `GET /v1/agent/memory?query=&scope=` to view the list and statistics; capped at 500 entries per scope,
  beyond which entries are evicted by pinned → use count → update time; duplicate writes are merged automatically.
- When to use memory instead of AGENT.md: hard conventions that must be followed **in full every turn** go in AGENT.md;
  retrievable experience like "what was done, what pitfalls were hit" goes in memory.

### 2. Writing AGENT.md and skills (two scope levels)

| Scope | Location | Effective range |
|---|---|---|
| **Global** | Service install directory: `AGENT.md`, `skills/<name>/SKILL.md` | All workspaces, all tasks |
| **Project** | Workspace root: `AGENT.md`, `skills/<name>/SKILL.md` | This project only |

- The two `AGENT.md` files are **merged on injection** (each ≤8000 characters, adjustable via `agent_md_max_chars`), identical paths are auto-deduplicated, and the **project level takes precedence**
- Skills use **progressive disclosure**: only "name + one-line summary" goes into the prompt; when the Agent judges one relevant it fetches the full text with `skill_read`; a project-level skill overrides a same-named global one
- **The Agent can distill skills on its own**: when wrapping up a task it first checks with `skill_list` whether a related skill exists; if not, it creates one with `skill_create` (by default written to the project level at `workspace/skills/<name>/SKILL.md`; writing to the global `skills/` requires `allow_self_upgrade`). Skill creation takes effect immediately (it appears in the next task's context) with no restart needed; disable the entry point with `allow_skill_write: false`
- SKILL.md supports YAML frontmatter, falling back to "first level-1 heading + first paragraph" by default:

```markdown
---
name: webui-change
description: Structure conventions and verification checklist for WebUI changes
---

# WebUI 修改规范
（正文：Agent 读到全文后照着执行）
```

- The Agent can also **update these two kinds of files itself** (to distill effective conclusions). Such changes are marked `instruction_update` on the timeline and can be undone via rollback.

### 3. Rollback (restore something you changed)

Before any write operation **executes**, the target file's original image is automatically saved to `data/snapshots/`. Versions are counted by **Copilot round**—one complete produces one version (multiple writes within the same round count as the same version), so you can either roll the whole task **back to before it started**, or **roll back to a specific round** (undoing changes made after that round). Three channels:

```powershell
# ① WebUI: session "More ▾ → Roll back task changes…" (when multiple round versions exist they are listed in groups;
#    enter 0/leave blank = roll back the whole task; enter a version number = roll back to that round; refused while running, pause first)

# ② HTTP API
curl -X POST http://127.0.0.1:8765/v1/agent/sessions/<sid>/rollback -H "Content-Type: application/json" -d '{"version":3}'
#    No body or empty version = roll back the whole task

# ③ Offline CLI (works even when the service/WebUI can't start)
python scripts/rollback.py --list                      # See what can be rolled back
python scripts/rollback.py --last --yes                # Roll back the most recent task's workspace changes
python scripts/rollback.py --sid <id> --versions        # List the available round version points
python scripts/rollback.py --sid <id> --rev 3 --yes     # Roll back to round 3
python scripts/rollback.py --scope self --last --yes   # Roll back the most recent "service self-upgrade"
```

Rules: modified files have their contents restored, files created by the task are deleted; per-path retained **round versions** are capped at 6 (the earliest plus the most recent few), 8MB per file, 800 entries per session (overflow goes into `skipped`, and those paths are not rolled back).

### 4. Service self-upgrade (off by default)

After setting `agent.allow_self_upgrade` to `true` in `config.json` and restarting, the Agent gains the `self_*` tools and can modify **the service's own code** (even if the current workspace is another project):

```powershell
# Confirm status after enabling
curl http://127.0.0.1:8765/v1/agent/self/status
```

Example task for the Agent: "Read gantry/tools.py, raise the max_count limit of outlook_search from 100 to 200, then self-upgrade, validate, and run the unit tests."

Built-in guardrails:

1. **Whitelist**: `gantry/`, `webui/`, `scripts/`, `docs/`, `tests/` plus a few top-level files; `config.json`, `data/`, `EdgeProfile/`, `.git/` are always refused
2. **Validation gate**: after writing, both `self_validate` (syntax check of the entire install directory) and `self_unittest` (the service's own unit tests) must pass; returning final early is intercepted by the `validation_gate`
3. **Separate snapshots**: self-upgrade changes are stored in `data/snapshots/_self_<session>/`, rolled back separately from workspace changes
4. **Restart marker**: a write drops `data/.self_upgrade_pending`; after you restart the service the marker is automatically archived, and the startup log prints "Loaded last round's self-upgrade changes"
5. **Failure fallback**: if an upgrade breaks the service so it can't start, the rollback command is printed directly when `app.py` fails to import; restore with `python scripts/rollback.py --scope self --last --yes` (this command does not depend on the service)

> ⚠️ Self-upgrade means handing the service code to the model to modify. Recommendations: make sure `tests/` runs first; have the Agent do only one thing at a time; after an upgrade, restart first to see whether it starts and runs tests normally, and only continue with the next change once confirmed.

### 5. Large files: have Copilot generate them directly, then download
The traditional path for writing large files is b64 chunking (each chunk ≤3000 characters, constrained by Copilot's per-message output limit). A much easier approach is to have Copilot generate the file itself and bridge the download:

```json
{"type":"tool","tool":"copilot_file","arguments":{
  "path": "minesweeper.py",
  "instruction": "实现一个可运行的扫雷游戏，命令行版，带难度选择"}}
```

- The content **does not pass through chat messages**, so there is no output limit, no truncation, no chunking—one round yields the complete file
- The bridge recognizes two kinds of entry points: `blob:`/`data:` links and "Download" buttons; when multiple entry points exist, the filename match wins
- The original image is saved as usual before writing to disk (rollback works); `.py/.json/.html` still must pass `validate_python` + `run_unittest` to count as done
- To turn it off: `agent.allow_copilot_file: false`
- **Model requirement**: generating the download entry point requires a **non-Auto** model (Auto usually only answers in chat). When the session model is Auto, configuring `agent.copilot_file_model` (e.g. `copilot-gpt-5-6-sol-think-deeper`) automatically switches to it; if not configured, the tool explicitly reports "Auto produces no download entry point", and the Agent can fall back to chunked writing accordingly.
- When it reports "no download entry point found", look at `last_answer.html` in the service directory (a live snapshot of the answer area) and send the structure to the maintainer; meanwhile the Agent automatically switches to chunked writing

**When to use which**: content generated by Copilot (writing a new large file / Office document) → `copilot_file` is cheapest; content comes from existing local material and needs precise control (byte-for-byte rewrites, binary transfers) → local tools + batch actions (`{"type":"actions",...}`, multiple small actions per answer) are more reliable.

#### One-shot mode (first round of a new session, saves rounds)
When a new session uses Copilot, the Agent's first-round prompt asks for "**one-shot completion**" instead of step-by-step exploration:

1. Directly use `copilot_file` to have Copilot generate the **complete file** in one go (for multiple files, call it several times in a row); don't list/read to explore one by one, and don't assemble via b64 chunks;
2. Once the file is obtained, run and verify it **locally** right away (`run_python`/`validate_python`/`run_unittest`);
3. On errors, **fix locally first** (locate with `read_text` + fix with `replace_text`); only after two local fix rounds still fail do you ask Copilot with the **specific error**, targeting just the broken spot (not regenerating the whole file).

The goal is to deliver one task in **1–3 Copilot rounds**. The protocol also adds a "local closed loop" rule: things that can be done locally—running, testing, small fixes—are not sent back to Copilot. The system prompt itself stays byte-stable (prompt cache); the first-round prompt is only added to that round's user message.

### 6. Extended tools (modeled on DSH's tool surface, a standalone module)

`gantry/tools_extra.py` is a **decoupled extended-tools module** (delete the file + remove one call line in the bridge to take the whole thing offline),
offered by group with per-group switches (`agent.tools_extra`):

| Group | Tool | Description |
|---|---|---|
| `search` | `grep` | Search by **content** with regex (`find_files` only matches filenames), returns `file:line:content` |
| `web` | `web_fetch` | Fetch the body of an HTTP(S) URL (HTML tags stripped automatically); **refuses intranet/local addresses by default** (SSRF protection) |
| `shell` | `pwsh` | Execute PowerShell commands (**requires one-time approval by default**); `run_in_background=true` starts a background job |
| `shell` | `job_list` / `job_output` / `job_kill` | Background job list / read output (can wait) / terminate |
| `plan` | `todo_write` | Task checklist (track progress on long tasks) |
| `plan` | `present` | Declare deliverables (up to 4 files, for WebUI display) |
| `vision` | `read_image` | Image metadata (format/dimensions/size) + hand to Copilot for visual description |
| `ask` | `ask_user_question` | Ask the user mid-task and **block waiting**; the WebUI pops up a question bar and continues after submission |

Configuration (`config.json` → `agent`):

```json
{
  "allow_shell": true,             // pwsh master switch (false disables everything)
  "shell_timeout_seconds": 120,
  "allow_web": true,               // web_fetch master switch
  "allow_private_web": false,      // true allows fetching intranet/local addresses
  "question_timeout_seconds": 600, // ask_user_question wait limit
  "tools_extra": {"search": true, "web": true, "shell": true,
                  "plan": true, "vision": true, "ask": true}
}
```

Security conventions: `pwsh`, like `delete_path`/`outlook_send`/`run_python`, goes through **one-time approval**
(when `approval_policy: "ask"` the WebUI authorizes each time, single-use); the visual analysis of `read_image` and
`ask_user_question` depend on the Copilot webpage/WebUI, and degrade gracefully when the corresponding channel is unavailable (return metadata / a clear error).

### 5.6.2 Plugin install / upgrade / uninstall (versioned)

```bash
gantry plugins install ./my-plugin           # Local directory
gantry plugins install ./pkg.zip             # zip (rejects ../ escaping entries, prevents zip-slip)
gantry plugins install https://x/y.git       # git clone --depth 1
gantry plugins registry                      # Installed versions/sources/backups
gantry plugins uninstall my-plugin           # Uninstall (keeps a backup by default)
```

HTTP equivalents: `POST /v1/plugins/install {source, name?, force?}`, `POST /v1/plugins/uninstall`,
`GET /v1/plugins/registry`.

- **Version comparison**: a newer version overwrites the older one and is **automatically backed up** to `data/plugin-backups/<name>-<time>/`
- **Downgrades are refused by default** (to avoid accidentally swapping new features back to an old version); use `force=true` / `--force` if a downgrade is truly needed
- Loaded immediately after install (tools/APIs/panels all take effect together), no restart needed
- Uninstall first detaches the runtime contributions (tools/routes/panels) and then deletes the directory

### 5.6.1 Plugin UI: declarative panels

Plugins can add **panels** to the interface (`permissions` must include `ui`), but only declaratively:

```python
ctx.register_panel({"id": "schedules", "title": "定时任务", "view": "table",
                    "api": {"method": "GET", "path": "api/list"}})
```

- `view` ∈ `kv` / `list` / `table` / `json` / `markdown`; plugins only provide a **data API**,
  rendering is done by the frontend—**plugins may not inject HTML/JS** (plugins are third-party code; allowing it would open an XSS surface)
- Panel entry: plugin management dialog → "Plugin Panels", or `GET /v1/plugins/panels`
- Uninstalling a plugin also removes its panels (no entry points left that 404 when clicked)
- Built-in examples: `schedule`'s "Scheduled Tasks" table, `mcp`'s "MCP Servers" JSON

### 5.7 Slash commands (`/`)

Starting the new-session input box with `/` enters command mode (the panel is keyboard-operable):

| Command | Aliases | Action |
|---|---|---|
| `/research <question>` | `/深研`、`/dr` | Deep research: multi-angle search + fetch full text → report with sources (new session) |
| `/agent` | `/执行` | Switch to **Agent execution** mode |
| `/chat` | `/对话` | Switch to **normal chat** mode |
| `/help` | `/?`、`/帮助` | List all commands again |
| `/goal <goal>` | `/目标` | **Goal mode**: `create_goal` first, then advance over multiple rounds; returning final while the goal is unfinished automatically continues (round cap defaults to 10); `update_goal` wraps up on achievement/blockage |
| `/plan <task>` | `/计划` | **Plan mode (enforced read-only)**: write operations during the exploration phase are rejected by `SANDBOX_PLAN_MODE`; the model submits a proposal with `exit_plan_mode(plan)`, which **ends the round**, and execution waits for your confirmation |
| `/server.template-name args…` | — | **MCP prompt template**: automatically generates commands from templates provided by MCP servers; the rendered result is loaded into the input box (does not appear when MCP is not configured) |

- Commands are registered in `SLASH_COMMANDS` in `webui/js/panels.js`; adding a new command just means appending one `{id,label,aliases,desc,run}` entry
- Enter without arguments = completion waiting for input; Enter with arguments = execute immediately
- Panel text goes through i18n (the i18n audit proactively expands panels to check)

### 5.6 Plugin system (semantically compatible with DSH plugins)

Gantry plugins are "**semantically compatible** with DSH" rather than "directly running DSH plugins": DSH plugins are npm packages + Cordis
(JS `apply(ctx)`) running in Node; Gantry is a Python service. What is aligned is the **contract**—same-shaped manifest,
the same set of contribution points, the same load→apply→unload lifecycle, the same `id + disabled + config` enable/disable semantics.
(Directly running JS plugins would require embedding Node, which is not within this system's goals.)

**Manifest** (`plugins/<name>/plugin.json`; a `"gantry"` field inside `package.json` is also accepted,
and the `"dsh"` field is passed through verbatim for migration reference):

```json
{
  "name": "web-search",
  "version": "0.1.0",
  "description": "联网搜索",
  "dsh":   {"bundle": {"patch": "./cordis.patch.yml"}},
  "gantry": {
    "entry": "plugin.py",
    "permissions": ["tools", "api"],
    "contributes": {"tools": ["web_search"], "api": ["GET api/search"]},
    "config": {"backend": "auto"}
  }
}
```

**Entry** (`plugin.py`):

```python
def setup(ctx):                      # ctx ≈ DSH/Cordis's ctx
    ctx.register_tool("web_search", handler, "说明", {"query": "..."})
    ctx.register_api("GET", "api/search", handler)   # → /v1/plugins/<name>/api/search
    ctx.on("step_done", callback)                    # session_start / step_done / session_end
def teardown(ctx): ...
```

**ctx capabilities**: `register_tool` / `register_provider` / `register_api` / `on` /
`state` + `save_state` / `read_text` + `write_text` (limited to the plugin directory) / `log`.
Permissions `tools/providers/api/ui/events/storage/files` must be declared in the manifest, otherwise calls raise an error.

**Management**: WebUI "More ▾ → Plugin Management…" can enable/disable/reload; corresponding APIs
`GET /v1/plugins`, `POST /v1/plugins/{name}/{enable|disable/reload}`.
Enable/disable state is recorded in `data/plugins.json`, **no service restart needed** (disabling detaches the tools/providers/routes/events).

**Built-in plugins (core capabilities of a modern bot, enable as needed)**

| Plugin | Capability | Dependencies |
|---|---|---|
| `web-search` | `web_search` web search | DuckDuckGo key-free; optional BRAVE/TAVILY/SERPER_API_KEY |
| `research` | `deep_research` multi-angle parallel search + full-text fetching, returns an evidence bundle | None |
| `schedule` | `schedule_add/list/remove/run` scheduled tasks (every / daily / once), run the task in a **separate new session** at the scheduled time; `workspace` specifies the working directory (defaults to the current workspace) | None |
| `image-gen` | `image_generate` text-to-image, written to `workspace/generated/` | DASHSCOPE_API_KEY or OPENAI_API_KEY |
| `mcp` | **MCP client**: connects to MCP servers (stdio / HTTP), registers tools and resources as `mcp__<server>__<tool>` | None (local process or HTTP endpoint) |

**Combined = Grok-style bot**: real-time search (web-search) + deep research (research) + scheduled tasks (schedule) +
image generation (image-gen) + cross-session memory (memory) + subagents/orchestration (subagent/ralph/workflow).

> Image generation requires a **valid** API key: the existing `DASHSCOPE_API_KEY` on this machine tested as 401 (invalid);
> switch to a valid key or use `OPENAI_API_KEY` instead; the plugin returns a structured error instead of a 500.

**Built-in example: `web-search`** (a core capability of a modern bot)
- Tool `web_search(query, max_results=6)`: DuckDuckGo key-free; if
  `BRAVE_API_KEY` / `TAVILY_API_KEY` / `SERPER_API_KEY` are configured they are preferred automatically
- API `GET /v1/plugins/web-search/api/search?q=...&n=6`

### 5.8 MCP (Model Context Protocol)

Gantry acts as an **MCP client**: connect to external MCP servers and register their tools and resources into the Agent, so the model can call them just like built-in
tools. Two transports are supported:

| Transport | Configuration | Notes |
|---|---|---|
| **stdio** | `{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","C:/data"]}` | Local process, the mainstream approach |
| **HTTP** | `{"url":"https://host/mcp","headers":{"Authorization":"Bearer ..."}}` | streamable HTTP; responses of either `application/json` or `text/event-stream` (SSE) are parsed; `Mcp-Session-Id` is maintained automatically |

```bash
# stdio
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/add \
  -H 'Content-Type: application/json' \
  -d '{"name":"fs","spec":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","C:/data"]}}'

# HTTP + auth header
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/add \
  -H 'Content-Type: application/json' \
  -d '{"name":"remote","spec":{"url":"https://example.com/mcp","headers":{"Authorization":"Bearer xxx"}}}'

curl http://127.0.0.1:8765/v1/plugins/mcp/api/list            # Connection status / tools / resources
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/call \
  -H 'Content-Type: application/json' \
  -d '{"server":"fs","tool":"read_file","arguments":{"path":"C:/data/a.txt"}}'
curl 'http://127.0.0.1:8765/v1/plugins/mcp/api/resources?server=fs'          # Resource list
curl 'http://127.0.0.1:8765/v1/plugins/mcp/api/resources?server=fs&uri=file:///C:/data/a.txt'  # Read a resource
```

**Capability mapping**
- `tools/list` → each tool is registered as `mcp__<server>__<tool>`; parameters are automatically converted from `inputSchema` into parameter descriptions
- `tools/call` → `content[].type=text` is concatenated into the result; `isError` is converted into a tool error
- `resources/list` → each server additionally registers one read-only tool `mcp__<server>__read_resource(uri)`,
  with the available uris listed in the tool description (avoids registering dozens of resources as individual tools and blowing up the tool table)
- `resources/read` → text is returned directly; binary (`blob`) reports the mime and base64 length
- Generic tools: `mcp_list` (status), `mcp_call` (manual call), `mcp_read_resource` (read a resource)
- `prompts/list` → each server with templates registers `mcp__<server>__use_prompt(name, arguments)`;
  the **WebUI also automatically generates slash commands**: `/server.template-name arg1 arg2 …`, with the rendered result filled directly into the input box
- `prompts/get` → the message sequence is concatenated into text (`[role] content`)
- Generic tool: `mcp_get_prompt(server, name, arguments)`

**Robustness**
- A single server failing to start is only recorded in `errors` and does not affect other servers or built-in tools
- `resources/list` is an optional capability: probed with a **short 8-second timeout** (servers that don't implement it may never reply;
  not probing would waste 30 seconds waiting during the connect phase)
- Configuration is persisted in `mcp.state.servers` in `data/plugins.json`, and reconnects automatically on restart

> Not yet supported: the legacy **HTTP+SSE dual-endpoint** transport (old 2024-11-05 spec; only streamable HTTP is supported).

### 5.3.4 Todo checklist: automatic sync from TODO files + dispatching subagents by checklist

A real user pain point (the pyshot remediation): after saying "complete the remediation per the todo", **the panel checklist stayed at 0/5**,
while the 4 entries in the subagent panel were leftovers from the previous phase—"like decorations".

**① Automatic TODO file sync** (`gantry/todo_sync.py`)
- At the start of each step, checks `*todo*.md` files in the workspace by **mtime** (`.txt` also recognized), parses `- [ ] / - [x]`
  checkboxes (degrades to a `- item` list when there are no boxes), and writes them into the session `todos`;
- **Does not overwrite progress**: an item already `completed` is not pulled back by a `[ ]` in the file; completed items
  deleted from the file are kept; when the file disappears, the existing checklist is **not cleared**;
- Previously Gantry completely ignored `pyshot_todo.md` (a 23KB plan file); the panel only recognized `todo_write`.

**② Automatic subagent dispatch by checklist** (`gantry/auto_team.py`, **off by default**)
- Conditions: `agent.auto_team_enabled=true`, there are **≥2** `pending` items whose **scopes don't overlap**
  (judged mutually independent by file paths/identifiers), and no subagent is currently running;
- Action: turns these items into self-contained task texts (goal, acceptance criteria, reporting format) handed to `fanout`;
  the subagent id is recorded back onto the corresponding todo, so the checklist shows "who is running what";
- Why it's off by default: each subagent is an independent Copilot conversation, **multiplying quota consumption**.

Configuration:

```json
{"agent": {"auto_team_enabled": false, "auto_team_max_agents": 4, "auto_team_max_steps": 30}}
```

**③ Collapsible subagent panel**: "Collapse / Expand" in the title bar; state is stored in `localStorage` and persists across refreshes.

### 5.3.3 Browser reclamation for test scripts

Running UI audit/screenshot scripts repeatedly accumulates leaked Playwright browsers in the system (measured: **21** msedge processes,
each with `--user-data-dir=%TEMP%\playwright_*`). They eat memory and can make people think
"some program is misbehaving". `scripts/reap_browsers.py` reclaims them by age (only kills those older than 15 minutes by default,
so it doesn't affect running scripts), and `scripts/regression.py` calls it automatically at startup.

```bash
python scripts/reap_browsers.py            # Report only
python scripts/reap_browsers.py --kill     # Reclaim
```

### 5.3.2 Long commands and "can't see what's running"

- **`pwsh` timeouts terminate the whole process tree**: a bare `proc.kill()` leaves grandchild processes (`python`/`git`/`pip`)
  still holding the stdout pipe, and `communicate()` never sees EOF—in one measured case a single `pip install PySide6`
  stalled a task for 10 minutes, making the timeout meaningless. Now `taskkill /T /F` (POSIX `killpg`) + a 5s fallback
  retrieves partial output, and the error directly suggests "switch strategy: split into smaller commands / use run_in_background + job_output".
- **The real-time panel shows the step being executed**: `runtime.phase` is set to `executing` when a step starts
  (`Executing step N · tool name`), and returns to `thinking` when it ends. Previously the UI stayed stuck at the
  previous round's "waiting · waiting for Copilot's answer" for the entire execution, looking frozen.
- **Timeline merged into the real-time panel**: `Current execution status / Current answer / Recently sent content / Continue conversation / Step timeline`
  —five tabs share one panel (see everything on one screen, like DSH), and the timeline scrolls within its tab; filtering and export are unchanged.

### 5.3.1 Editing tools: tolerant matching + patches (curing "edit failure → round explosion")

The pain point: when the model copies a snippet with slightly different indentation (tabs/spaces, nesting level) or trailing whitespace, `replace_text` fails to match,
leading to a "read once—guess once" loop that, in practice, dragged one small edit into dozens or even hundreds of rounds and blew up the token count.

- **`replace_text` multi-level fallback matching**: exact → trailing whitespace → **leading-indentation flexibility** → whitespace collapsing.
  A unique match is adopted; the result returns the `match` strategy and a `note` explaining "this was a tolerant match";
  **the new snippet is re-indented to the file's actual indentation** (a tab-indented file won't be written with 4 spaces, and nesting levels won't shift).
- **Failure diagnostics**: no longer just "not found"—it reports **the line/column of the first difference + the original text on both sides**, so the model fixes it in one round.
- **`apply_patch(path, patch|edits)`**: multiple edits in one call, **atomic** (if any block fails, nothing is written).
  Supports Codex-style patches (`*** Begin Patch` / `*** Update File:` / `@@` hunks,
  ` `=context `-`=old `+`=new) or simple `edits: [{old,new,count?}]`;
  each block goes through the same tolerant matching, and the result reports each block's `match` strategy and line number.
- Together with the existing `replace_lines` (edit by line number, no matching at all) and `convert_indentation` (batch indentation conversion),
  the three divide the work: **one spot** → `replace_text`, **multiple spots** → `apply_patch`, **whole sections/indentation** → `replace_lines`.

Measured (real rounds, DeepSeek): adding three comments to a tab-indented `maths.py` with trailing whitespace →
**10 steps, 0 failures**; adversarial scenario (model supplies a 4-space snippet to edit a tab file) → `match=indentation`,
result still in tabs; a two-spot patch applied atomically in one call.

### 5.4.0 Explicit fanout (not dependent on model judgment)

```bash
gantry fanout --task "审计 workspace/core" --task "审计 workspace/webui" --model kimi-coding/k3
```
Or in the input box: `/fanout task1 | task2 | task3`

- Each task gets **one subagent** (an independent session); when done, results are written back to one parent session (`fan-*`) and opened in the UI
- Provider models can run in parallel; the webpage channel still runs sequentially (`fanout` doesn't occupy the parent window; each subagent gets its own window, but the default window count = 1); a single failure doesn't affect the rest, and the failure reason is written into the parent session's messages
- Cap of 8; `POST /v1/agent/fanout` is the same capability, for script/CI calls
- Complementary to "the model decides whether to use subagents": the prompt + runtime hints remind it, while this entry makes it **always happen**

### 5.4.1 Adversarial audit (multiple auditors + a judge, looping until only minor issues remain)

`/audit <audit objective>` (or type `/audit` in the dialog and press Enter): starts an `audit-*` session in the background:

1. **N auditors** (3 different angles by default: correctness / robustness / security / consistency) each review read-only,
   reporting only problems "with evidence" (file + line number + severity p0/p1/p2), output as a JSON list;
2. **The judge** personally verifies every piece of evidence with `read_text`, merges and deduplicates, removes false positives, re-grades,
   and gives a verdict: `clean` (none) / `minor` (only p2 left) / `major` (p0/p1 still present);
3. If major and auto-fix is on (default) → dispatch a fix subagent to fix them one by one (minimal changes + verification loop),
   then enter the next round; **until clean/minor or rounds are exhausted** (default 3 rounds, cap 8);
4. Each round's report and the final summary are written into the audit session's messages (open the session to see them).

The webpage channel executes sequentially (doesn't grab the parent window when the window count is 1); the provider channel runs in parallel per `agent.subagent_parallel`.

HTTP: `POST /v1/agent/audit {objective, target?, auditors?, max_rounds?, auto_fix?, model?}`
runs in the background and returns `{session_id, started}`.

### 5.4 Subagents: create / continue / fork

| Tool | Action |
|---|---|
| `subagent(task, description, model, max_steps)` | Create a new subagent (independent session/context), returns the result when done |
| `subagent_send(session_id, message, ...)` | **Continue** an existing subagent (appends a round in the same context; also accepts `agent_id`) |
| `subagent_fork(session_id, description, keep_messages)` | **Fork** a new subagent from a session (copies the recent conversation to try another path) |
| `subagent_list(parent_id, limit)` | List subagents (status/steps/label) |

- Child sessions record user/assistant messages; click into one in the panel to see "what was asked, what was answered"
- Subagent depth cap is 1 (a subagent cannot spawn further subagents, preventing recursion)
- Inherits the parent session's session-level approvals (no repeated approval popups)
- HTTP: `POST /v1/agent/subagents/{sid}/send`, `POST /v1/agent/subagents/{sid}/fork`

> Implementation note: `subagent_send` **must not** go through `run_agent`—the parent round is holding its own automation window,
> and grabbing the same window again would deadlock (measured: a full 600-second client timeout); it shares
> `_drive_child` with `subagent`, a self-built runner (reuses the parent page when available, otherwise occupies its own window).

### 5.5 Subagent panel (see which subagents exist and what they're running)

Subagents spawned by `subagent` / `ralph` / `workflow` run as independent `sub-xxxxxxxxxx` sessions.
The WebUI has a **subagent panel** below the KPI area (appears only when subagents exist):

- Each row: status dot + subagent description + status/steps/current phase
- Click a row → switch to that child session to see the full timeline; click "Refresh" to pull immediately
- Data comes from `GET /v1/agent/subagents` (filterable by `?parent=<sid>`)
- **Hidden when empty**: both the panel and the todo bar only appear when the current session actually has content; clicking "+ New task" or switching sessions
  immediately clears the previous round's todos/subagents (including the outer empty card), without waiting for the new session's first runtime snapshot.

Child sessions record `parent_id` and `description` (two new columns in the session table, auto-migrated). Subagents **inherit the parent session's
session-level approvals** (if the parent approved pwsh, the subagent doesn't prompt again), but per-parameter one-time approvals are not inherited.

### 6. Common operations commands
```powershell
python start.py                              # Start the service (default 127.0.0.1:8765)
python scripts/login.py                      # Login wizard (--reset starts completely over; refused while the service is running)
python -m unittest discover -s tests         # Regression tests
python scripts/calibrate.py                  # Session threshold calibration (reads measured samples from data/agent.db)
python scripts/outlook_selftest.py           # Outlook step-by-step self-check (includes thread-pool/dedicated-COM-thread comparison)
python scripts/rollback.py --list            # View rollbackable tasks and self-upgrade records
```

### 7. Troubleshooting

| Symptom | Action |
|---|---|
| Service won't start, import errors | Most likely a self-upgrade broke it: run `python scripts/rollback.py --scope self --last --yes`, then restart |
| File only half-written, write interrupted | Export the session timeline from the WebUI and send it to the maintainer; confirm b64 chunking is used (each chunk ≤3000 characters); for large files prefer `copilot_file` to have Copilot generate and download directly |
| Task stuck at "waiting for service" | Rate limiting/service unavailable; automatic retries with exponential backoff; a **countdown card** appears in the center of the screen (anchored to the first over-limit event + `service_retry_window_seconds`, default 1 hour 1 minute, with wording tailored for Copilot vs. API providers) showing the estimated recovery time. The card **can be collapsed with ×** to a small badge in the bottom-right corner (click the badge to expand again), so it doesn't block the top controls and you can still switch the session model during rate limiting; a new round of rate limiting re-expands it automatically. If it doesn't recover for a long time, it pauses safely; click "Resume" after the service recovers. If the message says "message could not be sent" rather than rate limiting, that's not rate limiting—click "Switch to page" to inspect or "Retry browser" to rebuild the page |
| Page can't find the input box/model button | Login state expired: stop the service, then run `python scripts/login.py` (use `--reset` if necessary) |
| Sessions frequently rolled over / context compressed | Run `scripts/calibrate.py` and adjust `compact_at_tokens`/`rollover_at_tokens` as suggested |
| Outlook reports "unavailable" or 0x8000400x | Run `scripts/outlook_selftest.py` to locate the issue; the COM path must go through the dedicated message-pump thread (`_ComWorker`); for the OWA path check the login state |
| Some models missing after refreshing the model list | Click ↻ to see the hint: `scanned=false` means the scan failed; if the scan succeeded but second-level menu models are missing, send the maintainer `last_menu.html` / `last_menu.txt` in the service directory (a snapshot of the menu saved automatically on each scan) |
| Browser-layer anomaly | Check `last_page.txt` in the service directory (a full-page text snapshot taken on error); if necessary, append UI text anchors to `markers` in `config.json` |
| Deleting a session reports "partial failure" | Check the `verified-absent` marker in the result dialog; if it still fails, click "Retry remote and force-clean local records" |

## Security Warnings

1. **API authentication**: by default the service binds to `127.0.0.1` and is only reachable from the local machine; once `api_token` is configured, all `/v1/*` endpoints require a Bearer token. If you bind a non-loopback address without a token, the service **refuses to start**. `agent.workspace_roots` can further restrict the tool workspace to whitelisted root directories.
2. **Indirect prompt injection**: workspace file content read by the Agent enters the Copilot context, and the returned actions are executed directly. **Do not let the Agent process files from untrusted sources**; enabling `allow_python` / `allow_delete` escalates the risk to arbitrary code execution and file deletion.
3. **Login state is sensitive**: the `EdgeProfile/` directory contains your Copilot login cookies. It is excluded by `.gitignore` — do not leak it.
4. **Fault-tolerant file writes**: there is regex-based fault-tolerant recovery for unescaped write-file actions returned by Copilot, and the recovered result may be imperfect; back up important files first. `.py/.json/.html` have a syntax + unit-test validation loop as a safety net; other types do not.
5. **Self-upgrade is a high-privilege switch**: with `allow_self_upgrade=true`, the Agent can modify the service's own code (within the whitelist). Although every change is snapshotted beforehand, blocked by a verification gate if validation fails, and backed by an offline rollback channel, it is still "letting a model modify a running service" — enable it only if you accept that premise and the test suite is available; do not process untrusted files in the workspace while self-upgrade is on.
6. **Credentials and snapshots**: `data/snapshots/` stores the **pre-change originals** of modified files, which may contain sensitive content (e.g. files that once had keys written to them) — check before sending debug data out; do not commit or share `data/` or `EdgeProfile/`.
7. **Cross-site writes (CSRF)**: when the service runs without a token by default, any web page can use `fetch(..., {mode:'no-cors'})` to send simple requests to `127.0.0.1` (without triggering a CORS preflight), thereby cancelling tasks, rolling back the service's own code, or deleting sessions — **not being able to read the response does not mean there are no side effects**. Therefore the built-in `origin_guard` middleware judges write methods (POST/PUT/PATCH/DELETE) by browser signals: `Sec-Fetch-Site: cross-site|same-site` or an `Origin` that is non-local / from a different port always gets **403**; non-browser clients without `Origin`/`Sec-Fetch-*` headers (curl, evaluation scripts) are let through as usual. You can disable it with `agent.csrf_protect: false` (not recommended), and add whitelist entries via `agent.csrf_allowed_origins`.
8. **Security response headers and CSP**: all responses carry `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` (anti-clickjacking — in a local scenario, "tricking the user into clicking delete/rollback" is a real threat) and a CSP. The CSP is
   `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`.
   **`script-src` does not include `'unsafe-inline'` — this is a hard constraint**:
   - Pages **must not** contain inline event handlers (`onclick="..."`): always use `data-act="name"` (`data-act-input` for input-type controls, `data-act-key` for keyboard-type ones), and register the implementation in the `ACTIONS` table in `webui/js/settings.js` (`ACTIONS`) — the dispatcher invokes by `data-act`. A wrong name does not raise an error, it just "does nothing when clicked", so invariant #14 **cross-checks both directions** between `data-act` ↔ `ACTIONS` (missing implementations or dead entries both fail).
   - Inline `<script>` is also **not allowed**: first-paint theme initialization has been externalized to `webui/boot.js`; new static assets must be added to the `/ui/{asset}` whitelist in `gantry/routes_core.py` (currently `boot.js` / `i18n.js`, every `js/<name>.js` and every `css/<name>.css`). Invariant #15 checks that "page references ⊂ whitelist" — a missing entry fails immediately.
   - `style-src` still keeps `'unsafe-inline'`: pages heavily use `style="..."` attributes and dynamically injected `<style>` tags; the impact surface is smaller than scripts, so it is not tightened for now.

## UI Multilingual (Simplified / Traditional / EN)

The 「简 / 繁 / EN」 buttons in the sidebar toolbar cycle through the languages; the choice is stored in `localStorage.gantry-lang`. Implementation points and **constraints**:

- **Chinese source text as the key**: `tr('取消任务')` looks up the `I18N_EN` table in `webui/i18n.js`; when a translation is missing it **falls back to Chinese** — no blank space or `key.not.found` ever appears. Traditional Chinese is generated automatically from a "phrase override table + glyph conversion table".
- **Static UI**: add `data-i18n` / `data-i18n-title` / `data-i18n-ph` / `data-i18n-aria` to elements; there is also a "direct DOM translation" pass (leaf-node text and `title/placeholder/aria-label` replaced via the dictionary).
- **Dynamic content must call `tr()` itself**: containers such as conversation, timeline, answers, and logs are excluded via `I18N_SKIP_IDS` (they may contain model output or user input that happens to equal a key and would be mistranslated); after switching languages, `rerenderForLang()` redraws the dynamic regions.
- **Never write `tr(...)` as a string literal**: `'tr(重试浏览器)'` would make the UI literally display `tr(重试浏览器)`. This kind of mistake is **syntactically legal and unit tests cannot catch it**, so invariant #16 was added for static interception (`gantry/invariants.py`).
- **Coverage audit**: `python scripts/ui_i18n_audit.py` scans the UI shell surface by surface in all three languages (buttons/labels/tabs/table headers/hints/dialogs/placeholders) and reports three classes of problems: Chinese remaining in English mode, raw-key leakage such as `tr(`/`data-i18n`, and **missing traditional-conversion characters** (detected with an independently maintained simplified–traditional mapping table, not relying on i18n.js's own table — in a real incident, 「浏」 was absent from the table, and using the table itself as the detection set revealed nothing).
- **Coverage audit**: `python scripts/i18n_coverage.py` opens 12 UI states one by one in English mode
  (home / new task / manual / plugins / panels / workspace / global search / more / memory / session-more / filled state / model) and collects **visible** Chinese —
  currently **0 occurrences**; `ui_i18n_audit.py` only scans the static shell, so the two complement each other (in practice the former caught 30 extra occurrences: 28 in the memory panel + theme title + workspace selection).
- **Backend error messages**: the `HTTPException` detail is translated by the global handler according to `X-Gantry-Lang` (`gantry/i18n.py` exact table + pattern table,
  unknown text falls back to Chinese); the invariant "all backend error messages have English translations" guards newly added raises.
- **User manual**: two **complete template sets** for Chinese and English (`MANUAL_HTML` / `MANUAL_HTML_EN`, 18 sections each), switched wholesale with the UI language, including an auto-generated table of contents and collapsing of low-frequency sections (collapsed section names also follow the language); the invariant "Chinese and English manual versions have consistent sections" prevents changing only one side.
- **Plugin-provided translations**: plugins declare `gantry.i18n: {en: {...}, tw: {...}}` in `plugin.json`;
  `GET /v1/plugins/i18n` aggregates **loaded** plugins, and the frontend merges them into the dictionary on startup and when plugins are enabled/disabled → third-party plugins' Chinese can be translated too.
- **Traditional-Chinese glossary**: `I18N_TW_OVERRIDE` corrects by word (文件→檔案、数据→資料、插件→外掛、服务器→伺服器…),
  applied **in descending word-length order**, otherwise "服务" would swallow "服务器" (actually hit in practice).
- **System-generated titles**: deep-research session titles are generated according to `X-Gantry-Lang` (`[Research] …` / `[深研] …`); those without a request context, such as cron, still use Chinese.
- **Static attributes**: the invariant "static attribute text has English keys" statically checks whether
  Chinese-containing `title/placeholder/aria-label/alt` attributes in `index.html` all have entries (in practice 6 were added: follow-up message, input, collapse, rate-limiting…).
- **Known trade-offs**: backend and Agent protocol text (system prompts, tool descriptions, step reasons / original error text) stays in Chinese across the board — it is bound to the protocol and evaluations. Traditional Chinese is an automatic **glyph-level** conversion; proper nouns may be imperfect — when refinement is needed, add entries to the `I18N_TW_OVERRIDE` phrase table.

## Known Limitations

- Depends on the Copilot page's accessibility text (e.g. `Copilot said:`) and DOM structure — **Microsoft frontend updates may break the bridge**; when errors occur, check the `last_page.txt` diagnostic snapshot;
- Streaming output for normal conversation is based on common-prefix diffing; if Copilot rewrites already-output text, duplication/omission may occur (Agent mode is unaffected);
- Default of 1 automation window (`agent.copilot_windows: 1`): only one turn runs in the browser at a time;
  this preview branch can run 1–4 windows in parallel, see [docs/MULTI-WINDOW.md](docs/MULTI-WINDOW.md) (at the cost of quota and memory);
- Rollback is **task/scope-level**: if a single task mixes several unrelated changes, rollback reverts the whole block — you cannot pick just one of them;
- Snapshots only cover write operations through the tool layer; paths exceeding the snapshot limits (8MB per file / 800 entries per session) are not recorded and therefore cannot be rolled back.
- **Image generation requires a valid key**: `DASHSCOPE_API_KEY` (Tongyi Wanxiang) or `OPENAI_API_KEY`; the existing local DashScope
  key tested as 401 — without a valid key configured, `image_generate` returns a structured error instead of crashing;
- **System folder dialogs are unavailable inside the service**: processes hosted by the service inherit a non-interactive window station; PowerShell's
  `FolderBrowserDialog` can create a window but it is not on the visible desktop (the request would hang). Therefore the working-directory picker only uses
  **in-app browsing** (`/v1/agent/browse`); `/v1/agent/pick-directory` is retained but returns 400 after a 15s timeout;
- **Deep research is "search + one synthesis"**: it does not do multi-round follow-up questioning; sources are as returned by the search engine, with no guarantee of accessibility.

## Project Structure

```
app.py            Assembly entry (shim: from gantry.api import app, uvicorn load point)
start.py          Service entry
install.py        Dependency installation
gantry/              Core package
  api.py            FastAPI app and middleware/startup assembly (endpoints split into routes_*)
  routes_core.py    System and entry endpoints (health/models/chat/UI/shutdown-restart)
  routes_config.py  Settings endpoints (config/sandbox/plugins/browser/workspace)
  routes_agents.py  Subagent/deep-research/audit/memory/self-upgrade endpoints
  routes_sessions.py Session endpoints (list/messages/continue/queue/events/approvals/delete)
  bridge.py         Config, runtime state, tool assembly and wiring (facade re-exporting submodules)
  agentrun.py       Turn orchestration: run_agent main flow, answer waiting, turn markers/cache, todo sync
  downloads.py      Answer-area download parsing and copilot_file
  workspace_files.py Workspace file writing / directory creation / directory picking
  research.py       Deep-research tasks, plugin tasks, fan-out
  page.py           Page automation: new conversation, model menu, sending messages, fetching answers / streaming updates
  browser.py        Browser lifecycle and window pool (multi-window parallelism, provider logical slots)
  answer.py         Answer parsing: turn markers, noise/thinking placeholder removal, service-unavailable detection
  provider.py       External provider channel (HTTP turns, history trimming)
  queue.py          Session message queue (enqueue/cancel/insert)
  orchestration.py  ralph / workflow / adversarial audit
  subagents.py      Subagent dispatch, driving, continuation and fork
  copilot_delete.py Copilot sidebar session deletion flow (URL identity + same-owner marker check + reconciliation final verification)
  owa.py            Outlook Web App (OWA) automation backend
  agent.py          Agent core loop, state governance, verification gates, session switching/compaction
  agent_prompt.py   PromptMixin: system prompt assembly / AGENT.md / skills / tool-surface trimming
  agent_parse.py    ParseMixin: JSON action protocol / fault-tolerant parsing / batch recovery
  agent_text.py     TextMixin: token estimation / spill / keep-head-keep-tail compaction
  tools.py          Tool registry/dispatch, path sandbox and permission gates (core mixin)
  tools_files.py    File read/write/replace/patch/indent/directory operations
  tools_exec.py     validate_python / run_unittest / run_code / run_python
  tools_office.py   Excel / Word / PPT / PDF
  tools_outlook.py  Outlook (COM + OWA)
  tools_skills.py   Skill library list/read/create
  tools_memory.py   Long-term memory
  tools_self.py     Self-upgrade (read-write/validate/unit-test/rollback)
  providers.py      API provider integration layer (OpenAI/DeepSeek/Kimi/Grok etc.)
  session_store.py  SQLite session persistence, interruption recovery, session runtime statistics
  snapshots.py      Pre-change original snapshots and task-level rollback (self-upgrade failback)
webui/            Zero-dependency web console (index.html + css/*.css + js/*.js ES modules)
AGENT.md          Project-level Agent conventions (auto-injected into the system prompt when present at the workspace root)
skills/           Directory-based skill library (one SKILL.md per skill, loaded on demand)
scripts/          Utility scripts (login.py login wizard, calibrate.py threshold calibration,
                  outlook_selftest.py COM self-test, rollback.py offline rollback)
tests/            Unit/integration/invariant tests (python -m unittest discover -s tests, ~1030 cases)
```

## Disclaimer

This project uses the Copilot web version via browser automation and is for personal learning and research only. Please comply with the Microsoft 365 Copilot terms of use; do not use it for high-frequency, commercial, or terms-violating purposes.

## One-Time Approval for Destructive Operations (fail-closed)

Threat model: once `delete_path` / `outlook_send` / `run_python` are mistakenly invoked by the model, the consequences cannot be rolled back.
Therefore the default is `agent.approval_policy: "ask"`: **every** execution requires the user to grant one authorization first.
The authorization is precise down to "tool + argument hash" and is **burned after use** (no allow-always, no revocation storage).

```powershell
# View pending approvals (including approval_pending recorded for the session)
curl http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
# Grant a one-time authorization (when args_hash is omitted, authorizes exactly per approval_pending)
curl -X POST -H "Content-Type: application/json" \
  -d '{"tool":"delete_path"}' http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
```

Without authorization, the Agent will not execute the operation: it records an `approval_required` step, sets the status to
`waiting` (and writes `approval_pending` into the runtime), then stops and waits for your authorization; after authorizing, just click continue.
Setting `approval_policy` to `"never"` means you explicitly accept the risk of these operations (not recommended).

### Approval Scope: once / session

Observed pain point: after `pwsh` was blocked, the model reworded the command, the argument hash changed → another approval prompt appeared (per-argument is too fragmented).

`agent.approval_scope` controls the authorization scope:

| Value | Meaning | When to use |
|---|---|---|
| `"once"` (default) | Precise down to "tool + arguments", **burned after use** | Strictest; when destructive operations are rare |
| `"session"` | After one authorization, **that tool is allowed within this session** (expires when the session ends; does not cross sessions or tools) | Consecutive shell tasks; safer than `never` |

```powershell
# Session-level authorization (the WebUI button automatically takes this route when scope=session)
curl -X POST -H "Content-Type: application/json" \
  -d '{"tool":"pwsh","scope":"session"}' http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
```

The WebUI approval banner shows different wording according to `approval_scope` ("valid only for this one execution" /
"valid only for this session"); the runtime snapshot carries `approval_scope` for the frontend to decide.

## Sandbox Denials Are Structured

Out-of-bounds / sensitive paths no longer get just a bare "access denied"; instead they return an error code (`FS_SANDBOX_ESCAPE` /
`FS_SANDBOX_DENYLIST` / `FS_SANDBOX_NOT_ALLOWED`), the current mode, the reason, and **actionable remedies**
(use a workspace-relative path / enable self-upgrade / ask the user to put the file into the workspace). When tool arguments are written incorrectly, it also
lists the tool's **legal argument names**, preventing the model from probing repeatedly.

## Full Regression (actually exercising each capability)

Unit tests/audits are white-box; this script **actually runs through the user-facing capabilities** (with real models) and gives a verdict in a few minutes:

```bash
python scripts/regression.py                 # default kimi-coding/k3, auto-cleanup after the run
python scripts/regression.py --skip-slow     # skip deep-research/unit-tests/layout audit (~1.5 minutes)
python scripts/regression.py --model copilot-auto --keep   # keep the scene for investigation
```

Covers 22 items: health check, tool-surface completeness (62 tools), three sandbox tiers, **plan mode (submit a plan without writing files)**,
normal write tasks, subagent create/continue/fork, deep research (evidence + report), scheduled task execution, MCP (stdio tools available +
removed after removal), plugin install/uninstall, plugin panels, CLI read-only commands, unit tests, invariants, UI smoke test, i18n/layout/contrast audits.

Results are written to `data/regression/report.json`; on failure the exit code is non-zero (can be wired into CI).

## Runtime Invariant Checks (must run before committing)

```powershell
python scripts/invariants.py        # exit code 1 on failure; --json for CI use
```

It checks the implicit contracts that "don't necessarily error when broken, but cause wrong behavior": tool group tables consistent with the registry,
minimal tool surface of the core group, append-only steps table, checkpoint table and status values, AgentRunner optional callbacks
backward-compatible, evaluation assertion names consistent with implementations, single source of truth for the prompt tool manifest, destructive-tool approval contract.

## Checkpoint Semantics During Crash Recovery

- Register the turn **before sending the request** (`turn_results.status=inflight`); transition to `done` after success;
- Register the step **before tool dispatch** (`step_dispatch`); close it out as success/failure/unknown respectively;
- If recovery finds steps that were "dispatched but never closed out", it **never replays them as-is**: it first has the model verify the current state read-only
  (corresponding to DSH's `Its outcome is unknown.`), avoiding duplicated side effects.

## Oversized Tool Results Spill to Disk (spill)

When a single tool result exceeds `agent.spill_threshold_chars` (default 20000 characters), the **full content** is written to
the workspace `.spill/<session>/<step>-<tool>.json`, and the context keeps only a "keep-head-keep-tail" summary + path hint;
when the model needs the full text it reads that path with `read_text` (the path is inside the workspace, so the sandbox allows it and the user can inspect it directly).
Benefit: information is no longer **discarded** to save tokens — previously there was only a truncation value; now it is "summary + retrievable full text".
The cap is controlled by `spill_max_bytes` (default 2MB); set `spill_threshold_chars: 0` to disable spilling entirely.

## Honest Timeout Semantics (unknown result ≠ didn't happen)

COM (Outlook) calls **cannot be truly cancelled**: a timeout only means "we stop waiting" — the call in the thread is still executing.
The old implementation gave up waiting after a timeout and the outer layer might even retry once — equivalent to doing it again
when "the first attempt may already have gone out". It is now changed to:

1. Protect the underlying call with `asyncio.shield`, **not abandoning** it, and attach a callback: late-arriving results are written to the log
   (on success it clearly warns "the side effect has most likely already occurred");
2. **Never retry** the same action after a timeout;
3. Record "result unknown" and **phase-block non-idempotent actions** (`outlook_send`/`delete_path`/`move_path`/
   `append_text`/`copy_path`) until any **read-only action** succeeds (e.g. `outlook_unread`,
   `read_text`, `list_directory`) — i.e. "verify the current state first";
4. While blocked, the affected actions return a structured `[TOOL_TIMEOUT_UNKNOWN]` error explaining what to do.

The timeout ceiling can be adjusted with `agent.outlook_timeout_seconds` (default 60 seconds).

## Event Log and Projection (P2-10)

All key actions of a session are written as **append-only events** (`events` table, 13 emission points in total):
status transitions, turn begin/end, tool dispatch/closeout, tool result recording (`step_recorded`),
file changes (`file_event`, from the real write paths of `file_events`), queue in/out (`queue`, counts only, no content),
message metadata (`message`, WebUI conversations only), approval grant/consume/reject. It is currently a **side-channel record** (the runtime still reads the database
columns); its purpose is troubleshooting, auditing, and paving the way for a future where "state can be rebuilt from the log".

```powershell
# Historical events (returned in one shot; kinds can be filtered comma-separated)
curl "http://127.0.0.1:8765/v1/agent/sessions/<sid>/event-log?limit=50&kinds=turn_begin,turn_done"
# State snapshot rebuilt from events + consistency check against database columns (empty consistency = consistent)
curl "http://127.0.0.1:8765/v1/agent/sessions/<sid>/projection"
```

### Inspection: whether projection and columns agree (acceptance before P2-10 phase 3)

```powershell
python scripts/health.py            # inspect all sessions; --limit / --session / --json optional
```

It **replays the event stream** into state session by session, cross-checks it against the database columns, and additionally checks **event coverage**
(records in tables without corresponding events = coverage gaps). The output distinguishes four categories:

| Category | Meaning | Fatal? |
|---|---|---|
| Consistent | Projection and columns match exactly | — |
| **Inconsistent** | Events exist but the column facts cannot be rebuilt = real gap | ✅ exit code 1 |
| **Coverage warning** | Records in tables without corresponding events (mostly historical sessions predating the feature) | ❌ warning only by default; `--strict` treats as failure |
| Predates event feature | Historical sessions with 0 events | ❌ skipped, not counted |

Design trade-off: consistency is a **fatal item** (it means the log is insufficient to rebuild state), coverage gaps are **warnings** —
otherwise every newly added event type would turn CI red for all historical sessions, and the metric would lose its meaning.

### Read-Path Switch (P2-10 phase 3)

Default is `columns` (read database columns); when set to `projection`, the **"unknown-result step" determination reads the event projection instead**:

```powershell
# config.json: "agent": { "event_source": "projection" }
# or temporarily via environment variable (for gray verification without changing config)
$env:CBP_EVENT_SOURCE = "projection"
```

The switch is a **reversible small step**: when projection and columns disagree, it **logs a warning and automatically falls back to columns** (safety first);
`/health` shows the currently effective `event_source`. Verified in practice: in projection mode, evals 7/7, event checks all green,
behavior identical to column mode.

Session deletion **cascades to clear events** (privacy first), but "when a session was deleted" is written to a separate `audit` table
(kind + session_id only, no content), so the deletion fact does not disappear with the session.

⚠️ Do not call `/v1/agent/sessions/{sid}/events` with a plain GET — that is an **SSE long-lived connection** (used by the frontend
EventSource); a plain request will hang until disconnected — this is by design; for historical events use the
`event-log` endpoint above.

## Programmatic Tool Calling `run_code` (off by default)

Lets the model write a piece of code that batch-calls **read-only** tools (`read_text` / `list_directory` /
`find_files` / `skill_list` / `memory_search` / `tool_list`) in an **independent subprocess**, finally returning the result via `final(value)`.
Suitable for analysis tasks like "read many files then summarize": a tool loop needs N remote turns, while `run_code` needs only 1.

**Measured (same task, deepseek/deepseek-chat)**

| File count | run_code off | run_code on |
|---|---|---|
| 4 | 5 steps / 4.3s | 5 steps (**model did not adopt it**, still went through the tool loop) |
| 12 | **17 steps** / 10.2s | **7 steps** (find_files → run_code ×2 → read_text ×3) / 8.9s |

In other words: not worth it at small scale (the model won't use it on its own); once scale grows, turn count drops by about 59%.

**Safety boundaries (deliberately conservative in v1)**: only read-only tools are exposed (**no writes/deletes/sending at all**); subprocess isolation;
hard limits — timeout `run_code_timeout_seconds` (default 30s), tool-call count `run_code_max_calls`
(default 50), return-size cap `run_code_max_output_chars` (default 20000); the tool-side path sandbox remains in effect as usual.
Tool errors inside the subprocess are the **named exception ToolError**, which model code can catch for a graceful finish.

How to enable: `agent.allow_run_code: true` in `config.json` (default false). It is recommended to also add `run_code`
to `approval_required_tools`, so it is authorized per call like `run_python`.

### Handling Variance in Evaluations

Models are stochastic: the same task may finish in 3 steps or in 28. Therefore evaluations support declaring `"retries": 1`
for an individual task in `tests/evals/tasks.json` (**retry once** after failure; only two failures count as a failure) — this both
suppresses false failures from single sampling and does not mask real regressions. Currently only `code-with-validation` declares a retry.