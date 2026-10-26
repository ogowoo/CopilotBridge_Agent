# Gantry

> **語言**：[English](README.md) · [简体中文](README.zh-CN.md) · **繁體中文**

> **改名說明**：本專案原名 **CopilotBridge Agent**，自 11.0.0 起更名為 **Gantry**。
> Python 套件 `cbp` → `gantry`；環境變數 `CBP_*`、請求標頭 `X-CBP-*`、Copilot 工作階段標記
> `[CBP-AGENT]` 仍然相容（新寫 `[GANTRY-*]`，讀取時兩套都認），舊工作階段與舊腳本不受影響。

> **沒有 Copilot 也能用**：瀏覽器按需啟動——服務啟動時不碰瀏覽器，WebUI 與供應商模型（DeepSeek/Kimi/OpenAI 等）立即可用；只有用到 Copilot 時才會拉起它，介面頂部會顯示狀態並提供「重試瀏覽器」。**注意**：lazy 模式下，重新啟動後的第一個 Copilot 任務要等瀏覽器拉起與 SSO（企業租用戶可達數分鐘），這段時間不算任務逾時；供應商模型不受影響。

> **版本**：主線 `main` 目前是 **11.0.0-preview.5**（多視窗並行預覽線已併入）。
> 舊穩定線歸檔在 tag `v10.2.1-archive` / 分支 `archive/10.2.1-stable`。
> 版本規則、發版流程與多機器協作約定見 [docs/VERSIONING.md](docs/VERSIONING.md)，
> 每次發布的改動見 [CHANGELOG.md](CHANGELOG.md)。

把 **Microsoft 365 Copilot 網頁版**包裝成本機服務的橋接器：對外提供 **OpenAI 相容的 Chat API**，並內建一個能讀寫本機檔案、執行驗證閉環的**長期任務 Agent**。模型呼叫不經過任何官方 API——由 Playwright 自動化你本機的 Edge 瀏覽器，直接使用你已登入的 Copilot 網頁工作階段。

```
用戶端 ──HTTP──> FastAPI (app.py) ──Playwright──> Edge 持久化 Profile ──> m365.cloud.microsoft/chat
                     │
                     ├─ /v1/chat/completions      一般對話（OpenAI 協定，支援串流）
                     ├─ /v1/agent/completions     Agent 模式（工具呼叫 + 長任務）
                     ├─ /v1/agent/sessions/*      工作階段管理 / 暫停 / 恢復 / 刪除
                     └─ /ui                       內建 Web 管理台
```

> **只想趕快跑起來**：看[一頁版快速手冊](docs/QUICKSTART.md)（安裝 → 登入 → 啟動 → 第一個任務），
> 也可以直接存一張圖：[简体](docs/quickstart-zh.png) ｜ [繁體](docs/quickstart-zh-TW.png) ｜ [English](docs/quickstart-en.png)（`python scripts/make_quickstart_card.py` 可重新產生）。
> 更進一步的：看下面的**快速開始**（4 條命令）→ **操作手冊**（WebUI 怎麼用、AGENT.md/skills 怎麼寫、怎麼復原、怎麼自我升級、疑難排解）。

## 功能特性

- **OpenAI 相容**：`/v1/chat/completions` 支援 stream / non-stream，可接入任何 OpenAI SDK 用戶端；`/v1/models` 列出可用模型。模型清單以內建表為基礎（Auto / Quick response / Think deeper / GPT / GPT-5.6 / Sol / Think，可在設定中擴充），並**在啟動時自動開啟 Copilot 模型選單列舉選項（含 GPT 等父項下的二級子選單）**——選單裡新出現的模型會自動併入（ID 形如 `copilot-gpt-5-7`），選擇模型時也會自動展開子選單定位；也可用 `POST /v1/models/refresh` 隨時重新掃描。
- **API 供應商接入**：除 Copilot 網頁自動化外，可直接接入任何 OpenAI 相容 HTTP API（內建 OpenAI / DeepSeek / Kimi / Grok 預設，可自訂）。模型 ID 形如 `deepseek/deepseek-chat`，API Key 走環境變數；一般對話和 **Agent 模式（含工具迴圈）都可用**——JSON 動作協定是純 prompt 驅動，與供應商無關。
- **Agent 模式**：Copilot 每回合回傳一個 JSON 動作（呼叫工具或最終答覆），本機執行工具後把結果送回，循環直到任務完成。**支援批次動作**：一次回答可帶多個工具動作（`{"type":"actions","actions":[...]}` 或直接給動作陣列），依序執行、每步單獨記帳——寫多個小檔案/多處小替換不必一塊一個回合，省下大量 Copilot 回合；批次裡不允許 `final`（完成必須單獨回傳，驗證閉環不被繞過），動作數上限由 `max_actions_per_turn` 控制，批次被輸出上限截斷時仍會執行已完整的前幾個動作。
- **15+ 個工作區工具**：目錄/檔案讀寫（Base64 寫入防止 JSON 跳脫損壞）、精確替換、複製/移動/刪除、`validate_python`、`run_unittest`、`run_python`，全部限制在工作區根目錄內。
- **Copilot 直接產生檔案（`copilot_file`）**：讓 Copilot 把結果產生為**可下載檔案**，橋接再把檔案下載進工作區——內容**不經過聊天訊息**，因此完全繞開 Copilot 單條輸出上限，一個大檔案一個回合就能拿到，不再需要 b64 分塊。支援 `blob:`/`data:` 連結與「下載」按鈕（點擊並用瀏覽器下載事件接住），寫入磁碟前照樣存原始快照（可復原）、寫 `.py/.json/.html` 仍進驗證閉環。開關：`agent.allow_copilot_file`（預設開）。找不到下載入口時會儲存 `last_answer.html` 供排查，並提示改回分塊寫入。
- **兩級專案脈絡（全域 + 專案）**：`AGENT.md` 與 `skills/` 都有兩個範圍——**全域**放在服務安裝目錄（對所有工作區生效），**專案**放在工作區根目錄；兩份合併注入，同名技能/約定專案級優先，同一檔案自動去重。全域技能位於工作區之外，用 `skill_read` 工具讀取（`skill_list` 列清單）。`AGENT.md` 單份預設 8000 字元截斷（`agent_md_max_chars`），SKILL.md 支援 YAML frontmatter（`name`/`description`），缺省時回退「第一個一級標題 + 第一個段落」。本儲存庫本身即帶全域 [AGENT.md](AGENT.md) 與範例技能。
- **服務自我升級（可選，預設關閉）**：`agent.allow_self_upgrade: true` 後，Agent 可用 `self_read_text`/`self_write_text_b64`/`self_append_text_b64`/`self_replace_text` 直接修改**服務自身程式碼**（即使工作區是別的專案），白名單限定 `gantry/`、`webui/`、`scripts/`、`docs/`、`tests/` 與若干頂層檔案，`config.json`/`data/`/`EdgeProfile/` 禁止觸碰；寫完必須 `self_validate` + `self_unittest` 雙通過（提前 final 會被 `validation_gate` 攔下），並提供 `self_rollback` 工具復原本次自我升級。
- **變更快照與兩級復原（自我升級 failback）**：寫入操作**執行前**把目標原始快照存入 `data/snapshots/<sid>/`（工作區）與 `data/snapshots/_self_<sid>/`（服務程式碼），同一路徑保留最早狀態**並按 Copilot 回合計版本（一次 complete = 一個版本）**，因此可將整個任務復原到「任務開始前」，也可按回合復原。入口：WebUI「更多 ▾ → 復原任務變更…／復原服務自我升級…」、`POST /v1/agent/sessions/{sid}/rollback`（可帶 `{version}`）、`POST /v1/agent/self/rollback?last=true`，以及**服務/WebUI 起不來時的離線通道** `python scripts/rollback.py [--scope self] --last --yes [--rev N]`（`app.py` 匯入失敗會自動印出指引）。自我升級寫入磁碟會留下 `data/.self_upgrade_pending` 標記，服務重新啟動後自動歸檔。
- **M365 檔案與軟體整合**：
  - **Office 文件工具**：`read_excel`/`write_excel`（openpyxl）、`read_docx`/`write_docx`（python-docx）、`read_pptx`（python-pptx）、`read_pdf`（pypdf）——Agent 可直接分析/修改 xlsx、docx、pptx、pdf；
  - **Copilot 檔案上傳**：`copilot_attach` 工具把工作區檔案掛到 Copilot 網頁版附件框並取回分析結果，用於圖片/掃描件/複雜版面等本機無法解析的檔案；
  - **Outlook 整合**：`outlook_unread`/`outlook_read`/`outlook_search`/`outlook_send`。**預設走 OWA 網頁自動化**（複用橋接瀏覽器與 SSO 登入狀態；New Outlook 無 COM、EWS 2026 起被微軟停用、Graph 需管理員授權，網頁版是目前唯一可行路徑）；裝了經典 Outlook 的機器自動保留 COM 備援。讀取預設開啟，傳送由 `allow_outlook_send` 單獨控制。OWA 模式下 `outlook_read` 的參數按主旨關鍵字定位（沒有 entry_id 概念）。
- **長任務韌性**：
  - 每個回合注入唯一標記 `[GANTRY-TURN:...]`，確認送達後才讀取回答，杜絕讀到上一回合的舊答案；
  - 嚴格區分三類失敗——**限流**（原地指數退避重試）、**對話達到長度上限**（摘要進度後切換新工作階段重發）、**回答格式錯誤**（容錯解析 + 壓縮脈絡後在原工作階段繼續）；
  - **工作階段治理模型**：M365 Copilot 的對話長度上限不公開且動態變化，本機壓縮只縮短後續提示、**不會縮短遠端對話**，因此治理依賴組合拳——本機持續壓縮 + 保守閾值主動換工作階段 + 頁面上限偵測被動兜底 + 檢查點接力（摘要/修改檔案/驗證結果帶入新工作階段）；
  - **冪等保護**：換工作階段接力或回合重發時，與上一成功動作完全相同的非冪等操作（append/move/copy）會被跳過，避免重複修改檔案；
  - **實測校準**：每個 Copilot 工作階段結束時記錄實測資料（回合數/估算 Token/字元數/結束原因/模型/URL）存入 `conv_stats` 表，工作階段詳情介面可查，用於持續校準本機閾值；
  - 修改程式碼後強制驗證閉環：`validate_python` 與 `run_unittest` 都通過才允許宣稱完成。
- **工作階段持久化**：SQLite（WAL）保存步驟時間線、檔案變更雜湊、對話訊息；服務重新啟動後可從檢查點繼續。
- **Web 管理台**：`/ui` 提供任務發起（按鈕＋彈窗）、即時進度（執行狀態/目前回答/傳送內容/繼續對話 四個分頁）、全寬步驟時間線（可匯出 JSON）、暫停/恢復/取消、工作階段歸檔與遠端 Copilot 工作階段刪除（帶同 Session 標記防誤刪與對帳終驗）。詳見[操作手冊](#1-webui-怎么用)。

## 快速開始

```bash
git clone https://github.com/ogowoo/gantry.git
cd gantry
python install.py
```


環境需求：Windows + Microsoft Edge + Python 3.11+（其他平台將 `browser_channel` 改為 `chrome` 或去掉該設定使用內建 Chromium）。

```powershell
# 1. 安裝相依套件（pip + Playwright 瀏覽器）
python install.py

# 2. 複製並按需修改設定
copy config.example.json config.json

# 3. 登入 Copilot（會開啟 Edge，登入完成後按 Enter）
python scripts/login.py

# 4. 啟動服務（預設 http://127.0.0.1:8765）
python start.py
```

啟動後開啟 `http://127.0.0.1:8765/ui` 使用管理台，或造訪 `/docs` 查看 API 文件。

### 多帳號：換用有 M365 訂閱的帳號

登入狀態保存在 `EdgeProfile/`，Windows 的帳戶選擇器（WAM）常會**自動推薦系統目前登入的一般帳號**，導致橋接用錯帳號。切換方法（任選其一）：

1. **直接在自動化視窗裡換**（服務以視窗模式執行時）：在自動化 Edge 的 Copilot 頁面右上角頭像處「登出」，再用訂閱帳號登入——登入狀態自動保存，無需重新啟動服務；
2. **重新走登入精靈**（先停服務）：`python scripts/login.py`，彈出帳戶選擇器時務必點「使用其他帳戶 / Use another account」，輸入訂閱帳號；
3. **登入狀態已混亂時徹底重來**：`python scripts/login.py --reset` 刪除 EdgeProfile 後全新登入。

### 一般對話（OpenAI SDK）

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
resp = client.chat.completions.create(
    model="copilot-auto",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)
```

### API 供應商（DeepSeek / Kimi / OpenAI / Grok …）

模型 ID 用 `供應商/模型` 格式，例如 `deepseek/deepseek-chat`、`kimi/kimi-k2-0905-preview`、`openai/gpt-4o`、`grok/grok-4`。**API Key 透過環境變數設定**（不寫進設定檔、不進 git）：

```powershell
$env:DEEPSEEK_API_KEY="sk-..."   # 或 MOONSHOT_API_KEY / OPENAI_API_KEY / XAI_API_KEY
```

未設定 Key 的模型在 `/v1/models` 裡會標註「（未設定 API Key）」。

> 想把 **DeepSeek 網頁版**也當模型用（免費/備用）：見[三種接法與已落地的外掛](docs/PROVIDER-deepseek-web.md)——起一個反向代理 + 啟用 `deepseek-web` 外掛即可，模型 ID 形如 `dsweb/deepseek-reasoner`。自訂任意 OpenAI 相容供應商（含自建閘道）：

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

供應商模型在所有入口都可用：OpenAI 相容對話（串流透傳）、WebUI 新任務（一般對話/Agent 執行）、Session 跟進。一般對話用 Session 的訊息記錄作脈絡；Agent 模式複用同一套 JSON 動作協定與工具迴圈。供應商限流/5xx 會自動走與 Copilot 相同的指數退避重試。

**代理（重要）**：瀏覽器存取 API 走的是 **Windows 系統代理**，而 Python 的 httpx 預設只讀 `HTTP_PROXY`/`HTTPS_PROXY` 環境變數、**不看系統代理**——這會造成「瀏覽器能開、服務卻報供應商不可用」的假故障。現在供應商請求預設**跟隨系統代理**（讀登錄檔），傳輸層失敗且代理是自動偵測來的，還會**直連重試一次**。可明確設定：

```json
"providers": {
  "deepseek": { "proxy": "http://127.0.0.1:10809" },   // 明確代理
  "kimi":     { "use_system_proxy": false }            // 強制直連（代理故障時用）
}
```

**一條命令自我檢測連通性**（會分別印出「經代理/直連」兩條路徑的真實結果）：

```powershell
python scripts/provider_selftest.py                    # 全部已設定供應商
python scripts/provider_selftest.py --provider deepseek
```

### Agent 任務

```bash
curl -X POST http://127.0.0.1:8765/v1/agent/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "copilot-auto",
    "messages": [{"role": "user", "content": "分析工作区项目并生成 TODO 清单"}],
    "workspace": "C:/path/to/project"
  }'
```

## 設定說明（config.json）

### 頂層

| 欄位 | 預設 | 說明 |
|---|---|---|
| `copilot_url` | `https://m365.cloud.microsoft/chat/` | Copilot 入口網址 |
| `browser_channel` | `msedge` | Playwright 瀏覽器通道（`msedge` / `chrome` / 省略用內建 Chromium） |
| `headless` | `false` | 無頭模式；啟動失敗會自動退回有視窗模式 |
| `reply_timeout_seconds` | `180` | **無進展**逾時：回答連續這麼久沒有新文字才判定逾時 |
| `reply_hard_timeout_seconds` | `1200` | 單回合硬上限（兜底，防止無限等待） |
| `stable_seconds` | `3` | 回答文字穩定多久視為產生結束 |
| `poll_interval_seconds` | `0.35` | 頁面輪詢間隔 |
| `diagnostics` / `diagnostic_html` | `true` / `false` | 出錯時儲存 `last_page.txt`（/ `.html`）快照 |
| `default_model` | `copilot-auto` | 預設模型 ID |
| `server.host` / `server.port` | `127.0.0.1` / `8765` | 監聽位址。**綁定非回送位址且未設 api_token 時服務拒絕啟動** |
| `api_token` | 空 | 設定後所有 `/v1/*` 端點要求 `Authorization: Bearer <token>`（或 `?token=`） |
| `models` | `{}` | 擴充模型：`{"copilot-custom": {"ui_names": ["Custom"], "description": "..."}}` |
| `markers` | 見下 | Copilot 頁面可及性文字錨點，介面語言不同時在此追加文案 |

`markers` 的設定值會**併入**內建預設清單（不是覆蓋），任一命中即生效。
內建預設已含**英文 + 中文 + 日文**（`Copilot said:` / `You said:` / `Sources` /
`出典` / `あなたの発言:` / `Copilot の回答:` 等），另外按鈕/浮層文案
（`stop` / `new_chat` / `send_aria` / `dismiss`）也可在這裡追加：

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

- `assistant_turn` / `user_turn`：回合分隔文字，用於定位最新回答與統計使用者回合數——**介面非英文/中文導致讀取逾時時，優先在這裡追加對應語言錨點**；
- `answer_end`：回答正文的結束標記（來源區、輸入提示等）；
- `sources`：回答中出現即視為產生完成的區塊標題；
- `ignored_lines`：從回答中剔除的按鈕殘留文字；
- `stop` / `new_chat` / `send_aria` / `dismiss`：停止產生、新增對話、傳送按鈕、彈窗關閉的文案錨點（日文系統實測「發得出去但收不到回信」就是這組錨點沒命中）。

> 日文等其他語言的預設文案是**按常見寫法內建**的，若測試租用戶用了不同措辭，
> 直接用 `markers` 追加即可，不必改程式碼。

### agent 段

| 欄位 | 預設 | 說明 |
|---|---|---|
| `enabled` | `true` | 關閉後 Agent 介面回傳 503 |
| `workspace` | `workspace` | 工具根目錄（相對專案目錄） |
| `workspace_roots` | `[]` | 工作區允許根目錄白名單（絕對路徑陣列）；非空時切換工作區只能切到白名單內，防止沙箱被整體搬移 |
| `session_store` | `data/agent.db` | SQLite 工作階段資料庫路徑 |
| `max_total_steps` | `1000` | 單任務總步數上限（硬頂 10000） |
| `max_steps_per_conversation` | `80` | 單個 Copilot 工作階段內步數上限（觸發換工作階段） |
| `max_conversation_rollovers` | `25` | 最多切換多少個 Copilot 工作階段 |
| `context_token_budget` | `80000` | 估算脈絡預算（本機估算值，**不代表 M365 真實上限**） |
| `compact_at_tokens` | `35000` | 達到即壓縮歷史為摘要 |
| `rollover_at_tokens` | `55000` | 達到即換新工作階段（需 `auto_rollover`）；須滿足 `0 < compact < rollover ≤ budget` |
| `keep_recent_steps` | `8` | 壓縮/換工作階段時保留的最近步數 |
| `max_summary_tokens` | `5000` | 長期摘要估算上限 |
| `max_context_chars` | `240000` | 字元數兜底閾值 |
| `auto_rollover` | `true` | 建議開啟：本機壓縮不會縮短遠端對話，保守閾值主動換工作階段是主要防線；頁面上限偵測做被動兜底 |
| `min_steps_before_rollover` | `20` | 主動換工作階段的最小步數門檻 |
| `auto_compact` | `true` | 達到閾值時自動壓縮脈絡；`false` 時僅保留手動壓縮 |
| `auto_resume` | `true` | 服務重新啟動後自動恢復上次被中斷（非使用者暫停）的工作階段 |
| `session_timeout_seconds` | `86400` | `ready`/`waiting` 工作階段超過該時長未活動則標記為 `expired` |
| `completed_session_retention_days` | `30` | 已結束（完成/取消/失敗/過期）工作階段超過該天數後自動歸檔 |
| `max_sessions` | `100` | 未歸檔工作階段數量上限，超出時從最久未更新的開始歸檔 |
| `max_parse_failures` | `5` | 連續解析失敗多少次後強制壓縮脈絡 |
| `max_actions_per_turn` | `4` | 一次回答裡最多執行幾個動作（批次協定，硬頂 12）；調大能省回合但更容易被 Copilot 輸出上限截斷 |
| `allow_copilot_file` | `true` | 是否開放 `copilot_file`（讓 Copilot 直接產生檔案並下載進工作區，內容不走聊天訊息、不受輸出上限約束）。**注意**：`copilot_file` 與 `copilot_attach` 依賴 Copilot 網頁工作階段，因此在使用 API 供應商模型（如 `deepseek/…`）的工作階段裡**自動停用**（既不注入也不執行，避免白燒回合） |
| `copilot_file_model` | `""` | `copilot_file` 產生檔案時用的具體模型。工作階段模型是 `Auto` 時，Auto 通常只聊天、不產生下載入口（實測），設定此項（如 `copilot-gpt-5-6-sol-think-deeper`）即可自動改用；留空則沿用工作階段模型並在失敗時明確提示原因 || `max_service_unavailable` | `30` | 限流退避重試上限，超過後轉入安全暫停 |
| `service_retry_base_seconds` / `service_retry_max_seconds` | `30` / `600` | 限流指數退避的起始/上限 |
| `service_retry_window_seconds` | `3660` | **Copilot 網頁**的限流視窗：首次超限後到可再次使用大致要等這麼久（實測約 1 小時，多加 1 分鐘餘量），**置中倒數計時卡片**錨定「首次超限 + 該視窗」。**API 供應商不用這個視窗**（它們的 429/5xx/網路抖動通常幾秒~幾分鐘就恢復，套 1 小時會誤導），而是按本輪退避顯示「下次重試」；卡片標題按目前模型通道分別顯示「Copilot 限流/不可用」或「API 服務限流/不可用」 |
| `max_read_chars` / `max_write_chars` | `120000` / `200000` | 工具單次讀寫上限 |
| `allow_delete` | `false` | 是否開放 `delete_path` 工具 |
| `allow_python` | `false` | 是否開放 `run_python` 工具（**可執行任意程式碼，謹慎開啟**） |
| `allow_office` | `true` | 是否開放 Office 文件工具（Excel/Word/PPT/PDF） |
| `allow_memory_write` | `true` | 是否允許 Agent 用 `memory_write` 沉澱**長期記憶**（跨工作階段事實/偏好/結論，自動按相關性檢索注入；`memory_forget` 可刪除） |
| `browser_startup` | `lazy` | 瀏覽器啟動策略：`lazy`=按需啟動（沒有 Copilot 也能用 WebUI 與供應商模型）；`eager`=服務啟動即預熱 |
| `allow_skill_write` | `true` | 是否允許 `skill_create` 沉澱技能（專案級寫入工作區 `skills/`；全域級還需 `allow_self_upgrade`） |
| `allow_outlook` | `false` | 是否開放 Outlook 讀取工具（未讀/正文/搜尋） |
| `allow_outlook_send` | `false` | 是否開放 `outlook_send`（會真實寄出郵件，**謹慎開啟**） |
| `outlook_url` | `https://outlook.office.com/mail/` | OWA 自動化入口（`allow_outlook` 時使用） |
| `python_timeout_seconds` | `20` | `run_python` 逾時 |
| `snapshot_retention` | `50` | 保留多少個工作階段的變更快照，超出按最舊清理 |
| `agent_md_max_chars` | `8000` | 單份 `AGENT.md` 注入上限（全域、專案各自計算） |
| `allow_self_upgrade` | `false` | 是否允許 Agent 修改**服務自身程式碼**（`self_*` 工具，**高權限，理解風險後再開**） |
| `self_upgrade_paths` | 見 `config.example.json` | 自我升級可觸達的路徑白名單（安裝目錄相對路徑） |

## 命令列（headless）

```bash
pip install -e .                    # 之後可用 gantry 命令（也可以 python -m gantry.cli）

gantry serve --port 8765               # 啟動服務（等價 python start.py）
gantry status                          # 健康檢查
gantry run "把 docs 裡的失效連結列出來" --model kimi-coding/k3
gantry run "重構 X" --plan             # 計畫模式：唯讀探索 → 產出方案後停止
gantry run "分析這個儲存庫" --sandbox read-only --workspace C:/proj
gantry sessions --limit 10             # 會話清單
gantry tools                           # 62 個工具
gantry plugins list|enable|disable|reload [name]
gantry mcp list                        # MCP server 狀態
gantry mcp add --name fs --command npx --arg -y --arg @modelcontextprotocol/server-filesystem --arg C:/data
gantry sandbox read-only --persist     # 切換沙箱層級並寫回 config.json
```

- 通用參數：`--url`（預設 `http://127.0.0.1:8765`，或 `CBP_URL`）、`--token`（或 `CBP_TOKEN`）
- CLI **不會在處理程序內啟動瀏覽器**：它呼叫同一個服務的 HTTP 介面，和 WebUI 看到同一份資訊
- `--json` 輸出原始回應，方便指令碼使用；`--quiet` 不印出步驟摘要
- 連不上服務時回傳非零結束代碼，並說明請先 `gantry serve`

## API 一覽

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/health` | 健康檢查（含忙碌狀態） |
| GET | `/v1/models` | 模型清單 |
| POST | `/v1/models/refresh` | 重新列舉 Copilot 模型選單，並納入新模型 |
| POST | `/v1/chat/completions` | OpenAI 相容對話 |
| POST | `/v1/agent/completions` | 發起/繼續 Agent 任務 |
| GET | `/v1/agent/sessions` | 會話清單 |
| GET | `/v1/agent/sessions/{sid}` | 會話詳細資訊（步驟/檔案事件/訊息） |
| GET | `/v1/agent/sessions/{sid}/runtime` | 即時執行狀態（無 SSE 時的輪詢介面） |
| GET | `/v1/uploads/{name}` | 取「貼上附件」圖片（`data/uploads` 下的檔名；嚴格檔名檢查，禁止分隔符與 `..`；走 api_token 驗證，前端 img 標籤用 `?token=`） |
| POST | `/v1/shutdown` | **終止服務**（WebUI 的 ⏻ 按鈕）。先回應再停機；優雅停機（uvicorn `should_exit`），拿不到 server 時退回 SIGINT，寬限期後強制結束保底；冪等 |
| GET | `/v1/agent/sessions/{sid}/events` | **SSE 即時推播**（`text/event-stream`）：先送一份 runtime 快照，隨後推送 `runtime`/`queue`/`messages`/`session` 事件；15 秒無事件發心跳；用戶端斷線自動取消訂閱。`api_token` 可用 `?token=` |
| POST | `/v1/agent/sessions/{sid}/messages` | 在會話中附加訊息（Agent 或一般對話模式）；**入佇列即回傳** `queued/position`，背景依序處理（要等目前這輪跑完才起新一輪） |
| POST | `/v1/agent/sessions/{sid}/inject` | **強行插入指令**（DSH Ctrl+J 式）：寫進 `pending` 並**立即中斷目前回合**——提供者會話取消底層 HTTP 請求（無狀態，取消即「這輪沒發生」）；Copilot 網頁會話**只在回答尚未開始產生時**中止等待（已開始產生就讓它答完，避免遺失回答/錯位）。隨後按新指令繼續；會話閒置時自動轉為排隊跟進 |
| GET | `/v1/agent/sessions/{sid}/queue` | 查看該會話佇列（等待項目 + 正在處理的一筆，後者標 `processing`；介面只顯示等待項目） |
| POST | `/v1/agent/sessions/{sid}/queue/{item_id}` | 修改排隊中訊息的內容／模型（與訊息歷史同步） |
| DELETE | `/v1/agent/sessions/{sid}/queue/{item_id}` | 刪除排隊中訊息（與訊息歷史同步） |
| POST | `/v1/agent/sessions/{sid}/actions/{pause\|resume\|cancel\|compact\|rollover\|archive}` | 會話控制 |
| POST | `/v1/agent/sessions/{sid}/activate` | 把自動化瀏覽器切到該會話頁面 |
| DELETE | `/v1/agent/sessions/{sid}?delete_remote=true&force_local=true` | 刪除會話（可選連帶刪除 Copilot 網頁會話） |
| POST | `/v1/agent/sessions/{sid}/model` | 切換該會話使用的模型（寫入資料庫，下一回合生效，服務重啟後仍沿用） |
| GET | `/v1/agent/sessions/{sid}/snapshots` | 任務變更快照清單（可回滾的路徑/類型 + 回合版本清單 `versions`） |
| GET | `/v1/agent/sessions/{sid}/prompt` | 查看該會話的提示詞：`initial_prompt`（首回合完整內容，含系統提示詞/工具清單/專案慣例）與 `last_prompt`；舊會話未寫入資料庫時按目前工作區近似重建並標 `rebuilt=true` |
| POST | `/v1/agent/sessions/{sid}/rollback` | 回滾該任務的工作區變更；可選 body `{version:N}` 撤銷第 N 回合之後的變更（執行中回傳 409） |
| GET | `/v1/agent/self/status` | 自升級狀態（是否啟用、白名單、待重啟標記、快照範圍） |
| GET | `/v1/agent/self/snapshots?sid=` | 自升級變更快照清單 |
| POST | `/v1/agent/self/rollback?sid=&last=` | 回滾服務自身程式碼（`last=true` 取最近一次） |
| GET/POST | `/v1/agent/workspace` | 查詢/切換工具工作區 |
| GET/POST | `/v1/agent/browser` | 查詢/切換瀏覽器視窗顯示（無頭 ↔ 有視窗；保留登入狀態重啟瀏覽器內容，`persist=true` 可寫回 config.json） |
| GET | `/v1/agent/browse?path=` | 單層目錄列舉（只列子目錄），供工作目錄選擇器；不傳 path 從目前工作區開始 |
| POST | `/v1/agent/browse/mkdir` | 在選擇器裡新增子目錄（只允許單層名稱，拒絕分隔符/`..`） |
| POST | `/v1/agent/pick-directory` | 系統資料夾對話方塊（原生）。**註**：由服務託管啟動的處理程序若繼承了非互動視窗站，對話方塊不可見——逾時 15 秒回傳 400，WebUI 只用應用程式內瀏覽 |
| POST | `/v1/agent/deep-research` | 一鍵深研：`{query, model, angles}` → 證據包 + 一次直連綜合 → 附來源的報告（存成 `research-*` 會話 + 工作區 md） |
| GET | `/v1/plugins` | 外掛清單（已載入/啟用狀態、權限、貢獻點） |
| POST | `/v1/plugins/{name}/{action}` | 外掛操作：`enable` / `disable` / `reload` |
| ANY | `/v1/plugins/{plugin}/api/{path}` | 外掛自訂介面（如 `web-search` 的 `api/search`） |
| GET | `/ui` | Web 管理台 |

## 操作手冊

- `sandbox_mode`：`read-only` / `workspace-write`（預設）/ `danger-full-access`。
  `read-only` 下所有變更類工具（寫檔案/執行命令/office 寫/寄郵件…）在執行前就被拒絕；
  `danger-full-access` 允許寫到工作區之外，但**服務安裝目錄仍受保護**（除非開啟 `allow_self_upgrade`）。
- 執行時可用 `GET/POST /v1/agent/sandbox` 查詢/切換（`persist:true` 寫回 config.json）。

### 1. WebUI 怎麼用

開啟 `http://127.0.0.1:8765/ui`（建議強制重新整理 `Ctrl+F5` 確保載入最新前端）。

| 位置 | 操作 |
|---|---|
| 側欄頂部 **＋ 新任務** | **開啟新會話編輯器**（不再是彈出視窗）：大輸入框 + 模型 + 工作目錄（📁 開啟目錄選擇器，預設 workspace）+ **Agent 執行 / 一般對話** + 「開始」；`Enter` 送出、`Shift+Enter` 換行 |
| 輸入框裡的 **`/` 命令** | 輸入 `/` 彈出命令面板（↑↓ 選擇、Tab/Enter 自動完成、Esc 關閉）：`/research 問題`（深研）、`/goal 目標`（目標模式）、`/plan 任務`（計畫模式）、`/agent`（切 Agent 模式）、`/chat`（切一般對話）、`/help`（列出命令）。帶參數直接按 Enter 即執行，不帶參數則自動完成後等待輸入 |
| 側欄 **SESSIONS** 的工作區分組 | 會話按**工作區**分組（組頭：摺疊箭頭 + 資料夾圖示 + 名稱 + 會話數）；點組頭摺疊/展開（狀態存 localStorage）；組頭右側 **＋** = 在該工作區新增會話；滑鼠停留在工作區名字可看完整路徑 |
| 子代理會話的歸組 | 子會話**摺疊在父會話下**（父列左側 `▸`，點開縮排顯示，狀態存 localStorage）；刪除父會話會**串聯刪除**子代理，不留孤兒 |
| 側欄 **SESSIONS** | 會話清單（列首來源標記：⏱ 排程任務 / 🔎 深研 / 子 子代理；狀態圓點：綠=執行中、黃=等待/暫停、紅=失敗、藍=完成）；`篩選…` 按標題/ID/**工作區**過濾；圖示列：**＋新會話**、**📁新增工作區**、📖 使用手冊、▣ 切換自動化瀏覽器視窗、◐ 循環主題（**預設跟隨系統**；循環：跟隨系統 → 深色 → 琥珀 → 墨玉 → 淺色 → 護眼米 → 薄荷，3 暗 / 3 亮，選擇重新整理後保留，系統主題變化時自動跟隨） 等 |
| 側欄左緣細長拉手 | 收起/展開整個側欄（會話清單佔滿寬度） |
| 頂部操作列 | 暫停 / 繼續 / 壓縮上下文 / **深研**（多角度搜尋 + 抓內文 → 附來源報告，新會話）/ 繼續執行 / 切換到頁面（把自動化 Edge 跳到該會話）/ 開啟會話（在你自己的瀏覽器開啟）/ 取消任務 / 更多 ▾（外掛管理、封存、回滾任務變更、回滾服務自升級、刪除記錄、連同 Copilot 會話刪除） |
| 頂欄右側**模型選擇器** | 顯示**目前會話自己的模型**，可直接切換；寫入資料庫到該會話，**下一回合生效、服務重啟後仍沿用**。選項來自 `/v1/models` 的可用模型；若會話原先的模型已不在選單裡，會標註「（選單裡找不到）」 |
| 頂欄 ID 晶片 | 顯示目前會話 ID，點擊複製（刪除前對號用） |
| KPI 列 | 狀態、總步驟（含子代理）、目前會話步驟、會話（第幾個 Copilot 對話，與側欄同一口徑）、TOKEN 估算（含子代理，附預算進度條） |
| 目標 / 計畫列 | 計畫模式下顯示方案狀態；有目標時顯示 `🎯 目標（第 r/max 輪 · 狀態）`，有 todo 時顯示 `☑ 計畫 完成數/總數`；無目標/無計畫時整列隱藏 |
| 即時面板（四個分頁） | **目前執行狀態**（階段/等待詳情/計數）、**目前回答**（模型最新回答，結構化動作自動轉 JSON 檢視、一般回答按 Markdown 呈現；提供者串流回傳時**逐字增長**；有更新時分頁出現綠點）、**最近送出的內容**（發給模型的原文尾部）、**繼續對話**（見下）|
| 訊息編輯 / 刪除 | 對話裡每則已寫入資料庫的訊息，滑鼠停留出現 ✎ / 🗑：編輯改內容、刪除移除記錄。**誠實語義**：只改本地歷史，已發給網頁 Copilot 的內容無法撤回；**提供者會話**會同步記憶體裡的對話歷史（`_PROVIDER_AGENT_HISTORY` 是漸進累積而非每回合重建，不同步的話模型下一回合仍會看到舊文字），同步結果在提示裡說明（`history_synced`），記憶體裡已被裁剪找不到時如實告知而不是假裝成功 |
| 🔍 跨會話搜尋 | 側欄搜尋圖示開啟面板（再點 / Esc / 跳轉後自動收起），搜**所有會話的對話內文**（後端 `GET /v1/search`，LIKE 萬用字元已跳脫——搜 `100%`/`_` 是字面值），結果列出會話標題、角色、時間與命中片段；**點結果即跳轉**到該會話並把那則訊息捲到視野中間醒目標示一下。封存會話不參與搜尋 |
| 🏷 會話標籤與分組 | 會話卡片滑鼠停留出現 🏷：設標籤（留空取消）。側欄清單**按標籤分組**（有標籤的在前、未打標籤的收在最後一組），篩選框同時比對標題/ID/狀態/標籤。標籤是純本地組織手段（`POST /v1/agent/sessions/{sid}/tag`），不參與 Agent 邏輯 |
| 時間軸篩選 | 時間軸標頭兩個下拉：**按工具**（選項自動來自目前步驟裡出現過的工具）與**按狀態**（僅成功 / 僅失敗與被擋 / 僅事件），計數顯示為 `m/n 筆（篩選）`。預設無篩選時仍是漸進呈現（展開狀態不遺失），篩選變化才整表重建 |
| 對話區串流氣泡 | 提供者串流輸出除了「目前回答」面板，**對話區也會顯示一個「正在產生」暫時氣泡**（虛線邊框 + 暫時標籤，來源就是 `answer_preview` 的 SSE 推播），正式訊息寫入資料庫後自動被真實訊息取代。刻意與正式訊息樣式區分，避免把串到一半的 JSON 動作誤當成最終答覆 |
| ⬇ 匯出對話 Markdown | 聊天欄「匯出 MD」把目前對話匯出為 `.md`（會話 ID、模型、匯出時間、訊息數標頭 + 按角色分段 + 時間 + 附件參照），便於貼到別處或歸檔 |
| 工具結果雙檢視 | 時間軸每步的「查看結果」提供 **呈現 / JSON** 兩種檢視：呈現檢視把短純量排成鍵值列、長文字與物件用**和對話完全同一套程式碼區塊**呈現（語言按參數裡的副檔名或工具名推斷，如 `gantry/demo.py` → Python；`validate_python` → Python），因此同樣有語言標籤、語法醒目提示、行號、摺疊、複製與「…」選單；JSON 檢視保留原有的結構化瀏覽 |
| 🌐 多語言（簡 / 繁 / 英） | 側欄語言按鈕循環切換（`简 → 繁 → EN`），選擇記在 localStorage，重新整理後保留；切換即重繪會話清單、對話、時間軸與各面板。**涵蓋範圍**：全部靜態介面 + 全部動態文案（toast、對話方塊、面板、記憶/技能、回滾與刪除明細、示範資料、程式碼區塊與時間軸標籤）。**實作取捨**：以中文原文當 key（`tr('取消任务')`），缺翻譯回落原文——不會出現 key 對不上導致介面空白；英文手寫，**繁體由簡繁片語表 + 字元表自動轉換**並疊加台灣用語覆寫表（記憶體/檔案/設定/匯出/停機/強行/手冊…）。只翻譯展示文案，**判斷邏輯裡的字串一律不翻**；**JS 產生的 HTML 屬性走 DOM 層翻譯**（`translateAttrs()`，不改原始碼——把範本/拼接串裡的屬性包成 `tr()` 極易寫壞，實測踩過三次）。後端與 Agent 協定文案保持中文，主要 API 的 note 帶 `note_code`，前端據此本地化 |
| ⚡ 子代理並行扇出 | **API 提供者模型真並行**：`subagent_many` / `POST /v1/agent/fanout` 會把 2~6 個互不依賴的子任務按並行上限（`agent.subagent_parallel`，預設 4）同時跑，結果保序彙總、單項失敗不影響其餘；**網頁 Copilot 通道按自動化視窗數並行**（`agent.copilot_windows`，1–4）：每個子代理獨佔一個分頁，父任務自己佔一個，所以並行上限是「視窗數 − 1」；視窗數 = 1 時仍是循序執行（與以前一致）。並行的前提是把「目前會話 / 是否允許網頁工具 / 子代理遞迴深度 / 頁面」從全域變數改成**任務級上下文**（contextvars），否則並行子代理會互相覆寫 |
| 對話閱讀體驗 | 訊息卡片（頭像/角色名/時間戳/滑鼠停留複製）；助手訊息按內容型態**視覺化**：JSON 動作 → **工具卡**（工具名 + 原因 + 參數 JSON 檢視，`content_b64` 顯示為「[Base64 內容，共 N 字元]」）、批次動作 → 一卡多工具、`{"type":"final"}` → 內文按 **Markdown** 呈現並標「最終答覆」、其它 JSON → JSON 檢視、```json 程式碼區塊也用 JSON 檢視；解析不了的 JSON 明確標「JSON 解析失敗（原文）」而不是裝作沒事 |
| Markdown 程式碼區塊 | 對齊 Copilot 觀感：左側**行號**（橫向捲動時固定不跑）、**語言顯示名**（PowerShell/Python/…）、**軟換行**（預設折行，可在「…」裡關掉）、複製圖示與「**…**」選單（複製程式碼 / **下載為檔案**（按語言給副檔名）/ 行號開關 / 換行開關 / 摺疊）。**點行號選取整行**（可多行），程式碼列隨即出現「**複製選取 N 行**」（複製文字不含行號）。**語法醒目提示 11 種語言**（python/js/ts/json/bash/powershell/sql/yaml/html/css + ```diff/patch：新增綠、刪除紅、@@ 區塊頭藍）；超過 22 行摺疊，偏好記住選擇 |
| 圖片與附件 | Markdown 的 `![alt](url)` 呈現為圖（**僅 https/http 與 data:image**，svg 與 `javascript:` 不放行）、點擊放大，斷鏈顯示「圖片載入失敗」佔位而不是破圖圖示；**貼上的圖片附件**在訊息下方顯示縮圖、點擊放大（訊息列記錄檔名，經 `GET /v1/uploads/{name}` 供圖，走統一驗證），**會話清單**對帶附件的會話顯示 📎 標記 |
| 其餘閱讀輔助 | 長回答超過 1800 字摺疊；**🔍 對話內搜尋**（醒目標示命中、Enter/Shift+Enter 跳轉；不會命中行號欄）；新到達訊息淡入（尊重 `prefers-reduced-motion`）。全部零相依手寫，不引任何 CDN（離線可用） |
| ⏻ 終止服務 / 退出 | 側欄右上電源圖示（**任何狀態下都可達**）與會話「更多」選單裡都有；點擊彈出**二次確認**（說明正在跑的任務會中斷、狀態已寫入資料庫可復原）。後端 `POST /v1/shutdown` **先回應再停機**（否則前端看到連線重設像失敗），走 uvicorn **優雅停機**（`should_exit`，結束代碼 0），拿不到 server 物件時退回 SIGINT，並始終有「寬限期 8 秒後強制結束」保底——按鈕必須真的能停掉服務。停機後頁面停止一切輪詢並顯示「服務已終止」橫幅 |
| 提供者串流輸出 | `stream_provider_agent`（預設 `true`）開啟時，提供者回合走串流：增量邊收邊寫 `answer_preview`，經 **SSE 即時推播**到介面（見下），是真的逐字增長。**串流仍帶 `response_schema`**（結構化動作協定的解析率不犧牲）；只在**一個增量都沒收到**時才退回非串流（已收到內容再重發會造成重複計費/兩份回答） |
| SSE 即時推播 | WebUI 用 `EventSource` 連 `GET /v1/agent/sessions/{sid}/events`，由伺服器端**推播** runtime 快照、佇列變化、新訊息、會話清單變化——正常情況下不再輪詢。連線建立先送一份目前快照；15 秒無事件發一次心跳（`: ping`）防中介設備掐斷；斷線自動取消訂閱（不留記憶體）並**退回輪詢**（保留 350ms 快速輪詢 + 按退避重連）。`api_token` 走 `?token=`（EventSource 不能帶自訂標頭，驗證中介軟體已支援） |
| 📄 初始提示詞 | 面板標頭按鈕，查看該會話**首回合實際發給模型的完整提示詞**（系統提示詞 + 工具清單 + 專案慣例 + 驗證閉環規則 + 任務）——排查「Agent 為什麼這麼做」的第一現場。首回合內容會寫入資料庫（`initial_prompt`），不再被後續回合覆寫；**舊會話**未寫入時按目前工作區**近似重建**並在標題裡標註 |
| 🧾 任務完成卡片 | 任務跑完（completed/failed/cancelled）後，對話區底部出現「本次變更 N 個檔案 +X −Y」：逐檔案列出路徑與行級增刪（對齊 DSH 的 *Edited N files*），變更大的排前面；超過 6 個摺疊為「全部 N 個檔案」。點一列**看 unified diff**（重用檢視浮層，可複製）；`present` 登記的交付物以 chip 列出。資料來自**寫入前的快照原像**（不依賴 git），二進位檔案（xlsx/docx/圖片）只列路徑。介面：`GET /v1/agent/sessions/{sid}/changes`、`GET …/changes/diff?path=`（只允許快照裡記錄過的路徑，杜絕任意讀檔案） |
| 訊息佇列 | 繼續對話送出的訊息**入佇列即回傳**，不阻塞介面；任務正在跑時再發訊息會排隊顯示「已排隊（前面還有 N 則）」，處理完自動把回覆刷進聊天區。聊天頁佇列條只列**等待中**的訊息（正在處理的那則不顯示，進度看執行狀態面板），可**逐則編輯/刪除**（✎ / ×，與訊息歷史同步）。執行狀態面板顯示「佇列: N 則待處理」。支援貼上圖片（截圖）隨訊息一起送出 |
| ⚡ 強行插入（**Ctrl+J**） | 任務跑著的時候，用 **Ctrl+J**（或聊天區「⚡ 強行插入」按鈕）插入一條指令，並**立即中斷目前回合**：提供者會話直接取消那次 HTTP 請求（無狀態，取消即「這輪沒發生」）；Copilot 網頁會話**只在回答尚未開始產生時**停下等待（一旦已開始產生就讓它答完，避免遺失回答、以及「被放棄的回答」與新指令應答錯位）。插入方式可選（記住上次選擇）：**立即遵守**（`queue=step`：進 next-step 佇列，附加到目前執行中，且**批次執行途中搶先放棄剩餘動作**）/ **整輪重排**（`queue=turn`：目前回合作廢、按新指令重新規劃本回合）。插入會寫進訊息歷史並在時間軸留下 `user_inject`/`user_interrupt`/`step_preempted` 步驟；會話閒置時自動轉為排隊跟進 |
| 新話題直接產生檔案 | Copilot 網頁通道的**首輪**會附一條建議：任務要產出新檔案時，優先一次 `copilot_file(path, instruction)` 讓 Copilot 產生並下載——檔案內容不經過聊天訊息，不受單則輸出上限約束（大檔案一個回合拿到，不必 `write_text_b64` 分塊寫、也不會截斷）。只在首輪、且僅 Copilot 通道（provider 會話沒這個工具，提示了只會白燒回合）；**不寫進系統提示詞**（那裡要保持逐字穩定以命中 prompt cache）。另：`copilot_file` 已歸入 core 寫工具、**不參與關鍵字裁剪**——原先它在 copilot 組（關鍵字=圖片/截圖/附件），任何「產生檔案」類任務都會把它裁掉，模型看不到該工具，等於把這條路堵死 |
| 步驟時間軸 | 每步帶時間與狀態；「查看結果」展開為**呈現 / JSON 雙檢視**（見上）；右上「⬇ 匯出」下載完整時間軸 JSON（排查問題首選） |
| PATCH / DELETE | `/v1/agent/sessions/{sid}/messages/{mid}` | 編輯 / 刪除一則訊息（只改本地歷史；提供者會話同步記憶體上下文，回傳 `history_synced`） |
| GET | `/v1/search?q=&limit=` | 跨會話搜尋對話內文（LIKE 萬用字元已跳脫；排除封存會話；limit 上限 100） |
| POST | `/v1/agent/sessions/{sid}/tag` | 設定會話標籤（`{"tag": "..."}`，空字串取消；清單按標籤分組用） |

### 長期記憶（跨會話）

除了 AGENT.md 與 skills，Agent 還有一套**長期記憶**：把以往任務裡驗證過的零散事實
累積下來，後續相關任務自動檢索注入（關鍵詞+中文二元組比對，零相依）。

- 寫入：Agent 在收尾時自行呼叫 `memory_write`；也可讓它在任務中隨時記。
- 檢索：`memory_search`；提示詞裡只注入**最相關的幾條**（不是全量），衝突時以目前事實為準。
- 刪除：`memory_forget`，或在 WebUI/介面 `DELETE /v1/agent/memory/{id}`。
- 範圍：`global`（所有任務）與 `project`（僅目前工作區），互不洩漏。
- 管理：`GET /v1/agent/memory?query=&scope=` 查看清單與統計；上限 500 條/範圍，
  超出按 pinned → 使用次數 → 更新時間淘汰；重複寫入自動合併。
- 什麼時候用記憶而不是 AGENT.md：需要**每回合全文遵守**的硬慣例寫 AGENT.md；
  「做過什麼、踩過什麼坑」這類可檢索經驗寫記憶。

### 2. 寫 AGENT.md 與 skills（兩級範圍）

| 範圍 | 位置 | 生效範圍 |
|---|---|---|
| **全域** | 服務安裝目錄：`AGENT.md`、`skills/<名字>/SKILL.md` | 所有工作區、所有任務 |
| **專案** | 工作區根目錄：`AGENT.md`、`skills/<名字>/SKILL.md` | 僅該專案 |

- 兩份 `AGENT.md` **合併注入**（各自 ≤8000 字元，可用 `agent_md_max_chars` 調整），同路徑自動去重，**專案級優先**
- 技能**漸進式揭露**：提示裡只放「名稱 + 一行簡介」，Agent 判斷相關時用 `skill_read` 取全文；同名技能專案級覆寫全域
- **Agent 可自行沉澱技能**：任務收尾時它會先 `skill_list` 查有沒有相關技能，沒有就用 `skill_create` 新增（預設寫專案級 `工作區/skills/<名字>/SKILL.md`；要寫全域 `skills/` 必須開啟 `allow_self_upgrade`）。技能建立即時生效（下次任務上下文即出現），無需重啟；關閉入口用 `allow_skill_write: false`
- SKILL.md 支援 YAML frontmatter，缺省退回「首個一級標題 + 首個段落」：

```markdown
---
name: webui-change
description: 修改 WebUI 時的結構規範與驗證清單
---

# WebUI 修改规范
（正文：Agent 读到全文后照着执行）
```

- Agent 也可以**自行更新**這兩類檔案（沉澱有效結論），這類改動會在時間軸標記 `instruction_update`，且可透過回滾撤銷

### 3. 回滾（改了東西想還原）

寫入操作**執行前**會自動把目標檔案的原始映像存入 `data/snapshots/`。版本按 **Copilot 回合**計數——一次 complete 產生一個版本（同一回合內的多次寫入算同一個版本），因此既能把整個任務**回滾到開始前**，也能**回滾到某個回合**（撤銷該回合之後的改動）。三個管道：

```powershell
# ① WebUI：工作階段「更多 ▾ → 回滾任務改動…」（有多個回合版本時會分組列出，
#    輸入 0/留空 = 整個任務回滾；輸入版本號 = 回滾到該回合；執行中會拒絕，先暫停）

# ② HTTP API
curl -X POST http://127.0.0.1:8765/v1/agent/sessions/<sid>/rollback -H "Content-Type: application/json" -d '{"version":3}'
#    不帶 body 或 version 為空 = 整個任務回滾

# ③ 離線 CLI（服務/WebUI 起不來時也能用）
python scripts/rollback.py --list                      # 查看有哪些可回滾
python scripts/rollback.py --last --yes                # 回滾最近一次任務的工作區改動
python scripts/rollback.py --sid <id> --versions        # 列出可選的回合版本點
python scripts/rollback.py --sid <id> --rev 3 --yes     # 回滾到回合 3
python scripts/rollback.py --scope self --last --yes   # 回滾最近一次「服務自升級」
```

規則：修改過的檔案還原內容、任務新建的檔案被刪除；單一路徑保留的**回合版本**上限 6（最早 + 最近幾個），單一檔案 8MB、單一工作階段 800 條（超限記入 `skipped`，那些路徑不回滾）。

### 4. 服務自升級（預設關閉）

在 `config.json` 中把 `agent.allow_self_upgrade` 設為 `true` 並重新啟動後，Agent 獲得 `self_*` 工具，可以修改**服務自身的程式碼**（即使目前工作區是別的專案）：

```powershell
# 開啟後確認狀態
curl http://127.0.0.1:8765/v1/agent/self/status
```

給 Agent 的任務範例：「讀 gantry/tools.py，把 outlook_search 的 max_count 上限從 100 提到 200，然後自升級驗證並跑單元測試」。

內建護欄：

1. **白名單**：`gantry/`、`webui/`、`scripts/`、`docs/`、`tests/` + 若干頂層檔案；`config.json`、`data/`、`EdgeProfile/`、`.git/` 一律拒絕
2. **驗證門**：寫完必須 `self_validate`（整個安裝目錄語法檢查）+ `self_unittest`（服務自身單元測試）雙通過，提前返回 final 會被 `validation_gate` 攔下
3. **獨立快照**：自升級改動存 `data/snapshots/_self_<工作階段>/`，與工作區改動分開回滾
4. **重啟標記**：寫入磁碟時落下 `data/.self_upgrade_pending`，你重新啟動服務後標記自動歸檔，啟動記錄會印出「已載入上一輪自升級改動」
5. **失敗兜底**：若升級把服務改到起不來，`app.py` 匯入失敗時會直接印出回滾命令；用 `python scripts/rollback.py --scope self --last --yes` 還原（該命令不依賴服務）

> ⚠️ 自升級等於把服務程式碼交給模型改。建議：先確保 `tests/` 能跑；一次只讓 Agent 做一件事；升級後先重啟看能否正常啟動與跑測試，確認無誤再繼續下一個改動。

### 5. 大檔案：讓 Copilot 直接產生再下載
寫大檔案的傳統路徑是 b64 分塊（每塊 ≤3000 字元，受 Copilot 單條輸出上限約束）。更省事的是讓 Copilot 自己產生檔案、橋接下載：

```json
{"type":"tool","tool":"copilot_file","arguments":{
  "path": "minesweeper.py",
  "instruction": "实现一个可运行的扫雷游戏，命令行版，带难度选择"}}
```

- 內容**不經過聊天訊息**，所以沒有輸出上限、沒有截斷、沒有分塊，一個回合拿到完整檔案
- 橋接會認 `blob:`/`data:` 連結與「下載」按鈕兩種入口；多個入口時按檔名優先
- 寫入磁碟前照常存原始映像（可回滾）；`.py/.json/.html` 仍需 `validate_python` + `run_unittest` 通過才算完成
- 關掉它：`agent.allow_copilot_file: false`
- **模型要求**：產生下載入口需要**非 Auto** 模型（Auto 通常只在聊天裡回答）。工作階段模型是 Auto 時，設定 `agent.copilot_file_model`（例如 `copilot-gpt-5-6-sol-think-deeper`）會自動改用；沒設則工具會明確回報「Auto 不產生下載入口」，Agent 可據此退回分塊寫入。
- 回報「沒有找到下載入口」時看服務目錄的 `last_answer.html`（回答區現場快照），把結構發給維護者；同時 Agent 會自動改用分塊寫入

**什麼時候用哪個**：內容由 Copilot 產生（新寫一個大檔案/Office 文件）→ `copilot_file` 最省；內容來自本地已有材料、需要精確控制（逐位元組一致的改寫、二進位搬運）→ 本地工具 + 批次動作（`{"type":"actions",...}`，一次回答多個小動作）更穩。

#### 一次成型模式（新工作階段首輪，省回合）
新工作階段用 Copilot 時，Agent 的首輪提示會要求「**一次成型**」，而不是逐步探索：

1. 直接 `copilot_file` 讓 Copilot 一次產生**完整檔案**（多檔案就連續多次呼叫），不要 list/read 逐個探索、也不要 b64 分塊拼；
2. 檔案拿到後**本地**立刻跑起來驗證（`run_python`/`validate_python`/`run_unittest`）；
3. 出錯**先本地修**（`read_text` 定位 + `replace_text` 改），本地修兩輪仍不行才帶著**具體報錯**定向問 Copilot（只改出錯那一處，不整篇重新產生）。

目標是一次任務 **1~3 個 Copilot 回合**交付。協定裡也加了「本地閉環」規則：執行、測試、小修這類本地能做的事不回問 Copilot。系統提示詞本身保持逐字穩定（prompt cache），首輪提示只加在當輪使用者訊息裡。

### 6. 擴充工具（參考 DSH 的工具面，獨立模組）

`gantry/tools_extra.py` 是**解耦的擴充工具模組**（刪掉檔案 + 取消 bridge 裡一行呼叫即可整體下線），
按組合提供並逐組開關（`agent.tools_extra`）：

| 組 | 工具 | 說明 |
|---|---|---|
| `search` | `grep` | 按**內容**正則搜尋（`find_files` 只按檔名），返回 `檔案:行號:內容` |
| `web` | `web_fetch` | 抓 HTTP(S) URL 內文（HTML 自動去標籤）；預設**拒絕內網/本地位址**（SSRF 防護） |
| `shell` | `pwsh` | 執行 PowerShell 命令（預設**需要一次性審批**）；`run_in_background=true` 起背景任務 |
| `shell` | `job_list` / `job_output` / `job_kill` | 背景任務列表 / 讀輸出（可等待）/ 終止 |
| `plan` | `todo_write` | 任務清單（長任務追蹤進度） |
| `plan` | `present` | 宣告交付物（最多 4 個檔案，供 WebUI 展示） |
| `vision` | `read_image` | 圖片中繼資料（格式/尺寸/大小）+ 交給 Copilot 視覺描述 |
| `ask` | `ask_user_question` | 任務中途向使用者提問並**阻塞等待**；WebUI 會彈出提問條，提交後繼續 |

設定（`config.json` → `agent`）：

```json
{
  "allow_shell": true,             // pwsh 總開關（false 整體停用）
  "shell_timeout_seconds": 120,
  "allow_web": true,               // web_fetch 總開關
  "allow_private_web": false,      // true 才允許抓內網/本地位址
  "question_timeout_seconds": 600, // ask_user_question 等待上限
  "tools_extra": {"search": true, "web": true, "shell": true,
                  "plan": true, "vision": true, "ask": true}
}
```

安全約定：`pwsh` 與 `delete_path`/`outlook_send`/`run_python` 一樣走**一次性審批**
（`approval_policy: "ask"` 時 WebUI 逐次授權，用後即焚）；`read_image` 的視覺分析與
`ask_user_question` 依賴 Copilot 網頁/WebUI，沒有對應管道時優雅降級（返回中繼資料/明確報錯）。

### 5.6.2 外掛安裝 / 升級 / 解除安裝（版本化）

```bash
gantry plugins install ./my-plugin           # 本地目錄
gantry plugins install ./pkg.zip             # zip（拒絕 ../ 越界條目，防 zip-slip）
gantry plugins install https://x/y.git       # git clone --depth 1
gantry plugins registry                      # 已安裝版本/來源/備份
gantry plugins uninstall my-plugin           # 解除安裝（預設留備份）
```

HTTP 等價：`POST /v1/plugins/install {source, name?, force?}`、`POST /v1/plugins/uninstall`、
`GET /v1/plugins/registry`。

- **版本比較**：新版覆蓋舊版並**自動備份**到 `data/plugin-backups/<name>-<時間>/`
- **降級預設拒絕**（避免誤把新功能換回舊版），確需降級用 `force=true` / `--force`
- 安裝後立即載入（工具/介面/面板一起生效），無需重新啟動
- 解除安裝會先摘除執行期貢獻（工具/路由/面板）再刪目錄

### 5.6.1 外掛 UI：宣告式面板

外掛可以往介面加**面板**（`permissions` 需含 `ui`），但只能宣告式：

```python
ctx.register_panel({"id": "schedules", "title": "定时任务", "view": "table",
                    "api": {"method": "GET", "path": "api/list"}})
```

- `view` ∈ `kv` / `list` / `table` / `json` / `markdown`；外掛只給**資料介面**，
  渲染由前端完成——**不接受外掛注入 HTML/JS**（外掛是第三方程式碼，等於開放 XSS 面）
- 面板入口：外掛管理彈窗 →「外掛面板」，或 `GET /v1/plugins/panels`
- 解除安裝外掛會同時摘掉它的面板（不留點開就 404 的入口）
- 內建範例：`schedule` 的「定時任務」表、`mcp` 的「MCP 伺服器」JSON

### 5.7 斜線命令（`/`）

新工作階段輸入框以 `/` 開頭即進入命令模式（面板可用鍵盤操作）：

| 命令 | 別名 | 作用 |
|---|---|---|
| `/research <問題>` | `/深研`、`/dr` | 走深研：多角度搜尋 + 抓內文 → 帶來源的報告（新工作階段） |
| `/agent` | `/執行` | 切到 **Agent 執行** 模式 |
| `/chat` | `/對話` | 切到 **一般對話** 模式 |
| `/help` | `/?`、`/幫助` | 重新列出全部命令 |
| `/goal <目標>` | `/目標` | **目標模式**：先 `create_goal` 再分多輪推進，目標未完成時返回 final 會自動續輪（輪次上限預設 10），達成/受阻時 `update_goal` 收尾 |
| `/plan <任務>` | `/計劃` | **計劃模式（強制唯讀）**：探索階段寫入操作會被 `SANDBOX_PLAN_MODE` 拒絕；模型用 `exit_plan_mode(plan)` 提交方案即**結束本輪**，等你確認後再執行 |
| `/server.模板名 參數…` | — | **MCP prompt 模板**：自動按 MCP server 提供的模板產生命令，渲染結果載入輸入框（未設定 MCP 時不出現） |

- 命令註冊在 `webui/app.js` 的 `SLASH_COMMANDS`，加新命令只需追加一條 `{id,label,aliases,desc,run}`
- 不帶參數按 Enter = 補全等待輸入；帶參數按 Enter = 立即執行
- 面板文案走 i18n（i18n 稽核會主動展開面板檢查）

### 5.6 外掛系統（與 DSH 外掛語義相容）

Gantry 的外掛是「**語義相容** DSH」而不是「直接跑 DSH 外掛」：DSH 外掛是 npm 套件 + Cordis
（JS `apply(ctx)`）在 Node 裡跑；Gantry 是 Python 服務。對齊的是**契約**——同形清單、
同一套貢獻點、同樣的載入→apply→解除安裝生命週期、同樣的 `id + disabled + config` 啟停語義。
（要直跑 JS 外掛需要內嵌 Node，不在本系統目標內。）

**清單**（`plugins/<name>/plugin.json`，也接受 `package.json` 裡放 `"gantry"` 欄位，
`"dsh"` 欄位原樣透傳供遷移參考）：

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

**入口**（`plugin.py`）：

```python
def setup(ctx):                      # ctx ≈ DSH/Cordis 的 ctx
    ctx.register_tool("web_search", handler, "说明", {"query": "..."})
    ctx.register_api("GET", "api/search", handler)   # → /v1/plugins/<name>/api/search
    ctx.on("step_done", callback)                    # session_start / step_done / session_end
def teardown(ctx): ...
```

**ctx 能力**：`register_tool` / `register_provider` / `register_api` / `on` /
`state` + `save_state` / `read_text` + `write_text`（限外掛目錄）/ `log`。
權限 `tools/providers/api/ui/events/storage/files` 必須在清單裡宣告，否則呼叫報錯。

**管理**：WebUI「更多 ▾ → 外掛管理…」可啟用/停用/重新載入；對應 API
`GET /v1/plugins`、`POST /v1/plugins/{name}/{enable|disable|reload}`。
啟停記錄在 `data/plugins.json`，**不需要重新啟動服務**（停用即摘掉工具/提供者/路由/事件）。

**內建外掛（現代 bot 的核心能力，按需啟用）**

| 外掛 | 能力 | 依賴 |
|---|---|---|
| `web-search` | `web_search` 聯網搜尋 | DuckDuckGo 免 key；可選 BRAVE/TAVILY/SERPER_API_KEY |
| `research` | `deep_research` 多角度並行搜尋 + 抓內文，返回證據包 | 無 |
| `schedule` | `schedule_add/list/remove/run` 定時任務（every / daily / once），到點在**獨立新工作階段**跑任務；`workspace` 指定工作目錄（預設目前工作區） | 無 |
| `image-gen` | `image_generate` 文生圖，寫入磁碟到 `workspace/generated/` | DASHSCOPE_API_KEY 或 OPENAI_API_KEY |
| `mcp` | **MCP 用戶端**：連接 MCP server（stdio / HTTP），把工具與資源註冊成 `mcp__<server>__<tool>` | 無（本地處理程序或 HTTP 端點） |

**Combined = Grok 式 bot**：即時搜尋（web-search）+ 深研（research）+ 定時任務（schedule）+
生圖（image-gen）+ 跨工作階段記憶（memory）+ 子代理/編排（subagent/ralph/workflow）。

> 生圖需要**有效的** API key：本機現有 `DASHSCOPE_API_KEY` 實測 401（無效），
> 換一個有效 key 或改用 `OPENAI_API_KEY` 即可；外掛會返回結構化錯誤而不是 500。

**內建範例：`web-search`**（現代 bot 的核心能力）
- 工具 `web_search(query, max_results=6)`：DuckDuckGo 免 key；設了
  `BRAVE_API_KEY` / `TAVILY_API_KEY` / `SERPER_API_KEY` 會自動優選
- 介面 `GET /v1/plugins/web-search/api/search?q=...&n=6`

### 5.8 MCP（Model Context Protocol）

Gantry 當 **MCP 用戶端**：連上外部 MCP server，把它的工具與資源註冊進 Agent，模型就能像用內建
工具一樣呼叫。支援兩種傳輸：

| 傳輸 | 設定 | 說明 |
|---|---|---|
| **stdio** | `{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","C:/data"]}` | 本地處理程序，主流方式 |
| **HTTP** | `{"url":"https://host/mcp","headers":{"Authorization":"Bearer ..."}}` | streamable HTTP；回應是 `application/json` 或 `text/event-stream`(SSE) 都能解析，自動維護 `Mcp-Session-Id` |

```bash
# stdio
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/add \
  -H 'Content-Type: application/json' \
  -d '{"name":"fs","spec":{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","C:/data"]}}'

# HTTP + 驗證標頭
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/add \
  -H 'Content-Type: application/json' \
  -d '{"name":"remote","spec":{"url":"https://example.com/mcp","headers":{"Authorization":"Bearer xxx"}}}'

curl http://127.0.0.1:8765/v1/plugins/mcp/api/list            # 連線狀態 / 工具 / 資源
curl -X POST http://127.0.0.1:8765/v1/plugins/mcp/api/call \
  -H 'Content-Type: application/json' \
  -d '{"server":"fs","tool":"read_file","arguments":{"path":"C:/data/a.txt"}}'
curl 'http://127.0.0.1:8765/v1/plugins/mcp/api/resources?server=fs'          # 資源清單
curl 'http://127.0.0.1:8765/v1/plugins/mcp/api/resources?server=fs&uri=file:///C:/data/a.txt'  # 讀資源
```

**能力對應**
- `tools/list` → 每個工具註冊成 `mcp__<server>__<tool>`，參數由 `inputSchema` 自動轉成參數說明
- `tools/call` → `content[].type=text` 拼成結果；`isError` 轉成工具錯誤
- `resources/list` → 每個 server 額外註冊一個唯讀工具 `mcp__<server>__read_resource(uri)`，
  工具說明裡列出可用 uri（避免把幾十個資源各註冊成一個工具，撐爆工具表）
- `resources/read` → 文字直接返回；二進位（`blob`）給出 mime 與 base64 長度
- 通用工具：`mcp_list`（狀態）、`mcp_call`（手動呼叫）、`mcp_read_resource`（讀資源）
- `prompts/list` → 每個有模板的 server 註冊 `mcp__<server>__use_prompt(name, arguments)`；
  同時 **WebUI 自動產生斜線命令**：`/server.模板名 參數1 參數2 …`，渲染結果直接填進輸入框
- `prompts/get` → 訊息序列拼成文字（`[role] 內容`）
- 通用工具：`mcp_get_prompt(server, name, arguments)`

**健壯性**
- 單一 server 起不來只記錄在 `errors`，不影響其它 server 與內建工具
- `resources/list` 屬於可選能力：用 **8 秒短逾時**探測（未實作的 server 可能不回封包，
  不探測會讓連線階段白等 30 秒）
- 設定持久化在 `data/plugins.json` 的 `mcp.state.servers`，重新啟動自動重連

> 尚未接入：舊版 **HTTP+SSE 雙端點**傳輸（2024-11-05 舊規範，只支援 streamable HTTP）。

### 5.3.4 待辦清單：從 TODO 檔案自動同步 + 按清單派子代理

使用者實測痛點（pyshot 整改）：說「按 todo 完成整改」之後，**面板清單一直 0/5**，
而子代理面板裡那 4 個還是上一階段的遺留，「像擺設」。

**① TODO 檔案自動同步**（`gantry/todo_sync.py`）
- 每步開始時按 **mtime** 檢查工作區裡的 `*todo*.md`（也認 `.txt`），解析 `- [ ] / - [x]`
  核取方塊（沒有方框時退化成 `- 項` 列表），寫進工作階段 `todos`；
- **不覆蓋進度**：同一項已是 `completed` 不會被檔案裡的 `[ ]` 拉回；檔案裡刪掉的
  已完成項會保留；檔案消失時**不清空**現有清單；
- 之前 Gantry 完全不看 `pyshot_todo.md`（23KB 的計劃檔案），面板只認 `todo_write`。

**② 按清單自動派子代理**（`gantry/auto_team.py`，**預設關閉**）
- 條件：`agent.auto_team_enabled=true`、存在 **≥2 個** `pending` 且**作用域不重疊**
  （按檔案路徑/識別符判斷互不依賴）、目前沒有子代理在跑；
- 動作：把這幾項轉成自足的任務文字（目標、驗收、回報格式）交給 `fanout`，
  子代理 id 記回對應待辦，清單裡能看到「誰在跑」；
- 為什麼預設關：每個子代理都是一條獨立 Copilot 對話，**成倍消耗額度**。

設定項：

```json
{"agent": {"auto_team_enabled": false, "auto_team_max_agents": 4, "auto_team_max_steps": 30}}
```

**③ 子代理面板可摺疊**：標題列「摺疊 / 展開」，狀態記在 `localStorage`，重新整理後保持。

### 5.3.3 測試指令碼的瀏覽器回收

連續跑 UI 稽核/截圖指令碼會在系統裡累積洩漏的 Playwright 瀏覽器（實測 **21 個** msedge，
每個都帶 `--user-data-dir=%TEMP%\playwright_*`）。它們既吃記憶體，也會讓人誤以為
「有程式在搗亂」。`scripts/reap_browsers.py` 按年齡回收（預設 >15 分鐘的才終止，
不影響正在跑的指令碼），`scripts/regression.py` 啟動時會自動呼叫它。

```bash
python scripts/reap_browsers.py            # 只報告
python scripts/reap_browsers.py --kill     # 回收
```

### 5.3.2 長命令與「看不到在跑什麼」

- **`pwsh` 逾時會終止整棵處理程序樹**：只 `proc.kill()` 會留下孫處理程序（`python`/`git`/`pip`）
  繼續持有 stdout 管道，`communicate()` 永遠等不到 EOF——實測一條 `pip install PySide6`
  把任務卡住 10 分鐘、逾時形同虛設。現在 `taskkill /T /F`（POSIX `killpg`）+ 5s 兜底
  取回部分輸出，錯誤裡直接提示「換策略：拆小命令 / 用 run_in_background + job_output」。
- **即時面板顯示正在執行的步驟**：`runtime.phase` 在 step 開始時置為 `executing`
  （`正在執行第 N 步 · 工具名`），結束後回到 `thinking`。以前整個執行期間介面都停在
  上一輪的 "waiting · 等待 Copilot 回答"，看著像當掉。
- **時間軸併入即時面板**：`目前執行狀態 / 目前回答 / 最近發送內容 / 繼續對話 / 步驟時間軸`
  五個分頁共用一個面板（像 DSH 一屏看全），時間軸在該分頁內捲動；篩選與匯出照舊。

### 5.3.1 編輯工具：容錯匹配 + 修補（治「改檔案失敗→回合爆炸」）

痛點：模型抄片段時縮排（tab/空格、層級）、行尾空白稍有出入，`replace_text` 就匹配失敗，
於是「讀一遍—猜一遍」循環，實測把一次小改拖成幾十上百回合、token 直接爆掉。

- **`replace_text` 多級降級匹配**：精確 → 行尾空白 → **行首縮排彈性** → 空白摺疊。
  唯一命中即採用，結果裡回傳 `match` 策略並在 `note` 裡說明「這次是容錯匹配」；
  **新片段按檔案實際縮排重排**（tab 檔案不會寫成 4 空格、巢狀層級不會錯位）。
- **失敗診斷**：不再只說「未找到」——給出**首個差異的行/列 + 兩側原文**，讓模型一輪改對。
- **`apply_patch(path, patch|edits)`**：一次呼叫多處修改，**原子性**（任一塊失敗則整體不寫）。
  支援 Codex 風格修補（`*** Begin Patch` / `*** Update File:` / `@@` 分塊，
  ` `=上下文 `-`=舊 `+`=新）或簡單 `edits: [{old,new,count?}]`；
  每塊同樣走容錯匹配，結果回報每塊的 `match` 策略與行號。
- 已有 `replace_lines`（按行號改，完全不依賴匹配）與 `convert_indentation`（批次換縮排），
  三者分工：**一處**用 `replace_text`、**多處**用 `apply_patch`、**整段/縮排**用 `replace_lines`。

實測（真實回合，DeepSeek）：tab 縮排 + 行尾空白的 `maths.py` 加三處註解 →
**10 步、0 失敗**；對抗性場景（模型給 4 空格片段改 tab 檔案）→ `match=indentation`、
結果仍是 tab；兩處修補一次性原子套用。

### 5.4.0 顯式扇出（不依賴模型判斷）

```bash
gantry fanout --task "审计 workspace/core" --task "审计 workspace/webui" --model kimi-coding/k3
```
或在輸入框裡：`/fanout 任務1 | 任務2 | 任務3`

- 每個任務各派**一個子代理**（獨立工作階段），跑完把結果寫回一個父工作階段（`fan-*`）並在介面裡開啟
- 提供者模型可並行；網頁管道仍按順序跑（`fanout` 不占父視窗，子代理各自獨占視窗，但預設視窗數 = 1）；單項失敗不影響其餘，失敗原因寫進父工作階段訊息
- 上限 8 個；`POST /v1/agent/fanout` 是同一能力，供指令碼/CI 呼叫
- 與「模型自己決定要不要用子代理」互補：提示詞 + 執行期提示讓它想起來，這個入口讓它**必定發生**

### 5.4.1 對抗式稽核（多個稽核員 + 裁判，循環到只剩小問題）

`/audit 稽核目標說明`（或對話框輸入 `/audit` 按 Enter）：背景開一個 `audit-*` 工作階段：

1. **N 名稽核員**（預設 3 個不同角度：正確性 / 健壯性 / 安全 / 一致性）各自唯讀審查，
   只報「有證據」的問題（檔案 + 行號 + 分級 p0/p1/p2），輸出 JSON 清單；
2. **裁判**親自用 `read_text` 核驗每條證據，合併去重、剔除誤報、重新分級，
   給出 verdict：`clean`（沒有）/ `minor`（僅剩 p2）/ `major`（還有 p0/p1）；
3. major 且開了自動修復（預設開）→ 派一個修復子代理逐個修（最小改動 + 驗證閉環），
   再進入下一輪；**直至 clean/minor 或輪次用完**（預設 3 輪，上限 8）；
4. 每輪報告與最終彙總寫進稽核工作階段的訊息裡（點開工作階段就能看）。

網頁管道順序執行（視窗數 1 時不搶父視窗）；提供者管道按 `agent.subagent_parallel` 並行。

HTTP：`POST /v1/agent/audit {objective, target?, auditors?, max_rounds?, auto_fix?, model?}`
背景執行，返回 `{session_id, started}`。

### 5.4 子代理：新建 / 繼續 / 分叉

| 工具 | 作用 |
|---|---|
| `subagent(task, description, model, max_steps)` | 新建子代理（獨立工作階段/獨立上下文），跑完返回結果 |
| `subagent_send(session_id, message, ...)` | **繼續**已有子代理（同一上下文追加一輪；也接受 `agent_id`） |
| `subagent_fork(session_id, description, keep_messages)` | 從某工作階段**分叉**出新子代理（複製最近對話，試另一條路） |
| `subagent_list(parent_id, limit)` | 列出子代理（狀態/步數/標籤） |

- 子工作階段記錄 user/assistant 訊息，面板點進去能看到「問了什麼、答了什麼」
- 子代理深度上限 1（子代理裡不能再開子代理，防遞迴）
- 繼承父工作階段的工作階段級授權（不重複彈審批）
- HTTP：`POST /v1/agent/subagents/{sid}/send`、`POST /v1/agent/subagents/{sid}/fork`

> 實作注意：`subagent_send` **不能**走 `run_agent`——父回合正持著自己的自動化視窗，
> 再去搶同一個視窗會死結（實測卡滿 600 秒用戶端逾時）；它與 `subagent` 共用
> `_drive_child` 自建 runner（有父頁面時複用父頁面，沒有時自己占一個視窗）。

### 5.5 子代理面板（看有哪些子代理、在跑什麼）

用 `subagent` / `ralph` / `workflow` 派生出的子代理會以 `sub-xxxxxxxxxx` 獨立工作階段執行。
WebUI 在 KPI 區下方有**子代理面板**（有子代理時才出現）：

- 每列：狀態點 + 子代理描述 + 狀態/步數/目前階段
- 點一列 → 切到該子工作階段看完整時間軸；點「重新整理」立即拉取
- 資料來自 `GET /v1/agent/subagents`（可按 `?parent=<sid>` 過濾）
- **沒有就不顯示**：面板與待辦條都只在目前工作階段確實有內容時出現；點「+ 新任務」或切換工作階段會
  立即清空上一輪的待辦/子代理（包括外層空卡片），不等新工作階段的第一份 runtime 快照。

子工作階段會記錄 `parent_id` 與 `description`（工作階段表新增兩欄，自動遷移）。子代理**繼承父工作階段的
工作階段級授權**（父授權了 pwsh，子代理不再彈），但逐參數的一次性授權不繼承。

### 6. 常用維運命令
```powershell
python start.py                              # 啟動服務（預設 127.0.0.1:8765）
python scripts/login.py                      # 登入精靈（--reset 徹底重來；服務執行時會拒絕執行）
python -m unittest discover -s tests         # 回歸測試
python scripts/calibrate.py                  # 工作階段閾值校準（讀 data/agent.db 的實測樣本）
python scripts/outlook_selftest.py           # Outlook 分步自檢（含執行緒池/專用COM執行緒對照）
python scripts/rollback.py --list            # 查看可回滾的任務與自升級記錄
```

### 7. 疑難排解

| 現象 | 處理 |
|---|---|
| 服務起不來、匯入報錯 | 多半是自升級改壞了：`python scripts/rollback.py --scope self --last --yes` 後重新啟動 |
| 寫檔案只寫了一半、寫檔案中斷 | WebUI 匯出該工作階段時間軸發給維護者；確認用的是 b64 分塊（每塊 ≤3000 字元）；大檔案優先改用 `copilot_file` 讓 Copilot 直接產生下載 |
| 任務卡在「等待服務」 | 限流/服務不可用，指數退避自動重試；介面中央出現**倒數計時卡片**（錨定首次超限 + `service_retry_window_seconds`，預設 1 小時 1 分鐘，標題按 Copilot / API 提供者分別措辭）顯示預計恢復時間。卡片**可點 × 收起**為右下角小標（點小標可再展開），不會擋住頂部控制項，因此限流期間照樣能切換工作階段模型；新一輪限流會自動重新展開。長時間不恢復會安全暫停，服務恢復後點「恢復」。若提示的是「訊息未能發送」而非限流，那不是限流——點「切換到頁面」查看或「重試瀏覽器」重建頁面 |
| 頁面找不到輸入框/模型按鈕 | 登入狀態失效：停服務後 `python scripts/login.py`（必要時 `--reset`） |
| 頻繁換工作階段/上下文被壓縮 | 跑 `scripts/calibrate.py`，按建議調整 `compact_at_tokens`/`rollover_at_tokens` |
| Outlook 報「不可用」或 0x8000400x | 跑 `scripts/outlook_selftest.py` 定位；COM 路徑必須走專用訊息泵執行緒（`_ComWorker`），OWA 路徑看登入狀態 |
| 模型列表重新整理後看不到某些模型 | 點 ↻ 看提示：`scanned=false` 說明沒掃成；掃成了但缺少二級選單模型時，把服務目錄的 `last_menu.html` / `last_menu.txt`（每次掃描自動寫入磁碟的選單現場）發給維護者 |
| 瀏覽器層異常 | 看服務目錄 `last_page.txt`（出錯時的整頁文字快照），必要時在 `config.json` 的 `markers` 追加介面文案錨點 |
| 刪工作階段報「部分失敗」 | 看結果彈窗的 `verified-absent` 標記；仍失敗可點「重試遠端並強制清理本地記錄」 |

## 安全警告

1. **API 驗證**：預設綁定 `127.0.0.1`，僅本機可連；設定 `api_token` 後所有 `/v1/*` 端點都要求 Bearer 權杖。綁定非回環位址且未設定 token 時，**服務拒絕啟動**。`agent.workspace_roots` 可進一步把工具工作區限制在白名單根目錄內。
2. **間接 Prompt Injection**：Agent 讀取的工作區檔案內容會進入 Copilot 上下文，而回傳的動作會被直接執行。**不要讓 Agent 處理來源不可信的檔案**；開啟 `allow_python` / `allow_delete` 後，風險升級為任意程式碼執行與檔案刪除。
3. **登入狀態敏感**：`EdgeProfile/` 資料夾包含你的 Copilot 登入 Cookie，已被 `.gitignore` 排除，請勿外洩。
4. **容錯寫檔**：對 Copilot 回傳的未跳脫寫檔動作有正則容錯復原機制，復原結果可能不完美；重要檔案請先備份。`.py/.json/.html` 有語法＋單元測試驗證閉環兜底，其他類型沒有。
5. **自升級是高權限開關**：`allow_self_upgrade=true` 後，Agent 能修改服務自身的程式碼（白名單內）。雖然每次改動前都有快照、驗證不過會被閘門擋住、且有離線回滾通道，但它仍是「讓模型修改執行中的服務」——只在你能接受這個前提、且測試套件可用的情況下開啟；不要在工作區裡處理來源不可信的檔案時同時開著自升級。
6. **憑證與快照**：`data/snapshots/` 保存了被改檔案的**改動前原像**，其中可能包含敏感內容（例如曾寫入過金鑰的檔案），外傳除錯資料前請先確認；`data/` 與 `EdgeProfile/` 都不要提交或分享。
7. **跨站寫入（CSRF）**：服務預設無 token 時，任意網頁都能用 `fetch(..., {mode:'no-cors'})` 向 `127.0.0.1` 發送簡單請求（不觸發 CORS 預檢），進而取消任務、回滾服務自身程式碼、刪除工作階段——**讀不到回應不代表沒有副作用**。因此內建 `origin_guard` 中介軟體：寫入方法（POST/PUT/PATCH/DELETE）依瀏覽器訊號判定，`Sec-Fetch-Site: cross-site|same-site` 或 `Origin` 非本機／不同埠號一律 **403**；不帶 `Origin`/`Sec-Fetch-*` 的非瀏覽器用戶端（curl、評測腳本）照常放行。可用 `agent.csrf_protect: false` 關閉（不建議），用 `agent.csrf_allowed_origins` 加白名單。
8. **安全回應標頭與 CSP**：所有回應帶有 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`X-Frame-Options: DENY`（防點擊劫持——本機情境下「誘導點刪除／回滾」是現實威脅）與 CSP。CSP 為
   `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`。
   **`script-src` 不含 `'unsafe-inline'`，這是硬性限制**：
   - 頁面裡**不允許**再寫內聯事件處理器（`onclick="..."`）：一律用 `data-act="名稱"`（輸入類用 `data-act-input`，鍵盤類用 `data-act-key`），並在 `webui/app.js` 的 `ACTIONS` 表中註冊實作——分派器會依 `data-act` 呼叫。寫錯名稱不會報錯、只是「點了沒反應」，所以有第 14 條不變式**雙向核對** `data-act` ↔ `ACTIONS`（缺實作／有殘留條目都會失敗）。
   - 也**不允許**內聯 `<script>`：首屏主題初始化已外置為 `webui/boot.js`；新增靜態資源必須加進 `gantry/api.py` 裡 `/ui/{asset}` 的白名單（目前是 `style.css` / `app.js` / `boot.js` / `i18n.js`）。第 15 條不變式會核對「頁面引用 ⊂ 白名單」，漏加會立刻失敗。
   - `style-src` 仍保留 `'unsafe-inline'`：頁面大量使用 `style="..."` 屬性與動態注入的 `<style>`，影響面小於腳本，暫不收緊。

## 介面多語言（簡 / 繁 / EN）

側欄工具列的「简 / 繁 / EN」按鈕循環切換，選擇存在 `localStorage.gantry-lang`。實作要點與**限制**：

- **以中文原文當 key**：`tr('取消任务')` 查 `webui/i18n.js` 的 `I18N_EN` 表；缺譯文時**回落中文**，不會出現空白或 `key.not.found`。繁體由「片語覆寫表＋字形轉換表」自動產生。
- **靜態介面**：給元素加上 `data-i18n` / `data-i18n-title` / `data-i18n-ph` / `data-i18n-aria`；此外還有一遍「DOM 直譯」（葉節點文字與 `title/placeholder/aria-label` 依字典替換）。
- **動態內容必須自己呼叫 `tr()`**：對話、時間軸、回答、日誌等容器在 `I18N_SKIP_IDS` 裡被排除（其中可能有模型輸出或使用者輸入，恰好等於某個 key 就會被誤譯）；切換語言後由 `rerenderForLang()` 重繪動態區域。
- **禁止把 `tr(...)` 寫成字串字面量**：`'tr(重试浏览器)'` 會讓介面直接顯示 `tr(重试浏览器)`。這類錯誤**語法合法、單元測試也覆蓋不到**，因此加了第 16 條不變式靜態攔截（`gantry/invariants.py`）。
- **覆蓋率稽核**：`python scripts/ui_i18n_audit.py` 以三種語言逐面掃描介面外殼（按鈕／標籤／頁籤／表頭／提示／彈窗／佔位符），回報三類問題：英文模式仍含中文、`tr(`/`data-i18n` 等原始 key 洩漏、**繁化漏字**（用獨立維護的簡繁對照表偵測，不依賴 i18n.js 自己的表——真實事故裡「浏」不在表內，拿表自身當偵測集完全看不出來）。
- **覆蓋稽核**：`python scripts/i18n_coverage.py` 在英文模式下逐一開啟 12 個介面狀態
  （首頁／新任務／手冊／外掛／面板／工作區／全域搜尋／更多／記憶／工作階段更多／填充態／模型）並蒐集**可見**中文，
  目前 **0 處**；`ui_i18n_audit.py` 只掃靜態外殼，兩者互補（實測前者多抓出 30 處：記憶面板 28 ＋主題標題＋工作區選擇）。
- **後端錯誤文案**：`HTTPException` 的 detail 由全域處理器依 `X-Gantry-Lang` 翻譯（`gantry/i18n.py` 精確表＋模式表，
  未知文案回落中文）；不變式「後端錯誤文案都有英文翻譯」守著新增的 raise。
- **使用手冊**：中英**兩套完整範本**（`MANUAL_HTML` / `MANUAL_HTML_EN`，各 18 節），依介面語言整篇切換，含自動目錄與低頻章節摺疊（摺疊章節名也隨語言走）；不變式「手冊中英兩版章節一致」防止只改一邊。
- **外掛自帶譯文**：外掛在 `plugin.json` 宣告 `gantry.i18n: {en: {...}, tw: {...}}`，
  `GET /v1/plugins/i18n` 彙總**已載入**的外掛，前端啟動／啟停外掛時合併進字典 → 第三方外掛的中文也能翻譯。
- **繁體術語表**：`I18N_TW_OVERRIDE` 依詞修正（文件→檔案、数据→資料、插件→外掛、服务器→伺服器…），
  套用時**依詞長降序**，否則「服务」會吃掉「服务器」（實測踩到）。
- **系統產生的標題**：深研工作階段標題依 `X-Gantry-Lang` 產生（`[Research] …` / `[深研] …`）；cron 等無請求上下文的仍用中文。
- **靜態屬性**：不變式「靜態屬性文案都有英文鍵」靜態核對 `index.html` 裡含中文的
  `title/placeholder/aria-label/alt` 是否都有詞條（實測補了 6 條：跟進訊息、輸入、收起、限流中…）。
- **已知取捨**：後端與 Agent 協議文案（系統提示詞、工具描述、步驟 reason／錯誤原文）一律保持中文，它們與協議和評測綁定。繁體是**字形級**自動轉換，專有名詞可能不完美，需要精修時往 `I18N_TW_OVERRIDE` 片語表裡加。

## 已知限制

- 依賴 Copilot 頁面的無障礙文字（如 `Copilot said:`）與 DOM 結構，**Microsoft 前端更新可能導致橋接失效**，出錯時請查看 `last_page.txt` 診斷快照；
- 一般對話的串流輸出基於公共前綴 diff，若 Copilot 重寫已輸出文字可能出現重複／缺漏（Agent 模式不受影響）；
- 預設 1 個自動化視窗（`agent.copilot_windows: 1`）：同一時刻只有一個回合在瀏覽器中進行；
  本預覽分支可開到 1–4 個視窗並行，見 [docs/MULTI-WINDOW.md](docs/MULTI-WINDOW.md)（有額度與記憶體代價）；
- 回滾是**任務／作用域級**的：同一任務裡混做多件不相關的改動，回滾會整塊回退，無法只挑其中一件；
- 快照只覆蓋經工具層的寫入操作；快照超限（單檔 8MB／單一工作階段 800 條）的路徑不記錄、也就無法回滾。
- **產圖需要有效 key**：`DASHSCOPE_API_KEY`（通義萬相）或 `OPENAI_API_KEY`；本機現有的 DashScope
  key 實測 401，未設定有效 key 時 `image_generate` 回傳結構化錯誤而非當機；
- **系統資料夾對話框在服務裡不可用**：由服務託管的處理程序會繼承非互動視窗站，PowerShell 的
  `FolderBrowserDialog` 能建立視窗卻不在可見桌面上（請求會掛住）。因此工作目錄選擇器只用
  **應用程式內瀏覽**（`/v1/agent/browse`）；`/v1/agent/pick-directory` 保留但 15 秒逾時回傳 400；
- **深研是「檢索＋一次綜合」**：不做多輪 follow-up 追問；來源以搜尋引擎回傳為準，不保證可存取性。

## 專案結構

```
app.py            組裝入口（shim：from gantry.api import app，uvicorn 載入點）
start.py          服務入口
install.py        依賴安裝
gantry/              核心套件
  api.py            FastAPI 應用程式與中介軟體／啟動組裝（端點已拆成 routes_*）
  routes_core.py    系統與入口端點（health/models/chat/UI/關停重啟）
  routes_config.py  設定端點（設定／沙箱／外掛／瀏覽器／工作區）
  routes_agents.py  子代理／深研／稽核／記憶／自升級端點
  routes_sessions.py 工作階段端點（列表／訊息／繼續／佇列／事件／審批／刪除）
  bridge.py         設定、執行狀態、工具組裝與接線（facade 重匯出各子模組）
  agentrun.py       回合編排：run_agent 主流程、等待回答、回合標記／快取、待辦同步
  downloads.py      回答區下載解析與 copilot_file
  workspace_files.py 工作區寫檔／建目錄／目錄選擇
  research.py       深研任務、外掛任務、扇出
  page.py           頁面自動化：新對話、模型選單、發訊息、取回答／串流更新
  browser.py        瀏覽器生命週期與視窗池（多視窗並行、供應商邏輯槽）
  answer.py         回答解析：回合標記、雜訊／思考佔位剔除、服務不可用識別
  provider.py       外部供應商通道（HTTP 回合、歷史裁剪）
  queue.py          工作階段訊息佇列（排隊／取消／插入）
  orchestration.py  ralph / workflow / 對抗式稽核
  subagents.py      子代理派發、驅動、續跑與 fork
  copilot_delete.py Copilot 側欄工作階段刪除流程（URL 身分＋同屬標記校驗＋對帳終驗）
  owa.py            Outlook 網頁版（OWA）自動化後端
  agent.py          Agent 核心迴圈、狀態治理、驗證閘、換工作階段／壓縮
  agent_prompt.py   PromptMixin：系統提示詞組裝 / AGENT.md / skills / 工具面裁剪
  agent_parse.py    ParseMixin：JSON 動作協議 / 容錯解析 / 批次復原
  agent_text.py     TextMixin：token 估算 / spill / 保頭保尾壓縮
  tools.py          工具註冊表／分派、路徑沙箱與權限閘（核心 mixin）
  tools_files.py    檔案讀寫／取代／修補／縮排／目錄操作
  tools_exec.py     validate_python / run_unittest / run_code / run_python
  tools_office.py   Excel / Word / PPT / PDF
  tools_outlook.py  Outlook（COM + OWA）
  tools_skills.py   技能庫 list/read/create
  tools_memory.py   長期記憶
  tools_self.py     自升級（讀寫／校驗／單測／回滾）
  providers.py      API 供應商接入層（OpenAI/DeepSeek/Kimi/Grok 等）
  session_store.py  SQLite 工作階段持久化、中斷復原、工作階段實測統計
  snapshots.py      改動前原像快照與任務級回滾（自升級 failback）
webui/            零依賴 Web 管理台（index.html 結構 + style.css + app.js）
AGENT.md          專案級 Agent 約定（工作區根目錄存在時自動注入系統提示）
skills/           目錄式技能庫（每個技能一個 SKILL.md，按需載入）
scripts/          工具腳本（login.py 登入精靈、calibrate.py 閾值校準、
                  outlook_selftest.py COM 自檢、rollback.py 離線回滾）
tests/            單元／整合／不變式測試（python -m unittest discover -s tests，約 1030 個用例）
```

## 免責聲明

本專案透過瀏覽器自動化使用 Copilot 網頁版，僅供個人學習與研究。請遵守 Microsoft 365 Copilot 的使用條款，勿用於高頻率、商業化或違反服務條款的用途。

## 破壞性操作的一次性審批（fail-closed）

威脅模型：`delete_path` / `outlook_send` / `run_python` 一旦被模型誤呼叫，後果不可回滾。
因此預設 `agent.approval_policy: "ask"`：**每次**執行前都要使用者下達一次授權，
授權精確到「工具＋參數雜湊」，**用後即焚**（沒有 allow-always，也沒有撤銷儲存）。

```powershell
# 查看待授權（含工作階段記錄的 approval_pending）
curl http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
# 下達一次性授權（不傳 args_hash 時依 approval_pending 精確授權）
curl -X POST -H "Content-Type: application/json" \
  -d '{"tool":"delete_path"}' http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
```

未獲授權時 Agent 不會執行該操作：它會記錄一條 `approval_required` 步驟、把狀態設為
`waiting`（並在 runtime 裡寫入 `approval_pending`），然後停下等你授權；授權後按繼續即可。
把 `approval_policy` 設為 `"never"` 表示你明確承擔這些操作的風險（不建議）。

### 授權範圍：once / session

實測痛點：`pwsh` 被攔後模型改了命令措辭，參數雜湊變化 → 又彈一次授權（逐參數太碎）。

`agent.approval_scope` 控制授權範圍：

| 值 | 含義 | 適用 |
|---|---|---|
| `"once"`（預設） | 精確到「工具＋參數」，**用後即焚** | 最嚴格；破壞性操作少時 |
| `"session"` | 一次授權後，**該工具在本工作階段內**都放行（工作階段結束即失效；不跨工作階段、不跨工具） | 連續 shell 任務，比 `never` 安全 |

```powershell
# 本工作階段級授權（scope=session 時 WebUI 按鈕自動走這條）
curl -X POST -H "Content-Type: application/json" \
  -d '{"tool":"pwsh","scope":"session"}' http://127.0.0.1:8765/v1/agent/sessions/<sid>/approvals
```

WebUI 的授權橫幅會依 `approval_scope` 顯示不同措辭（「僅對這一次執行有效」／
「僅對本工作階段有效」）；runtime 快照裡帶有 `approval_scope` 供前端判斷。

## 沙箱拒絕是結構化的

越界／敏感路徑不再只回一句「拒絕存取」，而是給出錯誤碼（`FS_SANDBOX_ESCAPE` /
`FS_SANDBOX_DENYLIST` / `FS_SANDBOX_NOT_ALLOWED`）、目前模式、原因與**可行動方案**
（改用工作區相對路徑／開啟自升級／讓使用者把檔案放進工作區）。工具參數寫錯時也會
列出該工具的**合法參數名**，避免模型反覆試探。

## 整體回歸（真跑各能力）

單元測試／稽核是白盒；這個腳本**真跑一遍面向使用者的能力**（用真實模型），幾分鐘給出結論：

```bash
python scripts/regression.py                 # 預設 kimi-coding/k3，跑完自動清理
python scripts/regression.py --skip-slow     # 跳過深研/單測/版面稽核（約 1.5 分鐘）
python scripts/regression.py --model copilot-auto --keep   # 保留現場排查
```

覆蓋 22 項：健康檢查、工具面完整性（62 個）、沙箱三檔、**計畫模式（提交方案且不寫檔）**、
一般寫入任務、子代理建立／繼續／分叉、深研（證據＋報告）、排程任務執行、MCP（stdio 工具可用＋
移除後摘除）、外掛安裝／解除安裝、外掛面板、CLI 唯讀命令、單元測試、不變式、UI 冒煙測試、i18n／版面／對比度稽核。

結果寫到 `data/regression/report.json`，失敗時結束碼非零（可接 CI）。

## 執行期不變式檢查（提交前必跑）

```powershell
python scripts/invariants.py        # 失敗結束碼 1；--json 供 CI 使用
```

它檢查那些「壞了不一定報錯、但會讓行為錯誤」的隱含契約：工具分組表與註冊表一致、
core 組最小工具面、steps 表 append-only、檢查點表與狀態取值、AgentRunner 可選回呼
向後相容、評測斷言名與實作一致、提示詞工具清單單一資料來源、破壞性工具審批契約。

## 當機復原時的檢查點語義

- **發請求前**登記回合（`turn_results.status=inflight`），成功後轉為 `done`；
- **工具分派前**登記步驟（`step_dispatch`），成功／失敗／未知分別收尾；
- 復原時若發現「已分派但未收尾」的步驟，**絕不原樣重播**：先讓模型唯讀核對現況
  （對應 DSH 的 `Its outcome is unknown.`），避免重複副作用。

## 超大工具結果會落碟（spill）

單條工具結果超過 `agent.spill_threshold_chars`（預設 20000 字元）時，**完整內容**寫入
工作區 `.spill/<工作階段>/<步驟>-<工具>.json`，上下文裡只保留「保頭保尾」的摘要＋路徑提示；
模型需要全文時用 `read_text` 讀那個路徑（路徑在工作區內，因此沙箱允許、使用者也能直接查看）。
好處：不再為了省 token 而**丟掉資訊**——以前只有一個截斷值，現在是「摘要＋可取的全文」。
上限由 `spill_max_bytes` 控制（預設 2MB）；設為 `spill_threshold_chars: 0` 可整體關閉。

## 逾時的誠實語義（結果未知 ≠ 沒發生）

COM（Outlook）呼叫**無法真正取消**：逾時只表示「我們不再等」，執行緒裡的呼叫仍在執行。
舊實作在逾時後直接放棄等待、外層還可能重試一次——等於在「第一次可能已經發出去」的情況下
再來一遍。現在改為：

1. 用 `asyncio.shield` 保護底層呼叫，**不拋棄**它，並給它掛回呼：遲到的結果會寫進日誌
   （成功會明確提示「副作用很可能已發生」）；
2. 逾時**絕不重試**同一動作；
3. 記下「結果未知」並**階段性封鎖不可冪等動作**（`outlook_send`/`delete_path`/`move_path`/
   `append_text`/`copy_path`），直到任一**唯讀動作**成功（例如 `outlook_unread`、
   `read_text`、`list_directory`）——即「先核對現況再說」；
4. 封鎖期間相關動作會回傳 `[TOOL_TIMEOUT_UNKNOWN]` 的結構化錯誤，說明該做什麼。

逾時上限可用 `agent.outlook_timeout_seconds`（預設 60 秒）調整。

## 事件日誌與投影（P2-10）

工作階段的所有關鍵動作都會寫成 **append-only 事件**（`events` 表，共 13 個發點）：
狀態遷移、回合開始／結束、工具分派／收尾、工具結果入庫（`step_recorded`）、
檔案改動（`file_event`，來自 `file_events` 的真實寫入路徑）、佇列進出（`queue`，只記數量不記正文）、
訊息元資訊（`message`，僅 WebUI 對話）、審批下達／取用／拒絕。它現在是**旁路記錄**（執行期仍讀資料庫
欄位），用途是排障、稽核，以及為將來「狀態可由日誌重建」鋪路。

```powershell
# 歷史事件（一次性回傳；kinds 可逗號分隔過濾）
curl "http://127.0.0.1:8765/v1/agent/sessions/<sid>/event-log?limit=50&kinds=turn_begin,turn_done"
# 由事件重建的狀態快照＋與資料庫欄位的一致性核對（consistency 為空=一致）
curl "http://127.0.0.1:8765/v1/agent/sessions/<sid>/projection"
```

### 巡檢：投影與欄位是否一致（P2-10 第三期前的驗收）

```powershell
python scripts/health.py            # 巡檢全部工作階段；--limit / --session / --json 可選
```

它逐個工作階段把**事件流重播**成狀態，再與資料庫欄位交叉核對，並額外檢查**事件覆蓋**
（表裡有記錄卻沒有對應事件＝覆蓋缺口）。輸出區分四類：

| 類別 | 含義 | 是否致命 |
|---|---|---|
| 一致 | 投影與欄位完全相符 | — |
| **不一致** | 有事件卻重建不出欄位裡的事實＝真缺口 | ✅ 結束碼 1 |
| **覆蓋告警** | 表裡有記錄但沒有對應事件（多為功能上線前的歷史工作階段） | ❌ 預設僅告警；`--strict` 視為失敗 |
| 早於事件功能 | 0 事件的歷史工作階段 | ❌ 跳過不計 |

設計取捨：一致性是**致命項**（表示日誌不足以重建狀態），覆蓋缺口是**告警項**——
否則每逢新增事件類型，所有歷史工作階段都會讓 CI 變紅，指標就失去意義。

### 讀取路徑開關（P2-10 第三期）

預設 `columns`（讀資料庫欄位）；設為 `projection` 時，**「結果未知步驟」的判定改讀事件投影**：

```powershell
# config.json: "agent": { "event_source": "projection" }
# 或臨時用環境變數（便於灰度驗證，不必改設定）
$env:CBP_EVENT_SOURCE = "projection"
```

切換是**可回退的小步**：投影與欄位不一致時會**記告警並自動回退到欄位**（安全優先），
`/health` 會顯示目前生效的 `event_source`。已實測：投影模式下評測 7/7、事件核對全綠，
與欄位模式行為一致。

工作階段刪除會**級聯清掉事件**（隱私優先），但「某工作階段何時被刪」會寫進獨立的 `audit` 表
（只記 kind＋session_id，不含內容），刪除事實不隨工作階段消失。

⚠️ 不要用一般 GET 呼叫 `/v1/agent/sessions/{sid}/events`——那是 **SSE 長連線**（前端
EventSource 用），一般請求會一直掛著直到斷線，這是設計如此；要歷史事件請用上面的
`event-log`。

## 程式化工具呼叫 `run_code`（預設關閉）

讓模型寫一段程式碼，在**獨立子處理程序**裡批次呼叫**唯讀**工具（`read_text` / `list_directory` /
`find_files` / `skill_list` / `memory_search` / `tool_list`），最後用 `final(值)` 回傳結果。
適合「讀很多檔案再彙總」這類分析任務：工具迴圈要 N 個遠端回合，`run_code` 只要 1 個。

**實測（同一任務、deepseek/deepseek-chat）**

| 檔案數 | 關閉 run_code | 開啟 run_code |
|---|---|---|
| 4 | 5 步 / 4.3s | 5 步（**模型未採納**，仍走工具迴圈） |
| 12 | **17 步** / 10.2s | **7 步**（find_files → run_code ×2 → read_text ×3）/ 8.9s |

即：小規模不划算（模型自己不會用），規模上來後回合數減少約 59%。

**安全邊界（v1 刻意保守）**：只暴露唯讀工具（**無任何寫入／刪除／發信**）；子處理程序隔離；
硬性限額——逾時 `run_code_timeout_seconds`（預設 30 秒）、工具呼叫次數 `run_code_max_calls`
（預設 50）、回傳上限 `run_code_max_output_chars`（預設 20000）；工具側路徑沙箱照舊生效。
子處理程序內的工具錯誤是**具名例外 ToolError**，模型程式碼可捕捉後優雅收尾。

開啟方式：`config.json` 的 `agent.allow_run_code: true`（預設 false）。建議同時把 `run_code`
加進 `approval_required_tools`，讓它像 `run_python` 一樣逐次授權。

### 評測的變異處理

模型是隨機的：同一任務可能 3 步完成、也可能 28 步。因此評測支援在 `tests/evals/tasks.json`
裡給單個任務宣告 `"retries": 1`（失敗後**重試一次**，兩次都失敗才算失敗）——這樣既能壓掉
單次抽樣的假失敗，又不會掩蓋真實回歸。目前只有 `code-with-validation` 宣告了重試。