// version.js — 页签标题与版本号（原 app.js 的最后一块，迁移完成后 app.js 退役）。
//
// 为什么单独一块：标题里的版本以前是**硬编码**的预览号，一升版本就对不上（用户实测）。
// 现在统一由 GET / 拿真实 version 拼：`<品牌> - v<版本>`，并显示在右上角 #appVersion。

/** 把版本号写进页签标题：`Gantry - v11.0.0-preview.21`（name 为空时用 Gantry）。 */
function applyTitleVersion(version, name){
 const base=String(name||'Gantry').trim()||'Gantry';
 const v=String(version||'').trim();
 const t=v?(base+' - v'+v):base;
 if(document.title!==t)document.title=t;
}

/** 从 `GET /` 读版本号：填 #appVersion 芯片，并同步页签标题。失败就不显示（不打扰）。 */
async function loadVersion(){
 const el=document.getElementById('appVersion');
 if(!el)return;
 try{
  const r=await fetch('/',{cache:'no-store'});
  if(!r.ok)return;
  const d=await r.json();
  const v=String(d.version||'').trim();
  if(!v)return;
  el.textContent='v'+v;
  el.title=tr('版本')+'：'+v+'（'+(d.name||'Gantry')+'）';
  el.hidden=false;
  applyTitleVersion(v, d.name);
 }catch(e){/* 拿不到就不显示 */}
}

export { applyTitleVersion, loadVersion };
