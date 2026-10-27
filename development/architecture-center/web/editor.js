'use strict';
(() => {
  const element=(tag,text,parent,className)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;if(parent)parent.append(node);return node;};
  const button=(title,action,parent,className)=>{const node=element('button',title,parent,className);node.type='button';node.addEventListener('click',action);return node;};
  let metadata=null,selectedChange=null,selectedModule=null,approval=null,publishing=false;
  const token=document.querySelector('meta[name="upgrade-page-token"]')?.content;
  const stateLabels={awaiting_agent:'待 Agent 接续',claimed:'Agent 实施中',awaiting_validation:'待候选检查',validation_failed:'候选检查未通过',awaiting_package:'待接续构包与审查',awaiting_preflight:'待正式包预检',preflight_failed:'预检未通过',awaiting_release:'预检通过 · 等待用户正式发版',publishing:'正在正式发版',published:'已发布',release_failed:'发版失败 · 待人工核对'};
  const controls=element('div',undefined,document.querySelector('.header-actions'),'iteration-controls');
  const mode=element('span','架构查看',controls,'iteration-chip');
  button('模块规范',()=>openEditor(selectedModule),controls,'iteration-button');
  button('待发布 / 版本记录',()=>openCenter(),controls,'iteration-button');
  const panel=element('section',undefined,document.body,'upgrade-panel');panel.hidden=true;panel.setAttribute('aria-label','升级发布中心');
  const header=element('div',undefined,panel,'upgrade-head');const heading=element('div',undefined,header);element('h2','架构与升级发布中心',heading);element('p','模块提案 → 待发布 → Agent 接续 → 正式包预检 → 用户发版',heading);button('关闭',()=>panel.hidden=true,header);
  const body=element('div',undefined,panel,'upgrade-body');const list=element('aside',undefined,body,'upgrade-list');const item=element('article',undefined,body,'upgrade-item');
  const editor=element('dialog',undefined,document.body,'upgrade-dialog');
  const form=element('form',undefined,editor);element('h2','修改模块规范 · 创建待发布提案',form);element('p','保存只写入待发布工作区，不直接修改活动规则或运行代码。任意获授权的 Agent 可从接续任务入口继续实施。',form);
  function field(title,name,tag='input',parent=form){const label=element('label',title,parent),input=element(tag,undefined,label);input.name=name;return input;}
  const moduleSelect=field('所属模块','module_id','select');
  const goal=field('希望修改什么','goal','textarea');goal.required=true;
  const grid=element('div',undefined,form,'upgrade-grid');const version=field('当前基线版本','base_version','input',grid);const source=field('基线源码记录','base_source','input',grid);
  const target=field('目标版本（可留空，由接续构包确定）','target_version');target.placeholder='vX.Y.Z';
  const spec=field('规范提案内容','body','textarea');spec.className='spec-body';spec.required=true;
  const formMessage=element('div','',form,'upgrade-message');const formActions=element('div',undefined,form,'upgrade-actions');const save=element('button','保存到待发布',formActions,'primary');save.type='submit';button('取消',()=>editor.close(),formActions);
  const releaseDialog=element('dialog',undefined,document.body,'upgrade-dialog');element('h2','确认正式发版',releaseDialog);const releaseText=element('div',undefined,releaseDialog);const releaseActions=element('div',undefined,releaseDialog,'upgrade-actions');
  button('取消',()=>releaseDialog.close(),releaseActions);const releaseConfirm=button('确认正式发版',()=>publish(),releaseActions,'primary');
  const drafts=new Map();let currentSpec=null;
  function connected(){return metadata&&token;}
  async function api(path,payload){
    const response=await fetch('/api/release-center/'+path,payload===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-Xiaoba-Token':token||''},body:JSON.stringify(payload)});
    let result;try{result=await response.json();}catch{throw Error('此页面未连接本机监看中心的升级服务。');}
    if(!response.ok||!result.ok)throw Error(result.error||'请求失败');return result;
  }
  function readonly(){return '当前在线页面用于架构查看。模块编辑、待发布文件与正式发版，在已接入的本机监看中心执行；此页不会直接连接或修改你的电脑。';}
  async function refresh(){
    try{metadata=await api('meta');mode.textContent=token?'更新迭代入口已接入':'架构查看';mode.classList.toggle('connected',!!token);renderList();return true;}
    catch{metadata=null;mode.textContent='架构查看';return false;}
  }
  async function openEditor(id){
    if(!connected()&&!await refresh()){openCenter();return;}
    if(!token){openCenter();return;}
    moduleSelect.replaceChildren();for(const module of metadata.modules){const option=element('option',module.title,moduleSelect);option.value=module.id;}
    if(metadata.modules.some(m=>m.id===id))moduleSelect.value=id;
    goal.value='';target.value='';version.value='开发候选（生产未连接）';source.value='当前模块文件哈希';formMessage.textContent='';
    await loadModule();editor.showModal();
  }
  async function loadModule(){
    try{const result=await api('module?id='+encodeURIComponent(moduleSelect.value));currentSpec=result.result;spec.value=drafts.get(currentSpec.id)||currentSpec.body;source.value=currentSpec.sha256;}
    catch(error){formMessage.textContent=error.message;}
  }
  moduleSelect.addEventListener('change',()=>{if(currentSpec)drafts.set(currentSpec.id,spec.value);loadModule();});
  form.addEventListener('submit',async event=>{
    event.preventDefault();if(!currentSpec)return;save.disabled=true;formMessage.textContent='';
    try{const result=await api('create',{module_id:moduleSelect.value,body:spec.value,expected_sha256:currentSpec.sha256,goal:goal.value,base_version:version.value,base_source:source.value,target_version:target.value});drafts.delete(moduleSelect.value);editor.close();selectedChange=result.result.id;await openCenter();}
    catch(error){formMessage.textContent=error.message;}finally{save.disabled=false;}
  });
  function renderList(){
    list.replaceChildren();if(!connected()){element('p','本机更新工作区尚未连接',list);return;}
    button('新建模块提案',()=>openEditor(selectedModule),list,'iteration-button');
    const changes=metadata.changes||[];
    for(const section of [{title:'待发布任务',rows:changes.filter(c=>c.state!=='published')},{title:'版本与历史记录',rows:changes.filter(c=>c.state==='published')}]){
      element('h3',section.title,list);for(const change of section.rows){const b=button(change.goal,()=>showChange(change.id),list);element('small',change.module_id+' · '+(stateLabels[change.state]||change.state),b);element('small',change.id.slice(0,8)+' · '+(change.target_version||'目标版本待构包'),b);}if(!section.rows.length)element('p','暂无记录',list);
    }
  }
  async function openCenter(){panel.hidden=false;await refresh();renderList();if(!connected()){item.replaceChildren();element('p',readonly(),item,'upgrade-readonly');return;}if(selectedChange)await showChange(selectedChange);else{item.replaceChildren();element('h3','从架构节点进入模块修改',item);element('p','点击拓扑节点，在详情中选择“编辑所属模块规范”。保存后生成带差异、权限和接续说明的待发布任务。',item);element('p','候选检查只是文本、语法和清单检查，不能替代接续 Agent 的功能验证、独立审查和正式包预检。',item);}}
  async function showChange(id){
    selectedChange=id;try{const result=await api('change?id='+encodeURIComponent(id));renderChange(result.result);}catch(error){item.replaceChildren();element('p',error.message,item,'upgrade-message');}
  }
  function section(title,value){const details=element('details',undefined,item);element('summary',title,details);element('pre',typeof value==='string'?value:JSON.stringify(value,null,2),details);return details;}
  function renderChange(change){
    item.replaceChildren();element('span',change.module_id,item,'module-pill');element('span',stateLabels[change.state]||change.state,item,'badge');element('h3',change.goal,item);element('p','改动 ID：'+change.id+' · 修订 '+change.revision,item);element('p','基线：'+change.base_version+' / '+change.base_source+'；目标：'+(change.target_version||'由正式包确定'),item);
    const message=element('div','',item,'upgrade-message');
    const actions=element('div',undefined,item,'upgrade-actions');
    const check=button('检查当前候选',async()=>{check.disabled=true;try{const result=await api('validate',{cid:change.id,expected_revision:change.revision});renderChange(result.result);await refresh();}catch(error){message.textContent=error.message;}finally{check.disabled=false;}},actions);
    check.disabled=!!change.lease||['publishing','published'].includes(change.state);
    const preflight=button('正式包预检',async()=>{preflight.disabled=true;try{const result=await api('preflight',{cid:change.id,expected_revision:change.revision});approval=result.result;if(!approval.preflight.ok){message.textContent=approval.preflight.error||'预检未通过';await showChange(change.id);}else{renderChange(approval.change);await refresh();}}catch(error){message.textContent=error.message;}finally{preflight.disabled=false;}},actions);
    preflight.disabled=!metadata.driver_available||!change.package||!change.validation?.ok||!!change.lease||['publishing','published'].includes(change.state);
    const publishButton=button('正式发版',()=>{
      if(!approval||approval.change.id!==change.id||approval.revision!==change.revision){message.textContent='请先完成本次预检，阅读具体改动后确认。';return;}
      releaseText.replaceChildren();element('p','将执行原生正式切换流程。目标和源码清单如下；只有真实切换与回读通过才会记录为已发布。',releaseText);element('pre',JSON.stringify({change_id:change.id,previous_version:approval.preflight.previous_version,target_version:approval.preflight.version,files:change.files,checks:approval.preflight.checks,expires_at:approval.preflight.expires_at},null,2),releaseText);releaseConfirm.disabled=false;releaseDialog.showModal();
    },actions,'primary');
    publishButton.disabled=change.state!=='awaiting_release'||!approval||approval.change.id!==change.id||publishing;
    if(!metadata.driver_available)element('p','本机原生发布执行器未配置。模块提案和 Agent 接续可用，正式预检与发版尚不可用。',item);
    else if(!change.package)element('p','等待接续 Agent 实施、功能验证、独立审查并构建受审正式包；尚不能直接发版。',item);
    if(change.validation)element('p','候选检查范围：语法与文件清单；'+(change.validation.ok?'已通过。':'未通过。'),item);
    if(change.release_error)element('p',change.release_error,item,'upgrade-message');
    section('Agent 接续入口与写入权限',{folder:change.folder,handoff:'HANDOFF.md',permissions:'ACCESS.json',checkpoint:change.checkpoint,next_step:change.next_step,lease:change.lease?{actor:change.lease.actor,expires_at:change.lease.expires_at}:null});
    for(const [path,diff] of Object.entries(change.diffs||{}))section('文件差异 · '+path,diff||'文件内容未变化');
    section('源码与候选清单',change.files);if(change.validation)section('候选检查结果',change.validation);if(change.last_preflight||change.preflight)section('正式包预检记录',change.last_preflight||change.preflight.native);if(change.release_result)section('实际发布与回读回执',change.release_result);section('完整修改与接续历史',change.history);
  }
  async function publish(){
    if(!approval||publishing)return;publishing=true;releaseConfirm.disabled=true;
    const request={cid:approval.change.id,expected_revision:approval.revision,confirmation:approval.confirmation,request_id:approval.request_id||(approval.request_id=crypto.randomUUID())};
    try{const result=await api('publish',request);releaseDialog.close();approval=null;await refresh();await showChange(request.cid);if(!result.result.ok)element('p',result.result.error||'原生发布未完成，请核对回执。',item,'upgrade-message');}
    catch(error){releaseDialog.close();element('p',error.message+'；任务已暂停，不自动重试。发布请求 ID：'+request.request_id,item,'upgrade-message');approval=null;}
    finally{publishing=false;}
  }
  window.addEventListener('xiaoba:node-selected',event=>{
    if(!metadata)return;const module=metadata.modules.find(m=>m.nodes.includes(event.detail.id));selectedModule=module?.id;
    document.getElementById('module-edit-action')?.remove();if(!module)return;
    const b=button('编辑所属模块规范 · '+module.title,()=>openEditor(module.id),document.getElementById('detailLinks'),'upgrade-button-in-detail');b.id='module-edit-action';
  });
  document.addEventListener('keydown',event=>{if(event.key==='Escape')panel.hidden=true;});
  setInterval(()=>{if(!panel.hidden&&!editor.open&&!releaseDialog.open&&!publishing){refresh().then(()=>{if(selectedChange)showChange(selectedChange);});}},5000);
  refresh();
})();
