from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import prompt_lang
from .prompt import (PromptRegistry, agent_protocol, agent_team, deliverable_rule,
                     file_ops_rule, language_directive)

# 自升级任务的"别去工作区"强提示（实测模型会先 list_directory('.') 把工作区里
# 别的项目当目标——工作区只是用户项目的容器，服务代码在**安装目录**）。
SELF_UPGRADE_TASK_LEAD_ZH = """
【本任务是自升级】协议里"路径相对于当前工作目录"不适用本任务：目标代码在**服务安装目录**
（self_* 工具的根），不在当前工作区。
- 第一步就调 self_search_text / self_find_files 定位（不要 list_directory、不要读工作区文件）；
- 搜不到就停下来问我，不要去工作区找（工作区里只是别的项目，和本服务无关）。
"""


class PromptMixin:
    def _system_prompt(self) -> str:
        # copilot_attach / copilot_file 只在浏览器（Copilot 网页）会话可用。
        # provider 会话里不能提这两个工具名，否则模型会照着提示词去调用
        # （实测：提供商会话里 copilot_file 连续失败 10 次纯烧回合）。
        _web_allowed = bool(self.tools.web_copilot_allowed())
        if _web_allowed:
            _routing = ("只需分析（总结/找问题/提取信息）的大文件（约 20000 字符以上）优先用 "
                        "copilot_attach 丢给 Copilot 分析，一个回合搞定，不要逐段翻页；\n"
                        "产出**新文件**（脚本/页面/工具/demo/文档）优先用 copilot_file(path, instruction) "
                        "一次生成完整文件（多文件就多次调用，连续调用，不要写一段就停下来汇报），"
                        "不要用 write_text_b64 分块拼；分块写只用于 copilot_file 不可用、改写已有文件"
                        "或内容很短需要逐块校验时；\n"
                        "需要精确修改的文件必须本地工具操作；大文件只需看局部时用 read_text 的 "
                        "offset/limit 读目标段。")
        else:
            _routing = ("需要修改的文件直接本地工具操作；大文件只需看局部时用 read_text 的 "
                        "offset/limit 读目标段，分段读完再下结论。（本会话走 API 提供商，没有 "
                        "Copilot 网页可用，copilot_attach/copilot_file 不可用）")
        # 分段装配（P2-9）：协议 / 工具清单 / 自升级 / 工作区上下文。
        # 分隔符为空串且顺序与原拼接一致，因此输出**逐字不变**（有 golden 比对守着）。
        reg = PromptRegistry()
        reg.add("language", language_directive, order=-2000)   # 回答语言由它决定，放最前
        reg.add("protocol", agent_protocol, order=-1000)
        # 文件操作只用 Python 系工具，禁止拿 shell 改文件：
        # PowerShell 的 Get-Content -Raw / Set-Content -Encoding utf8 会破坏 UTF-8
        # 源码（实测把中文写成 "é¦–è½®æ…" 这类乱码，只能整篇重写）。文件工具本身
        # 就是 pathlib 实现（显式 utf-8、保留 CRLF），没有理由绕道 shell。
        # 顺序必须在 tools(100) 之后：提示词里"可用工具：\n"要**紧跟**工具清单 JSON，
        # 否则 test_web_tool_provider_gate 解析不到（实测踩过）。
        _pwsh_ready = "pwsh" in self._active_tool_names()
        if _pwsh_ready:
            reg.add("tools", json.dumps(self.tools.descriptions(only=self._active_tool_names()),
                                        ensure_ascii=False), order=100)
            reg.add("file_ops", file_ops_rule, order=120)
        else:
            reg.add("tools", json.dumps(self.tools.descriptions(only=self._active_tool_names()),
                                        ensure_ascii=False), order=100)
        reg.add("tools", json.dumps(self.tools.descriptions(only=self._active_tool_names()),
                                    ensure_ascii=False), order=100)
        # 交付物表述（"已在本地工作区"而不是"可下载"）：与 pwsh 是否可用无关，统一注入
        reg.add("deliverable", deliverable_rule, order=125)
        # 任务分工（子代理）：只在工具面里含 subagent 时才注入，避免不可用时误导模型
        if "subagent" in self._active_tool_names():
            reg.add("team", agent_team, order=150)
        reg.add("self_upgrade", self._self_upgrade_rules(), order=200)
        reg.add("workspace", self._workspace_context(), order=300)
        # 分流那句也随提示词语言变（英文/繁体时不至于整段提示词只有这一句是中文）
        try:
            from . import prompt as _pm
            _routing = prompt_lang.routing_text(
                _web_allowed, _routing, prompt_lang.effective_lang(_pm._cfg_lang()))
        except Exception:
            pass
        return reg.render(variables={"CBP_FILE_ROUTING": _routing})

    def _first_turn_copilot_hint(self) -> bool:
        """首轮是否提示"可直接让 Copilot 生成文件"。

        条件全部满足才提示：
        1. 浏览器（Copilot 网页）通道——provider 会话没有 copilot_file，
           提示了只会让模型去调用不存在的工具（实测连续失败 10 次）；
        2. copilot_file 确实在本次工具面里（allow_copilot_file / 工具裁剪都过了）。
        """
        if not self.tools.web_copilot_allowed():
            return False
        try:
            names = self._active_tool_names()
        except Exception:
            names = None
        if names is not None and "copilot_file" not in names:
            return False
        return "copilot_file" in getattr(self.tools, "_tools", {})

    def _active_tool_names(self) -> set[str] | None:
        """按任务文本裁剪工具面（P0-1）；None 表示不裁剪。

        core 组始终包含；其它组按关键词命中展开；allow_self_upgrade 时带上 self 组。
        config 的 tool_visibility="all" 可整体关闭裁剪（排障用）。
        """
        if str(self.config.get("tool_visibility", "auto")).lower() == "all":
            return None
        from .tools import GROUP_KEYWORDS, TOOL_GROUPS
        task = (getattr(self, "_task_text", "") or "").lower()
        names: set[str] = set(TOOL_GROUPS.get("core", ()))
        # **未登记任何分组的工具一律视为 core**：否则新加的工具（例如 run_code）会被
        # 裁剪从提示词里静默滤掉——模型看不到就不可能调用（本轮真机验证抓到的缺陷）。
        # 这也与 tool_group() 的口径一致（未登记→core）。
        _grouped: set[str] = set()
        for _members in TOOL_GROUPS.values():
            _grouped.update(_members)
        try:
            for _item in self.tools.descriptions():
                _n = _item.get("name")
                if _n and _n not in _grouped:
                    names.add(_n)
        except Exception:
            pass
        for group, words in GROUP_KEYWORDS.items():
            if any(w in task for w in words):
                names.update(TOOL_GROUPS.get(group, ()))
        if getattr(self.tools, "allow_self_upgrade", False):
            names.update(TOOL_GROUPS.get("self", ()))
        return names

    def _self_upgrade_rules(self) -> str:
        """自升级（修改服务自身代码）的专用规则，仅在 allow_self_upgrade 时注入。"""
        if not getattr(self.tools, "allow_self_upgrade", False):
            return ""
        # 走 localize：英文界面要拿到译本，繁体按字转换（不然英文模式下整段提示词露中文）
        from . import prompt_lang as _pl
        task = getattr(self, "_task_text", "") or ""
        is_self_task = ("[自升级任务]" in task) or ("自升级" in task)
        zh = self._self_upgrade_rules_zh()
        if not is_self_task:
            return _pl.localize("self_upgrade", zh)
        # 自升级任务：再加一段"别去工作区"的强提示（实测模型会先 list_directory('.')
        # 把工作区里别的项目（pyshot/wgime）当成目标，一路查错项目）。
        lead_zh = SELF_UPGRADE_TASK_LEAD_ZH
        target = _pl.request_lang()
        if target == "en":
            return _pl.EN.get("self_upgrade_lead", "") + _pl.EN.get("self_upgrade", zh)
        if target == "tw":
            return _pl.to_tw(lead_zh + zh)
        return lead_zh + zh

    def _self_upgrade_rules_zh(self) -> str:
        """中文版原文（localize 的唯一真源；英文用 prompt_lang.EN 的译本）。"""
        root = getattr(self.tools, "global_root", None)
        root_txt = str(root) if root else "（未设置）"
        return f"""

【自升级模式已开启】你可以修改 Gantry 服务自身的代码（gantry/、webui/、scripts/、tests/、app.py 等白名单路径），用 self_* 系列工具操作；
**安装目录的真实路径是 `{root_txt}`**（self_* 工具的根就是它，不用你猜；项目改名后文件夹名可能还是旧的，绝对不要按名字猜路径、更不要自己拼 c:\\gantry 之类的目录）；
**服务代码在安装目录，当前工作区里没有**——排查「服务自身的按钮/功能/报错」这类问题时，**先用 self_search_text / self_find_files 定位**（工作区的 find_files/list_directory 搜不到服务代码，实测会去工作区乱翻然后问用户路径）；定位到文件/行号后先报告给用户确认，再动手改；config.json、data/、EdgeProfile/ 禁止触碰。硬性流程：
1. 先 self_read_text 看清现状（要看具体第几行就传 line_start/line_count，返回带行号），修改优先用 self_replace_text 精确替换（old 必须逐字一致）；写文件用 self_write_text_b64/self_append_text_b64（Base64、每块≤3000字符、分块续写），一次只做一件相关的改动。
2. 全部写完后执行 self_validate（全安装目录语法检查）与 self_unittest（服务自身单测），两者 passed=true 前禁止返回 final。
3. 改坏了（验证不过且修不动）就用 self_rollback 回滚本次自升级，严禁在坏状态上继续叠加修改。
4. 自升级改动需要人工重启服务才生效，返回 final 时必须明确告知：改了什么、验证结果、需要重启、以及可用什么命令回滚。"""

    def _workspace_context(self) -> str:
        """项目上下文：全局 + 项目两级 AGENT.md 全量注入，skills 清单渐进式披露。

        - 全局：服务安装目录的 AGENT.md / skills（对所有工作区生效）
        - 项目：当前工作区的 AGENT.md / skills（同路径去重，同名技能覆盖全局）
        """
        parts: list[str] = []
        agent_md = getattr(self, "_agent_md", None)
        skills = getattr(self, "_skills", None)
        if agent_md is None or skills is None:   # 未经 run() 时按需加载
            agent_md, skills = self._context_values()
        agent_md = agent_md or ""
        if agent_md:
            parts.append("项目约定（全局 + 项目两级 AGENT.md，必须遵守；项目级优先）：\n" + agent_md)
        skills = skills or []
        if skills:
            lines = ["可用技能（判断相关时用 skill_read 读取全文再执行；[项目] 优先于 [全局]）："]
            for s in skills:
                scope = "项目" if s.get("scope") == "project" else "全局"
                lines.append(f"- [{scope}] {s['name']}：{s['description']}（skill_read \"{s['name']}\"）")
            parts.append("\n".join(lines))
        # 长期记忆：按当前任务文本检索最相关的若干条（关键词匹配，零依赖）。
        # 只注入少量：记忆是可检索知识，不是每回合都要读的硬约定。
        try:
            mem = getattr(self.tools, "memory", None)
            task_text = getattr(self, "_task_text", "") or ""
            if mem is not None and task_text:
                scopes = ["global", getattr(self.tools, "memory_scope", "global")]
                hits = mem.search(task_text, scopes, limit=5)
                if hits:
                    lines = ["相关长期记忆（来自以往任务，可能过时；与当前事实冲突时以当前为准，"
                             "过时或错误的用 memory_forget 删除）："]
                    for m in hits:
                        pin = "★" if m.get("pinned") else ""
                        lines.append(f"- {pin}[{m.get('kind')}#{m['id']}] {m['text']}")
                    lines.append("需要更多时用 memory_search 检索。")
                    parts.append("\n".join(lines))
        except Exception:
            pass
        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def _context_values(self) -> tuple[str, list[dict[str, Any]]]:
        """懒加载并缓存工作区上下文（run() 每轮会显式刷新）。"""
        if getattr(self, "_agent_md", None) is None:
            self._agent_md = self._load_agent_md()
        if getattr(self, "_skills", None) is None:
            self._skills = self._load_skill_catalog()
        return self._agent_md, self._skills

    def _md_roots(self) -> list[tuple[str, Path]]:
        """两级 AGENT.md 根目录：全局（服务安装目录）在前，项目（工作区）在后。"""
        roots: list[tuple[str, Path]] = []
        g = getattr(self.tools, "global_root", None)
        if g:
            roots.append(("全局", Path(g)))
        ws = getattr(self.tools, "root", None)
        if ws:
            roots.append(("项目", Path(ws)))
        return roots

    # 会被解析器当作"块边界"的闭合标签：正文里出现就可能越出注入区，统一转义。
    _ESCAPE_TAGS = ("</system-reminder>", "</instructions>", "</workspace_instructions>")

    @staticmethod
    def escape_instruction_text(text: str) -> str:
        """转义指令文本里的块闭合标签（DSH：注入正文里的 </system-reminder> 要转义）。"""
        for tag in PromptMixin._ESCAPE_TAGS:
            if tag in text:
                text = text.replace(tag, "<\\/" + tag[2:])
        return text

    def _load_agent_md(self) -> str:
        """合并注入：全局 AGENT.md + 项目 AGENT.md。

        预算策略参照 DSH 的 workspace instructions：**超预算先整体丢"宽泛"文件
        （全局），最后才截断"最具体"文件（项目）**，并渲染一条**可见的预算提示**，
        而不是对所有文件一视同仁地硬截断（那会把最具体的项目约定也砍掉）。
        """
        cap = int(self.config.get("agent_md_max_chars", 8000))
        total_cap = int(self.config.get("agent_md_total_max_chars", cap * 2))
        loaded: list[tuple[str, str]] = []          # (label, text) —— 顺序=宽泛→具体
        seen: set[str] = set()
        for label, root in self._md_roots():
            for name in ("AGENT.md", "agent.md"):
                p = root / name
                try:
                    if not p.is_file():
                        continue
                    rp = str(p.resolve()).lower()
                    if rp in seen:
                        break
                    seen.add(rp)
                    text = p.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        loaded.append((label, text))
                    break
                except Exception:
                    continue

        notes: list[str] = []
        # 超总预算：从头（最宽泛）开始整体丢弃，直到放进预算
        while len(loaded) > 1 and sum(len(t) for _l, t in loaded) > total_cap:
            label, text = loaded.pop(0)
            notes.append(f"[{label} AGENT.md 共 {len(text)} 字符，超出指令预算已整体省略]")
        parts: list[str] = []
        for label, text in loaded:
            if len(text) > cap:
                notes.append(f"[{label} AGENT.md 共 {len(text)} 字符，已截断到 {cap}]")
                text = text[:cap]
            parts.append(f"【{label} AGENT.md】\n{self.escape_instruction_text(text)}")
        if notes:
            parts.append("指令预算提示：" + "；".join(notes))
        return "\n\n".join(parts)

    def _load_skill_catalog(self) -> list[dict[str, Any]]:
        """技能清单：优先由工具层统一扫描（含全局+项目与去重），失败则本地兜底。"""
        try:
            listed = self.tools.skill_list()
            if listed:
                return listed
        except Exception:
            pass
        return []


