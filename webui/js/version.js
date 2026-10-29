// version.js — 页签标题与版本号（原 app.js 的最后一块，迁移完成后 app.js 退役）。
//
// 为什么单独一块：标题里的版本以前是**硬编码**的预览号，一升版本就对不上（用户实测）。
// 版本来源：`GET /health`（它本来就每 8 秒轮询一次，顺手带上 name/version）。
// 为什么不是 `GET /`：首页已改成 302 → /ui/（访问根地址直接进控制台），
// 再拿根地址当 JSON 用会解析失败、版本号静默消失（本次实测踩到）。
// 仍然保留一次 `/` 兜底：老部署的根路径还是 JSON。

/** 把版本号写进页签标题：`Gantry - v11.0.0-preview.21`（name 为空时用 Gantry）。 */
function applyTitleVersion(version, name){
 const base=String(name||'Gantry').trim()||'Gantry';
 const v=String(version||'').trim();
 const t=v?(base+' - v'+v):base;
 if(document.title!==t)document.title=t;
}

/** 从 `GET /` 读版本号：填 #appVersion 芯片，并同步页签标题。失败就不显示（不打扰）。 */
async function fetchMeta(){
 // 依次尝试：/health（新）→ /（老部署的根 JSON）。任一成功即返回对象。
 for(const url of ['/health','/']){
  try{
   const r=await fetch(url,{cache:'no-store'});
   if(!r.ok)continue;
   const ct=String(r.headers.get('content-type')||'');
   if(ct && !ct.includes('json'))continue;      // 302 到 /ui/ 会拿到 HTML，直接跳过
   const d=await r.json();
   if(d&&typeof d==='object')return d;
  }catch(e){/* 下一个 */}
 }
 return null;
}

async function loadVersion(){
 const el=document.getElementById('appVersion');
 if(!el)return;
 try{
  const d=await fetchMeta();
  if(!d)return;
  const v=String(d.version||'').trim();
  if(!v)return;
  el.textContent='v'+v;
  el.title=tr('版本')+'：'+v+'（'+(d.name||'Gantry')+'）';
  el.hidden=false;
  applyTitleVersion(v, d.name);
 }catch(e){/* 拿不到就不显示 */}
}

export { applyTitleVersion, loadVersion };
