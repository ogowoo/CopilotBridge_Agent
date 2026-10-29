---
name: self-upgrade-github-release
description: 自升级完成后，根据实际改动总结提交信息，并通过 bump_version.py 创建版本、提交、标签及推送 GitHub。
---

---
name: self-upgrade-github-release
description: 自升级完成后，根据实际改动总结提交信息，并通过 bump_version.py 创建版本、提交、标签及推送 GitHub。
---

# 自升级后推送 GitHub

## 什么时候使用

当用户在完成 Gantry 自升级后要求“推送更新到 GitHub”“发布新版本”或表达同等意图时使用。

## 前置条件

- 自升级改动已经完成。
- self_validate 与 self_unittest 均为 passed=true。
- 使用 git status --short 和 git diff --stat 检查实际改动，不能凭对话内容猜测。
- 不得把 config.json、data/、EdgeProfile/、凭据或其他敏感文件纳入提交。

## 提交信息生成规则

1. 根据实际 diff 总结主要更新，优先描述用户可感知的能力或修复。
2. 提交标题使用 Conventional Commits，例如：
   - feat(tools): add plugin-based workspace inspection tools
   - fix(webui): enable slash commands in continued conversations
   - refactor(agent): simplify tool plugin registration
3. 标题应简洁、具体，建议不超过 72 个字符。
4. 多项相关改动合并为一个准确标题；不要使用 update files、misc changes 等空泛描述。
5. 若工作树包含与本次自升级无关的改动，停止发布并向用户说明，禁止一并提交。

## 发布步骤

1. 检查仓库状态和远端差异：
   - git status --short --branch
   - git fetch origin main
2. 根据 diff 生成提交标题 MESSAGE。
3. 默认发布下一个 preview 版本：
   - python ./scripts/bump_version.py preview --commit --tag --push --message "MESSAGE"
4. 若用户明确要求 patch、minor、major、beta、alpha、stable 或指定版本，则替换 preview，或使用 --to X.Y.Z。
5. 如果 main 推送因 non-fast-forward 被拒绝，但标签已推送：
   - 执行 git pull --rebase origin main。
   - rebase 成功后执行 git push origin main。
   - 禁止使用 --force 或 --force-with-lease，除非用户明确授权。
6. 推送后核对本地分支、origin/main 和版本标签状态。

## PowerShell 与路径注意事项

- 在 JSON 命令中，Windows 路径里的 \b 可能被解析成退格字符；优先使用正斜杠路径。
- 安装目录命令可写为：python 'C:/gantry/scripts/bump_version.py' ...。
- pwsh 的 workdir 不得越过工作区沙箱；需要操作安装仓库时使用命令的 -C 'C:/gantry' 参数或脚本绝对路径。

## 验证与交付

- 发布前必须确认 self_validate 和 self_unittest 通过。
- 发布后报告：旧版本、新版本、提交标题、最终 commit SHA、tag、分支推送结果和测试统计。
- 自升级代码需要重启服务才能生效。
- 提供回滚命令：python scripts/rollback.py --scope self --last --yes。

## 常见失败处理

- 无参数运行 bump_version.py 只显示版本，不会发布。
- 脚本内部 Git 子命令返回失败时，脚本进程仍可能返回 0；必须检查 stdout/stderr 中每个 git push 的结果。
- 标签推送成功但分支推送失败，不得宣称发布全部完成；完成安全 rebase 和分支推送后再收尾。
- rebase 冲突时立即停止，报告冲突文件，不得自动丢弃远端或本地改动。
