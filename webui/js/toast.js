// toast.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
function toast(msg,type=''){let box=document.getElementById('toasts'),e=document.createElement('div');
 e.className='toast '+type;e.textContent=msg;box.appendChild(e);
 setTimeout(()=>{e.style.opacity='0';e.style.transition='opacity .3s';setTimeout(()=>e.remove(),320)},type==='err'?5000:2600)}

export { toast };
