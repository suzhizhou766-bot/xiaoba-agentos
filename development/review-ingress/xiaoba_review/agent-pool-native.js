// Mount nodes in the existing overview topology; no tab, iframe or separate page.
(() => {
  const topo=document.getElementById('topo'),pool=window.XiaobaAgentPool;
  if(!topo||!pool||typeof NODE_EL==='undefined'||typeof EDGES==='undefined')return;
  topo.setAttribute('role','group');
  for(const id of ['agy','dsh'])if(NODE_EL[id]){const old=NODE_EL[id].g;old.dataset.agentPoolLegacy='true';old.setAttribute('aria-hidden','true');old.setAttribute('tabindex','-1');}
  const legacyEdges=['gate>agy','gate>dsh','agy>judge','dsh>judge'];
  for(const id of legacyEdges)if(EDGES[id])EDGES[id].el.dataset.agentPoolLegacy='true';
  // The original mock/live monitor can refresh; never show its old Agent traffic
  // as evidence that these newly registered nodes have executed a task.
  const originalRender=renderTopo;
  renderTopo=function(){originalRender();for(const id of legacyEdges)if(EDGES[id])EDGES[id].rate=0;for(let i=pkts.length-1;i>=0;i--)if(legacyEdges.some(id=>EDGES[id]===pkts[i].e)){pkts[i].el.remove();pkts.splice(i,1);}};
  renderTopo();
  const ns='http://www.w3.org/2000/svg',group=document.createElementNS(ns,'g');group.id='agentNodeTopology';topo.append(group);
  const routes=document.createElementNS(ns,'g');routes.setAttribute('aria-hidden','true');group.append(routes);
  const frame=document.createElementNS(ns,'foreignObject');for(const [name,value] of Object.entries({x:373,y:63,width:128,height:400}))frame.setAttribute(name,value);frame.classList.add('ap-topology');group.append(frame);
  const column=document.createElement('div');column.className='ap-column';column.setAttribute('aria-label','后台 Agent 节点');frame.append(column);
  function drawRoutes(){
    routes.replaceChildren();const transform=topo.getScreenCTM();if(!transform)return;
    for(const card of column.querySelectorAll('.ap-card')){
      const box=card.getBoundingClientRect(),point=topo.createSVGPoint();point.x=box.left+box.width/2;point.y=box.top+box.height/2;const center=point.matrixTransform(transform.inverse()),y=center.y;
      if(y<63||y>463)continue;
      const route=document.createElementNS(ns,'path');route.setAttribute('d','M327,380 C350,380 350,'+y+' 375,'+y+' M499,'+y+' C524,'+y+' 524,150 547,150');route.setAttribute('class','ap-structure');route.style.opacity=card.classList.contains('ap-enabled')?'0.55':'0.25';routes.append(route);
    }
  }
  column.addEventListener('scroll',drawRoutes);window.addEventListener('resize',drawRoutes);
  const title=topo.closest('.panel')?.querySelector('.panel-head h2');if(title)title.textContent='实时架构拓扑 · 后台节点可管理';
  const flow=topo.closest('.panel')?.querySelector('.flow');if(flow)flow.textContent='后台 Agent 列可直接添加、开关和移除节点。节点状态来自本机登记，仅用于开放审查；其他运行数据仍按当前数据源显示，旧 Worker 后台名单沿用原配置。';
  const tableHeading=document.getElementById('agentRows')?.closest('.panel')?.querySelector('h2');if(tableHeading)tableHeading.textContent='旧 Worker 后台状态';
  function element(tag,text,parent){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(parent)parent.append(n);return n;}
  function action(parent,text,fn,cls){const b=element('button',text,parent);b.type='button';b.className=cls;b.addEventListener('click',async()=>{b.disabled=true;try{await fn();}catch(error){pool.report(error);}finally{b.disabled=false;}});return b;}
  pool.watch((state,writable)=>{
    // Avoid interrupting focused node controls during the five-second refresh.
    const signature=JSON.stringify(state?.nodes)+':'+writable;if(column.dataset.signature===signature)return;column.dataset.signature=signature;
    const focused=document.activeElement,focusId=focused?.dataset.nodeId,focusRole=focused?.getAttribute('role'),scroll=column.scrollTop;
    column.replaceChildren();
    const nodes=state?.nodes.filter(n=>!n.removed)||[],enabled=nodes.filter(n=>n.enabled).length;
    element('p',state?enabled+' 开启 / '+nodes.length+' 节点 · 本机':'节点服务未连接',column).className='ap-caption';
    for(const node of nodes){
      const card=element('div',undefined,column);card.className='ap-card'+(node.enabled?' ap-enabled':'');card.dataset.nodeId=node.node_id;
      const main=action(card,'',()=>pool.open(node),'ap-node-main');main.dataset.nodeId=node.node_id;main.setAttribute('aria-label','设置 '+node.name);main.title=node.name+' · '+node.agent+' · '+(node.model||'模型未登记');element('strong',node.name,main);element('span',node.model||'模型未登记',main);
      element('span',node.interface==='tmux'?(node.tmux_jobs?.some(j=>j.status==='waiting_user_agent')?'等待用户/Agent':node.enabled?'用户在场 · 仅协作':'已关闭'):node.readiness?.ready?'已测试 · 可直接运行':node.enabled?'待测试 / 需接线':'已关闭',main);
      const toggle=action(card,'',()=>pool.toggle(node),'ap-toggle');toggle.dataset.nodeId=node.node_id;toggle.setAttribute('role','switch');toggle.setAttribute('aria-label','启用 '+node.name);toggle.setAttribute('aria-checked',String(node.enabled));toggle.title=node.enabled?'关闭节点':'开启节点';toggle.disabled=!writable;
    }
    action(column,'＋ 添加节点',()=>pool.add(),'ap-add').disabled=!writable;
    action(column,'派工审查',()=>pool.dispatch(),'ap-add').disabled=!writable;
    action(column,'管理 / 已移除 '+(state?.nodes.filter(n=>n.removed).length||0),()=>pool.manage(),'ap-manage');
    column.scrollTop=scroll;
    drawRoutes();
    if(focusId)for(const control of column.querySelectorAll('button[data-node-id]'))if(control.dataset.nodeId===focusId&&control.getAttribute('role')===focusRole){control.focus();break;}
  });
})();
