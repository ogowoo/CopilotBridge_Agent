// apistate.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
import { ApiError } from './cards.js';
async function api(u,o={}){
  if(window.DEMO)return demoApi(u,o);
  const call=tk=>{const opt=Object.assign({},o);opt.headers=Object.assign({},o.headers||{});
    if(tk)opt.headers['X-Gantry-Token']=tk;
    // 语言头：后端据此翻译错误 detail（展示层；协议/日志仍是中文）
    try{opt.headers['X-Gantry-Lang']=currentLang()}catch(e){}
    return fetch(u,opt)};
  let r=await call(apiToken());
  if(r.status===401){
    // 服务端配置了 api_token：就地询问一次并记住（仅本机浏览器 localStorage）。
    // 复用同一个 promise，避免轮询期间连弹多次、把用户正在看的确认框顶掉。
    if(tokenPromptCancelled){throw new ApiError(401,'已取消令牌输入','');}
    if(!tokenPrompting){
      tokenPrompting=(async()=>{
        try{
          const tk=await dlgPrompt(tr('本服务启用了 api_token，请输入访问令牌（只保存在本机浏览器）'),apiToken(),'','需要访问令牌');
          if(tk){try{localStorage.setItem('gantry-token',tk);tokenPromptCancelled=false}catch(e){}}
          else{tokenPromptCancelled=true;showConnBanner(tr('未授权：已取消令牌输入（点此重新输入）'),true)}
          return tk;
        }finally{tokenPrompting=null}
      })();
    }
    const tk=await tokenPrompting;
    if(tk)r=await call(tk);
  }
  let t=await r.text();if(!r.ok)throw new ApiError(r.status,t,u);return t?JSON.parse(t):{}}
/* tr() 由 i18n.js 提供（该文件在 app.js 之前加载）。若它加载失败，这里兜底成"原样返回"，
   否则每个用了 tr() 的交互都会抛 ReferenceError。 */
if (typeof tr !== 'function') { window.tr = (k) => String(k == null ? '' : k); }
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
/* ── 通道无关的显示名 ──────────────────────────────────────────────
   服务现在既支持 Copilot 网页，也支持 API 提供商（deepseek/… 等），
   界面文案不该再写死 "Copilot"。 */
const BRAND={deepseek:'DeepSeek',kimi:'Kimi',openai:'OpenAI',grok:'Grok',moonshot:'Kimi',
             anthropic:'Claude',google:'Gemini',qwen:'Qwen',zhipu:'智谱',myapi:'API'};
function isProviderModel(m){return String(m||'').indexOf('/')>=0}
function assistantLabel(model){
 const m=String(model==null?(window.currentModel||''):model);
 if(!m)return tr('助手');
 if(isProviderModel(m)){
  const p=m.split('/')[0].toLowerCase();
  return BRAND[p]||(p.charAt(0).toUpperCase()+p.slice(1))}
 return 'Copilot'}
/* 消息时间：今天只显示时分秒，跨天补上日期 */
function fmtStamp(ts){
 if(!ts)return '';
 const d=new Date(ts*1000),now=new Date();
 const hm=d.toLocaleTimeString('zh-CN',{hour12:false});
 return d.toDateString()===now.toDateString()?hm
   :`${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')} ${hm}`}
function jv(v,key){
 if(v===null||v===undefined)return '<span class="nl">null</span>';
 if(typeof v==='boolean')return '<span class="bl">'+v+'</span>';
 if(typeof v==='number')return '<span class="nm">'+v+'</span>';
 if(typeof v==='string'){
  if(key==='content_b64')return '<span class="nl">'+esc(tr('[Base64 内容，共 {n} 字符]',{n:v.length}))+'</span>';
  let t=v.length>4000?v.slice(0,4000)+'\n'+tr('…[共 {n} 字符，已截断]',{n:v.length}):v;
  return '<span class="st">"'+esc(t)+'"</span>'}
 if(Array.isArray(v)){if(!v.length)return '[]';
  return '[<div class="ind">'+v.map(x=>'<div>'+jv(x)+'</div>').join('')+'</div><span class="nl">]</span>'}
 if(typeof v==='object'){let ks=Object.keys(v);if(!ks.length)return '{}';
  return '{<div class="ind">'+ks.map(k=>'<div><span class="ky">'+esc(k)+'</span>: '+jv(v[k],k)+'</div>').join('')+'</div><span class="nl">}</span>'}
 return esc(String(v))}

/* ── 状态呈现 ── */
const STATE_TEXT={running:'运行中',paused:'已暂停',waiting:'等待服务',ready:'就绪',completed:'已完成',failed:'失败',cancelled:'已取消',expired:'已过期',archived:'已归档'};
function stateText(s){return tr(STATE_TEXT[s]||s||'—')}
/* 会话状态图标：形状 + 颜色双编码，替掉原来占位的文字块（completed 这种太宽）。
   14×14 描边图标，title 里仍给完整文案（悬停可见 / 读屏可读）。 */
const STATE_ICON={
 running:'<circle cx="8" cy="8" r="6.2" stroke-dasharray="26 12" class="spin"/>',
 completed:'<circle cx="8" cy="8" r="6.2"/><path d="M5.1 8.2l2 2 3.8-4"/>',
 failed:'<circle cx="8" cy="8" r="6.2"/><path d="M6 6l4 4M10 6l-4 4"/>',
 paused:'<circle cx="8" cy="8" r="6.2"/><path d="M6.6 5.8v4.4M9.4 5.8v4.4"/>',
 waiting:'<circle cx="8" cy="8" r="6.2"/><path d="M8 4.6V8l2.4 1.6"/>',
 ready:'<circle cx="8" cy="8" r="6.2"/>',
 cancelled:'<circle cx="8" cy="8" r="6.2"/><path d="M4.4 11.6L11.6 4.4"/>',
 expired:'<circle cx="8" cy="8" r="6.2"/><path d="M4.4 11.6L11.6 4.4"/>',
 archived:'<circle cx="8" cy="8" r="6.2"/><path d="M5.4 5.4h5.2v5.2H5.4z"/>'};
function stateIcon(s){
 const k=STATE_ICON[s]||STATE_ICON.ready;
 return '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor"'
   +' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+k+'</svg>'}
function stateClass(s){return s==='running'?'run':(s==='paused'||s==='waiting')?'warn':(s==='failed')?'bad':(s==='completed')?'done':''}
/* 父会话已结束、但后代子代理还在跑：状态不能笼统显示"已完成"（用户实测会被误导成
   "任务全做完了"）。这里统一给出"已完成 · 子代理 N 仍在跑"的文案与颜色。 */
function subRunningOf(o){return Number((o&&(o.subagent_running??o.subagentRunning))||0)}
function stateLabel(o){
 const st=String((o&&(o.state||o.session_state))||'ready');
 const n=subRunningOf(o);
 if(n>0&&(st==='completed'||st==='failed'||st==='cancelled'))
  return stateText(st)+' · '+tr('子代理 {n} 仍在跑',{n});
 return stateText(st)}
function stateClassOf(o){
 const st=String((o&&(o.state||o.session_state))||'ready');
 return subRunningOf(o)>0&&(st==='completed'||st==='failed'||st==='cancelled')?'warn':stateClass(st)}
function setPill(s){let e=document.getElementById('statepill');e.textContent=stateText(s);e.className='pill '+stateClass(s)}
function setKpis(o){
 if(o.token_budget){window.budget=o.token_budget;document.getElementById('kpiBudget').textContent=' / '+Number(window.budget).toLocaleString()}
 const _st=String(o.state||o.session_state||'ready');
 const _card=document.getElementById('kpiStateCard');
 const _hasSubRun=subRunningOf(o)>0&&(_st==='completed'||_st==='failed'||_st==='cancelled');
 document.getElementById('kpiState').textContent=stateLabel(o);
 // 状态卡片按状态变色；后代仍在跑时也要跟着变黄，别让用户看到绿色"已完成"就走
 if(_card){const _c=_hasSubRun?'waiting':_st; if(_card.dataset.state!==_c)_card.dataset.state=_c}
 // （_card 已在上面取过）
 // （已由上面统一设置，避免把子代理运行态覆盖回完成态）
 // 步数与 token 要把**子代理**算进来：子代理是独立会话、各自记账，
 // 只读主会话字段时"跑了 4 个子代理、几百步"在界面上完全看不出来（用户实测）。
 const _ss=Number(o.subagent_steps||0), _stk=Number(o.subagent_tokens||0),
       _sc=Number(o.subagent_count||0);
 const _ownSteps=Number(o.total_steps??0), _ownTk=Number(o.estimated_tokens??0);
 const stepsAll=_ownSteps+_ss, tkAll=_ownTk+_stk;
 const _marksub=(el,total,own,extra,n)=>{
   if(!el)return;
   el.textContent=Number(total).toLocaleString();
   if(extra>0){
     el.title=tr('本会话 {a} + 子代理 {b}（{n} 个）',{a:Number(own).toLocaleString(),
       b:Number(extra).toLocaleString(),n});
     el.dataset.sub='1';
   }else{el.removeAttribute('title');delete el.dataset.sub}};
 _marksub(document.getElementById('kpiSteps'),stepsAll,_ownSteps,_ss,_sc);
 document.getElementById('kpiConv').textContent=o.conversation_steps??0;
 // 与侧栏行、活动面板的「会话序号」一致：显示"第几个 Copilot 对话"（1 起），
 // 而不是原始下标（0 起）——三处同一个数，避免用户看到两套"会话"数据。
 document.getElementById('kpiRoll').textContent=(o.conversation_index??0)+1;
 const _tkEl=document.getElementById('kpiToken');
 _marksub(_tkEl,tkAll,_ownTk,_stk,_sc);
 const tk=tkAll;
 // 有子代理时在小标里标一下，避免用户以为统计漏了
 try{const _note=document.getElementById('kpiSubNote');
  if(_note)_note.textContent=_sc?tr('含 {n} 个子代理',{n:_sc}):''}catch(e){}
 document.getElementById('tokenbar').style.width=(window.budget?Math.min(100,tk/window.budget*100):0)+'%'}

/* ── Session 列表（支持筛选） ── */
/* ── 模型列表：从 /v1/models 动态加载，与后端（含 config.models 扩展）保持一致 ── */
async function loadModels(){
 if(window.DEMO){window.modelCache=['copilot-auto','copilot-think-deeper','copilot-gpt-5-6-sol-think-deeper'];
  fillModelSelect(document.getElementById('sessModel'),window.modelCache.map(id=>({id})),window.currentModel);return}
 try{let d=await api('/v1/models'),sel=document.getElementById('model');
  if(!d.data||!d.data.length)return;
  // 只显示可用的：未配 API Key 的提供商模型、菜单扫描没见到的 Copilot 模型不显示
  window.__MODELS=d.data || [];   // 挂起卡片按"provider/model"筛提供商模型用
  let avail=d.data.filter(m=>m.available!==false);
  if(!avail.length)avail=d.data;  // 兜底：全不可用时不过滤，防止下拉为空
  window.modelCache=avail.map(m=>m.id);
  fillModelSelect(document.getElementById('sessModel'), avail, document.getElementById('sessModel').value);
  let prev=sel.value;
  sel.innerHTML='';
  avail.forEach(m=>{let o=document.createElement('option');o.value=m.id;o.textContent=m.id;const _d=modelDesc(m);if(_d)o.title=_d;sel.appendChild(o)});
  sel.value=avail.some(m=>m.id===prev)?prev:avail[0].id;
  refreshSessModelOptions()}catch(e){}
}
/* 填充模型下拉；keep 为当前会话的模型（不在可用列表里也要保留并标注） */
function fillModelSelect(sel, avail, keep){
 if(!sel)return;
 let prev=keep||sel.value||'';
 let seen=avail.map(m=>m.id);
 sel.innerHTML='';
 avail.forEach(m=>{let o=document.createElement('option');o.value=m.id;o.textContent=m.id;const _d=modelDesc(m);if(_d)o.title=_d;sel.appendChild(o)});
 if(prev&&!seen.includes(prev)){
  // 在当前模型被 available 过滤掉、但确实存在于模型表里时，正常显示（不打标记）：
  // 菜单是两级结构，扫描偶尔漏掉叶子，不该把用户正在用的模型说成"不存在"。
  const all=(window.__MODELS||[]).map(m=>m.id);
  const label=all.includes(prev)?prev:prev+tr('（菜单里未找到）');
  let o=document.createElement('option');o.value=prev;o.textContent=label;sel.appendChild(o)}}
/* 会话页模型选择器：显示当前会话自己的模型 */
function refreshSessModelOptions(){
 let sel=document.getElementById('sessModel');if(!sel)return;
 let want=window.currentModel||'';
 if(!want)return;
 let has=[...sel.options].some(o=>o.value===want);
 if(!has){
  const all=(window.__MODELS||[]).map(m=>m.id);
  let o=document.createElement('option');o.value=want;
  o.textContent=all.includes(want)?want:want+tr('（菜单里未找到）');
  sel.appendChild(o)}
 sel.value=want;
 sel.title=tr('当前会话模型：{n}（下一回合生效，服务重启后仍沿用）',{n:want})}	
async function setSessionModel(model){
 if(!window.current||!model)return;
 try{
  let d=await api(`/v1/agent/sessions/${encodeURIComponent(window.current)}/model`,
                  {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
  window.currentModel=d.model||model;
  toast(tr('会话模型已切换为 ')+(d.model||model)+(d.note?'（'+d.note+'）':'')+'，下一回合生效', d.note?'':'ok')
 }catch(e){toast(tr('切换模型失败: ')+(e.text||e),'err');refreshSessModelOptions()}}
/* 模型说明：原生 <option title> 的文案要跟着界面语言走。
   后端 /v1/models 同时给 description（中文）与 description_en（英文）。 */
function modelDesc(m){
 if(!m)return '';
 if(currentLang()==='en'&&m.description_en)return m.description_en;
 if(currentLang()==='tw'&&m.description)return (typeof toTraditional==='function')?toTraditional(m.description):m.description;
 return m.description||'';
}
async function refreshModels(){
 if(window.DEMO){toast(tr('演示模式（API 未连接）：无法刷新模型列表'),'err');return}
 let btn=document.getElementById('refreshModelsBtn');
 if(btn){btn.disabled=true;btn.textContent='…'}
 try{
  let d=await api('/v1/models/refresh',{method:'POST'});
  await loadModels();
  if(!d.scanned){toast(tr('模型菜单未能扫描：')+(d.reason||'未知原因'),'err');return}
  let n=(d.labels||[]).length;
  if(d.added&&d.added.length)toast(tr('发现新模型: ')+d.added.join(', '),'ok');
  else toast(tr('已重新扫描（')+n+' 个菜单项，无新增）','');
 }catch(e){toast(tr('模型菜单读取失败: ')+e,'err')}
 finally{if(btn){btn.disabled=false;btn.textContent='↻'}}}

/* ── 浏览器窗口显示/隐藏（headless 是启动参数，切换会带登录态重启浏览器上下文） ── */
function drawBrowserBtn(h){let b=document.getElementById('browserBtn');
 // 只更新提示，不动内容——之前 b.textContent='▣' 把 SVG 图标替换成了文字，
 // 按钮里显示"切换"两个字（26px 宽塞不下，竖排成两行）。
 b.title=tr(h?'当前为无头模式（窗口隐藏），点击显示自动化窗口':'当前显示自动化窗口，点击切换为无头模式（隐藏窗口）');
 b.style.color=h?'var(--muted)':'';  // 无头时淡一点，有窗口时正常
}
async function browserState(){if(window.DEMO)return;try{let d=await api('/v1/agent/browser');drawBrowserBtn(d.headless)}catch(e){}}
async function toggleBrowser(){
 if(window.DEMO)return;
 let b=document.getElementById('browserBtn');b.disabled=true;b.style.opacity=.5;
 try{let cur=await api('/v1/agent/browser');
  let d=await api('/v1/agent/browser',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({headless:!cur.headless})});
  drawBrowserBtn(d.headless);
  if(d.fallback)toast(tr('无头模式启动失败，已回退为有窗口模式（旧版 Edge 或安全策略不支持无头持久化）'),'err')}catch(e){toast(tr('切换失败: ')+e,'err');browserState()}
 finally{b.disabled=false;b.style.opacity=1}
}

/* 子代理会话归集到父会话下：默认折叠，点 ▸ 展开（状态存 localStorage） */
try{JSON.parse(localStorage.getItem('gantry-expanded-sess')||'[]').forEach(x=>expandedSess.add(x))}catch(e){}

export { api, esc, isProviderModel, assistantLabel, fmtStamp, jv, stateText, stateIcon, stateClass, subRunningOf, stateLabel, stateClassOf, setPill, setKpis, loadModels, fillModelSelect, refreshSessModelOptions, setSessionModel, modelDesc, refreshModels, drawBrowserBtn, browserState, toggleBrowser };
