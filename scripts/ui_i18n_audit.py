# -*- coding: utf-8 -*-
"""i18n 覆盖率审计：三语言逐面切换，列出未翻译/漏 key/原始 key 泄漏。

**为什么需要**：用户实测发现「未就绪」提示在英文模式下仍是中文、按钮上直接显示 `tr(重试浏览器)`
（源码里把 `tr(...)` 写成了字符串字面量）。这类问题肉眼很难穷尽——界面有几十个按钮、若干弹窗，
而且**切语言后动态生成的内容不会自动重翻**。

扫描范围刻意只覆盖**界面外壳**（按钮/标签/标签页/表头/提示/弹窗标题/placeholder 等），
**排除数据容器**（会话列表标题、对话记录、时间线、回答、提示词）——那里的中文是用户数据与
模型输出，不该被当成"未翻译"。

用法:
    python scripts/ui_i18n_audit.py            # 有问题退出码 1
    python scripts/ui_i18n_audit.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LANGS = [("zh", "简体"), ("tw", "繁体"), ("en", "English")]

# 只扫界面外壳；数据容器一律排除（含用户中文标题、模型输出）
CHROME_JS = r"""
() => {
  // 这些容器里是用户数据 / 模型输出 / 按取舍保持中文的长文档，不参与覆盖率判定
  const DATA_SKIP = new Set(['sessions','chatlog','steps','answer','prompt','activity','queueBox',
  'toolLog',   // 调用记录：内容是工具输出与模型写的理由，属数据（标题与按钮仍在 #toolLog 外层审计）
                             'gsResults','liveBody','toasts','memList','manualBody',
                             'stepTool','stepStatus','title','sidChip','status']);
  // 会话列表里"标题/ID"是用户数据要排除，但同一行的元信息（步骤/会话/Token）是界面文案
  const CHROME_ALLOW = '.smeta small';
  const skipNode = el => {
    for (let p = el; p; p = p.parentElement) {
      if (p.id && DATA_SKIP.has(p.id)) return true;
      // data-i18n-skip：**数据型文本**（插件描述、贡献点 JSON、第三方元数据），
      // 由作者语言决定，不该当"未翻译"报错。
      if (p.hasAttribute && p.hasAttribute('data-i18n-skip')) return true;
    }
    return false;
  };
  const containsData = el => {
    for (const id of DATA_SKIP) if (el.querySelector && el.querySelector('#' + id)) return true;
    return false;
  };
  // 混合容器只取**直接文本节点**：既避开子元素里的用户数据，又能抓到
  // "裸文本节点"这一类漏翻（真实事故：「步骤时间线」就在这种位置）。
  const ownText = el => {
    let s = '';
    for (const n of el.childNodes) if (n.nodeType === 3) s += n.textContent;
    return s.replace(/\s+/g, ' ').trim();
  };
  const out = [];
  const sels = ['button', '.tab', '.kpi span', '.chead', '.hint', '.listhead span', 'label',
                'summary', '.dhead b', '.dfoot button', '.seg', '.pill', 'th', 'option',
                'span', 'small', 'b', 'div', 'kbd', 'p', 'li', 'h1', 'h2', 'h3', 'h4'];
  document.querySelectorAll(sels.join(',')).forEach(el => {
    if (!el.offsetParent) return;
    if (skipNode(el) || containsData(el)) return;
    const allowed = el.closest && el.closest(CHROME_ALLOW);
    const t = el.children.length === 0 ? (el.textContent || '').replace(/\s+/g, ' ').trim() : ownText(el);
    if (!t || (skipNode(el) && !allowed)) return;
    if (!t) return;
    out.push({kind: 'text', tag: el.tagName.toLowerCase(), id: el.id || '',
              cls: (typeof el.className === 'string' ? el.className : '').slice(0, 20), text: t.slice(0, 90)});
  });
  document.querySelectorAll('[title],[placeholder],[aria-label]').forEach(el => {
    if (!el.offsetParent || skipNode(el) || containsData(el)) return;
    ['title', 'placeholder', 'aria-label'].forEach(a => {
      const v = el.getAttribute(a);
      if (v && v.trim()) out.push({kind: a, tag: el.tagName.toLowerCase(), id: el.id || '',
                                   cls: '', text: v.trim().slice(0, 90)});
    });
  });
  return out;
}
"""

CJK = re.compile(r"[\u4e00-\u9fff]")


KNOWN_S2T = {
 "浏": "瀏", "览": "覽", "业": "業", "记": "記", "忆": "憶", "国": "國", "报": "報", "挂": "掛",
 "拦": "攔", "检": "檢", "极": "極", "环": "環", "现": "現", "画": "畫", "离": "離", "种": "種",
 "积": "積", "竖": "豎", "竞": "競", "紧": "緊", "纯": "純", "细": "細", "经": "經", "绑": "綁",
 "绕": "繞", "绘": "繪", "给": "給", "绝": "絕", "继": "繼", "绪": "緒", "维": "維", "缀": "綴",
 "缘": "緣", "联": "聯", "节": "節", "获": "獲", "蓝": "藍", "虚": "虛", "补": "補", "装": "裝",
 "许": "許", "访": "訪", "评": "評", "识": "識", "诉": "訴", "诚": "誠", "详": "詳", "调": "調",
 "谱": "譜", "负": "負", "责": "責", "账": "賬", "质": "質", "贴": "貼", "费": "費", "赋": "賦",
 "赖": "賴", "车": "車", "轮": "輪", "轻": "輕", "较": "較", "辞": "辭", "迟": "遲", "邮": "郵",
 "术": "術", "专": "專", "转": "轉", "软": "軟", "载": "載", "输": "輸", "资": "資", "态": "態",
 "标": "標", "签": "簽", "简": "簡", "双": "雙", "对": "對", "导": "導", "层": "層", "归": "歸",
 "显": "顯", "单": "單", "关": "關", "设": "設", "计": "計", "时": "時", "间": "間", "门": "門",
 "长": "長", "开": "開", "员": "員", "应": "應", "该": "該", "无": "無", "见": "見", "观": "觀",
 "规": "規", "则": "則", "处": "處", "议": "議", "论": "論", "验": "驗", "类": "類", "页": "頁",
 "顶": "頂", "项": "項", "须": "須", "预": "預", "领": "領", "题": "題", "键": "鍵", "盘": "盤",
 "终": "終", "约": "約", "续": "續", "断": "斷", "义": "義", "众": "眾", "产": "產", "场": "場",
 "边": "邊", "过": "過", "进": "進", "远": "遠", "还": "還", "连": "連", "运": "運", "达": "達",
 "适": "適", "选": "選", "逻": "邏", "辑": "輯", "钥": "鑰", "锁": "鎖", "错": "錯", "钟": "鐘",
 "银": "銀", "钱": "錢", "额": "額", "频": "頻", "风": "風", "飞": "飛", "饰": "飾", "饭": "飯",
 "饮": "飲", "馆": "館", "骑": "騎", "驱": "驅", "骤": "驟", "鲜": "鮮", "鸣": "鳴", "鸦": "鴉",
 "龄": "齡", "齐": "齊", "齿": "齒", "龙": "龍", "龟": "龜", "机": "機", "强": "強", "册": "冊",
 "复": "復", "汇": "匯", "内": "內", "启": "啟", "确": "確", "谢": "謝", "彻": "徹", "厉": "厲",
 "严": "嚴", "够": "夠", "图": "圖", "团": "團", "换": "換",
}


def s2t_simplified_chars() -> set[str]:
    """检测集合 = **独立维护的可靠对照表** ∪ i18n.js 转换表里"简繁不同形"的键。

    为什么不用 i18n.js 的表当唯一来源：表里没有的字它自然检测不到——真实事故正是如此，
    「浏」不在表里，TW 模式渲染出混排的「重試浏覽器」，而用表自身当检测集时完全看不出来。
    """
    text = (BASE / "webui" / "i18n.js").read_text(encoding="utf-8")
    block = text[text.find("const S2T_CHARS"):text.find("const map = new Map(pairs);")]
    pairs = re.findall(r'\["([^"])","([^"])"\]', block)
    # 表里还有一批 ["放","放"] 同形占位对，只取不同形的
    return set(KNOWN_S2T) | {a for a, b in pairs if a != b}


BAD = re.compile(r"\btr\(|data-i18n|undefined|NaN|\[object Object\]|%s|\{state\}|\{err\}")


def en_sentence_values() -> list[str]:
    """英文表里"句子级"的译文（长度 ≥15 且含空格），用于检测**中文模式混入英文**。

    真事故：以英文重载页面后切回中文，DOM 直译拿"已经翻成英文的缓存原文"覆盖了
    data-i18n 设好的中文，界面变成中英混杂——而此前审计只查"英文模式仍含中文"，
    对这个反方向完全不看。
    """
    text = (BASE / "webui" / "i18n.js").read_text(encoding="utf-8")
    block = text[text.find("const I18N_EN = {"): text.find("\n};", text.find("const I18N_EN = {"))]
    vals: list[str] = []
    lines = block.splitlines()
    for idx, line in enumerate(lines):
        m = re.match(r'\s*"[^"]+":\s*(.*)$', line)
        if not m:
            continue
        rest = m.group(1).strip()
        if not rest:
            for nxt in lines[idx + 1:]:
                if nxt.strip():
                    rest = nxt.strip()
                    break
        vm = re.match(r'"((?:[^"\\]|\\.)*)"', rest)
        if vm:
            v = vm.group(1).replace('\\"', '"')
            if len(v) >= 15 and " " in v:
                vals.append(v)
    return vals


def audit(url: str, as_json: bool) -> int:
    from playwright.sync_api import sync_playwright

    S2T_SIMP = s2t_simplified_chars()
    EN_VALUES = en_sentence_values()
    report: dict = {}
    problems: list[str] = []
    with sync_playwright() as p:
        browser = None
        for kwargs in ({"channel": "msedge"}, {}, {"channel": "chrome"}):
            try:
                browser = p.chromium.launch(headless=True, **kwargs)
                break
            except Exception as exc:
                last = exc
        if browser is None:
            print(f"无法启动浏览器：{last}")
            return 2
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        for code, name in LANGS:
            page.goto(url, wait_until="domcontentloaded")
            # 关键：让页面**以英文加载**一次。真实事故的复现条件是"页面以英文加载 →
            # 切回中文"，此时 DOM 直译已经把英文当"原文"缓存下来，会把中文覆盖掉。
            # 只在同一次加载里 setLang('en')→setLang('zh') 是复现不出来的。
            try:
                page.evaluate("localStorage.setItem('cbp-lang','en')")
                page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            page.wait_for_timeout(700)
            # 选一个会话，让 KPI/标签页/工具栏都渲染出来
            sid = page.evaluate(
                "async()=>{try{const d=await (await fetch('/v1/agent/sessions')).json();"
                "return (d.sessions&&d.sessions[0]&&d.sessions[0].id)||''}catch(e){return ''}}")
            if sid:
                page.evaluate(f"load({json.dumps(sid)})")
            page.wait_for_timeout(900)
            # **确定性**地先切到英文、再切到目标语言：真实的"中英混杂"只在**从别的语言切回来**
            # 时出现（DOM 直译会把已翻成英文的文本当成"原文"缓存下来）。若只做全新加载，
            # 是否复现取决于 localStorage 残留 —— 那样审计会时灵时不灵。
            page.evaluate(f"setLang({json.dumps(code)})")
            page.wait_for_timeout(500)
            items = page.evaluate(CHROME_JS)
            # 空态（未选会话时显示的引导文案）与"Copilot 未就绪"浮层此前都扫不到：
            # 前者在选中会话后被隐藏，后者是懒创建的浮层。
            try:
                # 用 **failed + 错误详情** 触发：只测 needs_login 空错误会漏掉
                # "（健康检查失败）"这类**后端中文字符串被原样拼进英文句子**的缺口。
                page.evaluate("showBoot('failed', false, '健康检查失败')")
                page.wait_for_timeout(300)
                items += page.evaluate(CHROME_JS)
            except Exception:
                pass
            try:
                page.evaluate("current=null;document.getElementById('sessionView').style.display='none';"
                              "document.getElementById('emptyState').style.display='flex'")
                page.wait_for_timeout(300)
                items += page.evaluate(CHROME_JS)
            except Exception:
                pass
            # 「更多」下拉菜单：默认隐藏，不展开就扫不到（实测：插件管理… 漏翻就是这里）
            try:
                page.evaluate("document.getElementById('moreMenu').classList.add('open')")
                page.wait_for_timeout(250)
                items += page.evaluate(CHROME_JS)
                page.evaluate("document.getElementById('moreMenu').classList.remove('open')")
            except Exception:
                pass
            # 斜杠命令面板：只有输入 "/" 才出现，不主动打开就扫不到
            try:
                page.evaluate("()=>{const t=document.getElementById('task');"
                              "if(t){t.value='/';slashUpdate(true)}}")
                page.wait_for_timeout(300)
                items += page.evaluate(CHROME_JS)
            except Exception:
                pass
            # 再开一次"使用手册"与"新任务"弹窗（大字面量，最容易漏）
            for act in ("openManual", "openTask", "openPlugins"):
                try:
                    page.evaluate(f"{act}()")
                    page.wait_for_timeout(400)
                    items += page.evaluate(CHROME_JS)
                    page.evaluate("document.querySelectorAll('.mask.open').forEach(m=>m.classList.remove('open'))")
                except Exception:
                    pass
            report[name] = items
            if code == "en":
                for it in items:
                    if CJK.search(it["text"]):
                        problems.append(f"[en 未翻译] {it['tag']}#{it['id'] or it['cls']} {it['kind']}: {it['text']}")
            if code == "zh":
                # 中文模式不该出现英文句子（短词如 Token/Session 是合法的，故限长且含空格）
                for it in items:
                    leak = [v for v in EN_VALUES if v in it["text"]]
                    if leak:
                        problems.append(f"[zh 混入英文] {it['tag']}#{it['id'] or it['cls']} "
                                        f"{it['kind']}: {it['text'][:60]} ← {leak[0][:40]}")
            if code == "tw":
                for it in items:
                    miss = sorted({c for c in it["text"] if c in S2T_SIMP})
                    if miss:
                        problems.append(f"[tw 繁化漏字] {it['tag']}#{it['id'] or it['cls']} "
                                        f"{it['kind']}: {it['text']}（漏字 {' '.join(miss)}）")
            for it in items:
                if BAD.search(it["text"]):
                    problems.append(f"[{name} 原始 key/占位符泄漏] {it['tag']}#{it['id'] or it['cls']} "
                                    f"{it['kind']}: {it['text']}")
        browser.close()

    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        for code, name in LANGS:
            print(f"{name}: 扫描到 {len(report[name])} 条外壳文案")
    if problems:
        uniq = sorted(set(problems))
        print(f"\n发现 {len(uniq)} 类问题：")
        for x in uniq[:60]:
            print("  ✗", x)
        if len(uniq) > 60:
            print(f"  … 另有 {len(uniq) - 60} 类")
        return 1
    print("\n三语言外壳文案全部覆盖，无原始 key 泄漏")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765/ui")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    return audit(args.url, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
