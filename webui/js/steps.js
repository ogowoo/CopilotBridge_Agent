// steps.js — 
// 从原 webui/app.js 拆出的 ES 模块（该单体已退役，见 docs/ESM-MIGRATION.md）。
let stepFilterKey='';
let stepsPending=false, lastStepsRaw=[];   // 时间线在后台页签时先不建 DOM（长会话省 ~0.9s）
const BAD_STATUS=['error','invalid_action','failed','gate_blocked','noop_guard',
                  'send_failed','service_unavailable','denied'];
function stepMatch(s){
 if(stepFilter.tool&&String(s.tool||'')!==stepFilter.tool)return false;
 if(stepFilter.status==='ok'&&s.status!=='ok')return false;
 if(stepFilter.status==='bad'&&!BAD_STATUS.includes(String(s.status||'')))return false;
 if(stepFilter.status==='event'&&(s.status==='ok'||BAD_STATUS.includes(String(s.status||''))))return false;
 return true}
function fillStepTools(){
 const sel=document.getElementById('stepTool');if(!sel)return;
 const tools=[...new Set((lastSteps||[]).map(s=>String(s.tool||'').trim()).filter(Boolean))].sort();
 const cur=sel.value;
 sel.innerHTML='<option value="">'+esc(tr('全部工具'))+'</option>'+tools.map(t=>`<option value="${esc(t)}">${esc(t)}</option>`).join('');
 if(tools.includes(cur))sel.value=cur}
function stepFilterInput(el){
 const which=el.dataset.arg;
 stepFilter[which==='status'?'status':'tool']=el.value||'';
 stepFilterKey=JSON.stringify(stepFilter);
 renderSteps(lastSteps,true)}
/* 连续的"服务限流/超时"（transient）合成一行：
   pyshot 任务里这类记录有 105 条（占全部事件 21%），逐条铺开会让时间线看着像全在失败，
   而它们既不是模型的错、也不消耗步数预算。 */
function collapseTransient(steps){
 const out=[];
 (steps||[]).forEach(s=>{
  if(s&&s.transient){
   const last=out[out.length-1];
   if(last&&last._mergedTransient){last.times=(last.times||1)+1;
     last.reason=tr('服务限流/超时，自动重试 {n} 次（不计步）',{n:last.times});return}
   out.push({status:'info',tool:'',transient:true,_mergedTransient:true,times:1,
             _time:s._time,reason:tr('服务限流/超时，自动重试 {n} 次（不计步）',{n:1})});
   return;
  }
  out.push(s);
 });
 return out;
}
function renderSteps(steps,force){
 lastSteps=collapseTransient(steps||[]);
 if(document.hidden&&!force)return;       // 后台标签页不建步骤 DOM
 const box=document.getElementById('steps');
 const filtered=!!(stepFilter.tool||stepFilter.status);
 const shown=lastSteps.filter(stepMatch);
 const key=JSON.stringify(stepFilter);
 // 只在"筛选变了 / 被显式要求 / 步骤变少"时整表重建；其余情况继续增量 prepend，
   // 这样默认视图不会失去展开状态，筛选态也不会每秒重建整棵 DOM。
 const rebuild=force||key!==stepFilterKey||shown.length<renderedSteps;
 stepFilterKey=key;
 if(rebuild){box.innerHTML='';renderedSteps=0}
 else if(!shown.length){box.innerHTML=''}
 document.getElementById('stepCount').textContent=
  filtered?`${shown.length}/${lastSteps.length}`+tr(' 条（筛选）')
          :(lastSteps.length?lastSteps.length+tr(' 条'):'');
 fillStepTools();
 // 已被后续成功步骤"解决"的审批拦截：同一个 tool 在本步之后有 ok 步骤，
 // 说明用户后来授权并执行成功了——这条不该继续以"失败"的样子留在时间线里。
 // 注意区分两种解决方式：真的执行了（ok）才敢写"已执行"，只有授权记录时只说"已授权"。
 const resolvedApprovals=new Map();   // index -> 'ok' | 'granted'
 shown.forEach((s,i)=>{
  if(s.status!=='approval_required')return;
  const tool=String(s.tool||'');
  let by=null;
  for(let j=i+1;j<shown.length;j++){
   const t=shown[j];
   if(String(t.tool||'')!==tool)continue;
   if(t.status==='ok'||t.status==='approval_granted'){
    if(t.status==='ok'){by='ok';break}
    by=by||'granted';
   }}
  if(by)resolvedApprovals.set(i,by)});
 shown.slice(renderedSteps).forEach((s,_k)=>{
  const idx=renderedSteps+_k;
  const resolvedBy=resolvedApprovals.get(idx);
  const granted=(s.status==='approval_granted')||!!resolvedBy;
  let cls=s.status==='ok'?'ok':(s.status==='error'||s.status==='invalid_action'?'error':'info');
  if(granted)cls='ok';                     // 已授权：按成功样式呈现，不再误导为失败
  let e=document.createElement('div');e.className='step '+cls;
  let tm=s._time?new Date(s._time*1000).toLocaleTimeString('zh-CN',{hour12:false}):'';
  let detail='';
  if(s.result!==undefined&&s.result!==null){
   detail=`<details><summary>${esc(tr('查看结果'))}</summary>`+
    `<div class="stepviewbar">`+
    `<button type="button" class="btn seg on" data-act="stepView" data-arg="render">${esc(tr('渲染'))}</button>`+
    `<button type="button" class="btn seg" data-act="stepView" data-arg="json">JSON</button></div>`+
    `<div class="stepview" data-mode="render">${stepResultRender(s)}</div>`+
    `<div class="stepview" data-mode="json" style="display:none"><div class="jsonview">${jv(s.result)}</div></div>`+
    `</details>`}
  let chipTxt=(s.status==='approval_granted')?tr('已授权')
    :(resolvedBy==='ok'?tr('已授权，已执行'):(resolvedBy?tr('已授权'):(s.status||'')));
  // approval_granted 这步的 reason 是后端中文原文：这里按语言重写，并保留授权范围信息
  let whyTxt;
  if(s.status==='approval_granted'){
   const _scope=(s.result||{}).scope;
   whyTxt=tr('授权已下达')+(String(_scope)==='session'?tr('（本会话内不再逐次询问）'):tr('（用后即焚）'));
  }else if(resolvedBy){
   whyTxt=(resolvedBy==='ok')?tr('授权已下达，该操作已继续执行'):tr('授权已下达');
  }else{
   whyTxt=s.reason||s.error||'';
  }
  e.innerHTML=`<div class="head"><b>${tr('步骤')} ${esc(s.step??'-')} · <span data-i18n-skip="1" title="${esc(String(s.tool||s.status||'event'))}">${esc(s.tool?toolLabel(s.tool):(s.status||'event'))}</span></b><span class="schip">${esc(chipTxt)}</span>${tm?`<span class="schip" style="border-style:dashed">${tm}</span>`:''}</div><div class="why">${esc(whyTxt)}</div>${detail}`;
  box.prepend(e)});
 renderedSteps=shown.length;
 if(!shown.length&&!box.innerHTML.trim()){
  // 区分两种情况：真的没有步骤（普通对话），还是有步骤但被筛选掉了
  const hasMsgs=(window.lastMessages||[]).length>0;
  const emptyMsg = lastSteps.length===0 && hasMsgs
    ? tr('普通对话没有 Agent 步骤，见「继续对话」页')
    : (filtered ? tr('没有符合筛选条件的步骤') : tr('该会话还没有 Agent 步骤'));
  box.innerHTML='<div class="sess-empty">'+esc(emptyMsg)+'</div>'}
 applyCodePrefs(box);try{translateAttrs(box)}catch(e){}}

/* 时间线在「步骤时间线」页签里；默认停在「实时流」时它整块是隐藏的。
   长会话（1500 步）为它建 DOM 要 ~0.9s，纯属浪费 —— 先只记住数据，切过去再建。 */
function renderStepsMaybe(steps){
 lastStepsRaw=steps||[];
 if(liveTabName!=='timeline'){
  stepsPending=true;
  lastSteps=collapseTransient(lastStepsRaw);
  const cnt=document.getElementById('stepCount');
  if(cnt)cnt.textContent=lastSteps.length?lastSteps.length+tr(' 条'):'';
  return;
 }
 stepsPending=false;
 renderSteps(lastStepsRaw);
}
function flushStepsIfPending(){
 if(!stepsPending)return;
 stepsPending=false;
 renderSteps(lastStepsRaw);
}
/* 工具结果：渲染 / JSON 双视图切换 */
function stepView(btn){
 const bar=btn.closest('.stepviewbar');if(!bar)return;
 const wrap=bar.parentElement;
 const mode=btn.dataset.arg==='json'?'json':'render';
 [...bar.querySelectorAll('button')].forEach(b=>b.classList.toggle('on',b===btn));
 [...wrap.querySelectorAll(':scope > .stepview')].forEach(v=>{
  v.style.display=(v.dataset.mode===mode)?'':'none'});
 applyCodePrefs(wrap)}

/* ── 实时流：把消息与步骤按时间穿插成一条（DSH 的轨迹） ──
   两个数据源都有 epoch 时间：消息用 created，步骤用 _time，直接归并排序即可。
   消息沿用 msgEl()（气泡、附件、编辑、代码块全都照旧），步骤是新做的紧凑行。 */
let streamSig='';
/* 工具显示名（三语）。键是工具 id（与后端一致，永不翻译），值是给人看的名字。
   没收录的工具回落为原始 id —— 宁可见 id，也不要显示空白。 */
/* 运行时 phase 是后端英文枚举（thinking/waiting/executing…），界面按语言显示 */
/* 统一图标集：全部内联 SVG，14×14、stroke=currentColor、线宽 1.8。
   四条横条与步骤行都用它，避免 emoji（☑ ● ✓ 🔐）在不同字体下大小/基线不一。 */
const SVG_ICONS={
 todo:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 10h8M8 14h5"/></svg>',
 branch:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="7" cy="7" r="2.4"/><circle cx="17" cy="17" r="2.4"/><path d="M9.2 8.4 14.8 15.6"/></svg>',
 tool:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4.5a4 4 0 0 0 5 5L11 18a3 3 0 0 1-4.2-4.2z"/><path d="M4 20l2-2"/></svg>',
 check:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5 10 17 19 7"/></svg>',
 cross:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7l10 10M17 7 7 17"/></svg>',
 dot:'<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><circle cx="12" cy="12" r="4"/></svg>',
 ring:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="7"/></svg>',
 lock:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3"/></svg>',
 pulse:'<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12h4l2.5-6 4 12L16 12h5"/></svg>',
};
function svgIcon(name){return SVG_ICONS[name]||'';}

const PHASE_LABEL={
 thinking:'思考中', waiting:'等待中', queued:'排队中', executing:'执行中',
 followup_queued:'跟进排队', followup_sending:'发送跟进', followup_completed:'跟进完成',
 manual_sending:'发送中', manual_completed:'发送完成', completed:'已完成',
 failed:'失败', cancelled:'已取消', paused:'已暂停', suspended:'限流挂起',
};
const TOOL_LABELS={
 list_directory:{zh:'列出目录',tw:'列出目錄',en:'List dir'},
 find_files:{zh:'查找文件',tw:'尋找檔案',en:'Find files'},
 grep:{zh:'搜索内容',tw:'搜尋內容',en:'Grep'},
 read_text:{zh:'读取文本',tw:'讀取文字',en:'Read text'},
 write_text:{zh:'写文件',tw:'寫檔案',en:'Write'},
 append_text:{zh:'追加文本',tw:'追加文字',en:'Append'},
 write_text_b64:{zh:'写文件(b64)',tw:'寫檔案(b64)',en:'Write b64'},
 append_text_b64:{zh:'追加(b64)',tw:'追加(b64)',en:'Append b64'},
 replace_text:{zh:'替换文本',tw:'取代文字',en:'Replace'},
 replace_lines:{zh:'按行替换',tw:'按行取代',en:'Replace lines'},
 apply_patch:{zh:'批量修改',tw:'批次修改',en:'Apply patch'},
 convert_indentation:{zh:'转换缩进',tw:'轉換縮排',en:'Re-indent'},
 indent_lines:{zh:'调整缩进',tw:'調整縮排',en:'Indent lines'},
 make_directory:{zh:'创建目录',tw:'建立目錄',en:'Mkdir'},
 copy_path:{zh:'复制',tw:'複製',en:'Copy'},
 move_path:{zh:'移动',tw:'移動',en:'Move'},
 delete_path:{zh:'删除',tw:'刪除',en:'Delete'},
 validate_python:{zh:'语法检查',tw:'語法檢查',en:'Validate'},
 run_unittest:{zh:'跑测试',tw:'跑測試',en:'Unittest'},
 run_python:{zh:'跑脚本',tw:'跑腳本',en:'Run python'},
 pwsh:{zh:'执行命令',tw:'執行指令',en:'Shell'},
 read_image:{zh:'看图',tw:'看圖',en:'Read image'},
 subagent:{zh:'派子代理',tw:'派子代理',en:'Subagent'},
 subagent_many:{zh:'批量子代理',tw:'批次子代理',en:'Subagents'},
 subagent_send:{zh:'子代理追问',tw:'子代理追問',en:'Ask subagent'},
 subagent_list:{zh:'子代理列表',tw:'子代理清單',en:'List subagents'},
 subagent_fork:{zh:'分叉子代理',tw:'分叉子代理',en:'Fork subagent'},
 todo_write:{zh:'更新待办',tw:'更新待辦',en:'Update todos'},
 memory_write:{zh:'记一条记忆',tw:'記一條記憶',en:'Remember'},
 memory_search:{zh:'回忆',tw:'回憶',en:'Recall'},
 memory_forget:{zh:'遗忘',tw:'遺忘',en:'Forget'},
 skill_list:{zh:'技能列表',tw:'技能清單',en:'List skills'},
 skill_read:{zh:'读技能',tw:'讀技能',en:'Read skill'},
 skill_create:{zh:'存为技能',tw:'存為技能',en:'Save skill'},
 tool_list:{zh:'工具清单',tw:'工具清單',en:'List tools'},
 ask_user_question:{zh:'向你提问',tw:'向你提問',en:'Ask you'},
 create_goal:{zh:'建目标',tw:'建目標',en:'Create goal'},
 update_goal:{zh:'更新目标',tw:'更新目標',en:'Update goal'},
 get_goal:{zh:'读目标',tw:'讀目標',en:'Get goal'},
 job_list:{zh:'后台任务',tw:'背景任務',en:'List jobs'},
 job_output:{zh:'读任务输出',tw:'讀任務輸出',en:'Job output'},
 job_kill:{zh:'停后台任务',tw:'停背景任務',en:'Kill job'},
 present:{zh:'交付文件',tw:'交付檔案',en:'Present'},
 web_fetch:{zh:'抓网页',tw:'抓網頁',en:'Fetch URL'},
 web_search:{zh:'联网搜索',tw:'聯網搜尋',en:'Web search'},
 copilot_file:{zh:'让 Copilot 生成文件',tw:'讓 Copilot 生成檔案',en:'Copilot file'},
 copilot_attach:{zh:'交给 Copilot 分析',tw:'交給 Copilot 分析',en:'Copilot attach'},
 exit_plan_mode:{zh:'提交方案',tw:'提交方案',en:'Exit plan'},
 ralph:{zh:'目标迭代',tw:'目標迭代',en:'Ralph'},
 workflow:{zh:'编排子代理',tw:'編排子代理',en:'Workflow'},
};
function toolLabel(id){
 const key=String(id||'').trim();
 if(!key)return '';
 const m=TOOL_LABELS[key];
 if(!m)return key;                       // 没收录：显示原始 id
 const lang=(typeof currentLang==='function')?currentLang():'zh';
 return m[lang]||m.zh||key;
}

function stepRowEl(s){
 const st=String(s.status||'');
 const errText=String(s.error||'').trim();
 // 没有工具名的步骤是**系统级记录**（发送失败、回合状态），不是"汇总"：
 // 以前一律显示"汇总"，把这些报错藏了起来（实测 19 条 send_failed 全被显示成"汇总"）。
 const failed=!!errText||['error','send_failed','invalid_action','approval_required',
                          'repeat_guard'].includes(st);
 const cls=failed?'error':(st==='ok'?'ok':'info');
 const el=document.createElement('div');
 el.className='streamstep '+cls;
 const icon=st==='ok'&&!failed?'✓':(failed?(st==='approval_required'?'🔐':'✗'):'•');
 const time=s._time?new Date(Number(s._time)*1000).toLocaleTimeString():'';
 const tool=String(s.tool||'').trim()||(failed?tr('系统'):tr('汇总'));
 const reason=errText||String(s.reason||'').trim();
 const _icSvg=(st==='ok'&&!failed)?svgIcon('check')
   :(failed?(st==='approval_required'?svgIcon('lock'):svgIcon('cross')):svgIcon('dot'));
 el.innerHTML='<span class="ss-ic">'+_icSvg+'</span>'
   +'<span class="ss-tool" title="'+esc(tool)+'">'+esc(toolLabel(tool))+'</span>'
   +'<span class="ss-reason" data-i18n-skip="1" title="'+esc(reason)+'">'+esc(reason)+'</span>'
   +'<span class="ss-clock">'+esc(time)+'</span>';
 // 详情**照抄步骤时间线的渲染**（用户："可以像步骤时间线那样"）：
 // 渲染视图（kvlist / 表格 / jsonview）+ 渲染↔JSON 切换，比裸 JSON 好读得多。
 const hasResult = s.result !== undefined && s.result !== null;
 const hasArgs = s.arguments !== undefined && s.arguments !== null && s.arguments !== '';
 if(hasResult || hasArgs || s.error){
  const det=document.createElement('details');
  det.className='ss-det';
  // 不再显示「查看结果」链接：点说明文字本身展开/收起（summary 只作为无障碍标签）
  const sum=document.createElement('summary');
  sum.className='sr-only';
  sum.textContent=tr('查看结果');
  det.appendChild(sum);
  const _rs=el.querySelector('.ss-reason');
  if(_rs){
    _rs.classList.add('clickable');
    _rs.addEventListener('click',ev=>{
      ev.stopPropagation();
      det.open=!det.open;
      _rs.classList.toggle('open',det.open);
    });
  }
  const bar=document.createElement('div');
  bar.className='stepviewbar';
  bar.innerHTML='<button type="button" class="btn seg on" data-act="stepView" data-arg="render">'
    +esc(tr('渲染'))+'</button><button type="button" class="btn seg" data-act="stepView" '
    +'data-arg="json">JSON</button>';
  det.appendChild(bar);
  const rv=document.createElement('div');
  rv.className='stepview'; rv.dataset.mode='render';
  rv.innerHTML=stepResultRender(s);
  det.appendChild(rv);
  const jvv=document.createElement('div');
  jvv.className='stepview'; jvv.dataset.mode='json'; jvv.style.display='none';
  const payload = hasResult ? s.result
    : (hasArgs ? s.arguments : {error: String(s.error||''), status: String(s.status||'')});
  const jbox=document.createElement('div');
  jbox.className='jsonview';
  jbox.innerHTML=jv(payload);
  jvv.appendChild(jbox);
  det.appendChild(jvv);
  el.appendChild(det);
 }
 return el;
}
function renderStream(messages,steps,opts){
 opts=opts||{};
 if(document.hidden&&!opts.force){        // 后台标签页：不重建 DOM（可见后补渲染）
  try{renderToolLog(steps||[],{})}catch(e){}
  return;
 }
 const box=document.getElementById('chatlog');
 if(!box)return;
 window.lastMessages=messages||[];
 // 对话流里**只放消息**：工具调用记录搬到独立的「调用记录」区（状态条上方），
 // 长任务里几十条工具行不再把对话顶出去；顺序按用户要求：
 // todo → 子代理 → 调用记录 → 状态信息。
 const items=[];
 (messages||[]).forEach(m=>{
   const prev=items.length?items[items.length-1]:null;
   if(prev&&prev.v.role===m.role
      && String(prev.v.content||'').trim()===String(m.content||'').trim())return;
   items.push({t:Number(m.created)||0,v:m});
 });
 items.sort((x,y)=>x.t-y.t);
 renderToolLog(steps||[],opts);

 const sig=items.length+'|'+(items.length?items[items.length-1].t:'');
 if(sig===streamSig&&!opts.force)return;      // 没变化不重建
 streamSig=sig;
 const atBottom=box.scrollHeight-box.scrollTop-box.clientHeight<60;
 box.innerHTML='';
 if(!items.length){
  box.innerHTML='<div class="meta">'+esc(tr('当前没有对话记录。下面发送的消息会发给该 Session 正在使用的模型。'))+'</div>';
  try{renderLiveBubble()}catch(e){}
  return;
 }
 for(const it of items){
  box.appendChild(msgEl(it.v.role,it.v.content,it.v.created,
    {attachments:it.v.attachments||[],id:it.v.id}));
 }
 // 刚发出的是用户消息 → 一定贴底（否则用户看不到自己发的那条）
 const _lastIsUser=!!(items.length&&items[items.length-1].v
   &&items[items.length-1].v.role==='user');
 applyMsgFold(box);
 try{applyCodePrefs(box)}catch(e){}
 try{translateAttrs(box)}catch(e){}
 if(findQuery)applyFind();
 try{renderLiveBubble()}catch(e){}
 if(atBottom||opts.scrollEnd||_lastIsUser)pinChatBottom(box);
}
/* 贴底：长会话用了 content-visibility，离屏消息的高度是**估算值**——
   一次性把 scrollTop 设成 scrollHeight 会停在估算位置，真实高度展开后又跑偏
   （实测打开长会话停在最顶部）。所以在 1.5s 内反复贴底，让高度收敛；
   用户一旦滚动（wheel/触摸）就立刻停手，不跟用户抢滚动。 */
let chatPinTimer=null, chatPinUntil=0;
/* 最近的可滚动祖先（#chatlog 不再自己滚时，要滚 #chat） */
function nearestScroller(el){
 for(let n=el;n&&n!==document.body;n=n.parentElement){
  const cs=getComputedStyle(n);
  if((cs.overflowY==='auto'||cs.overflowY==='scroll')&&n.scrollHeight-n.clientHeight>2)return n;
 }
 return el;
}

function pinChatBottom(box){
 if(!box)return;
 chatPinUntil=Date.now()+1500;
 if(chatPinTimer)return;
 const tick=()=>{
  chatPinTimer=null;
  if(Date.now()>chatPinUntil)return;
  try{const sc=nearestScroller(box);sc.scrollTop=sc.scrollHeight}catch(e){}
  try{chatPinTimer=setTimeout(tick,60)}catch(e){}
 };
 tick();
}
document.addEventListener('wheel',ev=>{
 const t=ev.target;
 if(t&&t.closest&&t.closest('#chatlog'))chatPinUntil=0;
},{passive:true,capture:true});
document.addEventListener('touchstart',ev=>{
 const t=ev.target;
 if(t&&t.closest&&t.closest('#chatlog'))chatPinUntil=0;
},{passive:true,capture:true});
function scrollChatToEnd(){
 const box=document.getElementById('chatlog');
 if(box)pinChatBottom(box);
}

/* 调用记录：默认只显示**最新一条**（与状态条同款一行），点它浮动展开全部。
   不占布局（浮层定位于该行上方），和状态条的"点开看详情"是同一个交互。 */
// toolLogSteps/toolLogSig/toolLogBodySig/toolLogBodyTimer 在 app.js（var，挂 window），跨块共享
/* ── 视图缓存：从子代理返回父会话时不要整棵重建 ─────────────────────
   用户："从 subagent 返回到 live stream 的时候会很卡。"
   实测（父会话 120 条消息 + 400 步）：返回一次 load() 要 1.5s，其中
   renderStream（Markdown 解析 + 语法高亮）1.0s、renderSteps 0.44s，全同步阻塞。
   这些 DOM 刚刚就在界面上：切走时把节点摘进 DocumentFragment 留着，切回来直接
   append（展开状态 / 代码块 / 滚动位置都保留），只需几十毫秒；随后 runtime 再
   按需增量刷新。只留最近 3 个会话，避免内存膨胀。 */
const VIEW_CACHE_MAX=3;
var skipDetailOnce=null;   // load() 刚渲染完详情：第一次 runtime 不要再拉一遍并重渲染

function stashView(sid){
 if(!sid)return;
 const chat=document.getElementById('chatlog'),steps=document.getElementById('steps');
 if(!chat||!steps)return;
 // 滚动位置必须在**搬走节点之前**读：清空容器后 scrollHeight 变 0，scrollTop 会被夹到 0
 const scrollTop=chat.scrollTop;
 const chatFrag=document.createDocumentFragment();
 while(chat.firstChild)chatFrag.appendChild(chat.firstChild);
 // 完成卡片按当前会话重新拉，不跟着旧片段走（否则会显示别的会话的改动）
 const oldCard=chatFrag.querySelector&&chatFrag.querySelector('#changesCard');
 if(oldCard)oldCard.remove();
 const stepFrag=document.createDocumentFragment();
 while(steps.firstChild)stepFrag.appendChild(steps.firstChild);
 const one=document.getElementById('toolLogOne'),body=document.getElementById('toolLogBody');
 const oneFrag=document.createDocumentFragment(),bodyFrag=document.createDocumentFragment();
 if(one)while(one.firstChild)oneFrag.appendChild(one.firstChild);
 if(body)while(body.firstChild)bodyFrag.appendChild(body.firstChild);
 const wrap=document.getElementById('toolLog');
 viewCache.delete(sid);                 // 同一会话重复保存：先释放旧片段
 viewCache.set(sid,{chat:chatFrag,steps:stepFrag,one:oneFrag,body:bodyFrag,
  renderedSteps,lastStepCount,streamSig,toolLogSig,
  stepFilter:{tool:stepFilter.tool,status:stepFilter.status},stepFilterKey,
  lastSteps,lastMessages:window.lastMessages,session:lastSessionObj,scroll:scrollTop,
  atBottom:(chat.scrollHeight-chat.scrollTop-chat.clientHeight)<40,
  stepCountText:(document.getElementById('stepCount')||{}).textContent||'',
  stepsPending,lastStepsRaw,
  toolLogHidden:!!(wrap&&wrap.hidden)});
 while(viewCache.size>VIEW_CACHE_MAX)viewCache.delete(viewCache.keys().next().value);
}
function restoreView(cut){
 const chat=document.getElementById('chatlog'),steps=document.getElementById('steps');
 if(!chat||!steps)return false;
 chat.innerHTML='';steps.innerHTML='';
 chat.appendChild(cut.chat);steps.appendChild(cut.steps);
 const one=document.getElementById('toolLogOne'),body=document.getElementById('toolLogBody');
 if(one){one.innerHTML='';one.appendChild(cut.one)}
 if(body){body.innerHTML='';body.appendChild(cut.body)}
 const wrap=document.getElementById('toolLog');
 if(wrap)wrap.hidden=!!cut.toolLogHidden;
 renderedSteps=cut.renderedSteps;lastStepCount=cut.lastStepCount;
 streamSig=cut.streamSig;toolLogSig=cut.toolLogSig;
 stepFilter={tool:(cut.stepFilter&&cut.stepFilter.tool)||'',status:(cut.stepFilter&&cut.stepFilter.status)||''};
 stepFilterKey=cut.stepFilterKey||JSON.stringify(stepFilter);
 lastSteps=cut.lastSteps||[];window.lastMessages=cut.lastMessages||[];
 stepsPending=!!cut.stepsPending;lastStepsRaw=cut.lastStepsRaw||[];
 const _cnt=document.getElementById('stepCount');
 if(_cnt)_cnt.textContent=cut.stepCountText||'';
 try{fillStepTools()}catch(e){}
 chat.scrollTop=cut.scroll||0;
 // 切走时停在底部：回来也贴底（content-visibility 下高度是估算值，不能只恢复像素）
 if(cut.atBottom)try{pinChatBottom(chat)}catch(e){}
 return true;
}
/* 会话外壳（标题/状态/KPI/模型/父会话）：恢复缓存时也要走一遍，不能只还原消息区 */

export { fillStepTools, stepMatch, collapseTransient, renderSteps, renderStepsMaybe, flushStepsIfPending, stepView, svgIcon, toolLabel, stepRowEl, renderStream, pinChatBottom, scrollChatToEnd, stashView, restoreView, nearestScroller, stepFilterInput, PHASE_LABEL };
