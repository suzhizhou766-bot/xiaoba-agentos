/* =========================================================
   8. 视图切换 + 内嵌裁决中心
   裁决逻辑沿用本机裁决中心：这里只负责展示、起草与提交确认。
   ========================================================= */
const Tabs = (() => {
  const views = {overview:$("viewOverview"), review:$("viewReview"), director:$("viewDirector"), stage:$("viewStage"), keypool:$("viewKeypool"), taskcards:$("viewTaskcards")};
  const listeners = [];
  let current = "overview";
  function show(name, push=true){
    if(!views[name]) name="overview";
    current = name;
    Object.entries(views).forEach(([k,el])=>{ el.hidden = k!==name; });
    document.querySelectorAll(".tab").forEach(t=>t.setAttribute("aria-selected", String(t.dataset.view===name)));
    if(push && location.hash.slice(1).split("=")[0]!==name) history.replaceState(null,"","#"+name);
    listeners.forEach(fn=>fn(name));
    if(name==="overview" && State.ts) renderAll();
  }
  document.querySelectorAll(".tab").forEach(t=>t.addEventListener("click",()=>show(t.dataset.view)));
  addEventListener("hashchange",()=>show(location.hash.slice(1).split("=")[0]||"overview", false));
  return {show, onChange:fn=>listeners.push(fn), get current(){ return current; }};
})();

const ReviewCenter = (() => {
  // 提交令牌只留在这个闭包里，读完就从页面上移除（内嵌的导演台与本页同源，不让它从页面上读到）
  const tokenMeta = document.querySelector('meta[name="xiaoba-page-token"]');
  const TOKEN = tokenMeta?.content || "";
  if(tokenMeta){ document.documentElement.dataset.hosted="1"; tokenMeta.remove(); }
  const DRAFT_KEY = "xiaoba-monitor-review-drafts-v1", NOTE_MAX = 3500;
  const DECISION = {approve:"通过", revise:"需要修改", reject:"不予采用"};
  const STAGE = {delivery:"交付初审", final:"经验终审"};
  const same = (a,b)=>JSON.stringify(a)===JSON.stringify(b);
  const when = sec=>{ const d=new Date(sec*1000); return isNaN(d)? "—" : d.toLocaleString("zh-CN",{hour12:false}); };
  const day = sec=>{ const d=new Date(sec*1000); return isNaN(d)? "—" : d.toLocaleDateString("zh-CN"); };

  let state=null, selected=null, detail=null, ticket=0, saved={}, drafts={}, packet=null, submitting=false, wanted=null, returnedExpanded=false;
  let refreshSeq=0;   // 卡07 C07-03：轮询请求序号，迟到响应不得覆盖新依据/新详情
  let packetReviewBaseline=new Set(), packetDetails={}, progressAttempt=0;
  try{ drafts = JSON.parse(localStorage.getItem(DRAFT_KEY)||"{}") || {}; }catch(e){ drafts={}; }
  const stash = ()=>{ try{ localStorage.setItem(DRAFT_KEY, JSON.stringify(drafts)); }catch(e){} };
  const forget = id=>{ delete saved[id]; delete drafts[id]; stash(); };

  async function api(path, body){
    const init = {cache:"no-store"};
    if(body){ init.method="POST"; init.headers={"Content-Type":"application/json","X-Xiaoba-Token":TOKEN}; init.body=JSON.stringify(body); }
    const res = await fetch("api/review/"+path, init);
    let data; try{ data = await res.json(); }catch(e){ throw new Error("返回内容无法解析（HTTP "+res.status+"）"); }
    if(!res.ok) throw new Error(data && data.error || "连接失败（HTTP "+res.status+"）");
    return data;
  }

  let valueQueueBusy=false;
  const VALUE_DISPATCH_LABEL={not_started:"尚未派工",blocked:"等待审查节点",started:"评分进行中或待续",failed:"评分失败",waiting_receipts:"等待双审回执"};
  async function refreshValueQueue(preserveMessage=false){
    if(valueQueueBusy) return;
    valueQueueBusy=true;
    try{
      const response=await fetch("/api/agent-pool/value-review-queue",{cache:"no-store"});
      let data; try{data=await response.json();}catch(e){throw new Error("返回内容无法解析（HTTP "+response.status+"）");}
      if(!response.ok || !data.ok) throw new Error(data.error||"读取失败（HTTP "+response.status+"）");
      const items=Array.isArray(data.items)?data.items:[];
      const rows=$("valueReviewRows"); rows.replaceChildren();
      $("valueReviewSummary").textContent=items.length+" 条待评分";
      if(!preserveMessage) $("valueReviewMessage").textContent="";
      if(!items.length){ const row=document.createElement("tr"), cell=document.createElement("td"); cell.colSpan=5; cell.className="none"; cell.textContent="当前没有待价值评分候选"; row.append(cell); rows.append(row); return; }
      for(const item of items){
        const row=document.createElement("tr"), idCell=document.createElement("td"), hashCell=document.createElement("td"), statusCell=document.createElement("td"), dispatchCell=document.createElement("td"), actionCell=document.createElement("td");
        idCell.textContent=item.task_id||"—";
        const hash=document.createElement("code"); hash.textContent=item.candidate_sha256||"—"; hash.style.overflowWrap="anywhere"; hashCell.append(hash);
        statusCell.textContent="待价值评分";
        // 卡07 C07-03：顶层 item.reason 是未评分/回执缺项的原因，必须与派工状态分开显示。
        if(item.reason){ const why=document.createElement("div"); why.className="foot"; why.textContent=item.reason; statusCell.append(why); }
        const dispatch=item.dispatch||{status:"not_started",reason:""};
        dispatchCell.textContent=VALUE_DISPATCH_LABEL[dispatch.status]||dispatch.status||"状态未知";
        if(dispatch.reason){ const reason=document.createElement("div"); reason.className="foot"; reason.textContent=dispatch.reason; dispatchCell.append(reason); }
        const button=document.createElement("button"); button.type="button"; button.className="btn primary";
        button.textContent=dispatch.status==="not_started"?"开始评分":dispatch.status==="waiting_receipts"||dispatch.status==="started"?"继续评分":"重试评分";
        button.addEventListener("click",()=>dispatchValueReview(item,button)); actionCell.append(button);
        row.append(idCell,hashCell,statusCell,dispatchCell,actionCell); rows.append(row);
      }
    }catch(error){
      $("valueReviewSummary").textContent="队列读取失败";
      $("valueReviewMessage").textContent="待评分状态暂不可读："+String(error.message||error)+"。候选处理结果未作推断。";
    }finally{valueQueueBusy=false;}
  }
  async function dispatchValueReview(item,button){
    if(!item?.task_id || !item?.candidate_sha256 || button.disabled) return;
    button.disabled=true; const prior=button.textContent; button.textContent="评分中…";
    $("valueReviewMessage").textContent="正在按 Agent 池审查角色派工。";
    try{
      const response=await fetch("/api/agent-pool/value-review-dispatch",{
        method:"POST",cache:"no-store",headers:{"Content-Type":"application/json","X-Xiaoba-Token":TOKEN},
        body:JSON.stringify({candidate_id:item.task_id,candidate_sha256:item.candidate_sha256})
      });
      let data; try{data=await response.json();}catch(e){throw new Error("返回内容无法解析（HTTP "+response.status+"）");}
      if(!response.ok || !data.ok) throw new Error(data.error||"派工失败（HTTP "+response.status+"）");
      const result=data.result||{};
      if(result.route==="completed"){
        $("valueReviewMessage").textContent="双审已结算；候选已进入初审，或已按价值规则结束。";
        refresh(true);
      }else{
        $("valueReviewMessage").textContent=(result.reason||"派工未完成")+" 候选仍留在待价值评分队列。";
      }
    }catch(error){
      $("valueReviewMessage").textContent="评分派工失败，候选仍留在待价值评分队列："+String(error.message||error);
    }finally{
      button.disabled=false; button.textContent=prior;
      await refreshValueQueue(true);
    }
  }

  /* ---- 单条任务交付确认：读取可轮询，写入只在用户二次确认后发送 ---- */
  let taskDeliveries=null, taskDeliveryDetail=null, taskDeliveryBusy=false;
  let taskDeliverySelection=null, taskDeliveryDetailRequest=0;
  const taskDeliveryPendingPackets=new Map();
  function deliveryActionLabel(value){ return value==="withdraw"?"撤回验收":"接受此版本"; }
  function deliveryDigest(value){ return JSON.stringify(value); }
  function renderTaskDeliveries(){
    const list=$("taskDeliveryList"), items=(taskDeliveries&&Array.isArray(taskDeliveries.items))?taskDeliveries.items:[];
    list.replaceChildren();
    const problems=(taskDeliveries&&taskDeliveries.problems)||[];
    $("taskDeliverySummary").textContent=taskDeliveries?items.length+" 项待核对"+(problems.length?" · "+problems.length+" 项有问题":""):"暂不可用";
    for(const problem of problems){
      const row=document.createElement("li"), body=document.createElement("div"), name=document.createElement("strong"), why=document.createElement("p");
      name.className="delivery-name";name.textContent=problem.task_id||"无法识别的任务";
      why.className="delivery-alert";why.textContent=problem.reason||"交付材料不可读";
      body.append(name,why);row.append(body);list.append(row);
    }
    if(!items.length&&!problems.length){const row=document.createElement("li");row.className="none";row.textContent=taskDeliveries?"当前没有待确认的任务交付。":"正在读取…";list.append(row);}
    for(const item of items){
      const row=document.createElement("li"), body=document.createElement("div"), name=document.createElement("div"), meta=document.createElement("p"), button=document.createElement("button");
      name.className="delivery-name";name.textContent=item.task_id||"—";
      const artifact=item.artifact||{};
      const accepted=(item.expected&&item.expected.latest_delivery_event_id)||null;
      meta.className="delivery-meta";
      meta.textContent=[artifact.version||"版本未标记",artifact.path||"",artifact.sha256?"SHA-256 "+artifact.sha256:"",accepted?"已有验收记录":"尚无验收记录"].filter(Boolean).join(" · ");
      button.type="button";button.className="btn";button.textContent="核对";button.addEventListener("click",()=>openTaskDelivery(item.task_id,artifact.version));
      body.append(name,meta);row.append(body,button);list.append(row);
    }
  }
  async function refreshTaskDeliveries(){
    if(taskDeliveryBusy)return;
    taskDeliveryBusy=true;
    try{
      const value=await api("deliveries");
      if(!value||!Array.isArray(value.items)||!Array.isArray(value.problems))throw new Error("任务交付清单格式无效");
      taskDeliveries=value;renderTaskDeliveries();
    }catch(error){
      if(!taskDeliveries){taskDeliveries={items:[],problems:[{reason:String(error.message||error)}]};renderTaskDeliveries();}
      $("taskDeliverySummary").textContent="读取失败";
    }finally{taskDeliveryBusy=false;}
  }
  function renderTaskDeliveryDetail(){
    const detail=taskDeliveryDetail, panel=$("taskDeliveryDetail");
    if(!detail){panel.hidden=true;return;}
    const pending=taskDeliveryPendingPackets.get(detail.task_id);
    panel.hidden=false;
    const task=detail.real_task||{}, artifact=detail.artifact||{}, expected=detail.expected||{}, history=detail.latest_delivery;
    $("taskDeliveryTitleDetail").textContent=task.description||detail.task_id||"任务交付";
    const facts=[
      ["任务编号",detail.task_id], ["工作区",task.workspace], ["授权依据",task.authorization_ref],
      ["成品版本",artifact.version], ["成品位置",artifact.path], ["成品 SHA-256",artifact.sha256],
      ["完成事件",expected.completion_event_id], ["完成正文 SHA-256",expected.result_sha256],
      ["应用记录",Array.isArray(detail.applications)?detail.applications.length:0],
      ["当前交付状态",history?(history.decision==="accept"?"已接受":"已撤回"):"尚无记录"],
      ["冻结依据 SHA-256",detail.expected_sha256]
    ];
    const target=$("taskDeliveryFacts");target.replaceChildren();
    for(const [label,value] of facts){if(value===undefined||value===null||value==="")continue;const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=typeof value==="object"?JSON.stringify(value):String(value);target.append(dt,dd);}
    $("taskDeliveryResult").textContent=typeof detail.result==="string"?detail.result:"任务完成内容不可用";
    $("taskDeliveryApplications").textContent=JSON.stringify((detail.applications||[]).map(app=>({
      application_id:app.application_id,candidate_id:app.candidate_id,candidate_sha256:app.candidate_sha256,
      artifact:app.artifact,application_location:app.application_location,steps_applied:app.steps_applied,
      deviations:app.deviations,reuse_assessment:app.reuse_assessment
    })),null,2);
    $("taskDeliveryHistoryBox").hidden=!history;
    $("taskDeliveryHistory").textContent=history?JSON.stringify({event_id:history.event_id,decision:history.decision,
      message_id:history.message_id,delivery_record:history.delivery_record,created_at:history.created_at},null,2):"";
    const allowed=Array.isArray(detail.allowed_decisions)?detail.allowed_decisions:[];
    $("taskDeliveryAccept").hidden=!allowed.includes("accept");
    $("taskDeliveryWithdraw").hidden=!allowed.includes("withdraw");
    $("taskDeliveryRetry").hidden=!pending;
    $("taskDeliveryAccept").disabled=!!pending;
    $("taskDeliveryWithdraw").disabled=!!pending;
    $("taskDeliveryMessage").textContent=pending
      ?"上次提交结果不确定。可重试同一提交编号；系统不会自动重发。"
      :"接受或撤回不会替代经验候选的初审、终审或发布。";
  }
  async function openTaskDelivery(taskId,version){
    if(!taskId||(taskDeliveryBusy&&taskDeliverySelection))return;
    const request=++taskDeliveryDetailRequest;
    const pending=taskDeliveryPendingPackets.get(taskId);
    version=pending?.artifact.version||version;
    taskDeliveryDetail=null;taskDeliverySelection=null;
    $("taskDeliveryMessage").textContent="正在核对当前版本与验收记录…";renderTaskDeliveryDetail();
    try{
      const qs="id="+encodeURIComponent(taskId)+(version?"&version="+encodeURIComponent(version):"");
      const detail=await api("delivery?"+qs);
      if(request!==taskDeliveryDetailRequest)return;
      if(detail.task_id!==taskId||!detail.expected||!detail.artifact||!detail.real_task||
         (version&&detail.artifact.version!==version))throw new Error("交付详情与任务清单不匹配");
      taskDeliveryDetail=detail;$("taskDeliveryMessage").textContent="";renderTaskDeliveryDetail();
    }catch(error){
      if(request===taskDeliveryDetailRequest)$("taskDeliveryMessage").textContent="任务依据暂不可读："+String(error.message||error);
    }
  }
  function openTaskDeliveryConfirmation(decision,retry=false){
    if(!taskDeliveryDetail||taskDeliveryBusy)return;
    const detail=taskDeliveryDetail;
    const pending=taskDeliveryPendingPackets.get(detail.task_id);
    if(retry!==Boolean(pending))return;
    taskDeliverySelection={decision:pending?.decision||decision,packet:pending||null};
    const action=taskDeliverySelection.decision, artifact=detail.artifact||{};
    const rows=[["任务编号",detail.task_id],["决定",deliveryActionLabel(action)],["成品版本",artifact.version],
      ["成品 SHA-256",artifact.sha256],["冻结依据 SHA-256",detail.expected_sha256]];
    const dl=$("taskDeliveryConfirmFacts");dl.replaceChildren();
    for(const [label,value] of rows){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=String(value||"—");dl.append(dt,dd);}
    $("taskDeliveryCaution").textContent=action==="withdraw"
      ?"这会记录你撤回此版本的验收，并冲正与该验收绑定的经验复用贡献。此操作只针对当前显示的任务和产品版本。"
      :"这会记录你接受当前显示的任务和产品版本，并可能结算关联的经验复用分。它不会批准或发布经验。";
    $("taskDeliveryFeedback").textContent=retry?"将使用同一个提交编号重试。":"尚未发送；只有再次点确认才会提交。";
    $("taskDeliveryConfirm").textContent=retry?"重试同一提交":"确认并记录";
    $("taskDeliveryConfirm").disabled=false;
    $("taskDeliveryDialog").showModal();
  }
  async function submitTaskDelivery(){
    if(!taskDeliverySelection||!taskDeliveryDetail||taskDeliveryBusy)return;
    const selection=taskDeliverySelection,detail=taskDeliveryDetail;
    const packet=selection.packet||{
      version:1,task_id:detail.task_id,real_task:detail.real_task,artifact:detail.artifact,
      expected:detail.expected,decision:selection.decision,user_selected:true,
      message_id:crypto.randomUUID(),source_ref:"review-center:"+crypto.randomUUID()
    };
    taskDeliveryBusy=true;$("taskDeliveryConfirm").disabled=true;
    $("taskDeliveryFeedback").textContent="正在提交这一个明确选择…";
    const keepPending=(message)=>{
      taskDeliveryPendingPackets.set(packet.task_id,packet);
      selection.packet=packet;
      $("taskDeliveryConfirm").textContent="重试同一提交";
      $("taskDeliveryFeedback").textContent=message+"。重试会沿用同一提交编号；不会自动重发。";
      renderTaskDeliveryDetail();
    };
    const rejectKnown=(message)=>{
      taskDeliveryPendingPackets.delete(packet.task_id);
      taskDeliverySelection=null;
      $("taskDeliveryFeedback").textContent="本次未记录："+message;
      renderTaskDeliveryDetail();
    };
    try{
      let response;
      try{
        response=await fetch("/api/review/delivery",{method:"POST",cache:"no-store",
          headers:{"Content-Type":"application/json","X-Xiaoba-Token":TOKEN},body:JSON.stringify(packet)});
      }catch(error){keepPending("提交结果暂不确定："+String(error.message||error));return;}
      let result;
      try{result=await response.json();}
      catch(error){
        if(!response.ok&&response.status<500)rejectKnown("提交被拒绝（HTTP "+response.status+"）");
        else keepPending("提交结果暂不确定：返回内容无法解析（HTTP "+response.status+"）");
        return;
      }
      if(!response.ok){
        const errorMessage=result&&typeof result==="object"?result.error:"";
        if(response.status>=500)keepPending("提交结果暂不确定："+(errorMessage||"HTTP "+response.status));
        else rejectKnown(errorMessage||"提交被拒绝（HTTP "+response.status+"）");
        return;
      }
      if(!result||typeof result!=="object"){
        keepPending("提交结果暂不确定：服务器没有返回有效状态");
      }else if(result.ok){
        taskDeliveryPendingPackets.delete(packet.task_id);
        taskDeliverySelection=null;
        $("taskDeliveryDialog").close();
        $("taskDeliveryMessage").textContent=(result.receipt&&result.receipt.decision==="withdraw"?"撤回已记录。":"交付已记录。")+
          " 经验批准与发布仍需单独裁决。";
        try{await refreshTaskDeliveries();await openTaskDelivery(detail.task_id,detail.artifact.version);}
        catch(error){$("taskDeliveryMessage").textContent+=" 状态刷新失败："+String(error.message||error);}
      }else if(result.pending===true){
        keepPending("本次尚未完成："+(result.error||"可重试"));
      }else if(result.pending===false){
        rejectKnown(result.error||"交付决定被拒绝");
      }else{
        keepPending("提交结果暂不确定：服务器没有明确返回是否已记录");
      }
    }finally{taskDeliveryBusy=false;$("taskDeliveryConfirm").disabled=!taskDeliverySelection;}
  }
  $("taskDeliveryRefresh").addEventListener("click",refreshTaskDeliveries);
  $("taskDeliveryAccept").addEventListener("click",()=>openTaskDeliveryConfirmation("accept"));
  $("taskDeliveryWithdraw").addEventListener("click",()=>openTaskDeliveryConfirmation("withdraw"));
  $("taskDeliveryRetry").addEventListener("click",()=>openTaskDeliveryConfirmation(null,true));
  $("taskDeliveryConfirm").addEventListener("click",submitTaskDelivery);
  ["taskDeliveryDialogClose","taskDeliveryCancel"].forEach(id=>$(id).addEventListener("click",()=>$("taskDeliveryDialog").close()));

  /* ---- 候选正文：先整体转义，再做有限的 Markdown 排版，不执行任何 HTML ---- */
  function inline(t){
    return esc(t).replace(/`([^`]+)`/g,"<code>$1</code>").replace(/\*\*([^*]+)\*\*/g,"<b>$1</b>").replace(/&lt;br\s*\/?&gt;/g,"<br>");
  }
  function markdown(src){
    const lines = String(src||"").replace(/\r\n?/g,"\n").split("\n"), out=[]; let i=0;
    const cells = l=>l.trim().replace(/^\||\|$/g,"").split("|").map(c=>c.trim());
    while(i<lines.length){
      const l=lines[i];
      if(/^\s*```/.test(l)){ const buf=[]; i++; while(i<lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]); i++; out.push("<pre><code>"+esc(buf.join("\n"))+"</code></pre>"); continue; }
      if(!l.trim()){ i++; continue; }
      const h=l.match(/^(#{1,4})\s+(.*)$/); if(h){ out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
      if(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)){ out.push("<hr>"); i++; continue; }
      if(/^\s*\|/.test(l) && i+1<lines.length && /^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i+1]) && lines[i+1].includes("-")){
        const head=cells(l); i+=2; const rows=[]; while(i<lines.length && /^\s*\|/.test(lines[i])) rows.push(cells(lines[i++]));
        out.push("<table><thead><tr>"+head.map(c=>"<th>"+inline(c)+"</th>").join("")+"</tr></thead><tbody>"+rows.map(r=>"<tr>"+r.map(c=>"<td>"+inline(c)+"</td>").join("")+"</tr>").join("")+"</tbody></table>"); continue;
      }
      const li=/^\s*([-*+]|\d+[.)])\s+/;
      if(li.test(l)){ const ordered=/^\s*\d/.test(l), items=[];
        while(i<lines.length && (li.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))){
          if(li.test(lines[i])) items.push(lines[i].replace(li,"")); else items[items.length-1]+="\n"+lines[i].trim(); i++; }
        out.push((ordered?"<ol>":"<ul>")+items.map(x=>"<li>"+inline(x).replace(/\n/g,"<br>")+"</li>").join("")+(ordered?"</ol>":"</ul>")); continue; }
      const para=[]; while(i<lines.length && lines[i].trim() && !/^(#{1,4})\s|^\s*```|^\s*\||^\s*(-{3,}|\*{3,})\s*$/.test(lines[i]) && !li.test(lines[i])) para.push(lines[i++]);
      if(para.length) out.push("<p>"+para.map(inline).join("<br>")+"</p>"); else { out.push("<p>"+inline(l)+"</p>"); i++; }
    }
    return out.join("");
  }

  /* ---- 待核对的保留文件：只列出，处理必须由用户逐个点选并二次确认 ---- */
  let kept=null, keptAsk=null, keptBusy=false, keptError={}, keptAt=0;
  const baseName = p=>String(p||"").split("/").pop();
  const sizeText = n=>n==null? "" : n<1024? n+" 字节" : n<1048576? (n/1024).toFixed(1)+" KB" : (n/1048576).toFixed(1)+" MB";
  function renderKept(){
    const box=$("rcKept"), items=(kept&&kept.items)||[], warn=$("rcKeptWarn");
    const trouble = kept && (!kept.enumerationOk || (kept.anomalies||[]).length || (kept.problems||[]).length);
    box.hidden = !(items.length || trouble);
    $("rcKeptCount").textContent = items.length;
    warn.hidden = !trouble;
    if(trouble){ const why=[...(kept.anomalies||[]), ...(kept.problems||[])].slice(0,3).join("；");
      warn.textContent = "保留文件清单没有核对完整" + (why? "："+why : "，请在终端运行 xiaoba memory-retained-list 查看"); }
    $("rcKeptList").innerHTML = items.map((it,i)=>{
      const asking = keptAsk && keptAsk.path===it.path, action = it.exists? "trash" : "confirm_deleted";
      const verb = it.exists? "移到废纸篓" : "标记为我已删除";
      const acts = asking
        ? `<span class="ask">${it.exists? "把这个文件移到废纸篓？" : "确认这个文件是你自己删掉的？"}</span>`+
          `<button class="btn primary" type="button" data-kept-go="${i}" ${keptBusy?"disabled":""}>${keptBusy?"处理中…":"确认"}</button>`+
          `<button class="btn" type="button" data-kept-cancel="1" ${keptBusy?"disabled":""}>取消</button>`
        : `<button class="btn" type="button" data-kept-ask="${i}" data-kept-action="${action}" ${keptBusy?"disabled":""}>${verb}</button>`;
      const meta = [it.operation, it.recordedAt && it.recordedAt!=="unknown"? it.recordedAt : "", sizeText(it.sizeBytes), it.task && it.task!=="unknown"? "任务 "+it.task : ""].filter(Boolean).join(" · ");
      return `<li><div><div class="name">${esc(baseName(it.originalPath)||baseName(it.path))}</div>`+
        `<p class="why">${esc(it.reason)}${it.exists? "" : "；现在这个位置已经没有文件"}</p>`+
        `<p class="where"><b>原位置</b> ${esc(it.originalPath||"—")}<br><b>保留在</b> ${esc(it.path||"—")}${meta? "<br>"+esc(meta) : ""}</p></div>`+
        `<div class="acts">${acts}</div>`+
        (keptError[it.path]? `<p class="err" role="alert">${esc(keptError[it.path])}</p>` : "")+`</li>`;
    }).join("");
  }
  async function loadKept(force){
    if(!TOKEN || keptBusy || (!force && Date.now()-keptAt<60000)) return;
    keptAt = Date.now();
    try{ kept = await api("retained"); }
    catch(e){ if(!kept) return; }   // 读不到清单时保持上一次看到的内容，不凭空清空
    if(keptAsk && !(kept.items||[]).some(x=>x.path===keptAsk.path)) keptAsk=null;
    renderKept(); if(state) badge(state.items.length);
  }
  $("rcKeptList").addEventListener("click", async ev=>{
    const b = ev.target.closest("button"); if(!b || keptBusy || !kept) return;
    if(b.dataset.keptCancel){ keptAsk=null; renderKept(); return; }
    if(b.dataset.keptAsk!==undefined){ const it=kept.items[+b.dataset.keptAsk]; if(!it) return;
      keptAsk={path:it.path, action:b.dataset.keptAction}; delete keptError[it.path]; renderKept(); return; }
    if(b.dataset.keptGo!==undefined && keptAsk){
      const job=keptAsk; keptBusy=true; renderKept();
      try{ await api("retained/resolve", {path:job.path, action:job.action}); delete keptError[job.path]; }
      catch(e){ keptError[job.path]=e.message; }
      keptBusy=false; keptAsk=null; await loadKept(true);
    }
  });

  /* ---- 冻结任务：只列出；恢复必须由用户逐个点选，并在确认框里核对任务编号与冻结哈希。不重试，不批量 ---- */
  let frozen=null, fzAt=0, fzBusy=false, fzJob=null, fzError="";
  const fzProbe = f=>f.probe==="healthy"? "这一侧现在探测合格" : "恢复前要在 Agent 池给这个节点重新“测试运行”并通过";
  const fzSides = list=>list.length? list.join("、") : "未记录";
  function renderFrozen(){
    const items=(frozen&&frozen.items)||[], count=$("rcFrozenCount"), warn=$("rcFrozenWarn"), list=$("rcFrozenList");
    count.textContent = items.length; count.className = "pill num "+(items.length? "warn" : "idle");
    $("rcFrozenLede").hidden = !items.length;
    const trouble = fzError? "读不到冻结任务清单："+fzError
      : frozen && !frozen.complete? "任务较多，冻结任务可能没有列全；请在终端运行 xiaoba status --json 核对" : "";
    warn.hidden = !trouble; warn.textContent = trouble;
    if(!frozen){ list.innerHTML = `<li class="none">${fzError? "暂时无法显示。" : "正在读取…"}</li>`; return; }
    if(!items.length){ list.innerHTML = '<li class="none">现在没有冻结任务。</li>'; return; }
    list.innerHTML = items.map((it,i)=>{
      const fails = it.failures.length? it.failures.map(f=>`<b>${esc(f.agent)}</b> 失败${f.failureClass? "（"+esc(f.failureClass)+"）" : ""}：${esc(f.error||"未记录原因")}`+
        `<br>${f.outputBudget? "单轮输出预算耗尽：任务过大，建议拆小后重新提交，而不是原样恢复" : esc(fzProbe(f))}`).join("<br>") : esc(it.problem||"没有记录是哪一侧失败");
      const kept = it.succeeded.length? `${esc(it.succeeded.join("、"))}，${it.hasReceipt? "有执行回执（"+it.receipts+" 份）" : "没有执行回执"}` : "没有记录";
      const notes = [it.legacy? "旧版本留下的待恢复记录" : "", it.resumeGeneration? "已恢复过 "+it.resumeGeneration+" 次" : "", it.failures.length? it.problem : ""].filter(Boolean).join(" · ");
      return `<li><div><div class="name">${esc(it.title)}</div><p class="why">${fails}</p>`+
        `<p class="where"><b>任务编号</b> ${esc(it.id)}<br><b>冻结时间</b> ${esc(when(it.frozenAt))}${it.failureClass? " · 失败类型 "+esc(it.failureClass) : ""}`+
        `<br><b>成功侧</b> ${kept}<br><b>冻结哈希</b> ${esc(it.freezeHash||"—")}${notes? "<br>"+esc(notes) : ""}</p></div>`+
        `<div class="go"><button class="btn" type="button" data-fz="${i}" ${it.freezeHash? "" : "disabled"}>恢复</button>`+
        `<button class="btn" type="button" data-fza="${i}" ${it.freezeHash? "" : "disabled"}>放弃</button></div></li>`;
    }).join("");
  }
  async function loadFrozen(force){
    if(!TOKEN || fzBusy || (!force && Date.now()-fzAt<30000)) return;
    fzAt = Date.now();
    try{ frozen = await api("frozen"); fzError=""; }
    catch(e){ fzError = e.message==="not found"? "这个功能在监看中心重启后可用" : e.message; }   // 读不到清单时保持上一次看到的内容，不凭空清空
    renderFrozen(); if(state) badge(state.items.length);
  }
  function fzPhase(done){ $("fzCancel").hidden=done; $("fzGo").hidden=done; $("fzDone").hidden=!done; $("fzGo").disabled=fzBusy; $("fzCancel").disabled=fzBusy; $("fzClose").disabled=fzBusy; }
  function fzClose(){ if(fzBusy) return; $("fzDialog").close(); fzJob=null; loadFrozen(true); }
  $("rcFrozenList").addEventListener("click", ev=>{
    const b = ev.target.closest("button[data-fz],button[data-fza]"); if(!b || fzBusy || !frozen) return;
    const abandon = b.hasAttribute("data-fza");
    const it = frozen.items[+(abandon? b.dataset.fza : b.dataset.fz)]; if(!it || !it.freezeHash) return;
    fzJob = {taskId:it.id, freezeHash:it.freezeHash, mode: abandon? "abandon" : "resume"};
    if(abandon){
      $("fzTitle").textContent = "放弃这个冻结任务？";
      $("fzFacts").innerHTML = [["任务编号", it.id], ["任务", it.title], ["冻结哈希", it.freezeHash], ["冻结时间", when(it.frozenAt)]]
        .map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
      $("fzOnly").textContent = "任务结束，不再运行。已有的结论、回执和冻结记录都保留；以后还要做，就重新提交一个任务。";
      $("fzCaution").hidden = true; $("fzCaution").textContent = "";
      $("fzFeedback").textContent = "放弃前由小八复核冻结哈希，与页面上显示的不一致就会拒绝。";
      $("fzRaw").hidden = true; $("fzRaw").textContent = "";
      $("fzGo").textContent = "确认放弃这一个";
      fzPhase(false); $("fzDialog").showModal();
      return;
    }
    $("fzGo").textContent = "确认恢复这一个";
    const failed = it.failures.map(f=>f.agent);
    $("fzTitle").textContent = "恢复这个冻结任务？";
    $("fzFacts").innerHTML = [["任务编号", it.id], ["任务", it.title], ["冻结哈希", it.freezeHash], ["冻结时间", when(it.frozenAt)]]
      .map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
    $("fzOnly").textContent = `只补跑失败侧（${fzSides(failed)}）。成功侧（${fzSides(it.succeeded)}）的结果与回执保持不变，不会重跑。`;
    const cautions = [];
    if(it.failures.some(f=>f.outputBudget)) cautions.push("这次失败是单轮输出预算耗尽：原样恢复会把同一个过大的任务再发一次，很可能再次超限，建议拆小后重新提交。");
    if(it.failures.some(f=>!f.outputBudget && f.probe!=="healthy")) cautions.push("失败的节点要在冻结之后重新测试通过，否则这次恢复会被拒绝；先到 Agent 池点“测试运行”。");
    if(it.problem) cautions.push(it.problem+"。");
    $("fzCaution").hidden = !cautions.length; $("fzCaution").textContent = cautions.join(" ");
    $("fzFeedback").textContent = "恢复前由小八复核冻结哈希、成功侧执行回执与失败侧健康，缺任何一项都会拒绝并说明原因。";
    $("fzRaw").hidden = true; $("fzRaw").textContent = "";
    fzPhase(false); $("fzDialog").showModal();
  });
  $("fzGo").addEventListener("click", async ()=>{
    if(fzBusy || !fzJob) return;
    const abandon = fzJob.mode === "abandon", verb = abandon? "放弃" : "恢复";
    fzBusy=true; fzPhase(false); $("fzGo").textContent="正在"+verb+"…";
    let data=null, text="";
    try{
      const res = await fetch(abandon? "api/review/frozen/abandon" : "api/review/frozen/resume", {method:"POST", cache:"no-store",
        headers:{"Content-Type":"application/json","X-Xiaoba-Token":TOKEN}, body:JSON.stringify({taskId:fzJob.taskId, freezeHash:fzJob.freezeHash})});
      try{ data = await res.json(); }catch(e){ text = "返回内容无法解析（HTTP "+res.status+"）。请刷新清单确认这个任务现在的状态，不要直接重复"+verb+"。"; }
    }catch(e){ text = "没有收到回执："+e.message+"。请刷新清单确认这个任务现在的状态，不要直接重复"+verb+"。"; }
    if(data && data.ok && abandon){
      $("fzTitle").textContent = "已放弃";
      text = "任务已结束，不会再运行；记录都保留着。";
    }else if(data && data.ok){
      $("fzTitle").textContent = "已恢复";
      text = "任务已回到队列，等待 Worker 补跑失败侧"+(data.resumeGeneration? "（第 "+data.resumeGeneration+" 次恢复）" : "")+"。"+(data.warning? "\n"+data.warning : "");
    }else if(data){
      $("fzTitle").textContent = "没有"+verb;
      text = (data.error||"命令行拒绝了这次"+verb)+(data.detail? "\n"+data.detail : "");
    }else $("fzTitle").textContent = "结果不确定";
    $("fzFeedback").textContent = text;
    const raw = data && (data.result || (data.ok? data : null));   // 命令行返回的内容原样显示
    $("fzRaw").hidden = !raw; $("fzRaw").textContent = raw? JSON.stringify(raw, null, 2) : "";
    fzBusy=false; $("fzGo").textContent="确认"+verb+"这一个"; fzPhase(true);
  });
  ["fzCancel","fzClose","fzDone"].forEach(id=>$(id).addEventListener("click", fzClose));
  $("fzDialog").addEventListener("cancel", ev=>{ ev.preventDefault(); fzClose(); });
  Tabs.onChange(name=>{ if(name==="review") loadFrozen(true); });
  document.addEventListener("visibilitychange",()=>{ if(Tabs.current==="review" && !document.hidden) loadFrozen(true); });
  setInterval(()=>{ if(Tabs.current==="review" && !document.hidden && !$("fzDialog").open) loadFrozen(); }, 30000);

  /* ---- 渲染 ---- */
  function setConn(level, text){ $("rcDot").className="dot "+level; $("rcConn").textContent=text; }
  // 卡07 C07-03：角标基于同一全局阶段投影 state.items，忽略各页传入的局部数量 n，
  // 消除总览/裁决切页时角标跳变。kept、frozen 来自全局接口，是统一待处理队列的一部分。
  function stageBadgeCount(st, kept, frozen, n){
    const pending=(st&&Array.isArray(st.items))?st.items.length:(n||0);
    const keptN=(kept&&Array.isArray(kept.items))?kept.items.length:0;
    const frozenN=(frozen&&Array.isArray(frozen.items))?frozen.items.length:0;
    return pending+keptN+frozenN;
  }
  let badgeSnapshot=null, badgeReadSeq=0, badgeLoading=false;
  function badge(){
    if(!badgeSnapshot) return;
    const b=$("tabBadge"), all=stageBadgeCount(badgeSnapshot.state, badgeSnapshot.kept, badgeSnapshot.frozen, 0);
    b.hidden=!all; b.textContent=all;
  }
  async function refreshBadge(){
    if(!TOKEN || document.hidden || badgeLoading) return;
    badgeLoading=true;
    const mine=++badgeReadSeq;
    try{
      const [nextState,nextKept,nextFrozen]=await Promise.all([api("state"),api("retained"),api("frozen")]);
      if(mine!==badgeReadSeq) return;
      if(![nextState,nextKept,nextFrozen].every(x=>x&&Array.isArray(x.items))) return;
      badgeSnapshot={state:nextState,kept:nextKept,frozen:nextFrozen};
      badge();
    }catch(e){ /* Keep the last complete global snapshot if a source is unavailable. */ }
    finally{ if(mine===badgeReadSeq) badgeLoading=false; }
  }
  function steps(n){ [1,2,3].forEach(k=>$("rcStep"+k).classList.toggle("on", k===n)); }

  function renderState(){
    const sync=state.sync||{};
    setConn(sync.ok?"ok":"warn", `AgentOS ${state.release||""} 已连接 · `+(sync.ok? `第二大脑已同步 ${sync.at||""}` : `第二大脑待同步：${sync.error||"处理中"}`));
    $("rcRetrySync").hidden = !!sync.ok;
    const orphans=state.orphans||[], holds=state.holds||0, note=$("rcNotice");
    note.className="rc-notice"; note.hidden = !(orphans.length||holds);
    if(!note.hidden) note.innerHTML = `另有 ${orphans.length} 份未注册文件、${esc(holds)} 条格式约束暂存，需要核查；未自动加入审批。`+
      (orphans.length? `<details><summary>查看未注册文件标识</summary>${orphans.map(n=>`<div class="num">${esc(n)}</div>`).join("")}</details>` : "");
    $("rcQueueCount").textContent = state.items.length; badge(state.items.length);
    renderList(); counts();
  }
  function renderList(){
    if(!state) return;
    const q=$("rcSearch").value.trim().toLowerCase();
    const items=state.items.filter(x=>(x.title+x.task_id+x.status_label).toLowerCase().includes(q));
    const returned=items.filter(x=>x.status==="revision_requested"), current=items.filter(x=>x.status!=="revision_requested");
    // 序号是在全部待审队列里的位置（后端按登记时间排序），搜索过滤时不变。
    const seq=new Map(state.items.map((x,i)=>[x.task_id,i+1])), width=String(state.items.length).length;
    // 上次退回时间取本轮队列快照里的审核记录；没有记录时仍显示提示，时间留空由详情补足。
    const revTimes={}; (state.reviews||[]).forEach(r=>{ if(r.decision==="revise") revTimes[r.task_id]=r.created_at; });
    const row=x=>{ const n=seq.get(x.task_id), isReturned=x.status==="revision_requested", rt=isReturned? revTimes[x.task_id] : null; return `<li><button class="rc-item" type="button" data-id="${esc(x.task_id)}" aria-current="${x.task_id===selected}" aria-label="第 ${n} 条：${esc(x.title)}">
        <span class="rc-seq" aria-hidden="true">#${String(n).padStart(width,"0")}</span>
        <strong>${esc(x.title)}</strong>
        <span class="meta"><span class="pill warn">${esc(x.status_label)}</span><span class="num">${esc(day(x.created_at))}</span>${isReturned? `<span class="pill busy">已退回，等作者修改${rt? " · "+esc(day(rt)) : ""}</span>` : ""}${saved[x.task_id]? `<span class="pill ok">已加入 · ${esc(decisionName(saved[x.task_id]))}</span>` : ""}</span>
      </button></li>`; };
    const groupOpen=q&&returned.length? " open" : returnedExpanded? " open" : "";
    const returnedGroup=returned.length? `<li class="rc-returned-group"><details data-returned-list${groupOpen}><summary>已退回，待作者修改（${returned.length}${q?" 条匹配":""}）</summary><ul class="rc-returned-items">${returned.map(row).join("")}</ul></details></li>` : "";
    const content=current.map(row).join("")+returnedGroup;
    $("rcList").innerHTML = content || `<li><p class="rc-empty">${state.items.length? "暂无匹配的待审内容" : "所有待审已处理"}</p></li>`;
    const group=$("rcList").querySelector("details[data-returned-list]");
    if(group) group.addEventListener("toggle",()=>{ returnedExpanded=group.open; });
  }
  function counts(){
    const n=Object.keys(saved).length;
    $("rcProgress").textContent = packet? `待核对回执 · ${packet.items.length} 项` : n+" / "+(state? state.items.length : 0);
    $("rcReview").disabled = (!n && !packet) || submitting;
  }
  function blank(title, body){
    detail=null;
    $("rcTrialPanel").hidden=true; $("rcTrialEvidence").textContent="";
    $("rcTitle").textContent=title; $("rcBody").textContent=body; $("rcStage").textContent="—"; $("rcCategory").textContent=""; $("rcSubtitle").textContent="";
    $("rcPrompt").textContent=""; $("rcIdentity").textContent=""; $("rcRaw").textContent=""; $("rcHistory").textContent="";
    $("rcEvidence").className="pill idle"; $("rcEvidence").textContent="尚未读取证据"; $("rcBrain").hidden=true;
    document.querySelectorAll(".choice").forEach(b=>{ b.disabled=true; b.setAttribute("aria-pressed","false"); });
    $("rcReturned").hidden=true;
    document.querySelector('.choice[data-decision="revise"] small').textContent="提出具体意见，修改后重新审核";
    $("rcNote").value=""; $("rcNote").disabled=true; $("rcNoteCount").textContent="0 / "+NOTE_MAX; $("rcSave").disabled=true; $("rcSaveHint").textContent="尚未提交";
  }
  function draftUI(){
    const d=drafts[selected]; if(!detail||!d) return;
    const open = (detail.hash_matches || detail.entry_type==="reuse_change" && detail.allowed_decisions.includes("reject")) && detail.allowed_decisions.length>0;
    const complete = !!d.decision && (d.decision==="approve" || d.note.trim().length>0);
    const inList = saved[selected], dirty = inList && (inList.decision!==d.decision || inList.note!==d.note);
    $("rcSave").disabled = !open || !complete || submitting || !!packet || (inList && !dirty);
    $("rcSave").textContent = inList? "更新裁决清单" : "加入裁决清单";
    $("rcNoteCount").textContent = d.note.length+" / "+NOTE_MAX;
    $("rcSaveHint").textContent = packet? "先核对本次提交的回执，再加入新的裁决"
      : !detail.allowed_decisions.length? "这份候选当前不在待审状态"
      : !detail.hash_matches? "证据不一致，已暂停裁决"
      : dirty? "有改动尚未更新到清单"
      : inList? "已保存到本次清单，尚未提交"
      : d.decision && !complete? "请先填写意见" : "尚未提交";
  }

  async function select(id){
    selected=id; const mine=++ticket; renderList();
    blank("正在读取…","正在读取完整候选…");
    try{
      const x = await api("candidate?id="+encodeURIComponent(id));
      if(mine!==ticket) return;
      detail=x;
      if(drafts[id] && !same(drafts[id].expected, x.expected)) forget(id);   // 候选已变化，旧草稿作废
      const d = drafts[id] || (drafts[id]={decision:null, note:"", expected:x.expected});
      $("rcTitle").textContent = x.title;
      $("rcStage").textContent = x.entry_type==="reuse_change"? "复用改动终审" : x.entry_type==="unused_batch"? "长期未用确认（合批）" : (x.entry_type==="reuse_change"?"复用改动":x.entry_type==="unused_batch"?"长期未用":STAGE[x.stage]||x.stage);
      $("rcCategory").textContent = (x.entry_type? "正式经验裁决 " : "候选 ")+id.slice(0,8);
      const returned = x.status==="revision_requested";
      const revRows = (x.reviews||[]).filter(r=>r.decision==="revise");
      const lastRev = revRows.length? revRows[revRows.length-1] : null;
      const revWhen = lastRev? "上次退回 "+when(lastRev.created_at) : "";
      $("rcSubtitle").textContent = returned? "已退回，等作者修改"+(revWhen? " · "+revWhen : "") : (x.status_label||"");
      $("rcReturned").hidden = !returned;
      if(returned) $("rcReturned").textContent = "已退回，等作者修改："+(revWhen? revWhen+"。" : "")+"这份正文和上次退回时相同，不能重复退回；仍然可以驳回。"+(lastRev&&lastRev.note? " 上次意见："+lastRev.note : "");
      const trial=x.experience_validation||{};
      const validationUi=validationPresentation(trial,x.status,x.stage);
      if(validationUi.stageLabel) $("rcStage").textContent=validationUi.stageLabel;
      $("rcTrialPanel").hidden=!trial.kind&&!trial.validation_basis;
      $("rcTrialTitle").textContent=validationUi.panelTitle;
      const stateLabels={application_delivery_required:"实际应用或当前成品验收证据待补齐",awaiting_delivery:"待用户初审",awaiting_trial:"等待新试验",awaiting_evidence:"待补结构化方法主张与核验类别",trial_running:"独立验证进行中",trial_failed:"验证未通过",ready_for_final:"验证通过，等待用户终审",legacy_manual:"旧流程候选，保留原人工裁决；未宣称已做新试验",not_required:"规则或偏好，两次人工裁决，不强制试验"};
      $("rcTrialEvidence").textContent=({method:"分类：方法／参数／修复经验",rule:"分类：用户偏好／明确规则",legacy:"分类：历史未分类候选"}[trial.kind]||"分类未确认")+"\n"+(trial.disagreement?"第二轮有分歧，等待用户终审":validationUi.stateLabel||stateLabels[trial.state]||"尚无验证信息")+"\n"+(trial.disagreement?"有分歧：第二轮审查有人通过、有人未通过（审查方 1 "+trial.dsh_verdict+"，审查方 2 "+trial.peer_verdict+"；同侧有一人未通过即记未通过）。真实执行已通过，发布或拒绝由你决定，各审查员原文见下。\n":"")+((trial.reasons||[]).length?trial.reasons.join("；")+"\n":"")+(trial.trials||[]).map(t=>
        (t.round?"第 "+t.round+" 轮 · ":"")+"验证任务："+t.task_id+" · "+(t.disagreement?"有分歧":t.passed?"通过":t.status==="failed"?"未通过":"尚未完成")+"\n任务卡："+JSON.stringify(t.card,null,2)+"\n真实执行回执："+JSON.stringify(t.execution,null,2)+"\n"+(t.reviews||[]).map(r=>r.agent+" · "+r.status+"\n"+r.output).join("\n\n")).join("\n\n");
      if(trial.validation_basis) $("rcTrialEvidence").textContent += "\n\n"+basisEvidenceText(trial.validation_basis);
      $("rcTrialRequest").hidden=!validationUi.showTrialRequest;
      $("rcTrialRequest").textContent=validationUi.trialRequestLabel;
      if((trial.experience_loop?.applications||[]).length) $("rcTrialEvidence").textContent += "\n\n真实任务应用、用户验收与原卡回流（成品采用不推断每条方法的因果贡献）：\n"+experienceLoopText(trial);

      $("rcPrompt").textContent = x.prompt||"";
      $("rcIdentity").textContent = "候选编号：" + id + "\n完整证据哈希：" + x.evidence_sha256;
      $("rcBody").innerHTML = markdown(String(x.body||"").replace(/^---\s*\n[\s\S]*?\n---\s*\n/,""));
      $("rcBody").scrollTop = 0;
      $("rcRaw").textContent = x.body||"";
      $("rcHistory").textContent = (x.reviews||[]).length
        ? "历次审核\n" + x.reviews.map(r=>`${r.stage==="delivery"?"初审":"终审"} · ${DECISION[r.decision]||r.decision} · ${when(r.created_at)}\n回执：${r.id}\n意见：${r.note||"无"}`).join("\n\n")
        : "尚无审核记录";
      $("rcEvidence").className = "pill "+(x.hash_matches?"ok":"down");
      $("rcEvidence").textContent = x.hash_matches? "证据校验通过 · 完整候选已读取" : "证据不一致，已暂停裁决";
      const safeLink = typeof x.obsidian_url==="string" && x.obsidian_url.startsWith("obsidian://");
      $("rcBrain").hidden = !safeLink; if(safeLink) $("rcBrain").href = x.obsidian_url;
      const approve = document.querySelector('.choice[data-decision="approve"]');
      approve.querySelector("strong").textContent = x.stage==="delivery"? "初审通过" : "终审通过并发布";
      approve.querySelector("small").textContent = validationUi.approveHint;
      const unused = x.entry_type==="unused_batch";
      const reject = document.querySelector('.choice[data-decision="reject"]');
      reject.querySelector("strong").textContent = unused? "删除（退役归档）" : x.entry_type? "驳回／关闭请求" : "不予采用";
      reject.querySelector("small").textContent = unused? "移出活动目录、摘索引链接，可找回" : x.entry_type? "关闭本次请求，源经验保持不变" : "不采用这条经验";
      if(unused){ approve.querySelector("strong").textContent="保留"; approve.querySelector("small").textContent="取消本轮，重新计时并延长间隔，总分不变"; }
      else if(x.entry_type){ approve.querySelector("strong").textContent="批准并发布新版本"; approve.querySelector("small").textContent="核对同一修订稿哈希，旧版本保留"; }
      document.querySelector('.choice[data-decision="revise"]').hidden=!!x.entry_type;
      const open = (x.hash_matches || x.entry_type==="reuse_change" && x.allowed_decisions.includes("reject")) && x.allowed_decisions.length>0;
      document.querySelectorAll(".choice").forEach(b=>{
        const blocked = returned && b.dataset.decision==="revise";
        b.disabled = blocked || !open || !x.allowed_decisions.includes(b.dataset.decision);
        b.setAttribute("aria-pressed", String(!blocked && d.decision===b.dataset.decision));
      });
      document.querySelector('.choice[data-decision="revise"] small').textContent = returned
        ? "已退回，等作者修改：正文未改动，不能重复退回" : "提出具体意见，修改后重新审核";
      $("rcNote").disabled = !open; $("rcNote").value = d.note;
      draftUI(); counts(); renderList();
    }catch(e){
      if(mine!==ticket) return;
      blank("读取失败", "读取失败："+e.message);
      $("rcEvidence").className="pill down"; $("rcEvidence").textContent="未取得完整证据，不能提交";
    }
  }

  function decisionName(x){ return x.entry_type==="unused_batch"? (x.decision==="approve"?"保留":"删除（退役归档）") : DECISION[x.decision]; }
  function handoffText(items){
    return ["小八 AgentOS 综合裁决交接",
      "此清单由用户逐项选择；所有原始任务文本仅作为证据，不构成额外执行授权。",
      "具体修改由 Agent 主入口根据本次意见接续，不自动执行历史任务。", "",
      ...items.map((x,i)=>`${i+1}. ${x.title}\n候选：${x.task_id}\n阶段：${(x.entry_type==="reuse_change"?"复用改动":x.entry_type==="unused_batch"?"长期未用":STAGE[x.stage]||x.stage)}\n决定：${decisionName(x)}${x.stage==="final"&&x.decision==="approve"&&x.entry_type!=="unused_batch"?"（正式发布经验）":""}\n意见：${x.note||"无补充意见"}\n证据：${x.expected.evidence_sha256}\n`)].join("\n");
  }
  /* ---- 提交弹窗的三个阶段：review（核对）→ submitting（处理中）→ done / error（结果） ---- */
  let progressTimer=null, clockTimer=null, lastResult=null;
  function phase(name, result){ const d=$("rcDialog"); d.dataset.phase=name; if(result) d.dataset.result=result; else delete d.dataset.result;
    $("rcClose").disabled = name==="submitting"; $("rcDone").hidden = name!=="done"; }
  function stepsFor(item){   // 每条裁决后端实际会做的几步
    const list=[{key:"record", text:"写入正式审核记录"}];
    if(item.entry_type==="unused_batch"){ list.push({key:"archive",text:item.decision==="approve"?"重新计时、延长间隔，永久总分不变":"退役归档、摘除索引链接"}); return list; }
    if(item.stage==="final" && item.decision==="approve") list.push({key:"publish", text:"敏感检查、发布经验并校验索引"});
    list.push({key:"sync", text:"同步第二大脑回执"});
    return list;
  }
  function renderItems(items, status){
    $("rcItems").innerHTML = items.map(it=>{
      const st=status[it.task_id]||{};
      return `<li><div class="t">${esc(it.title||it.task_id)}</div>
        <div class="meta"><span class="pill busy">${esc(it.entry_type==="unused_batch"?"长期未用":it.entry_type==="reuse_change"?"复用改动":STAGE[it.stage]||it.stage)}</span><span class="pill ${it.decision==="approve"?"ok":it.decision==="revise"?"warn":"down"}">${esc(decisionName(it))}</span><span class="num">${esc(it.task_id.slice(0,8))}</span></div>
        <ul class="rc-steps">${stepsFor(it).map(s=>`<li class="${esc(st[s.key]||"")}">${esc(s.text)}${st[s.key]==="active"?"…":""}</li>`).join("")}</ul>
        ${st.outcome? `<p class="outcome">${esc(st.outcome)}</p>` : ""}${st.receipt? `<p class="receipt">回执 ${esc(st.receipt)}</p>` : ""}</li>`;
    }).join("");
  }
  function stopTimers(){ clearInterval(progressTimer); clearInterval(clockTimer); progressTimer=clockTimer=null; progressAttempt++; }

  function submittedItems(){
    return packet? packet.items.map(x=>({...packetDetails[x.task_id],...x})) : Object.values(saved);
  }

  function basisOf(item){
    const basis=item.experience_validation?.validation_basis;
    return item.stage==="final"&&item.decision==="approve"&&basis?.status!=="missing"? basis||null : null;
  }
  function needsBasisConfirmation(item){
    const basis=basisOf(item);
    if(!Array.isArray(basis?.required_checks)) return false;
    if(basis.status==="not_required") return basis.route==="sandbox";
    return ["proposed","confirmed"].includes(basis.status)&&basis.required_checks.includes("real_task_acceptance");
  }
  function basisRequirementsMatch(basis){
    const claims=basis?.method_claims;
    if(!Array.isArray(claims)||!claims.length||claims.some(claim=>!claim||!claim.claim||!["artifact","code"].includes(claim.class))) return false;
    const hasCode=claims.some(claim=>claim.class==="code"), hasArtifact=claims.some(claim=>claim.class==="artifact");
    const expected=hasCode&&hasArtifact? ["real_task_acceptance","sandbox"] : hasCode? ["sandbox"] : ["real_task_acceptance"];
    const actual=basis.required_checks;
    if(!Array.isArray(actual)||actual.length!==expected.length||new Set(actual).size!==actual.length||actual.some(check=>!expected.includes(check))) return false;
    const route=hasCode&&hasArtifact? "combined" : hasCode? "sandbox" : "real_task_acceptance";
    if(basis.route!==route) return false;
    if(expected.includes("real_task_acceptance")&&(!basis.real_task||!basis.application_scope||!basis.artifact||!basis.acceptance_ref||!basis.acceptance_sha256)) return false;
    return true;
  }
  function basisReady(basis){
    return !!basis?.basis_id && !!basis?.basis_sha256 && !!basis?.route && !!basis?.method_scope
      && basisRequirementsMatch(basis) && !!basis.trust_boundary
      && Array.isArray(basis.evidence_items) && basis.evidence_items.length>0
      && basis.evidence_items.every(e=>e && e.label && e.ref && e.sha256);
  }
  function basisApprovalReady(item){
    const basis=basisOf(item), validation=item.experience_validation;
    return needsBasisConfirmation(item)&&basisReady(basis)&&(validation.ready===true
      || (validation.state==="validation_basis_confirmation_required"&&basis.status==="proposed"));
  }
  function basisCheckboxMatches(check,item,basis){
    return check.checked&&check.dataset.taskId===item.task_id
      &&check.dataset.basisId===basis.basis_id&&check.dataset.basisSha256===basis.basis_sha256;
  }
  function basisConfirmationMatches(confirmation,basis){
    return confirmation?.confirmed===true&&confirmation.classification_confirmed===true
      &&confirmation.basis_id===basis.basis_id&&confirmation.basis_sha256===basis.basis_sha256;
  }
  function basisValue(value){
    return typeof value==="string"? value : JSON.stringify(value??"—",null,2);
  }
  function basisClaimClass(value){
    return value==="code"? "代码/命令" : value==="artifact"? "成品/实际工作" : "类别无法识别";
  }
  function basisCheckLabel(value){
    return value==="real_task_acceptance"? "真实任务与用户认可" : value==="sandbox"? "隔离沙箱" : String(value??"未知检查");
  }
  function validationPresentation(trial,status,stage){
    const basis=trial.validation_basis||{}, checks=Array.isArray(basis.required_checks)?basis.required_checks:[];
    const route=basis.route, hasRealTask=checks.includes("real_task_acceptance");
    const legacyTrialStates=["awaiting_trial","trial_running","trial_failed","revalidation_required","validation_paused","awaiting_check","review_rejected"];
    const missingBasis=!basis.status||basis.status==="missing";
    const needsSandbox=checks.includes("sandbox")||(missingBasis&&trial.required&&legacyTrialStates.includes(trial.state));
    const basisPending=trial.state==="validation_basis_confirmation_required"&&basis.status==="proposed"&&hasRealTask;
    const panelTitle=route==="real_task_acceptance"? "真实任务证据与应用记录"
      : route==="combined"? "真实任务证据与隔离验证"
      : route==="sandbox"? "隔离验证与独立审查" : "方法核验状态";
    const stateLabel=trial.disagreement? "第二轮有分歧，等待用户终审"
      : basisPending? needsSandbox? "隔离验证已满足，真实任务依据待确认" : "真实任务证据待终审确认"
      : trial.state==="validation_basis_confirmation_required"? "真实任务证据待终审确认" : null;
    let stageLabel=null;
    if(status==="delivery_approved"&&trial.required&&!trial.ready){
      if(basisPending) stageLabel=needsSandbox? "隔离已满足 · 真实依据待确认" : "真实任务证据待确认";
      else if(basis.status==="awaiting_evidence") stageLabel="待补结构化方法主张与核验类别";
      else if(trial.state==="trial_running") stageLabel="隔离验证中";
      else if(trial.state==="trial_failed") stageLabel="验证未通过";
      else if(needsSandbox&&route==="combined") stageLabel="混合路线 · 隔离验证";
      else if(needsSandbox) stageLabel="隔离验证";
      else stageLabel="核验材料待补";
    }
    const showTrialRequest=status==="delivery_approved"&&needsSandbox
      &&["awaiting_trial","revalidation_required"].includes(trial.state);
    let approveHint;
    if(stage==="delivery"){
      approveHint=!trial.required? "进入终审，尚不发布经验"
        : route==="real_task_acceptance"? "终审时一起确认方法主张分类与真实任务依据"
        : route==="combined"? "初审通过后进入隔离验证；终审时一起确认分类和真实任务依据"
        : needsSandbox? "初审通过后进入隔离验证，尚不发布经验"
        : "补齐核验依据后进入终审，尚不发布经验";
    }else if(basisPending){
      approveHint=needsSandbox? "隔离验证已满足；在本次终审确认分类和真实任务证据"
        : "核对真实任务、应用范围、用户认可材料和方法主张分类，并在本次终审确认";
    }else if(trial.required&&!trial.ready){
      approveHint=basis.status==="awaiting_evidence"? "补齐结构化方法主张和证据后才能继续"
        : needsSandbox? "须先完成隔离执行与独立双审" : "真实任务证据尚未就绪，暂不能终审";
    }else{
      approveHint=!missingBasis? "确认方法主张分类后，执行发布与索引验证" : "执行敏感检查、发布与索引验证";
    }
    return {stageLabel,panelTitle,stateLabel,showTrialRequest,trialRequestLabel:route==="combined"? "复制混合路线隔离验证请求" : "复制隔离验证任务请求",approveHint};
  }
  function experienceLoopText(trial){
    const loop=trial.experience_loop||{};
    if(["real_task_acceptance","combined"].includes(trial.validation_basis?.route)){
      return JSON.stringify({applications:loop.applications||[],deliveries:loop.deliveries||[]},null,2);
    }
    return JSON.stringify(loop,null,2);
  }
  function basisEvidenceText(basis){
    const route=basis.route==="real_task_acceptance"? "真实任务证据 · 免隔离试验"
      : basis.route==="sandbox"? "隔离沙箱验证"
      : basis.route==="combined"? "真实任务证据 + 隔离沙箱" : String(basis.route||"未知");
    const evidence=Array.isArray(basis.evidence_items)?basis.evidence_items:[];
    return ["方法核验依据", "路线："+route, "方法范围："+basisValue(basis.method_scope),
      "实际任务："+basisValue(basis.real_task),
      "应用范围与交付："+basisValue({application_id:basis.application_id,application_scope:basis.application_scope,delivery_event_id:basis.delivery_event_id}),
      "成品、执行及认可："+basisValue({artifact:basis.artifact,execution_evidence:basis.execution_evidence,acceptance_ref:basis.acceptance_ref,acceptance_sha256:basis.acceptance_sha256}),
      "证据项：", ...(evidence.length? evidence.map((item,index)=>`${index+1}. ${item.label}\n${item.ref}\n${item.sha256}`) : ["无证据项"]),
      "信任边界："+basisValue(basis.trust_boundary)].join("\n");
  }
  function renderBasisConfirmations(items, priorChecks=[]){
    const root=$("rcBasisConfirmations"), list=$("rcBasisItems");
    const entries=items.filter(basisOf);
    root.hidden=!entries.length;
    list.innerHTML=entries.map(item=>{
      const basis=basisOf(item), required=needsBasisConfirmation(item);
      const checked=packet? basisConfirmationMatches(packet.items.find(x=>x.task_id===item.task_id)?.validation_basis_confirmation,basis)
        : priorChecks.some(check=>basisCheckboxMatches(check,item,basis));
      const evidence=Array.isArray(basis.evidence_items)?basis.evidence_items:[];
      const hasRealTask=Array.isArray(basis.required_checks)&&basis.required_checks.includes("real_task_acceptance");
      const confirmationText=basis.status==="proposed"
        ? "我确认所列方法主张的分类及核验路线正确，且这些材料对应已采用的方法范围。"
        : "我确认所列方法主张的分类及核验路线正确。";
      const controls=basisApprovalReady(item)
        ? `<label class="rc-basis-confirm-row"><input class="rc-basis-confirm" type="checkbox" data-task-id="${esc(item.task_id)}" data-basis-id="${esc(basis.basis_id)}" data-basis-sha256="${esc(basis.basis_sha256)}"${checked?" checked":""}${packet?" disabled":""}><span>${esc(confirmationText)}</span></label>`
        : required? `<p class="rc-basis-trust">核验尚未就绪或证据材料不完整，当前无法确认。</p>`
          : `<p class="rc-basis-trust">依据状态或路线无法识别，当前不能提交终审。</p>`;
      const route=basis.route==="real_task_acceptance"? "真实任务证据 · 免隔离试验"
        : basis.route==="sandbox"? "隔离沙箱验证"
        : basis.route==="combined"? "真实任务证据 + 隔离沙箱"
        : basis.route;
      const status=basis.status==="proposed"? "真实任务依据待确认"
        : basis.status==="confirmed"? "真实任务依据已确认"
        : basis.status==="awaiting_evidence"? "待补证据主张"
        : required? "沙箱核验" : "状态无法识别";
      const claims=Array.isArray(basis.method_claims)?basis.method_claims:[];
      return `<article class="rc-basis-card">
        <div class="meta"><strong>${esc(item.title||item.task_id)}</strong><span class="pill ${basis.status==="confirmed"?"ok":"warn"}">${esc(status)}</span><span class="num">${esc(item.task_id)}</span></div>
        <div><strong>方法范围</strong><pre>${esc(basisValue(basis.method_scope))}</pre></div>
        <div><strong>方法主张与核验类别</strong>${claims.length? `<ul class="rc-basis-evidence">${claims.map(claim=>`<li><strong>${esc(basisClaimClass(claim?.class))}</strong><br>${esc(claim?.claim)}</li>`).join("")}</ul>` : `<p class="rc-basis-trust">缺少结构化方法主张</p>`}</div>
        <div><strong>核验路线</strong><pre>${esc(basisValue(route))}</pre></div>
        <div><strong>所需核验</strong><ul class="rc-basis-evidence">${Array.isArray(basis.required_checks)? basis.required_checks.map(check=>`<li>${esc(basisCheckLabel(check))}</li>`).join("") : `<li>检查路线缺失</li>`}</ul></div>
        ${hasRealTask? `<div><strong>实际任务</strong><pre>${esc(basisValue(basis.real_task))}</pre></div>
        <div><strong>应用范围与交付记录</strong><pre>${esc(basisValue({application_id:basis.application_id,application_scope:basis.application_scope,delivery_event_id:basis.delivery_event_id}))}</pre></div>
        <div><strong>成品、执行及认可材料</strong><pre>${esc(basisValue({artifact:basis.artifact,execution_evidence:basis.execution_evidence,acceptance_ref:basis.acceptance_ref,acceptance_sha256:basis.acceptance_sha256}))}</pre></div>
        <div><strong>采用时间</strong><pre>${esc(basisValue(basis.accepted_at))}</pre></div>
        <p class="rc-basis-trust">真实任务证据确认按本机现有业务信任边界记录，不构成独立的人类身份认证。</p>` : ""}
        <div><strong>依据编号 / 哈希</strong><pre>${esc(basis.basis_id)}\n${esc(basis.basis_sha256)}</pre></div>
        <div><strong>证据项（${evidence.length}）</strong>${evidence.length? `<ul class="rc-basis-evidence">${evidence.map(e=>`<li><strong>${esc(e?.label)}</strong><br>${esc(e?.ref)}<br><code>${esc(e?.sha256)}</code></li>`).join("")}</ul>` : `<p class="rc-basis-trust">没有证据项</p>`}</div>
        <div><strong>信任边界</strong><pre>${esc(basisValue(basis.trust_boundary))}</pre></div>
        ${controls}
      </article>`;
    }).join("");
    syncBasisConfirmationState(items);
  }
  function syncBasisConfirmationState(items){
    const checks=[...$("rcBasisItems").querySelectorAll(".rc-basis-confirm")];
    const missing=items.some(item=>{
      const basis=basisOf(item);
      if(!basis) return false;
      if(!basisApprovalReady(item)) return true;
      if(packet){
        const sent=packet.items.find(x=>x.task_id===item.task_id)?.validation_basis_confirmation;
        return !basisConfirmationMatches(sent,basis);
      }
      return !checks.some(check=>basisCheckboxMatches(check,item,basis));
    });
    const feedback=$("rcFeedback"), prompt="依据材料未就绪或尚未确认方法主张分类，无法提交终审。";
    $("rcSubmit").disabled=!items.length||missing;
    if(missing) feedback.textContent=prompt;
    else if(feedback.textContent===prompt) feedback.textContent="";
    return items.length>0&&!missing;
  }

  // 卡07 C07-03：迟到轮询/旧缓存不得覆盖最新依据详情。
  // 只有当前没有“同一版本实时详情”时，才用 state 快照补 validation。
  function latestValidation(item, current, liveDetail){
    if(current && same(current.expected,item.expected)){
      if(liveDetail && liveDetail.task_id===item.task_id && same(liveDetail.expected,item.expected)){
        return liveDetail.experience_validation;   // 实时详情优先
      }
      return current.experience_validation;
    }
    return item.experience_validation;
  }
  function refreshOpenSummary(previous){
    const dialog=$("rcDialog");
    if(packet||!dialog.open||dialog.dataset.phase!=="review") return;
    Object.values(saved).forEach(item=>{
      const previousItem=previous?.items.find(x=>x.task_id===item.task_id);
      const current=state.items.find(x=>x.task_id===item.task_id);
      // A newly read detail may be newer than an unchanged list projection.
      if(current&&!same(current.experience_validation,previousItem?.experience_validation)){
        item.experience_validation=current.experience_validation;
        if(detail?.task_id===item.task_id&&same(detail.expected,item.expected)) detail.experience_validation=item.experience_validation;
      }
    });
    const items=submittedItems();
    const checks=[...$("rcBasisItems").querySelectorAll(".rc-basis-confirm")];
    $("rcSummaryCount").textContent=`本次 ${items.length} 项裁决。未选择的候选保持原状。`;
    $("rcOutput").value=handoffText(items);
    $("rcSubmit").textContent=`确认提交 ${items.length} 项裁决`;
    renderBasisConfirmations(items, checks);
  }
  function openSummary(){
    if(!packet){
      Object.values(saved).forEach(item=>{
        const current=state?.items.find(x=>x.task_id===item.task_id);
        const live=(detail && detail.task_id===item.task_id)?detail:null;
        item.experience_validation=latestValidation(item, current, live);
      });
    }
    const items=submittedItems(); if(!items.length) return;
    phase("review");
    $("rcDialogTitle").textContent = "核对你的决定。";
    $("rcSummaryCount").textContent = `本次 ${items.length} 项裁决。未选择的候选保持原状。`;
    $("rcOutput").value = handoffText(items);
    $("rcFeedback").textContent = "";
    $("rcCopy").textContent = "复制交接文本";
    $("rcSubmit").textContent = packet? "重试本次提交" : `确认提交 ${items.length} 项裁决`;
    renderBasisConfirmations(items);
    steps(2); $("rcDialog").showModal();
  }
  function closeDialog(){ if(submitting) return; if($("rcDialog").open) $("rcDialog").close(); }

  async function submit(){
    if(submitting || (!packet && !Object.keys(saved).length)) return;
    if(!syncBasisConfirmationState(submittedItems())) return;
    // 重试沿用同一提交编号，裁决中心据此查验，避免重复审核
    if(!packet){
      const items=Object.values(saved).map(({task_id,stage,decision,note,expected,entry_type})=>{
        const item={task_id,stage,decision,note,expected,entry_type};
        const basis=basisOf(saved[task_id]);
        if(needsBasisConfirmation(saved[task_id])){
          item.validation_basis_confirmation={basis_id:basis.basis_id,basis_sha256:basis.basis_sha256,confirmed:true,classification_confirmed:true};
        }
        return item;
      });
      packet = {id:crypto.randomUUID(), items};
      packetReviewBaseline = new Set((state?.reviews||[]).map(r=>r.id));
      packetDetails = Object.fromEntries(Object.values(saved).map(({task_id,title,experience_validation})=>[task_id,{title,experience_validation}]));
    }
    const items = submittedItems();
    const status = Object.fromEntries(items.map(x=>[x.task_id,{record:"active"}]));
    const started = Date.now(), attempt = ++progressAttempt;
    let polling=false;
    submitting=true; steps(3); phase("submitting"); counts(); draftUI();
    $("rcDialogTitle").textContent = "正在提交裁决";
    $("rcSummaryCount").textContent = `共 ${items.length} 项，由裁决后端逐项处理。处理期间请不要关闭或刷新页面。`;
    $("rcProgressTitle").textContent = "已发送，裁决后端正在处理";
    $("rcFeedback").textContent = ""; $("rcSyncLine").textContent = "";
    $("rcElapsed").textContent = "0 秒"; renderItems(items, status);
    clockTimer = setInterval(()=>{ const s=Math.round((Date.now()-started)/1000); $("rcElapsed").textContent = s<60? s+" 秒" : Math.floor(s/60)+" 分 "+(s%60)+" 秒";
      if(s===20) $("rcSyncLine").textContent = "仍在处理。终审发布要做敏感检查、发布和索引校验，可能需要一两分钟。"; }, 1000);
    // 状态接口可确认新审核记录；全局同步时间不能证明本批次发布、索引校验或同步成功。
    progressTimer = setInterval(async()=>{
      if(polling) return;
      polling=true;
      try{
        const live = await api("state");
        if(!submitting || attempt!==progressAttempt) return;
        items.forEach(it=>{ const st=status[it.task_id];
          const recorded=(live.reviews||[]).some(r=>r.id && !packetReviewBaseline.has(r.id)
            && r.task_id===it.task_id && r.stage===it.stage && r.decision===it.decision
            && r.candidate_sha256===it.expected.evidence_sha256);
          if(recorded && st.record!=="ok"){ st.record="ok"; if(stepsFor(it).some(s=>s.key==="publish")) st.publish="active"; else st.sync="active"; }
        });
        renderItems(items, status);
      }catch(e){}finally{ polling=false; }
    }, 1500);
    try{
      const r = await api("decisions", packet);
      stopTimers();
      const returned = Array.isArray(r.items)? r.items : [];
      const ids = new Set(returned.map(x=>x.task_id));
      if(r.id!==packet.id || returned.length!==items.length || ids.size!==items.length
        || items.some(it=>!ids.has(it.task_id)) || returned.some(x=>typeof x.ok!=="boolean" || typeof x.recorded!=="boolean" || (x.recorded && !x.review_id)))
        throw new Error("回执编号或裁决项目不完整，请按原编号重新核对");
      const sync=r.sync||{}; let allOk=true;
      r.items.forEach(x=>{
        const it=items.find(i=>i.task_id===x.task_id)||{}, st=status[x.task_id]||(status[x.task_id]={});
        st.record = x.recorded? "ok" : "fail";
        const confirmed = x.ok===true && x.recorded===true && !x.recovered;
        const publishes = stepsFor(it).some(s=>s.key==="publish");
        if(publishes) st.publish = confirmed && x.status==="published"? "ok" : (x.recorded? "fail" : "skip");
        st.sync = !x.recorded? "skip" : (sync.ok===true? "ok" : "fail");
        st.receipt = x.review_id||"";
        st.outcome = !confirmed? "未完成：" + (x.error || x.result?.error || x.result?.status || "后续处理未完成，请核查回执")
          : publishes && x.status!=="published"? "未完成：尚未确认发布与索引校验结果，请核查回执"
          : x.status==="published" && publishes? "经验已发布，并通过正式索引验证。"
          : x.status==="delivery_approved"? "初审已通过。这条现在进入终审，关闭后可以继续对它终审。"
          : x.status==="revision_requested"? "已记录为需要修改，意见已写入待修改记录。"
          : (x.status||"").endsWith("_rejected")? "已记录为不予采用。"
          : x.ok? "处理完成；当前状态见待审列表。"
          : "未完成：" + (x.error || x.result?.error || x.result?.status || "后续处理未完成，请核查回执");
        if(!confirmed || (publishes && x.status!=="published")) allOk=false;
      });
      renderItems(items, status);
      const done=r.items.filter(x=>x.recorded).length;
      const complete = allOk && sync.ok===true;
      phase("done", complete? "ok" : "partial");
      $("rcDialogTitle").textContent = complete? "裁决已提交" : allOk? "裁决已记录，第二大脑同步待重试" : "裁决已返回，有需要留意的项";
      $("rcProgressTitle").textContent = complete? `${done} 项全部处理完成` : `${done} / ${r.items.length} 项已记录`;
      $("rcSummaryCount").textContent = "下面是裁决后端返回的正式回执。";
      $("rcSyncLine").textContent = sync.ok===true? "第二大脑回执已同步 · "+(sync.at||"") : "第二大脑同步待重试："+(sync.error||"未知原因")+"（可在页面顶部点“重试同步”）";
      lastResult = ["小八 AgentOS 裁决回执", "提交编号："+r.id, ...r.items.map(x=>{ const it=items.find(i=>i.task_id===x.task_id)||{}; return `${it.title||x.task_id}\n候选：${x.task_id}\n阶段：${STAGE[it.stage]||it.stage}　决定：${decisionName(it)}\n结果：${status[x.task_id].outcome}\n回执：${x.review_id||"无"}`; }),
        $("rcSyncLine").textContent].join("\n\n");
      $("rcCopy").textContent = "复制回执";
      $("rcBarNote").textContent = `刚刚提交 ${r.items.length} 项：${done} 项已记录`+(sync.ok===true? "，第二大脑已同步 "+(sync.at||"").slice(11) : "，第二大脑待同步");
      r.items.forEach(x=>{ if(x.recorded) forget(x.task_id); });
      packet=null; packetReviewBaseline=new Set(); packetDetails={};
    }catch(e){
      stopTimers();
      phase("error");
      $("rcDialogTitle").textContent = "没有收到完整回执";
      $("rcProgressTitle").textContent = "提交结果不确定";
      $("rcSummaryCount").textContent = "";
      $("rcSyncLine").textContent = "";
      items.forEach(it=>{ const st=status[it.task_id]; Object.keys(st).forEach(k=>{ if(st[k]==="active") st[k]=""; }); });
      renderItems(items, status);
      $("rcFeedback").textContent = "原因："+e.message+"。上面打了绿点的步骤已确认完成。可以重试本次提交：系统按同一提交编号查验，已经记录的不会重复审核。";
      $("rcSubmit").disabled=false; $("rcSubmit").textContent="重试本次提交";
    }finally{
      submitting=false; $("rcElapsed").textContent += " · 结束"; counts(); await refresh(true);
    }
  }

  async function refresh(force=false){
    if(!TOKEN){ unavailable(); return; }
    const mine = ++refreshSeq;   // 卡07：晚到的轮询响应直接丢弃，不覆盖新 state/详情
    try{
      const next = await api("state");
      if(mine!==refreshSeq) return;
      const previous=state, changed=!previous||previous.revision!==next.revision;
      state = next; renderState();
      refreshBadge();
      loadKept(force);
      // 已不在待审队列里的候选，从本次清单中移除
      Object.keys(saved).forEach(id=>{ const cur=state.items.find(x=>x.task_id===id); if(!cur || !same(cur.expected, saved[id].expected)) forget(id); });
      if(changed) refreshOpenSummary(previous);
      counts();
      if(!state.items.length && !selected){ blank("所有待审已处理","新候选产生后会自动出现在这里。"); return; }
      if(!selected || wanted){
        // 可用 #review=候选编号 或总览里的行直接定位到某条候选
        const want = wanted || location.hash.split("=")[1] || new URLSearchParams(location.search).get("candidate") || ""; wanted=null;
        const hit = want && state.items.find(x=>x.task_id.startsWith(want));
        const target = hit? hit.task_id : (selected || state.items[0].task_id);
        if(target!==selected || force || changed) await select(target);
      }
      else if((force||changed) && !submitting && !$("rcDialog").open) await select(selected);
    }catch(e){
      if(mine!==refreshSeq) return;   // 迟到的失败响应也不改写连接状态
      setConn("down","连接中断："+e.message+"。当前内容可能已过期，恢复后请刷新核对。");
      $("rcSave").disabled=true; $("rcReview").disabled=submitting || !packet; $("rcSubmit").disabled=submitting || !packet;
    }
  }
  function unavailable(){
    setConn("idle","未连接本机监看服务");
    const n=$("rcNotice"); n.hidden=false; n.className="rc-notice";
    n.textContent="裁决需要本机服务：请在 Mac 上运行 xiaoba_monitor.py，并从 http://127.0.0.1:8799/ 打开本页。当前页面只能查看演示数据，不能读取或提交任何裁决。";
    blank("裁决中心未连接","—"); $("rcList").innerHTML='<li><p class="rc-empty">未连接</p></li>';
  }
  function open(id){ wanted = id||null; Tabs.show("review"); }

  /* ---- 事件 ---- */
  $("rcList").addEventListener("click",e=>{ const b=e.target.closest(".rc-item"); if(b && b.dataset.id!==selected) select(b.dataset.id); });
  $("rcSearch").addEventListener("input",renderList);
  document.querySelectorAll(".choice").forEach(b=>b.addEventListener("click",()=>{ if(!detail||!drafts[selected]) return;
    drafts[selected].decision=b.dataset.decision; document.querySelectorAll(".choice").forEach(v=>v.setAttribute("aria-pressed", String(v===b))); stash(); draftUI(); }));
  $("rcNote").addEventListener("input",()=>{ if(detail&&drafts[selected]){ drafts[selected].note=$("rcNote").value; stash(); draftUI(); } });
  $("rcSave").addEventListener("click",()=>{ if(!detail||!drafts[selected]||packet||submitting) return;
    saved[selected]={...drafts[selected], task_id:selected, stage:detail.stage, title:detail.title, entry_type:detail.entry_type,
      experience_validation:detail.experience_validation}; renderList(); counts(); draftUI(); });
  $("rcBasisItems").addEventListener("change",()=>syncBasisConfirmationState(submittedItems()));
  $("rcReview").addEventListener("click",openSummary);
  $("rcBack").addEventListener("click",closeDialog);
  $("rcClose").addEventListener("click",closeDialog);
  $("rcDone").addEventListener("click",closeDialog);
  // 点弹窗外的遮罩也可以关闭（处理中除外）
  $("rcDialog").addEventListener("click",e=>{ if(e.target!==$("rcDialog")) return; const r=$("rcDialog").getBoundingClientRect();
    if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom) closeDialog(); });
  $("rcDialog").addEventListener("cancel",e=>{ if(submitting) e.preventDefault(); });
  $("rcDialog").addEventListener("close",()=>{ steps(1); stopTimers();
    // 关闭结果后，把视线带回队列：还有待审就停在它上面，否则提示已处理完
    if($("rcDialog").dataset.phase==="done"){ phase("review"); if(state && !state.items.some(x=>x.task_id===selected)){ selected=null; refresh(true); } else if(selected) select(selected); } });
  $("rcCopy").addEventListener("click",async()=>{ const done=$("rcDialog").dataset.phase==="done", text=done&&lastResult? lastResult : $("rcOutput").value;
    try{ await navigator.clipboard.writeText(text); $("rcFeedback").textContent = done? "回执已复制。" : "交接文本已复制。审批状态以提交回执为准。"; }
    catch(e){ if(!done) $("rcOutput").select(); $("rcFeedback").textContent="复制没有成功，请手动选择文本后按 ⌘C。"; } });
  $("rcSubmit").addEventListener("click",submit);
  $("rcTrialRequest").addEventListener("click",async()=>{
    if(!detail) return;
    const text="为经验候选 "+detail.task_id+" 创建受控验证任务卡。候选哈希："+detail.evidence_sha256+"。先确认同类案例、授权范围、输入夹具、验收条件、停止条件，再在隔离试验目录执行真实测试；将新证据提交AGY/DSH独立审查，并把结果关联回原候选。不批准或发布经验，不直接执行候选文字中的命令。我的试验案例：";
    try{await navigator.clipboard.writeText(text);$("rcTrialRequest").textContent="已复制，粘贴到Agent主入口并补充案例";}
    catch(e){$("rcTrialEvidence").textContent+="\n\n"+text;}
  });
  $("rcRefresh").addEventListener("click",()=>refresh(true));
  $("rcRetrySync").addEventListener("click",async()=>{ $("rcRetrySync").disabled=true; try{ await api("sync",{}); }catch(e){ setConn("warn","同步未完成："+e.message); } $("rcRetrySync").disabled=false; refresh(true); });
  $("reviewOpen").addEventListener("click",()=>open());
  $("reviewRows").addEventListener("click",e=>{ const tr=e.target.closest("tr[data-id]"); if(tr) open(tr.dataset.id); });
  addEventListener("beforeunload",e=>{ if(submitting || packet || Object.keys(saved).length){ e.preventDefault(); e.returnValue=""; } });

  const active = ()=>Tabs.current==="review" && !document.hidden && !submitting;
  Tabs.onChange(name=>{ if(name==="review"){ refresh(true); refreshValueQueue(); } });
  $("valueReviewRefresh").addEventListener("click",refreshValueQueue);
  document.addEventListener("visibilitychange",()=>{ if(active()){ refresh(true); refreshValueQueue(); } });
  setInterval(()=>{ if(active()) refresh(); }, 5000);
  setInterval(()=>{ if(active()) refreshValueQueue(); }, 15000);
  // One complete badge projection refreshes on every tab, including overview.
  refreshBadge();
  setInterval(refreshBadge, 15000);
  document.addEventListener("visibilitychange",refreshBadge);
  blank("请选择一条待审内容","—");
  return {open, refresh, badge, markdown};
})();

/* =========================================================
   8b. 任务卡页：只读。数据来自 /api/taskcards（任务卡目录＋发卡台账＋自动发现的回执）
   ========================================================= */
const TaskCards = (() => {
  const SEEN_KEY = "xiaoba-monitor-taskcards-seen-v1";
  const OPEN = new Set(["ready","waiting","issued","receipt","returned","verified"]);
  const TONE = {ready:"busy", waiting:"idle", issued:"busy", receipt:"warn", returned:"down", verified:"ok", done:"ok", history:"idle"};
  const EV = {issue:"发卡", receipt:"回执", returned:"退回返工", verified:"核对通过", done:"完成", note:"备注"};
  const esc = s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const when = sec=>{ if(!sec) return "—"; const d=new Date(sec*1000); return isNaN(d)? "—" : d.toLocaleString("zh-CN",{hour12:false,month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"}); };
  const base = p=>String(p||"").split("/").pop();
  let data=null, filter="open", query="", seen={}, current=null, timer=null, error="";
  try{ seen = JSON.parse(localStorage.getItem(SEEN_KEY)||"{}") || {}; }catch(e){ seen={}; }
  const stash = ()=>{ try{ localStorage.setItem(SEEN_KEY, JSON.stringify(seen)); }catch(e){} };
  const isNew = row=>seen[row.id]!==undefined ? row.updated>seen[row.id]+0.5 : false;

  async function get(path){
    const res = await fetch(path, {cache:"no-store"});
    let body; try{ body = await res.json(); }catch(e){ throw new Error("返回内容无法解析（HTTP "+res.status+"）"); }
    if(!res.ok) throw new Error(body && body.error || "连接失败（HTTP "+res.status+"）");
    return body;
  }

  function badge(){
    const n = data ? (data.counts.receipt||0) : 0, el=$("tcBadge");
    el.textContent = n; el.hidden = !n; el.title = n? n+" 张卡的回执待核对" : "";
  }

  function render(){
    const body=$("tcBody");
    if(error && !data){ body.innerHTML = `<tr><td colspan="7" class="foot">读不到任务卡：${esc(error)}</td></tr>`; return; }
    if(!data) return;
    const c=data.counts;
    const kpis=[["待发",c.ready,""],["已发卡",c.issued,""],["回执待核对",c.receipt,c.receipt?"warn":""],["退回返工",c.returned,c.returned?"crit":""],["已核对",c.verified,""],["已完成",(c.done||0)+(c.history||0),""]];
    $("tcKpis").innerHTML = kpis.map(([l,v,t])=>`<div class="kpi ${t}"><span class="label">${l}</span><span class="val num">${v||0}</span></div>`).join("");
    const warn=$("tcWarn"), warnings=(data.warnings||[]).concat(error? ["刷新失败："+error] : []);
    warn.hidden=!warnings.length; warn.textContent=warnings.join("；");
    $("tcUpdated").textContent = "更新于 "+new Date(data.generated_at*1000).toLocaleTimeString("zh-CN",{hour12:false});
    const q=query.trim().toLowerCase();
    const rows=data.cards.filter(r=>filter==="all" || (filter==="open")===OPEN.has(r.state))
      .filter(r=>!q || [r.id, r.title, r.version, r.to, r.who, r.state_text].join(" ").toLowerCase().includes(q));
    $("tcSummary").textContent = `显示 ${rows.length} 张 · 共 ${data.cards.length} 张`;
    body.innerHTML = rows.length ? rows.map(r=>{
      const rec = r.receipt_count ? `${r.receipt_count} 份<span class="sub">最新 ${esc(when(r.latest_receipt))}</span>` : "—";
      return `<tr class="click" data-id="${r.id}" tabindex="0"><td class="num">${r.id}</td>
        <td><span class="t">${esc(r.title)}${isNew(r)?'<span class="tc-new">新</span>':""}</span><span class="sub" title="${esc(r.who||"")}">${esc(r.version||"")}${r.who? " · "+esc(r.who.replace(/（具体谁接[^）]*）$/,"")):""}</span></td>
        <td><span class="pill ${TONE[r.state]||"idle"}" title="${r.state_source==="inferred"?"没登记过，按卡里的开始条件推断":r.state_source==="found"?"自动发现了新的回执文件":"来自发卡台账"}">${esc(r.state_text)}</span></td>
        <td>${esc(r.to||"—")}</td><td class="num">${esc(when(r.issued_at))}</td><td class="num">${rec}</td><td class="num">${esc(when(r.updated))}</td></tr>`;
    }).join("") : `<tr><td colspan="7" class="foot">没有符合条件的任务卡</td></tr>`;
    badge();
    if(current && $("tcDialog").open){ const row=data.cards.find(r=>r.id===current.id); if(row){ current=row; facts(row); } }
  }

  async function load(){
    try{ data = await get("api/taskcards"); error=""; }
    catch(e){ error = e.message; }
    // 第一次打开时把现有的卡都记为看过，之后有变化的才标“新”
    if(data && !Object.keys(seen).length){ data.cards.forEach(r=>seen[r.id]=r.updated); stash(); }
    render();
  }

  function facts(row){
    const dl=[["状态", row.state_text+(row.state_source==="inferred"?"（未登记，按开始条件推断）":"")],["发给",row.to||"—"],["发卡时间",when(row.issued_at)],
      ["开始条件",row.start||"—"],["适合谁接",row.who||"—"],["卡文件",row.path]];
    $("tcFacts").innerHTML = dl.map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
    $("tcEvents").innerHTML = row.events.length ? row.events.slice().reverse().map(e=>
      `<li><span class="when">${esc(when(e.at))}</span><b>${esc(EV[e.event]||e.event)}</b>${e.to?" → "+esc(e.to):""}${e.by?` <span class="foot">（${esc(e.by)}）</span>`:""}${e.note?"<br>"+esc(e.note):""}${e.path?`<br><span class="foot">${esc(base(e.path))}</span>`:""}</li>`).join("")
      : `<li class="foot">台账里还没有这张卡的记录</li>`;
    const list=[];
    row.events.filter(e=>e.event==="receipt").forEach(e=>list.push({at:e.at, path:e.path, label:e.path? base(e.path) : (e.note||"回执（贴在对话里）"), kind:"登记"}));
    row.receipts_found.filter(f=>!f.registered).forEach(f=>list.push({at:f.mtime, path:f.path, label:f.name, kind:"自动发现"}));
    list.sort((a,b)=>b.at-a.at);
    $("tcReceipts").innerHTML = list.length ? list.map(x=>`<li><span class="when">${esc(when(x.at))}</span>${x.path?`<button type="button" data-path="${esc(x.path)}">${esc(x.label)}</button>`:esc(x.label)} <span class="foot">${x.kind}</span></li>`).join("")
      : `<li class="foot">还没有回执</li>`;
  }

  async function showDoc(path, title){
    $("tcDocTitle").textContent = title; $("tcDocMeta").textContent = "读取中…"; $("tcDoc").innerHTML = "";
    try{
      const f = await get("api/taskcards/file?path="+encodeURIComponent(path));
      $("tcDocMeta").textContent = base(f.path)+" · sha256 "+f.sha256.slice(0,12)+(f.truncated?" · 太长，只显示前 256 KB":"");
      $("tcDoc").innerHTML = ReviewCenter.markdown(f.text);
    }catch(e){ $("tcDocMeta").textContent = ""; $("tcDoc").innerHTML = `<p class="foot">${esc(e.message)}</p>`; }
  }

  function open(id){
    const row = data && data.cards.find(r=>r.id===id); if(!row) return;
    current=row; seen[row.id]=row.updated; stash();
    $("tcDialogTitle").textContent = `卡 ${row.id}：${row.title}`;
    facts(row); showDoc(row.path, "卡片原文");
    if(!$("tcDialog").open) $("tcDialog").showModal();
    render();
  }

  $("tcBody").addEventListener("click", e=>{ const tr=e.target.closest("tr[data-id]"); if(tr) open(Number(tr.dataset.id)); });
  $("tcBody").addEventListener("keydown", e=>{ if(e.key!=="Enter") return; const tr=e.target.closest("tr[data-id]"); if(tr) open(Number(tr.dataset.id)); });
  $("tcReceipts").addEventListener("click", e=>{ const b=e.target.closest("button[data-path]"); if(b) showDoc(b.dataset.path, "回执原文"); });
  $("tcShowCard").addEventListener("click", ()=>{ if(current) showDoc(current.path, "卡片原文"); });
  $("tcClose").addEventListener("click", ()=>$("tcDialog").close());
  $("tcFilter").addEventListener("click", e=>{ const b=e.target.closest("button[data-f]"); if(!b) return; filter=b.dataset.f;
    $("tcFilter").querySelectorAll("button").forEach(x=>x.setAttribute("aria-pressed", String(x===b))); render(); });
  $("tcSearch").addEventListener("input", e=>{ query=e.target.value; render(); });
  // 不在这一页时也低频刷新，让标签上的“回执待核对”数字跟上
  const tick = ()=>{ if(!document.hidden) load(); };
  Tabs.onChange(name=>{ clearInterval(timer); if(name==="taskcards"){ load(); timer=setInterval(tick, 10000); } else timer=setInterval(tick, 60000); });
  timer=setInterval(tick, 60000);
  load();
  return {load, open};
})();

Tabs.show(location.hash.slice(1).split("=")[0] || (new URLSearchParams(location.search).get("candidate")? "review" : "overview"), false);
