# telegram 插件

用 Telegram 机器人在手机上操作 Gantry：下任务、看进度、收回答。

## 快速开始

1. 在 Telegram 里找 **@BotFather** → `/newbot` → 拿到 token（形如 `123456789:ABC…`）；
2. 把 token 放进 `.env`（重启后生效；也可以填到插件配置里）：

   ```bash
   TELEGRAM_BOT_TOKEN=123456789:ABC…
   # 强烈建议加白名单（自己的 chat id，逗号分隔；用 @userinfobot 查）
   TELEGRAM_ALLOWED_CHAT_IDS=123456789
   ```

3. WebUI → 插件管理 → 启用 `telegram`（默认启用；无 token 时插件待命不报错）；
4. 手机上跟你的 bot 说话：

   | 命令 | 作用 |
   |---|---|
   | `/new 把 docs 跑一遍死链检查` | 开新任务（agent 模式） |
   | 直接发文字 | 继续当前会话 |
   | `/status` | 当前会话状态/模型/步数 |
   | `/todo` | 待办清单 |
   | `/cancel` `/pause` `/resume` | 控制 |
   | `/model copilot-auto` | 换模型 |
   | `/answer 可以，继续` | 回答 Agent 的中途提问 |
   | `/help` | 帮助 |

## 安全

- **白名单**：`allowed_chat_ids`（插件配置或 `TELEGRAM_ALLOWED_CHAT_IDS`）里的 chat 才受理；
  没配白名单时任何 chat 都能驱动你的服务——**不建议**；
- token 只从 `.env` 或插件配置读，不写进任何会入库的地方；
- bot 用的是服务本地 loopback 的 Gantry，不需要公网地址（长轮询 getUpdates）。

## 状态

`GET /v1/plugins/telegram/api/status` → 是否已配 token、白名单、已知 chat、在跑任务数、offset。
