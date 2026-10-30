'use strict';
// One controller shared by the existing topology and its same-page drawer.
(() => {
  const byId=id=>document.getElementById(id),drawer=byId('apDrawer');
  if(!drawer)return;
  const token=drawer.dataset.pageToken;
  let state=null,selected=null,mode=null,pendingAdd=null,pendingDispatch=null,removeConfirm=false,returnFocus=null,drawerSignature=null;
  const watchers=[];
  let previewOpen=false,previewTimer=null,previewAbort=null,previewGeneration=0;
  function stopPreview(){clearTimeout(previewTimer);previewTimer=null;previewAbort?.abort();previewAbort=null;previewGeneration++;}
  function el(tag,text,parent){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.append(n);return n;}
  function note(message){byId('apMessage').textContent=message?.message||message||'';}
  async function api(path,value,signal){const response=await fetch('/api/agent-pool/'+path,value===undefined?{cache:'no-store',signal}:{method:'POST',cache:'no-store',signal,headers:{'Content-Type':'application/json','X-Xiaoba-Token':token},body:JSON.stringify(value)});const data=await response.json();if(!response.ok||!data.ok)throw Error(data.error||'节点操作失败');return data;}
  function button(parent,text,action,cls){const b=el('button',text,parent);b.type='button';if(cls)b.className=cls;b.addEventListener('click',async()=>{b.disabled=true;try{await action();}catch(error){note(error);}finally{b.disabled=false;}});return b;}
  function close(){stopPreview();previewOpen=false;selected=null;mode=null;drawer.hidden=true;byId('apScrim').hidden=true;returnFocus?.focus();}
  function open(next,node){
    stopPreview();previewOpen=false;
    returnFocus=document.activeElement;mode=next;selected=node?.node_id||null;removeConfirm=false;drawerSignature=null;
    drawer.hidden=false;byId('apScrim').hidden=false;byId('apForm').hidden=next!=='add';byId('apDetail').hidden=next!=='detail';byId('apManage').hidden=next!=='manage';byId('apDispatch').hidden=next!=='dispatch';
    byId('apTitle').textContent=next==='add'?'添加 Agent 节点':next==='manage'?'管理后台节点':next==='dispatch'?'派工审查':node.name;note('');
    if(next==='add'){byId('apForm').reset();byId('apForm').elements.name.focus();}
    else {renderDrawer();if(next==='dispatch')byId('apDispatchGo').disabled=!writable();byId('apClose').focus();}
  }
  function writable(){return Boolean(token&&token!=='__TOKEN__'&&state?.event_chain.ok);}
  function terminal(root,node){
    const view=el('details');view.className='ap-terminal';view.open=previewOpen;
    el('summary','实时终端预览 · 只读',view);
    const status=el('p','展开后查看；收起不停止后台任务。',view);status.className='ap-note';
    const output=el('pre','尚未读取终端输出。',view);output.className='ap-terminal-output';output.dataset.nodeId=node.node_id;output.tabIndex=0;output.setAttribute('aria-label',node.name+' 只读终端输出');
    el('p','真实终端当前画面的文本视图，隐藏可识别的敏感内容。预览只读，不作为任务成功或经验批准的证据。',view).className='ap-note';
    let reading=false;
    async function poll(){
      if(reading||!view.isConnected||!view.open||document.hidden||selected!==node.node_id||mode!=='detail')return;
      reading=true;const generation=previewGeneration;previewAbort=new AbortController();
      try{
        const data=await api('preview',{node_id:node.node_id},previewAbort.signal);
        if(generation!==previewGeneration||!view.isConnected||!view.open)return;
        const p=data.preview,labels={not_started:'会话尚未启动',starting:'正在初始化',waiting_user:'等待你确认原生目录',busy:'后台执行中',receipt_received:'当日文件回执已收到',idle:'等待下一条任务',closed:'会话已结束',not_connected:'此节点尚无终端预览调用器'};
        status.textContent=(labels[p.state]||'状态待核对')+(p.session_id?' · 会话 '+p.session_id.slice(0,8):'')+(p.trimmed?' · 仅显示近期输出':'');
        const follow=output.scrollHeight-output.clientHeight-output.scrollTop<32;
        const text=p.text||'当前没有终端输出；查看预览不会启动 Agent 或发送任务。';
        if(output.textContent!==text){output.textContent=text;if(follow)output.scrollTop=output.scrollHeight;}
      }catch(error){if(error.name!=='AbortError'&&generation===previewGeneration)status.textContent='预览连接中断；后台任务不受影响。';}
      finally{reading=false;if(view.isConnected&&view.open&&!document.hidden&&selected===node.node_id&&mode==='detail')previewTimer=setTimeout(poll,1000);}
    }
    view.addEventListener('toggle',()=>{if(!view.isConnected||selected!==node.node_id||mode!=='detail')return;previewOpen=view.open;stopPreview();if(view.open)poll();});
    root.append(view);view._poll=poll;return view;
  }
  async function change(node,operation,enabled){
    await api(operation,{node_id:node.node_id,expected_revision:node.revision,...(operation==='toggle'?{enabled}:{})});
    if(operation==='remove')open('manage');
    note(operation==='remove'?'节点已移除，历史保留，可恢复':operation==='restore'?'节点已恢复，保持关闭':enabled?'节点已开启':'节点已关闭，不再接受新指派或调用');
    await refresh();
  }
  function detail(node){
    const root=byId('apDetail'),old=root.querySelector('.ap-terminal-output'),saved=old?.dataset.nodeId===node.node_id?{text:old.textContent,scroll:old.scrollTop}:null;stopPreview();root.replaceChildren();root.className='ap-detail';byId('apTitle').textContent=node.name;
    if(node.execution_mode==='native_interactive_serial'){const view=terminal(root,node),out=view.querySelector('pre');if(saved){out.textContent=saved.text;out.scrollTop=saved.scroll;}}
    const dl=el('dl',undefined,root);
    for(const [label,value] of [['Agent',node.agent],['调用方式',{cli:'CLI',desktop:'桌面端',web:'Web'}[node.interface]||'旧来源，资料尚未登记'],['调用入口',node.entry||'未登记'],['模型',node.model||'未登记'],['当前状态',node.enabled?'已开启':'已关闭']]){el('dt',label,dl);el('dd',value,dl);}
    el('p',node.backend_available?(node.execution_mode==='native_interactive_serial'?'已接入本机交互调用器，按节点填写的入口与模型配置派发任务。':'已接入本机调用器；调用仍须返回本任务模型的执行证据。'):'主入口按任务卡中的入口和模型调用；本服务未接入此节点的自动调用器。',root).className='ap-note';
    if(node.execution_mode==='native_interactive_serial')el('p','同一交互会话逐条执行任务卡，过程与回执写入当日Markdown；终端只供查看进度。',root).className='ap-note';
    const actions=el('div',undefined,root);actions.className='ap-actions';
    const readiness=node.readiness||{},labels={disabled:'已关闭',removed:'已移除',missing_configuration:'资料待补齐',adapter_required:'调用器待接入',entry_changed_or_unavailable:'入口待核对',test_required:'尚未通过当前配置的测试',test_evidence_invalid:'测试证据无效',native_confirmation_required:'等待你确认AGY原生工作区',testing:'正在测试',tested:'测试通过，可直接运行'};
    const status=el('p','运行资格：'+(labels[readiness.reason]||'待测试'),root);status.className='ap-note';root.insertBefore(status,actions);
    if(node.test?.test_id){const last=el('p','最近测试：'+node.test.test_id+' · '+node.test.status,root);last.className='ap-note';root.insertBefore(last,actions);}
    if(node.test?.message&&!node.pending_action)el('p',node.test.message,root).className='ap-note';
    if(node.test?.reason==='interactive_result_invalid')el('p','上面的失败来自先前的终端结果读取。新的文件回执方式尚未在此节点实测。',root).className='ap-note';
    if(node.pending_action?.type==='native_workspace_trust'){
      const pending=node.pending_action,consent=el('div');consent.className='ap-consent';root.insertBefore(consent,root.firstChild);
      el('p',pending.message,consent).className='ap-note';el('code',pending.directory,consent);
      button(consent,'确认信任此节点工作区',async()=>{await api('interactive-confirm',{node_id:node.node_id,expected_revision:node.revision,action_id:pending.action_id,session_id:pending.session_id,decision:'trust'});await refresh();note('原生目录已确认；尚未发送模型任务，请再点击测试运行。');},'ap-primary').disabled=!writable()||!node.enabled;
      el('p','这会授予AGY此目录的读、改和执行权限；请你本人决定。关闭节点可取消。',consent).className='ap-note';
    }
    const warning=el('p','测试运行会按节点配置向此入口发送任务；不读取或修改你的登录设置。',root);warning.className='ap-note';root.insertBefore(warning,actions);
    button(actions,'测试运行',async()=>{const test=await api('test',{node_id:node.node_id,expected_revision:node.revision,test_id:crypto.randomUUID()});await refresh();note(test.result.status==='passed'?'测试通过，后续相同配置的适用任务直接运行，不再补问资料。':test.result.message||'测试未通过；不会自动调用或换用其它后台。');},'ap-primary').disabled=!writable()||!node.enabled||Boolean(node.test_running)||Boolean(node.pending_action);
    button(actions,'本周表现',async()=>{
      const day=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
      const data=await api('weekly-scores',{day});root.querySelector('[data-weekly-scores]')?.remove();const box=el('section',undefined,root);box.className='ap-note';box.dataset.weeklyScores=node.node_id;
      el('strong',data.result.week_start+' 至 '+data.result.week_end+' · Agent＋模型周表现',box);
      const rows=data.result.ranking.filter(row=>row.node_ids.includes(node.node_id));
      if(!rows.length)el('p','本周暂无主入口核验后的任务评分。健康检测不计业务分。',box);
      for(const row of rows)el('p',row.model+'：'+row.points+' 分 / '+row.tasks+' 项任务，完成得好 '+Math.round(row.success_rate*100)+'%',box);
      el('p','按任务记＋1或0，不计资源投入，不形成永久总分。',box);
    }).disabled=!writable();
    if(node.enabled){const a=el('a','用于审查任务 →',actions);a.className='ap-primary';a.href='/open-review?'+(node.kind==='dsh'?'dsh=':'peer=')+encodeURIComponent(node.node_id);}
    button(actions,node.enabled?'关闭节点':'开启节点',()=>change(node,'toggle',!node.enabled)).disabled=!writable();
    const remove=button(actions,removeConfirm?'确认移除（历史保留）':'移除节点',async()=>{if(!removeConfirm){removeConfirm=true;remove.textContent='确认移除（历史保留）';return;}await change(node,'remove');},'ap-danger');remove.disabled=!writable();
    button(actions,'管理其他节点',()=>open('manage'));
  }
  function manage(){
    const root=byId('apManage');root.replaceChildren();const list=el('div',undefined,root);list.className='ap-list';
    for(const node of state?.nodes.filter(n=>!n.removed)||[])button(list,node.name+' · '+(node.enabled?'开启':'关闭'),()=>open('detail',node));
    button(list,'＋ 添加节点',()=>open('add'),'ap-primary').disabled=!writable();
    const removed=state?.nodes.filter(n=>n.removed)||[];
    if(removed.length){const group=el('details',undefined,root);group.className='ap-removed';el('summary','已移除的节点 · '+removed.length,group);for(const node of removed){const row=el('div',undefined,group);row.className='ap-removed-row';el('span',node.name,row);button(row,'恢复节点',()=>change(node,'restore')).disabled=!writable();}}
  }
  function renderDrawer(){
    const signature=JSON.stringify([mode,selected,removeConfirm,writable(),state?.nodes]);if(signature===drawerSignature)return;drawerSignature=signature;
    if(mode==='detail'){const node=state?.nodes.find(n=>!n.removed&&n.node_id===selected);if(node)detail(node);else open('manage');}else if(mode==='manage')manage();
  }
  function publish(){for(const watcher of watchers)watcher(state,writable());renderDrawer();}
  async function refresh(){
    try{state=await api('state');byId('apConnection').textContent=state.event_chain.ok?'本机节点已保存 · 开放审查':'审计链异常，暂停节点写入';publish();}
    catch(error){state=null;byId('apConnection').textContent='节点服务未连接，暂停操作';publish();note(error);}
  }
  byId('apClose').addEventListener('click',close);byId('apScrim').addEventListener('click',close);
  document.addEventListener('visibilitychange',()=>{stopPreview();if(!document.hidden)drawer.querySelector('.ap-terminal[open]')?._poll();});
  byId('apForm').addEventListener('submit',async event=>{
    event.preventDefault();const submit=event.submitter;submit.disabled=true;
    try{const value=Object.fromEntries(new FormData(event.target)),signature=JSON.stringify(value);if(!pendingAdd||pendingAdd.signature!==signature)pendingAdd={signature,id:crypto.randomUUID()};const added=await api('nodes',{node_id:pendingAdd.id,...value,agent:value.name});const id=added.result.reviewer_id;pendingAdd=null;await refresh();open('detail',state.nodes.find(n=>n.node_id===id));note('节点已添加，保持关闭；请开启并测试运行。');}catch(error){note(error);}finally{submit.disabled=false;}
  });
  byId('apDispatchGo').addEventListener('click',async()=>{
    const go=byId('apDispatchGo');go.disabled=true;
    try{
      const review_type=byId('apReviewType').value;
      if(!review_type)throw Error('请选择审查类型');
      const candidate_id=byId('apCandidate').value.trim(),scope=byId('apScope').value.trim();
      if(!candidate_id||!scope)throw Error('请填写原经验卡 ID 和审查范围');
      let evidence;try{evidence=JSON.parse(byId('apEvidence').value||'[]');}catch(error){throw Error('证据引用需要 JSON 数组');}
      const value={candidate_id,scope,evidence,review_type},signature=JSON.stringify(value);
      if(!pendingDispatch||pendingDispatch.signature!==signature)pendingDispatch={signature,id:crypto.randomUUID()};
      const result=(await api('dispatch',{...value,request_id:pendingDispatch.id})).result;pendingDispatch=null;
      if(result.route==='completed')note('派工完成；任务卡与回执在开放审查页查看。');
      else if(result.route==='ask_user')note((result.questions||[]).map(q=>q.question).join(' ')||'需要先补齐节点资料或选定节点。');
      else note('派工未完成：'+((result.blockers||[]).map(b=>b.reason).join('、')||'节点不可用'));
      await refresh();
    }catch(error){note(error);}finally{go.disabled=!writable();}
  });
  window.XiaobaAgentPool={watch(fn){watchers.push(fn);fn(state,writable());},add(){open('add');},manage(){open('manage');},dispatch(){open('dispatch');},open(node){open('detail',node);},toggle(node){return change(node,'toggle',!node.enabled);},report:note};
  refresh();setInterval(()=>{if(!document.hidden&&mode!=='add')refresh();},5000);
})();
