// themeboot.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
import { THEMES, THEME_IDS } from './composer.js';
function themeChoice(){
 try{
  const t=localStorage.getItem('gantry-theme');
  if(t===null||t===''||t==='auto')return 'auto';
  if(t==='dark')return '';
  return THEME_IDS.indexOf(t)>=0?t:'auto';
 }catch(e){return 'auto'}
}
function resolveTheme(c){
 if(c!=='auto')return c;
 try{return (window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches)?'light':''}catch(e){return ''}
}
function applyTheme(c,persist){
 const r=resolveTheme(c);
 if(r){document.documentElement.dataset.theme=r}else{document.documentElement.removeAttribute('data-theme')}
 document.documentElement.dataset.themeChoice=c;
 if(persist){try{localStorage.setItem('gantry-theme',c===''?'dark':c)}catch(e){}}
 const btn=document.getElementById('themeBtn');
 if(btn){
  // 名称与括号都必须过 tr()：标题是**拼出来的**，i18n 的 DOM 直译只会按整串精确匹配，
  // 拼串里夹着中文就会在英文界面漏出来（实测：Theme: 跟随系统（current 浅色…））
  const nm=tr((THEMES.find(x=>x[0]===c)||THEMES[0])[1]);
  const cur=tr((THEMES.find(x=>x[0]===r)||{1:'深色'})[1]);
  btn.title=c==='auto'
   ?tr('主题：{n}（当前 {c}，点击切换）',{n:nm,c:cur})
   :tr('主题：{n}（点击切换）',{n:nm});
 }
}
function toggleTheme(){
 const cur=themeChoice();
 const i=Math.max(0,THEME_IDS.indexOf(cur));
 applyTheme(THEME_IDS[(i+1)%THEME_IDS.length],true);
}
/* 系统主题变化时实时跟随（仅当 choice==='auto'） */
function watchSystemTheme(){
 try{
  const mq=window.matchMedia('(prefers-color-scheme: light)');
  const on=()=>{if(themeChoice()==='auto')applyTheme('auto',false)};
  if(mq.addEventListener)mq.addEventListener('change',on);
  else if(mq.addListener)mq.addListener(on);
 }catch(e){}
}
/* ── 左侧栏折叠（归零只剩边缘拉手） ── */
function toggleRail(){let r=document.documentElement;
 // 窄屏(≤860)把侧栏做成**抽屉浮层**（DSH 式）：默认收起（无属性=收起），
 // 打开写 'open'；宽屏沿用原语义（无属性=展开、'closed'=收起成拉手）。
 const narrow=matchMedia('(max-width:860px)').matches;
 const open=narrow ? (r.dataset.rail==='open') : (r.dataset.rail!=='closed');
 const c=!open;                                  // 取反后的状态
 if(narrow){r.dataset.rail=c?'open':'closed'}
 else if(c){delete r.dataset.rail}else{r.dataset.rail='closed'}
 // 图标：窄屏收起态用 ☰（"打开会话列表"比一个小尖括号好认），展开态用 ‹ 收起
 const btn=document.getElementById('railBtn');
 if(btn)btn.textContent = narrow ? (c?'‹':'☰') : (c?'‹':'›');
 try{localStorage.setItem('gantry-rail',narrow?(c?'open':'closed'):(c?'':'closed'))}catch(e){}}
try{const _r=document.documentElement,_b=document.getElementById("railBtn");
 if(_b){const _narrow=matchMedia("(max-width:860px)").matches;
  const _open=_narrow?(_r.dataset.rail==="open"):(_r.dataset.rail!=="closed");
  _b.textContent=_narrow?(_open?"‹":"☰"):(_open?"›":"‹")}}catch(e){}
/* ── 长期记忆管理面板 ───────────────────────────────────────────────
   外观复用项目既有的 .mask/.dlg 弹窗类（与确认框同款），图标用内联 SVG
   而不是 emoji：emoji 在 Windows 上是彩色位图，和界面风格不搭。 */
let memScope='all',memQuery='';
const MEM_ICON='<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" '
 +'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
 +'<path d="M12 5a3 3 0 0 0-3 3v1a2.5 2.5 0 0 0 0 5v1a3 3 0 0 0 6 0v-1a2.5 2.5 0 0 0 0-5V8a3 3 0 0 0-3-3z"/>'
 +'<path d="M9 9H7.5a2 2 0 0 0 0 4H9M15 9h1.5a2 2 0 0 1 0 4H15"/></svg>';
const MEM_CSS=`
#memBtn svg{width:16px;height:16px;display:block}
#memStats{color:var(--muted);font-size:12px;margin:6px 0;font-variant-numeric:tabular-nums}
#memList{overflow:auto;flex:1;min-height:120px}
.mem-row{border-top:1px solid var(--line);padding:9px 2px}
.mem-row:first-child{border-top:0}
.mem-meta{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--muted)}
.mem-meta .sp{margin-left:auto}
.mem-kind{color:var(--accent)}
.mem-pin{color:var(--amber)}
.mem-text{white-space:pre-wrap;margin-top:4px;line-height:1.5;font-size:13px}
.mem-empty{color:var(--muted);padding:14px 2px}
`;
function memInjectCss(){
 if(document.getElementById('memCss'))return;
 const st=document.createElement('style');st.id='memCss';st.textContent=MEM_CSS;document.head.appendChild(st);
}
function memBtn(){
 memInjectCss();
 let b=document.getElementById('memBtn');
 if(b)return b;
 b=document.createElement('button');
 b.id='memBtn';b.type='button';b.className='iconbtn';b.innerHTML=MEM_ICON;
 b.title=tr('长期记忆：查看 / 检索 / 置顶 / 删除（点击打开面板）');
 // 放到顶部工具栏：优先贴着手册按钮，其次常见工具栏容器，最后才退回 body
 // 与「使用手册 / 主题」按钮同一行：左侧栏 .listhead 内（排在手册按钮后面）
 // 图标容器可能是 .listacts（两行布局后的图标行），退回 .listhead
 const bar=document.querySelector('.listacts')||document.querySelector('.listhead');
 if(bar){bar.appendChild(b)}
 else{const a=document.getElementById('btnRefreshSess')||document.querySelector('.trow');
  if(a&&a.parentElement){a.parentElement.insertBefore(b,a.nextSibling)}else{document.body.appendChild(b)}}
 b.onclick=openMemory;
 return b;
}
async function openMemory(){
 let mask=document.getElementById('memMask');
 if(!mask){
  mask=document.createElement('div');
  mask.id='memMask';mask.className='mask';
  mask.innerHTML=`<div class="dlg" style="width:min(720px,94vw)">
   <div class="chead" style="border-bottom:1px solid var(--line)">${tr('长期记忆')}
    <span class="hint"><button class="btn ghost" id="memClose" style="padding:2px 10px;font-size:12px">${tr('关闭')}</button></span></div>
   <div style="padding:12px 15px;display:flex;flex-direction:column;gap:8px;min-height:0">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
     <select id="memScopeSel" class="btn ghost" style="padding:4px 8px">
      <option value="all">${tr('全部作用域')}</option><option value="global">${tr('仅全局')}</option><option value="project">${tr('仅当前项目')}</option>
     </select>
     <input id="memSearch" class="btn ghost" style="flex:1;min-width:150px;padding:4px 10px;text-align:left" placeholder="${tr('关键词检索…（回车）')}">
     <button class="btn ghost" id="memReload" style="padding:4px 12px">${tr('检索')}</button>
    </div>
    <div id="memStats"></div>
    <div id="memList"></div>
   </div>
  </div>`;
  document.body.appendChild(mask);
  mask.querySelector('#memClose').onclick=()=>mask.style.display='none';
  mask.querySelector('#memReload').onclick=loadMemory;
  mask.querySelector('#memScopeSel').onchange=e=>{memScope=e.target.value;loadMemory()};
  mask.querySelector('#memSearch').onkeydown=e=>{if(e.key==='Enter')loadMemory()};
  mask.onclick=e=>{if(e.target===mask)mask.style.display='none'};
 }
 mask.style.display='flex';
 await loadMemory();
}
async function loadMemory(){
 const q=document.getElementById('memSearch'),sel=document.getElementById('memScopeSel');
 if(q)memQuery=q.value.trim();
 if(sel)memScope=sel.value;
 const d=await api('/v1/agent/memory?scope='+encodeURIComponent(memScope)+'&query='+encodeURIComponent(memQuery));
 const st=d.stats||{};
 document.getElementById('memStats').textContent=
  tr('共 {t} 条 · 置顶 {p} · 从未命中 {u} · {kinds} · {scopes}',{t: st.total||0, p: st.pinned||0, u: st.unused||0, kinds: JSON.stringify(st.by_kind||{}), scopes: (d.scopes||[]).join(', ')});
 const box=document.getElementById('memList');
 if(!(d.items||[]).length){
  box.innerHTML=('<div class="mem-empty">'+esc(tr('没有匹配的记忆。Agent 完成任务时会自动沉淀，也可以直接让它「记到长期记忆」。'))+'</div>');return}
 box.innerHTML=d.items.map(m=>`<div class="mem-row">
   <div class="mem-meta">
    <span>#${esc(m.id)}</span><span class="mem-kind">${esc(m.kind)}</span><span>${esc(m.scope)}</span>
    <span>${tr('命中 {n}',{n:m.use_count||0})}</span>${m.pinned?'<span class="mem-pin">★ '+tr('置顶')+'</span>':''}
    ${(m.tags||[]).map(t=>`<span>#${esc(t)}</span>`).join('')}
    <span class="sp"></span>
    <button class="btn ghost" data-pin="${m.id}" data-v="${m.pinned?1:0}" style="padding:2px 10px;font-size:12px">${m.pinned?tr('取消置顶'):tr('置顶')}</button>
    <button class="btn ghost" data-del="${m.id}" style="padding:2px 10px;font-size:12px;color:var(--danger-text);border-color:var(--danger-border)">${tr('删除')}</button>
   </div>
   <div class="mem-text">${esc(m.text)}</div>
  </div>`).join('');
  box.querySelectorAll('[data-del]').forEach(b=>b.onclick=async()=>{
  if(!await dlgConfirm(tr('删除这条长期记忆？删除后不再参与检索。'),'删除记忆',true))return;
  try{await api('/v1/agent/memory/'+b.dataset.del+'?scope='+encodeURIComponent(memScope),{method:'DELETE'});
   toast(tr('已删除记忆 #')+b.dataset.del,'ok');loadMemory()}
  catch(e){toast(tr('删除失败: ')+e,'err')}});
 box.querySelectorAll('[data-pin]').forEach(b=>b.onclick=async()=>{
  const want=b.dataset.v!=='1';
  try{await api('/v1/agent/memory/'+b.dataset.pin+'/pin?pinned='+want+'&scope='+encodeURIComponent(memScope),{method:'POST'});
   toast(want?tr('已置顶（检索时恒定注入）'):tr('已取消置顶'),'ok');loadMemory()}
  catch(e){toast(tr('操作失败: ')+e,'err')}});
}
try{memBtn()}catch(e){}
/* ── Copilot 未就绪提示条（按需启动后：没有 Copilot 也能用提供商模型）── */
let bootStateSeen=null;
function bootBanner(){
 let el=document.getElementById('bootBar');
 if(!el){
  el=document.createElement('div');el.id='bootBar';
  el.innerHTML='<span id="bootIcon" aria-hidden="true">⚠</span>'
   +'<span id="bootText"></span>'
   +'<button class="btn ghost" id="bootRetry">重试浏览器</button>';
  document.body.appendChild(el);
  // 这个浮层是**按需创建**的（第一次 pollBoot 才建），而 applyI18n 在页面加载时就跑完了
  // ——所以它的静态中文永远不会被扫到。建好立刻自己翻一次。
  try{if(typeof applyI18n==='function')applyI18n(el)}catch(e){}
  el.querySelector('#bootRetry').onclick=async()=>{
   const b=el.querySelector('#bootRetry');b.disabled=true;b.textContent=tr('启动中…');
   try{await api('/v1/agent/browser/retry',{method:'POST'});toast(tr('已请求启动浏览器，稍后自动复查'),'ok')}
   catch(e){toast(tr('请求失败: ')+e,'err')}
   finally{b.disabled=false;b.textContent=tr('重试浏览器')}};
 }
 return el;
}
function showBoot(state,ready,err){
 _lastBoot=[state,ready,err];
 const el=bootBanner();
 if(ready){
  el.style.display='none';
  document.body.classList.remove('bootbar-on');
  syncFixedLanes();return}
 document.body.classList.add('bootbar-on');
 el.style.display='flex';   // 先写文字再 syncFixedLanes：否则量到的是空框高度，
                            // 内容会在 1.5s 心跳重算时跳一下（截图抓到过空框）
 // 这条提示在 i18n 的动态容器跳过名单里（#bootText），所以必须自己查表
 const map={idle:tr('浏览器未启动（首次使用 Copilot 时自动启动）'),
            starting:tr('浏览器启动中…'),
            browser_up:tr('浏览器已启动，等待 Copilot 就绪（可能需要在自动化窗口登录）'),
            needs_login:tr('需要在自动化窗口完成 Microsoft 登录'),
            failed:tr('浏览器启动失败')};
 // 错误详情也要过 tr()：它是后端/本地给的中文字符串（如"健康检查失败"），
 // 直接拼会在一句英文里夹中文（实测评测抓到的语言覆盖缺口）。
 const errPart=err?tr('（{e}）',{e:tr(String(err)).slice(0,120)}):'';
 const txt=tr('Copilot 未就绪：')+(map[state]||state||tr('未知'))+errPart+tr('　提供商模型不受影响，可直接使用。');
 document.getElementById('bootText').textContent=txt;
 syncFixedLanes();
}
/* ── 终止服务 / 退出 ────────────────────────────────────────────────
   优雅停机：后端先回响应、再延迟触发 uvicorn 停机（拿不到 server 对象就退回
   信号，并始终有"宽限期后硬退出"兜底）。停机后本页停止一切轮询，
   否则满屏失败请求 + 健康检查横幅乱跳。 */
async function shutdownService(){
 if(!await dlgConfirm(
  tr('即将终止本服务（进程退出）：')+'\n\n'
  +'· '+tr('正在运行的任务会中断（状态已落库，重启后可恢复）')+'\n'
  +'· '+tr('自动化浏览器会被关闭，本页面随即断开')+'\n'
  +'· '+tr('需要时重新运行 start.py 启动服务')+'\n\n'
  +tr('确定终止？'),tr('终止服务'),true))return;
 try{
  const d=await api('/v1/shutdown',{method:'POST'});
  showStopped(d?noteText(d,''):'');
 }catch(e){
  // 连接可能比响应先断：同样视为已停止，不要报"操作失败"
    showStopped(tr('服务已终止（连接已断开）'));
 }
}
/* ── 重启服务：优雅退出 → 就地 exec 重启；本页轮询 /health，恢复后自动刷新 ── */
let restarting=false, restartStartedAt=0;
async function restartService(){
 if(window.restarting)return;
  if(!await dlgConfirm(
    tr('即将重启本服务（用于让改过的代码生效）：\n\n' +
       '· 服务会先优雅退出，然后**就地重启**（同一个进程，端口不变）\n' +
       '· 正在运行的任务会中断（状态已落库；开着 auto_resume 会自动续跑）\n' +
       '· 自动化浏览器会重新连接\n' +
       '· 本页会在服务恢复后**自动刷新**，无需手动操作\n\n' +
       '确定重启？'),tr('重启服务'),false))return;
 window.restarting=true; restartStartedAt=Date.now();
 try{ await api('/v1/restart',{method:'POST'}) }catch(e){}
 showRestarting();
 const ok=await waitServiceBack(90000);
 if(ok){ location.reload(); return }
 window.restarting=false;
 toast(tr('服务在 90 秒内未恢复，请检查控制台或重新运行 start.py'),'err');
}
/* 挂一条重启提示条（复用停止横幅的位置与视觉） */
function showRestarting(extra){
 let el=document.getElementById('stoppedBanner');
 if(!el){el=document.createElement('div');el.id='stoppedBanner';el.className='stoppedbanner';
  document.body.insertBefore(el,document.body.firstChild)}
 const secs=Math.max(0,Math.round((Date.now()-restartStartedAt)/1000));
 el.innerHTML='<b>↻ '+esc(tr('服务正在重启'))+'</b><span>'+esc(extra||tr('正在等待服务恢复（通常几秒），恢复后本页会自动刷新…'))+'</span>'
  +'<span class="meta" id="restartElapsed">'+esc(String(secs))+'s</span>';
 el.style.display='flex';
 try{hideRetryUI()}catch(e){}; try{hideConnBanner()}catch(e){}; try{stopEvents()}catch(e){}
 syncFixedLanes();
}
/* 轮询 /health：任一 2xx 且能解析即认为服务回来了 */
async function waitServiceBack(timeoutMs){
 const t0=Date.now();
 for(;;){
  if(Date.now()-t0>timeoutMs) return false;
  const secs=Math.round((Date.now()-restartStartedAt)/1000);
  const el=document.getElementById('restartElapsed'); if(el) el.textContent=secs+'s';
  try{
   const r=await fetch('/health',{cache:'no-store'});
   if(r.ok){ await r.json(); return true }
  }catch(e){}
  await new Promise(r=>setTimeout(r,700));
 }
}
function showStopped(note){
 window.serviceStopped=true;
 let el=document.getElementById('stoppedBanner');
 if(!el){el=document.createElement('div');el.id='stoppedBanner';el.className='stoppedbanner';
  document.body.insertBefore(el,document.body.firstChild)}
 el.innerHTML='<b>⏻ '+esc(tr('服务已终止'))+'</b><span>'+esc(note||'')+'</span>'
  +'<span class="meta">'+esc(tr('本页可以关闭了；需要时重新运行'))+' <code>python start.py</code> '+esc(tr('启动'))+'。</span>';
 el.style.display='flex';
 try{hideRetryUI()}catch(e){}
 try{hideConnBanner()}catch(e){}
 try{stopEvents()}catch(e){}
 syncFixedLanes();
}
async function pollBoot(){
 if(window.serviceStopped)return;
 try{const h=await api('/health');showBoot(h.browser_state,h.copilot_ready,h.browser_error)}
 catch(e){if(!window.serviceStopped)showBoot('failed',false,tr('健康检查失败'))}
}
try{setInterval(pollBoot,8000);setTimeout(pollBoot,1200)}catch(e){}

try{const _rs=document.getElementById('btnRefreshSess');if(_rs)_rs.onclick=async()=>{
 if(_rs.disabled)return;_rs.disabled=true;_rs.style.opacity=.5;
 try{await refreshModels();refreshSessModelOptions();toast(tr('模型列表已刷新'),'ok')}
 catch(e){toast(tr('刷新失败: ')+e,'err')}
 finally{_rs.disabled=false;_rs.style.opacity=1}};}catch(e){}





/* ── 事件委托（CSP 收紧后的统一入口）─────────────────────────────────
   原先页面有 49 处内联 onclick/oninput/onkeydown，加上动态生成的 4 处。
   内联处理器有两个问题：① 迫使 CSP 保留 script-src 'unsafe-inline'，
   而内联脚本正是 XSS 的主要放大器；② 动态 HTML 每次重建都要重新绑定。
   现在统一走 data-act / data-act-input / data-act-key + 一个分发器：
   处理器集中在此，可审计；动态内容天然可用；CSP 可以只留 'self'。 */
/* ── 设置页 ───────────────────────────────────────────────────────────
   齿轮入口：外观 / 执行 / 浏览器 / 数据 / 入口 / 危险区。
   服务端配置走 /v1/config（白名单，见 api.CONFIG_SCHEMA），外观与语言即时生效。 */
const SETTINGS_GROUPS=[
 ['ui', '外观'], ['exec', '执行'], ['browser', '浏览器'],
 ['data', '数据'], ['entries', '入口'], ['danger', '危险区']];
/* 枚举值的显示名：直接显示 "session"/"each"/"lazy" 太像代码（用户看到的是生词）。 */
const ENUM_LABEL={each:'每次操作都询问',session:'本会话内不再逐次询问',
  lazy:'首次使用时启动',eager:'服务启动时预热'};

/* ── 权限档位（仿 DSH）─────────────────────────────
   后端早有 SANDBOX_MODES 与 /v1/agent/sandbox（含 persist），这里只是把它做成
   输入框上方一眼可见的 chip：点开三档、选中即生效并落库。 */
const SANDBOX_UI={
 'read-only':{ico:'👁', label:'仅可查看'},
 'workspace-write':{ico:'🛠', label:'工作区内修改'},
 'danger-full-access':{ico:'⚠', label:'完全权限'},
};

export { themeChoice, resolveTheme, applyTheme, toggleTheme, watchSystemTheme, toggleRail, memInjectCss, memBtn, openMemory, loadMemory, bootBanner, showBoot, shutdownService, restartService, showRestarting, waitServiceBack, showStopped, pollBoot, SETTINGS_GROUPS, SANDBOX_UI, ENUM_LABEL };
