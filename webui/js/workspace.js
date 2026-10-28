// workspace.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
function wsCollapsedList(){try{return JSON.parse(localStorage.getItem('gantry-ws-collapsed')||'[]')||[]}catch(e){return []}}
function wsIsCollapsed(k){return wsCollapsedList().indexOf(k)>=0}
function wsToggleCollapse(k){
 let arr=wsCollapsedList();
 const i=arr.indexOf(k);
 if(i>=0)arr.splice(i,1);else arr.push(k);
 try{localStorage.setItem('gantry-ws-collapsed',JSON.stringify(arr))}catch(e){}
 sessSig='';   // 强制重绘
 drawSessions();
}
/* 新建工作区：先切到新会话视图，再打开目录选择（native 优先，可新建目录） */
async function newWorkspace(){
 openTask();
 await pickWorkspace();
}
/* 默认走**应用内浏览**：立刻可用的界面反馈。
   系统对话框（native）会阻塞到用户点选，非交互桌面下等于永不返回（实测：
   点按钮没反应），所以改成弹窗里的可选按钮。 */
async function pickWorkspace(){
 const cur=(document.getElementById('workspace')||{}).value||'';
 await openWsBrowser(cur);
}
async function openWsBrowser(start){
 openMask('wsMask');
 await wsNavigate(start||'');
}
async function wsNavigate(path){
 try{
  const q=path?('?path='+encodeURIComponent(path)):'';
  const d=await api('/v1/agent/browse'+q);
  wsCurrent=d.path||'';
  const p=document.getElementById('wsPath'); if(p)p.textContent=wsCurrent||'—';
  const box=document.getElementById('wsList'); if(!box)return;
  box.innerHTML='';
  if(!(d.entries||[]).length){ box.innerHTML='<div class="meta">'+esc(tr('（没有子目录）'))+'</div>'; return }
  (d.entries||[]).forEach(x=>{
   const row=document.createElement('div');row.className='wsrow';
   row.textContent='📁 '+x.name;
   row.onclick=()=>wsNavigate(x.path);
   box.appendChild(row)});
 }catch(e){ toast(tr('读取目录失败: ')+(e.text||e),'err') }
}
async function wsUp(){
 if(!wsCurrent)return;
 const parent=wsCurrent.replace(/[\\/][^\\/]+$/, '');
 if(parent&&parent!==wsCurrent) await wsNavigate(parent);
}
async function wsDefault(){ await wsNavigate('') }   // 不传 path = 默认工作区
function wsChoose(){
 const w=document.getElementById('workspace');
 if(w&&wsCurrent){ w.value=wsCurrent; toast(tr('已选择工作目录: ')+wsCurrent,'ok') }
 closeMask('wsMask');
}
async function wsMkdir(){
 const name=await dlgPrompt(tr('新目录名（建在当前目录下）'), '', tr('创建'), tr('新建目录'));
 if(!name||!String(name).trim()) return;
 try{
  const d=await api('/v1/agent/browse/mkdir',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({parent:wsCurrent,name:String(name).trim()})});
  toast(tr('已创建目录: ')+(d.path||''),'ok');
  await wsNavigate(d.path||wsCurrent);
 }catch(e){ toast(tr('创建目录失败: ')+(e.text||e),'err') }
}
/* 在指定工作区直接新建会话（DSH 式：组头 ＋ 按钮） */
function newSessionInWorkspace(ws){
 openTask();
 const w=document.getElementById('workspace');
 if(w)w.value=ws||'';
 const t=document.getElementById('task');
 if(t){t.focus()}
}
function closeWs(){closeMask('wsMask')}

export { wsCollapsedList, wsIsCollapsed, wsToggleCollapse, newWorkspace, pickWorkspace, openWsBrowser, wsNavigate, wsUp, wsDefault, wsChoose, wsMkdir, newSessionInWorkspace, closeWs };
