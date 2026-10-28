// panels.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
function _panelUrl(p){ return '/v1/plugins/'+encodeURIComponent(p.plugin)+'/api/'+String(p.path||'').replace(/^\/+/, ''); }
function _panelData(p){
 return api(_panelUrl(p), {method:p.method||'GET'});
}
function _renderKv(obj){
 if(!obj||typeof obj!=='object'||Array.isArray(obj))return _renderJson(obj);
 const rows=Object.entries(obj).map(([k,v])=>{
  const val=(v&&typeof v==='object')?JSON.stringify(v):String(v===null||v===undefined?'':v);
  return '<tr><th>'+esc(k)+'</th><td>'+esc(val)+'</td></tr>';});
 return '<table class="ptable">'+rows.join('')+'</table>';
}
function _renderList(arr){
 if(!Array.isArray(arr))return _renderJson(arr);
 if(!arr.length)return '<div class="meta">'+esc(tr('（空）'))+'</div>';
 return '<ul class="plist">'+arr.map(x=>'<li>'+esc(typeof x==='object'?JSON.stringify(x):String(x))+'</li>').join('')+'</ul>';
}
function _renderTable(arr){
 if(!Array.isArray(arr))return _renderJson(arr);
 if(!arr.length)return '<div class="meta">'+esc(tr('（空）'))+'</div>';
 const cols=[...new Set(arr.flatMap(x=>Object.keys(x||{})))];
 const head='<tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>';
 const body=arr.map(x=>'<tr>'+cols.map(c=>{
  const v=x&&x[c]; const s=(v&&typeof v==='object')?JSON.stringify(v):(v===null||v===undefined?'':String(v));
  return '<td>'+esc(s)+'</td>';}).join('')+'</tr>').join('');
 return '<table class="ptable">'+head+body+'</table>';
}
function _renderJson(v){
 try{ return '<pre class="pjson">'+esc(JSON.stringify(v,null,2))+'</pre>' }
 catch(e){ return '<pre class="pjson">'+esc(String(v))+'</pre>' }
}
function _renderPanel(p,data){
 const body=document.getElementById('panelBody');
 if(!body)return;
 const box=(data&&typeof data==='object'&&!Array.isArray(data)&&Array.isArray(data[p.id])
            ? data[p.id] : (data&&data.items!==undefined?data.items:data));
 let html='';
 const v=p.view||'kv';
 if(v==='table')html=_renderTable(Array.isArray(box)?box:_firstArray(data));
 else if(v==='list')html=_renderList(Array.isArray(box)?box:_firstArray(data));
 else if(v==='json')html=_renderJson(data);
 else if(v==='markdown')html='<div class="md-body">'+mdToHtml(String((data&&(data.text||data.markdown))||''))+'</div>';
 else html=_renderKv(box);
 body.innerHTML=html;
 if(p.hint)body.insertAdjacentHTML('afterbegin','<div class="meta">'+esc(tr(p.hint))+'</div>');
}
function _firstArray(data){
 if(Array.isArray(data))return data;
 if(data&&typeof data==='object'){
  for(const k of Object.keys(data)) if(Array.isArray(data[k])) return data[k];
 }
 return [];
}
async function openPanels(){
 openMask('panelMask');
 await refreshPanels(true);
}
async function refreshPanels(silent){
 const list=document.getElementById('panelList'), body=document.getElementById('panelBody');
 if(!list||!body)return;
 let panels=[];
 try{ const d=await api('/v1/plugins/panels'); panels=(d&&d.panels)||[] }
 catch(e){ list.innerHTML='<div class="meta">'+esc(tr('读取失败: ')+(e.text||e))+'</div>'; return }
 panelCache=panels;
 if(!panels.length){ list.innerHTML='<div class="meta">'+esc(tr('（没有面板）'))+'</div>'; body.innerHTML=''; return }
 list.innerHTML='';
 panels.forEach((p,i)=>{
  const row=document.createElement('div');
  row.className='panelrow'+(i===0?' on':'');
  row.setAttribute('data-i18n-skip','1');     // 面板标题来自插件（数据）
  row.textContent=tr(p.title||p.id);
  row.title=p.plugin+' · '+p.view;
  row.onclick=()=>{ [...list.children].forEach(c=>c.classList.remove('on')); row.classList.add('on'); loadPanel(p) };
  list.appendChild(row);
 });
 await loadPanel(panels[0]);
}
async function loadPanel(p){
 const body=document.getElementById('panelBody');
 if(!body)return;
 body.innerHTML='<div class="meta">'+esc(tr('加载中…'))+'</div>';
 try{
  const data=await _panelData(p);
  _renderPanel(p,data);
 }catch(e){
  body.innerHTML='<div class="meta">'+esc(tr('读取失败: ')+(e.text||e))+'</div>';
 }
}
function closePanels(){closeMask('panelMask')}

async function openPlugins(){
 openMask('pluginsMask');
 const box=document.getElementById('pluginsBody');
 if(!box) return;
 box.innerHTML='<div class="meta">'+esc(tr('加载中…'))+'</div>';
 let list=[];
 try{ const d=await api('/v1/plugins'); list=d.plugins||[] }catch(e){
  box.innerHTML='<div class="meta">'+esc(tr('读取插件失败: ')+(e.text||e))+'</div>';return}
 if(!list.length){ box.innerHTML='<div class="meta">'+esc(tr('没有安装插件（把插件放进 plugins/ 目录）'))+'</div>';return}
 box.innerHTML='';
 list.forEach(p=>{
  const row=document.createElement('div');row.className='pluginrow';
  const head=document.createElement('div');head.className='phead';
  const name=document.createElement('b');name.textContent=p.name+'  ';
  const ver=document.createElement('span');ver.className='meta';ver.textContent='v'+(p.version||'')+' · ';
  const st=document.createElement('span');st.className='pstat '+(p.status==='loaded'?'on':(p.status==='error'?'bad':''));
  st.textContent=stateText(p.status==='loaded'?'已启用':(p.status==='disabled'?'已禁用':'未加载'));
  head.appendChild(name);head.appendChild(ver);head.appendChild(st);
  const desc=document.createElement('div');desc.className='meta';
  desc.setAttribute('data-i18n-skip','1');   // 插件作者给的描述：数据，不参与覆盖率审计
  desc.textContent=p.description||'';
  const meta=document.createElement('div');meta.className='meta';
  meta.setAttribute('data-i18n-skip','1');   // 贡献点是结构化数据
  meta.textContent=tr('权限: ')+(p.permissions||[]).join(', ')+' · '+tr('贡献: ')+JSON.stringify(p.contributes||{});
  const acts=document.createElement('div');acts.className='pacts';
  const btn=document.createElement('button');btn.type='button';btn.className='btn';
  btn.textContent=p.enabled?tr('禁用'):tr('启用');
  btn.onclick=async()=>{ btn.disabled=true;
    try{ await api('/v1/plugins/'+encodeURIComponent(p.name)+'/'+(p.enabled?'disable':'enable'),{method:'POST'});
      toast(tr('已')+(p.enabled?tr('禁用'):tr('启用'))+': '+p.name,'ok'); await openPlugins();mergePluginI18n(false); }
    catch(e){ btn.disabled=false; toast(tr('操作失败: ')+(e.text||e),'err') } };
  const rb=document.createElement('button');rb.type='button';rb.className='btn ghost';rb.textContent=tr('重载');
  rb.onclick=async()=>{ try{ await api('/v1/plugins/'+encodeURIComponent(p.name)+'/reload',{method:'POST'});
      toast(tr('已重载: ')+p.name,'ok'); await openPlugins(); }catch(e){ toast(tr('重载失败: ')+(e.text||e),'err') } };
  acts.appendChild(btn);acts.appendChild(rb);
  row.appendChild(head);row.appendChild(desc);row.appendChild(meta);row.appendChild(acts);
  if(p.error){ const er=document.createElement('div');er.className='perr';er.textContent=p.error; row.appendChild(er) }
  box.appendChild(row)});
}
function closePlugins(){closeMask('pluginsMask')}

/* ── 斜杠命令（/）：模式与工具的快捷入口 ─────────────────────────
   输入框以 "/" 开头时列出命令；↑↓ 选择、Tab/Enter 补全、Esc 关闭。
   带参数直接回车即执行（/research 问题…），不带参数则补全后等输入。 */
const SLASH_COMMANDS=[
 {id:'research',label:'research',aliases:['research','深研','dr'],
  desc:'深研：多角度搜索 + 抓正文，生成带来源的报告',
  run:(arg)=>{
   if(!arg){toast(tr('用法：/research 你的问题'),'err');return}
   const t=document.getElementById('task'); if(t)t.value='';
   deepResearchNow(arg)}},
 {id:'fanout',label:'fanout',aliases:['fanout','扇出','并行'],
  desc:'扇出：把多个互不依赖的任务各派一个子代理（用 | 分隔），不经模型判断',
  run:(arg)=>{
   const tasks=String(arg||'').split('|').map(x=>x.trim()).filter(Boolean);
   if(tasks.length<2){toast(tr('用法：/fanout 任务1 | 任务2 | 任务3（至少两个）'),'err');return}
   fanoutNow(tasks)}},
 {id:'audit',label:'audit',aliases:['audit','审计','对抗审计','review'],
  desc:'对抗式审计：多名审计员 + 裁判核验，循环到只剩小问题',
  run:(arg)=>{auditNow(arg)}},
 {id:'agent',label:'agent',aliases:['agent','执行'],
  desc:'切到 Agent 执行模式',run:()=>{setTaskMode('agent');toast(tr('已切到 Agent 执行'),'ok')}},
 {id:'chat',label:'chat',aliases:['chat','对话'],
  desc:'切到普通对话模式',run:()=>{setTaskMode('chat');toast(tr('已切到普通对话'),'ok')}},
 {id:'goal',label:'goal',aliases:['goal','目标'],
  desc:'目标模式：建立目标后多轮推进，未完成不会停',
  run:(arg)=>{
   if(!arg){toast(tr('用法：/goal 你的目标'),'err');return}
   const t=document.getElementById('task');
   if(t)t.value=arg+'\n\n'+'[目标模式] 请把上面这件事当作一个长期**目标**：'
    +'先调用 create_goal 记录下来（max_rounds 取 8-12），必要时用 todo_write 拆出 3-8 个可执行子步骤；'
    +'然后分多轮持续推进，每轮做完一部分就继续做下一部分，目标未达成时不要返回 final；'
    +'只有目标真正达成（或确实被外部条件阻塞）时，先调用 update_goal（complete / blocked）再返回 final。';
   runTask()}},
 {id:'plan',label:'plan',aliases:['plan','计划'],
  desc:'计划模式（只读）：探索后产出方案，exit_plan_mode 提交',
  run:(arg)=>{
   if(!arg){toast(tr('用法：/plan 你的任务'),'err');return}
   const t=document.getElementById('task');
   if(t)t.value=arg;
   planModeOnce=true;        // 由后端强制只读，不只靠提示词
   runTask()}},
 {id:'self-upgrade',label:'self-upgrade',aliases:['self-upgrade','自升级','改服务'],
  desc:'自升级任务模板：一键填进输入框（记得先在 设置→危险区 打开「允许自升级」）',
  run:(arg)=>{
   const t=document.getElementById('task');
   if(t){t.value=(arg?arg+'\n\n':'')+'[自升级任务]\n文件：gantry/<文件名>.py（安装目录相对路径，不要写绝对路径）\n现状：<你看到的现象，例如「重启后旧会话恢复失败」>\n改成：<具体要改什么；能贴函数名/字段名/代码片段最好>\n范围：只改上面的文件/逻辑，别动无关文件\n如果不记得文件名：先调 self_search_text 搜出现象里的关键词（服务代码在安装目录，工作区里没有），把定位到的文件/行号报给我确认后再改；不要猜。\n验收：self_validate + self_unittest 都通过\n交付：改了哪几行、验证结果、是否要重启、以及回滚命令';
    setTaskMode('agent');
    toast(tr('已载入自升级模板：把 <…> 部分填掉即可'),'ok');
    t.focus()}}},
 {id:'help',label:'help',aliases:['help','?','帮助'],
  desc:'列出全部可用命令',run:()=>{const t=document.getElementById('task');
   if(t){t.value='/';slashUpdate(true);t.focus()}}},
];

export { openPanels, refreshPanels, loadPanel, closePanels, openPlugins, closePlugins, SLASH_COMMANDS };
