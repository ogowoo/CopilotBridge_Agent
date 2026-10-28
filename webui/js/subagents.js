// subagents.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
import { PHASE_LABEL } from './steps.js';
async function refreshSubagents(force){
 const now=Date.now();
 if(subagentBusy) return;
 // 切了会话必须立刻刷新（否则会短暂显示**上一个会话**的子代理，看起来像"串台"）
 if(current!==lastSubagentParent){force=true;lastSubagentParent=current}
 if(!force && now-lastSubagentFetch<3000) return;
 // **必须按父会话过滤**：不带 parent 会返回全部子代理，面板里就会混进别的任务的
 // 子代理（实测：用户真实会话里冒出 19 个与它无关的子代理）。
 if(!current){renderSubagents([]);return}
 subagentBusy=true;lastSubagentFetch=now;
 try{
  // 当前会话本身可能是子代理：此时按它的**父会话**查询，面板才不会一片空白
  const _scope=parentOfCurrent||current;
  const d=await api('/v1/agent/subagents?parent='+encodeURIComponent(_scope));
  renderSubagents(d.subagents||[]);
 }catch(e){}
 finally{subagentBusy=false}
}


function renderSubagents(list){
 if(!window.current){list=[]}
 const bar=document.getElementById('subagentBar');
 const box=document.getElementById('subagentList');
 const cnt=document.getElementById('subagentCount');
 if(!bar||!box) return;
 // 正在看子代理时即使它自己没有下级，也要保留面板 + 返回入口（否则用户被困住）
 if(!list.length && !parentOfCurrent){bar.style.display='none';box.innerHTML='';if(cnt)cnt.textContent='';try{syncTeamCard()}catch(e){};return}
 // 用 'block' 而非 ''：'' 会回退到样式表里的 .subagents{display:none}（实测踩到）
 bar.style.display='block';
 const running=list.filter(s=>s.state==='running').length;
 cnt.textContent=String(list.length)+(running?tr(' · {r} 运行中',{r:running}):'');
 box.innerHTML='';
 // 返回父会话（只有当前会话是子代理时才出现）
 if(parentOfCurrent){
  const back=document.createElement('div');
  back.className='subagentrow subagentback';
  back.dataset.act='openSubagent';back.dataset.arg=parentOfCurrent;
  back.title=tr('返回父会话');
  const bd=document.createElement('span');bd.textContent='←';
  const bt=document.createElement('span');bt.className='sa-desc';
  bt.textContent=tr('返回父会话')+' · '+parentOfCurrent;
  back.appendChild(bd);back.appendChild(bt);box.appendChild(back)}
 list.slice(0,8).forEach(s=>{
  const row=document.createElement('div');
  const _sid=s.agent_id||s.id||'';
  row.className='subagentrow'+(_sid===window.current?' sel':'');
  row.dataset.act='openSubagent';row.dataset.arg=_sid;
  const dot=document.createElement('span');
  dot.className=s.state==='running'?'dot-on':(s.state==='failed'?'dot-bad':'dot-off');
  const desc=document.createElement('span');desc.className='sa-desc';
  // 子代理描述是模型给的任务文本（数据），不参与 i18n 覆盖率判定
  desc.setAttribute('data-i18n-skip','1');
  desc.textContent=s.description||s.task||_sid;
  const meta=document.createElement('span');meta.className='sa-meta';
  // 阶段只在**运行中**有意义：已结束的子代理 phase 会停在最后一次的值
  //（实测显示成"已完成 · 41 步 · 等待中"，自相矛盾）。
  const _live=String(s.state||'')==='running'||String(s.state||'')==='waiting';
  meta.textContent=stateText(s.state)+' · '+s.steps+' '+tr('步')
    +((_live&&s.phase)?' · '+tr(PHASE_LABEL[s.phase]||s.phase):'');
  row.appendChild(dot);row.appendChild(desc);row.appendChild(meta);
  box.appendChild(row)});
 try{syncTeamCard()}catch(e){}
   // 非空路径以前从不调用 syncTeamCard：#teamCard 在“空列表”那次被 hidden 之后
   // 就再也没被显示回来，于是点进子代理后整块面板消失（用户实测）。
}

/* ── 插件管理（对应 gantry/plugins.py 的贡献点与启停）──────────────────── */
/* ── 插件面板（声明式）：插件给数据，前端按 view 渲染 ──────────────
   view: kv / list / table / json / markdown。插件不注入 HTML，避免 XSS。 */

export { refreshSubagents, renderSubagents };
