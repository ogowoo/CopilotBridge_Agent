"""回答解析与界面锚点：从 Copilot 页面文本里找出"最新回答"的边界。

为什么单独成模块：bridge.py 原来把这些和浏览器自动化、会话编排、子代理、
审计全堆在一个文件里（5112 行、21 个职责簇）。这一块是纯文本处理
（外加 conversation_full_check 只读页面），是最容易拆、风险最低的一块。

设计约定（本包内所有从 bridge 拆出的模块都遵守）：
- 顶部 `from . import bridge as b`，**只在运行时**通过 `b.` 读 bridge 的
  可变状态（page / AGENT_SESSIONS / MARKERS / RUNTIME 等），保证：
  ① 测试里 `b.AGENT_SESSIONS = ...` 这类 rebind 仍然生效；
  ② 不存在 import 期循环依赖。
- 纯函数（不依赖任何页面/会话状态）可以安全独立成单测。
"""

from __future__ import annotations

import json
import re

from . import bridge as b
from .agent import AgentRunner


def _last_turn(text: str, markers: list) -> tuple:
    """Return (position, length) of the last occurring marker, or (-1, 0)."""
    best_pos, best_len = -1, 0
    for marker in markers:
        pos = text.rfind(marker)
        if pos > best_pos:
            best_pos, best_len = pos, len(marker)
    return best_pos, best_len


def extract_latest_answer(text):
    # Select only the final Copilot said block to avoid sidebar/history text.
    pos, marker_len = _last_turn(text or "", b.MARKERS["assistant_turn"])
    if pos < 0:
        return ""
    answer = text[pos + marker_len:]
    positions = [answer.find(m) for m in b.MARKERS["answer_end"] if answer.find(m) >= 0]
    if positions:
        answer = answer[:min(positions)]
    ignored = set(b.MARKERS["ignored_lines"])
    lines = []
    for line in answer.splitlines():
        line = line.rstrip()
        if line.strip() in ignored:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _status_before_marker(text: str) -> str:
    """回答区上方那行"状态气泡"文本（Copilot 会把思考状态同时渲染在答案上方）。

    这是判断"占位文本不是答案"的**结构信号**：状态短语会轮换（Checking that now… /
    Lining things up… / Digging in… …），穷举不可靠，但"回答区上方最后一行"与
    "Copilot said: 后面那行"相同，就说明答案还没开始。
    """
    pos, _marker_len = _last_turn(str(text or ""), b.MARKERS["assistant_turn"])
    if pos < 0:
        return ""
    lines = [ln.strip() for ln in str(text)[:pos].splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _has_complete_agent_action(text: str) -> bool:
    try:
        # 用模块级已导入的 AgentRunner：函数体内 `from agent import ...`
        # 在包结构下必然 ModuleNotFoundError，会被下面裸 except 吞掉，
        # 使本函数恒返回 False（完整动作被当畸形、合法 final 被当限流）。
        return AgentRunner._parse(text).get("type") in {"tool", "final"}
    except Exception:
        return False


# Copilot 限流/服务不可用的典型整段提示。仅在回答很短（<=600 字符）且命中
# 特征时判定为服务问题；可解析的 Agent JSON 动作优先于本判定，不会误判。
COPILOT_UNAVAILABLE_PATTERNS = [
    r"temporarily unable to respond",
    r"try again later",
    r"too many requests",
    r"rate\s*limit(?:ed)?",
    r"something went wrong",
    r"currently (?:busy|unavailable)",
    r"service (?:is )?currently unavailable",
    r"having trouble (?:responding|processing)",
    r"请稍后(?:再试|重试)",
    r"稍后(?:再试|重试)",
    r"暂时无法(?:响应|处理|为你服务|回答)",
    r"请求(?:过多|过于频繁|频率过高)",
    r"服务(?:暂时)?不可用",
    r"出了点问题",
    # 日文
    r"しばらく(?:お待ちください|してからもう一度)",
    r"現在(?:応答|利用)(?:できません|できない)",
    r"リクエストが(?:多すぎます|多すぎる)",
    r"サービスは現在利用できません",
    r"問題が発生しました",
]


def copilot_unavailable_reason(text: str) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if not value or len(value) > 600:
        return ""
    # 含有 Agent 动作结构的文本一律视为正常回答，不参与服务判定。
    if re.search(r"['\"]type['\"]\s*:", value):
        return ""
    for pattern in COPILOT_UNAVAILABLE_PATTERNS:
        if re.search(pattern, value, re.I):
            return value[:200]
    return ""


# Copilot 许可证/订阅不可用：不是限流，也不是回答格式问题，重试没有意义。
# 识别后直接给出可操作提示，避免用户只看到“页面没反应/超时”。
LICENSE_PATTERNS = [
    # 直引号/弯引号都要认：微软页面常用 “don’t”，只写 don't 会漏。
    r"don[’']?t have (?:a |an )?(?:valid )?licen[cs]e",
    r"do not have (?:a |an )?(?:valid )?licen[cs]e",
    r"no (?:valid )?licen[cs]e",
    r"valid licen[cs]e",
    r"not licensed",
    r"licen[cs]e (?:is )?(?:required|needed|expired|invalid|not available)",
    r"copilot (?:is )?not (?:available|included).{0,80}licen[cs]e",
    r"your (?:current )?licen[cs]e.{0,80}(?:does not|doesn't|not).{0,40}(?:include|support)",
    r"it looks like you don[’']?t have.{0,80}licen[cs]e",
    r"没有.{0,20}(?:许可证|授权|授權)",
    r"(?:许可证|授權|授權).{0,20}(?:过期|過期|无效|無效|不支持|不支援|不可用)",
    r"ライセンス.{0,40}(?:ありません|が必要|が無効|が期限切れ|がありません)",
    r"ライセンスが(?:見つかりません|不足しています)",
]


def license_reason(text: str) -> str:
    """返回许可证提示原文片段；空字符串表示没有命中。"""
    value = re.sub(r"\s+", " ", (text or "").strip())
    if not value or len(value) > 600:
        return ""
    for pattern in LICENSE_PATTERNS:
        m = re.search(pattern, value, re.I)
        if m:
            return value[max(0, m.start() - 40):m.end() + 80][:240]
    return ""


# Copilot 对话长度达到上限的典型提示（英文/中文/日文）。命中后的正确处理是
# 切换新会话，而不是原地重试，因此与服务限流分开识别。
CONVERSATION_FULL_PATTERNS = [
    r"reached (?:its|the) (?:maximum |max )?(?:length|limit)",
    r"conversation (?:is|has become) too long",
    r"start a new (?:conversation|chat) to continue",
    r"longer than (?:the )?(?:maximum|allowed)",
    r"对话(?:已)?达到(?:长度)?上限",
    r"对话(?:内容)?(?:过长|太多|超出)",
    r"(?:请)?开始新(?:的)?(?:对话|会话|聊天)(?:以继续|再继续)",
    r"新(?:建)?(?:对话|会话|聊天)以继续",
    # 日文
    r"会話(?:が)?(?:上限|長さ制限)(?:に達しました|を超えました)",
    r"新しい会話を開始して(?:ください|続行)",
    r"会話が長くなりすぎました",
]


def conversation_full_reason(text: str) -> str:
    value = re.sub(r"\s+", " ", (text or "").strip())
    if not value:
        return ""
    for pattern in CONVERSATION_FULL_PATTERNS:
        m = re.search(pattern, value, re.I)
        if m:
            return value[max(0, m.start() - 60):m.end() + 60]
    return ""


async def conversation_full_check() -> str:
    """在整页文本中探测对话上限横幅；剔除最近一条回答正文，
    避免把 Copilot 正常回答里的相关措辞误判为上限提示。"""
    body = await b.body_text()
    answer = extract_latest_answer(body)
    if answer:
        body = body.replace(answer, " ")
    return conversation_full_reason(body)


_ANSWER_NOISE = (
    "Press Tab to access the More options button.",
    "按 Tab 访问“更多选项”按钮。",
    "按 Tab 鍵以存取 [其他選項] 按鈕。",
    "Tab キーを押して [その他のオプション] ボタンにアクセスします。",
    "Press Tab to access the More options",
)

# Copilot 在真正回答前会先在回答区渲染一条"思考中"占位文本，随后**整段替换**成答案。
# 实测（2026-10，英文租户）：Checking that now… / Lining things up… / Gathering details…
# / Just a sec… / Give me a moment…。它们不是答案，但会被 extract_latest_answer 当成
# 回答正文 → 流式通道先把占位文本发出去，真答案出现时前缀对不上、再也发不出去，
# 调用方拿到的就是 "Checking that now…"。所以在这里整行剔除。
_THINKING_PHRASES = (
    "checking that now", "lining things up", "gathering details", "just a sec",
    "give me a moment", "working on it", "one moment", "hold on",
    "正在查找", "正在整理", "正在准备", "正在思考", "稍等一下", "请稍等",
    "まとめています", "考えています", "準備しています", "確認しています",
    "処理しています", "作成しています", "生成しています",
    "少々お待ちください", "お待ちください",
)


def _strip_thinking(text: str) -> str:
    """剔除"思考中"占位行（只匹配**整行且很短**的提示，避免误删正文）。"""
    lines = []
    for line in str(text or "").splitlines():
        s = line.strip().lower().rstrip(".…。 ")
        if s and any(s == p or (s.startswith(p) and len(s) <= len(p) + 12)
                     for p in _THINKING_PHRASES):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _clean_answer(text: str) -> str:
    """去掉答案里的 CBP 标记与 Copilot 的无障碍提示（它们会混进正文）。

    这些噪声以前会一路混到模型手里（实测：模型收到的"回答"其实是自己的问题
    加一句 Press Tab…），导致 JSON 解析失败、任务空转。
    """
    t = str(text or "")
    # 标记是 `[CBP-AGENT][sid][C000]`（连续多个方括号），要把整串都去掉，
    # 只去第一个会留下 `[sid][C000]` 混进正文。
    t = re.sub(r"\[CBP-(?:AGENT|TURN)\](?:\[[^\]\n]*\])*", "", t)
    t = re.sub(r"\[CBP-TURN:[^\]]*\]\s*", "", t)
    for junk in _ANSWER_NOISE:
        t = t.replace(junk, "")
    t = _strip_thinking(t)
    # 收尾可能有零宽/多余空白
    return t.strip()


def _echo_only(value: str, sent: str) -> bool:
    """清洗后的内容是否只是"我刚发出去的那句"（含标记残留），不含真正回答。"""
    v = _clean_answer(value)
    q = _clean_answer(sent)
    if not v:
        return True
    if not q:
        return False
    short = q.splitlines()[0].strip()[:40]
    if not short:
        return False
    return short in v and len(v) <= len(short) + 80


_SUGGESTION_ZH = re.compile(
    r"^(?:改成|换一种|換一種|再提供|提供|总结|總結|翻译|翻譯|列出|解释|解釋|写|寫|"
    r"生成|比较|比較|用|给|給|添加|删除|刪除|优化|優化|简化|簡化|扩展|擴展|"
    r"继续|繼續|搜索|搜尋|查看|读取|讀取|分析|整理|润色|潤色|改写|改寫|"
    r"缩短|縮短|补充|補充)"
)
_SUGGESTION_JA = re.compile(
    r"(?:にして|してください|教えて|翻訳して|要約して|書き換えて|説明して|"
    r"もっと|別の|ビジネス向け|英語に|日本語に|簡潔に|詳しく|"
    r"具体例|箇条書き)"
)
_SUGGESTION_EN = re.compile(
    r"^(?:Make|Write|Translate|Summarize|Explain|Give|Add|Remove|Change|Show|"
    r"List|Compare|Continue|Search|Read|Analyze|Analyse|Provide|Rewrite|Use|"
    r"Try|Generate|Create|Draft|Shorten|Expand|Improve|Polish)\b",
    re.I,
)


def _looks_like_suggestion(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 80:
        return False
    if s[0] in "-*0123456789":
        return False
    if s[-1] in "。！？.!?":
        return False
    return bool(_SUGGESTION_ZH.match(s) or _SUGGESTION_JA.search(s)
                or _SUGGESTION_EN.match(s))


def _strip_plain_suggestion_tail(raw: str) -> str:
    """剥离普通对话尾部的建议按钮文字（中/繁/日/英）。

    这些文字是页面的快捷建议按钮，不是回答正文。只处理**最后一个空行之后**
    的 2~4 行短句，且至少 2 行命中建议句式；带编号/项目符号或句末标点的
    正常列表不处理，避免误删正文。
    """
    text = raw.rstrip()
    if not text:
        return raw
    lines = text.splitlines()
    # 从末尾往前找最后一个空行，只检查它之后的那一段
    cut = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].strip():
            cut = i + 1
            break
    tail = [x for x in lines[cut:] if x.strip()]
    if not 2 <= len(tail) <= 4:
        return raw
    if sum(1 for x in tail if _looks_like_suggestion(x)) < 2:
        return raw
    return "\n".join(lines[:cut]).rstrip()


def strip_suggestion_tail(text: str) -> str:
    """去掉 Copilot 回答尾部的"后续建议"文字。

    实测（task-afa55d82ff7a）：回答正文是一个完整 JSON 动作，后面紧跟三行建议——
    「继续读取匹配代码 / 搜索菜单初始化函数 / 查看快捷键相关测试」。
    那些是页面上的建议按钮，被 `inner_text` 一起读进来了。JSON 动作本身不受影响
    （解析器只取第一个完整对象），但存库/展示的正文会被污染。
    """
    raw = text or ""
    body = raw.strip()
    if not body.startswith("{"):
        return _strip_plain_suggestion_tail(raw)
    try:
        _obj, end = json.JSONDecoder().raw_decode(body)
    except Exception:
        return raw                      # 解析不了就原样返回，交给后面的解析容错
    tail = body[end:].strip()
    if not tail:
        return body
    return body[:end].rstrip()
