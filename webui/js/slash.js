// slash.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
import { SLASH_COMMANDS } from './panels.js';
function slashMatch(q){
 const s=(q||'').toLowerCase();
 if(!s)return SLASH_COMMANDS.slice();
 return SLASH_COMMANDS.filter(c=>c.aliases.some(a=>a.toLowerCase().startsWith(s)));
}
/* 斜杠菜单可以挂在两个输入框上：新任务（#task）与继续对话（#followup）。
   每个输入框配自己的菜单容器（都绝对定位在输入框上方），逻辑完全共用。
   `_ctxInput` 记住"最近一次由谁打开的菜单"，供 pick/key 回填到正确的输入框。 */
const SLASH_PAIRS=[
 {input:'task', menu:'slashMenu'},
 {input:'followup', menu:'slashMenuFollow'},
];
let _ctxInput=null;
function _pair(inputEl){
 const want=inputEl||_ctxInput||document.getElementById('task');
 for(const p of SLASH_PAIRS){
  const el=document.getElementById(p.input);
  if(!el)continue;
  if(el===want||p.input===want)return {input:el, menu:document.getElementById(p.menu)};
 }
 return {input:document.getElementById(SLASH_PAIRS[0].input),
         menu:document.getElementById(SLASH_PAIRS[0].menu)};
}
function _hideMenus(){
 for(const p of SLASH_PAIRS){
  const b=document.getElementById(p.menu);
  if(b){b.hidden=true;b.innerHTML=''}
 }
}
function slashOpen(){
 return SLASH_PAIRS.some(p=>{const b=document.getElementById(p.menu);return b&&!b.hidden});
}
function slashUpdate(force, inputEl){
 const _tgt=_pair(inputEl), t=_tgt.input, box=_tgt.menu;
 if(!t||!box)return;
 _ctxInput=t;
 const m=/^\/(\S*)$/.exec(t.value);
 if(!m&&!force){box.hidden=true;box.innerHTML='';return}
 const items=slashMatch(m?m[1]:'');
 if(!items.length){box.hidden=true;box.innerHTML='';return}
 if(slashIdx>=items.length)slashIdx=0;
 box.hidden=false;box.innerHTML='';
 items.forEach((c,i)=>{
  const row=document.createElement('div');
  row.className='slashrow'+(i===slashIdx?' on':'');
  const sd=document.createElement('span');sd.className='sdesc';
  if(c.raw)sd.setAttribute('data-i18n-skip','1');   // MCP 模板描述是数据
  sd.textContent=tr(c.desc);
  row.innerHTML='<code>/'+esc(c.label)+'</code>';
  row.appendChild(sd);
  row.onmousedown=(ev)=>{ev.preventDefault();slashPick(c)};
  box.appendChild(row)});
}
/* MCP prompt 模板 → 动态斜杠命令（/server.模板名 参数1 参数2 …）。
   模板是 server 给的**数据**，描述不参与 i18n 覆盖率判定（raw:true）。 */
let mcpPromptSig='';
async function loadMcpPrompts(){
 try{
  const d=await api('/v1/plugins/mcp/api/prompts');
  const prompts=(d&&d.prompts)||[];
  const sig=JSON.stringify(prompts.map(p=>[p.server,p.name,p.description]));
  if(sig===mcpPromptSig)return;              // 没变化就别重建
  mcpPromptSig=sig;
  for(let i=SLASH_COMMANDS.length-1;i>=0;i--) if(SLASH_COMMANDS[i].mcp) SLASH_COMMANDS.splice(i,1);
  prompts.forEach(p=>{
   const label=p.server+'.'+p.name;
   const argNames=(p.arguments||[]).map(a=>a.name).filter(Boolean);
   SLASH_COMMANDS.push({
    id:'mcp_'+p.server+'_'+p.name, label, aliases:[label], raw:true,
    desc:p.description||('MCP '+p.server+' 的 '+p.name+' 模板'),
    mcp:{server:p.server,name:p.name,argNames},
    run:(arg)=>{
     const parts=String(arg||'').trim()?String(arg).trim().split(/\s+/) : [];
     const args={};
     argNames.forEach((n,i)=>{ if(parts[i]!==undefined) args[n]=parts[i] });
     const missing=argNames.filter(n=>args[n]===undefined);
     if(missing.length){ toast(tr('用法：/')+label+' '+missing.map(n=>'<'+n+'>').join(' '),'err'); return }
     post('/v1/plugins/mcp/api/prompt',{server:p.server,name:p.name,arguments:args})
      .then(r=>{
       if(!r||r.error){ toast(tr('模板渲染失败: ')+(r&&r.error||''),'err'); return }
       const t=document.getElementById('task');
       if(t){ t.value=(r.text||''); t.focus(); t.setSelectionRange(t.value.length,t.value.length) }
       toast(tr('已载入模板: ')+label,'ok');
      })
      .catch(e=>toast(tr('模板渲染失败: ')+(e.text||e),'err'));
    }});
  });
 }catch(e){ /* 没装/没配 MCP 时静默 */ }
}
function post(u,body){
 return api(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
}

function slashPick(c, inputEl){
 const _tgt=_pair(inputEl), t=_tgt.input, box=_tgt.menu;
 if(!t)return;
 t.value='/'+c.label+' ';t.focus();
 if(box){box.hidden=true;box.innerHTML=''}
 _hideMenus();
}
function slashKey(e, inputEl){
 const _tgt=_pair(inputEl), t=_tgt.input, box=_tgt.menu;
 if(!t||!box||box.hidden)return false;
 _ctxInput=t;
 const rows=[...box.querySelectorAll('.slashrow')];
 const n=Math.max(rows.length,1);
 if(e.key==='ArrowDown'){slashIdx=(slashIdx+1)%n;slashUpdate(true);e.preventDefault();return true}
 if(e.key==='ArrowUp'){slashIdx=(slashIdx-1+n)%n;slashUpdate(true);e.preventDefault();return true}
 if(e.key==='Enter'){
  // 已是**完整命令**（/chat、/help、/research 问题…）→ 直接执行；
  // 只有还在输入前缀时才补全。否则 /help 会被当成补全、永远执行不了。
  if(slashRun(t.value)){e.preventDefault();return true}
  const m1=/^\/(\S*)$/.exec(t.value||'');
  const c1=slashMatch(m1?m1[1]:'')[slashIdx];
  if(c1){slashPick(c1);e.preventDefault();return true}
 }
 if(e.key==='Tab'){
  const m=/^\/(\S*)$/.exec(t.value||'');
  const c=slashMatch(m?m[1]:'')[slashIdx];
  if(c){slashPick(c);e.preventDefault();return true}
 }
 if(e.key==='Escape'){_hideMenus();e.preventDefault();return true}
 return false;
}

/* 插件自带的译文：拉一次并合并进字典，让 tr() 能覆盖插件文案。
   平台不知道第三方插件写了哪些中文，由插件清单声明（gantry.i18n）最不失真。 */
async function mergePluginI18n(quiet){
 try{
  const d=await api('/v1/plugins/i18n');
  const en=d.en||{}, tw=d.tw||{};
  let n=0;
  Object.keys(en).forEach(k=>{ if(I18N_EN[k]!==en[k]){I18N_EN[k]=en[k];n++} });
  Object.keys(tw).forEach(k=>{ if(I18N_TW_OVERRIDE[k]!==tw[k]){I18N_TW_OVERRIDE[k]=tw[k];n++} });
  if(n){ applyI18n(document); if(!quiet) toast(tr('已合并 {n} 条插件译文',{n:n}),'ok') }
  return n;
 }catch(e){ return 0 }
}
/* 显式扇出：不经过模型判断，直接把 N 个任务派给 N 个子代理（确定性）。
   与"让模型自己决定要不要用子代理"互补——弱模型经常有捷径就自己串行做完了。 */
async function fanoutNow(tasks){
 closeDlg();
 toast(tr('已派出 {n} 个子代理，正在跑…',{n:tasks.length}),'ok');
 try{
  const d=await api('/v1/agent/fanout',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({tasks,model:currentModel||undefined})});
  const ok=d.succeeded||0,fail=d.failed||0;
  toast(tr('扇出完成：成功 {a} · 失败 {b} · {t} 秒',{a:ok,b:fail,t:d.seconds||0}),
        fail?'err':'ok');
  if(d.session_id) load(d.session_id);
 }catch(e){toast(tr('扇出失败: ')+e,'err')}
}
async function auditNow(arg){
 let objective=String(arg||'').trim();
 if(!objective){
  objective=await dlgPrompt(tr('审计目标：要审什么改动/项目？（范围默认当前工作区）'),'',tr('开始审计'),tr('对抗式审计'));
  if(objective==null)return;
 }
 try{
  const d=await api('/v1/agent/audit',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({objective,model:currentModel||undefined})});
  toast(tr('对抗式审计已启动'),'ok');
  await refreshSessions();
  if(d.session_id)await load(d.session_id);
 }catch(e){toast(tr('审计启动失败: ')+((e&&(e.text||e.detail))||e),'err')}
}
function slashRun(text){
 const m=/^\/(\S+)\s*([\s\S]*)$/.exec(String(text||'').trim());
 if(!m)return false;
 const c=SLASH_COMMANDS.find(x=>x.aliases.some(a=>a.toLowerCase()===m[1].toLowerCase()));
 if(!c)return false;
 _hideMenus();
 c.run((m[2]||'').trim());
 return true;
}

/* ── 深研一键命令（research 插件 + 模型综合）────────────────────────
   点「深研」→ 输入问题 → 后端收集证据包 → Agent 写成带来源的报告 → 打开新会话。 */
async function deepResearchNow(query){
 // 斜杠命令可以带问题（/research 问题），按钮则弹输入框
 let q=query;
 if(!q){
  q=await dlgPrompt(
   tr('输入研究问题：会用多角度搜索 + 抓取正文，生成带来源的调研报告（约 20-60 秒）'),
   '', tr('开始深研'), tr('深研'));
 }
 if(!q||!String(q).trim())return;
 toast(tr('深研中…（多角度搜索 + 抓正文）'),'ok');
 try{
  const model=currentModel||'';
  const d=await api('/v1/agent/deep-research',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({query:String(q).trim(),model})});
  if(d&&d.session_id){ await load(d.session_id); toast(tr('深研完成：已打开报告会话'),'ok'); }
  else{ toast(tr('深研完成'),'ok') }
 }catch(e){ toast(tr('深研失败: ')+(e.text||e),'err') }
}

/* ── 工作目录选择器（对齐 DSH：native 优先，失败回退 browse）──────────
   native：POST /v1/agent/pick-directory（系统文件夹对话框）
   browse：GET /v1/agent/browse（应用内树，对话框打不开时用） */
/* 工作区分组折叠状态（localStorage，跨刷新保留） */

export { slashMatch, slashOpen, slashUpdate, loadMcpPrompts, slashPick, slashKey, mergePluginI18n, fanoutNow, auditNow, slashRun, deepResearchNow };
