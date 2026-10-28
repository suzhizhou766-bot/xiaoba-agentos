'use strict';
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="xiaoba-page-token"]').content;
let snapshot;
let pendingRequest;
const labels = {waiting_receipts:'等待独立回执',pending_center:'材料齐备 · 等待裁决中心确认',submitted:'裁决材料中心已接收 · 待用户裁决',stale:'原卡或初审版本已变化',invalidated:'操作者已标记证据失效'};
function el(tag, value, parent) {const node=document.createElement(tag);if(value!==undefined)node.textContent=value;if(parent)parent.append(node);return node;}
function note(error) {$('message').textContent=error ? String(error.message||error) : '';}
async function api(path, data) {const response=await fetch('/api/open-review/'+path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Xiaoba-Token':token},body:JSON.stringify(data)});const result=await response.json();if(!response.ok||!result.ok)throw Error(result.error||'请求失败');return result;}
function action(parent, title, handler) {const b=el('button',title,parent);b.type='button';b.addEventListener('click',async()=>{b.disabled=true;try{note('');await handler();await refresh();}catch(e){note(e);}finally{b.disabled=false;}});return b;}
function download(name, value) {const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));const a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function template(card) {const receipt={};for(const key of ['review_request_id','assignment_id','reviewer_role','plan_revision','reviewer_id','source_ref','candidate_sha256','input_digest','card_digest'])receipt[key]=card[key];return {...receipt,receipt_id:crypto.randomUUID(),verdict:'NEEDS_INFO',findings:'',evidence_refs:[],started_at:null,finished_at:null};}
const selection=new URLSearchParams(location.search);
function selectSources(id, sources) {const select=$(id),previous=select.value||selection.get(id==='dshSelect'?'dsh':'peer');select.replaceChildren();for(const source of sources.filter(s=>!s.node||s.node.enabled&&!s.node.removed)){const option=el('option',source.node?source.node.name+' · '+(source.node.model||'模型未登记'):source.reviewer_id,select);option.value=source.reviewer_id;}if([...select.options].some(o=>o.value===previous))select.value=previous;}
function render(state) {
  snapshot=state;$('connection').textContent='已连接 · 裁决材料收件箱'+(state.inbox_online?'在线':'离线，等待重试');
  selectSources('dshSelect',state.sources.filter(s=>s.kind==='dsh'));selectSources('peerSelect',state.sources);
  $('requests').replaceChildren();
  for(const request of state.requests){const box=el('section',undefined,$('requests'));el('h3',labels[request.state]||request.state,box);el('small',request.id+' · 计划版本 '+request.revision,box);el('p','原经验卡 '+request.candidate_id,box);
    const a=el('a','打开原卡人工裁决',box);a.href='/?candidate='+encodeURIComponent(request.candidate_id)+'#review';
    const grid=el('div',undefined,box);grid.className='grid';
    for(const assignment of request.assignments){const area=el('details',undefined,grid);area.open=Boolean(assignment.active);const card=assignment.card;const latest=request.receipts.filter(r=>r.assignment_id===assignment.id).at(-1);el('summary',(assignment.role==='dsh'?'固定 DSH':'第二审查 Agent')+' · '+card.reviewer_id+(assignment.active?'':' · 已替换'),area);const status=!assignment.active?'已替换，保留旧来源和历史':assignment.running?'后台实际执行中':latest?.status==='valid'?'审查已完成 · '+latest.data.verdict:latest?.status==='identity_pending'?'已收到回执 · 来源待核对':latest?.status==='invalid'?'回执退回 · '+latest.reason:card.channel==='external_handoff'?'外部交接，等待回执':'后台接入任务卡，等待执行';el('p',status,area);el('small','实际来源 '+card.source_ref,area);action(area,'导出身份任务卡',()=>download('review-card-'+assignment.id+'.json',card));action(area,'导出回执模板',()=>download('receipt-'+assignment.id+'.json',template(card)));
      if(assignment.active&&card.channel==='backend_adapter')action(area,'调用已登记后台',()=>api('run',{assignment_id:assignment.id}));
      if(assignment.active&&assignment.role==='peer')action(area,'更换第二审查者',async()=>{const source=prompt('已登记的新审查者身份 ID');if(!source)return;const reason=prompt('替换原因（旧历史将保留）');if(!reason)return;await api('replace',{request_id:request.id,source_id:source,reason});});
    }
    for(const receipt of request.receipts){const block=el('div',undefined,box);block.className='receipt';el('strong',receipt.data.reviewer_id+' · '+receipt.data.verdict+' · '+receipt.status,block);el('p',receipt.reason||receipt.attestation||'身份待核对；尚未计入双审材料',block);const detail=el('details',undefined,block);el('summary','原始回执与评分',detail);el('pre',JSON.stringify(receipt.data,null,2),detail);if(receipt.status==='identity_pending')action(block,'核对实际来源与交接依据',async()=>{const note=prompt('记录你已核对的来源或交接依据（不是模型自报身份）');if(note)await api('attest',{receipt_id:receipt.id,expected_digest:receipt.digest,note});});}
    const timeline=el('details',undefined,box);el('summary','完整时间线与投递状态',timeline);el('pre',JSON.stringify({history:request.history,deliveries:request.deliveries},null,2),timeline);
  }
  if(!state.requests.length)el('p','尚无审查请求。请关联已有经验卡创建任务。',$('requests'));
  $('decisions').replaceChildren();
  for(const packet of state.decision_packets){const box=el('details',undefined,$('decisions'));const data=packet.data;el('summary',(packet.current?'当前待用户裁决':'历史材料，已失效或被替代')+' · '+data.subject.candidate_id+' · '+data.reviews.map(r=>r.receipt.verdict).join(' / '),box);el('p',data.all_pass?'两方均 PASS；仍需现行验证与用户终审':'存在 FAIL 或资料不足；不能标记为验证通过',box);el('pre',JSON.stringify(data,null,2),box);}
  if(!state.decision_packets.length)el('p','尚未收到齐备的双审材料。',$('decisions'));
}
async function refresh() {try{render(await api('state'));}catch(e){$('connection').textContent='连接失败';note(e);}}
$('sourceForm').addEventListener('submit',async event=>{event.preventDefault();try{await api('sources',Object.fromEntries(new FormData(event.target)));note('');await refresh();}catch(e){note(e);}});
$('requestForm').addEventListener('submit',async event=>{event.preventDefault();try{const values=Object.fromEntries(new FormData(event.target));values.evidence=JSON.parse(values.evidence);const signature=JSON.stringify(values);if(!pendingRequest||pendingRequest.signature!==signature)pendingRequest={signature,id:crypto.randomUUID()};values.request_id=pendingRequest.id;await api('requests',values);pendingRequest=null;note('');await refresh();}catch(e){note(e);}});
$('receiptFile').addEventListener('change',async event=>{try{$('receiptBody').value=await event.target.files[0].text();}catch(e){note(e);}});
$('importReceipt').addEventListener('click',async()=>{try{await api('receipts',JSON.parse($('receiptBody').value));note('');await refresh();}catch(e){note(e);}});
$('retry').addEventListener('click',async()=>{try{await api('retry',{});await refresh();}catch(e){note(e);}});
refresh();setInterval(refresh,5000);
