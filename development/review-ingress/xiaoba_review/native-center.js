'use strict';
// Render the same acknowledged materials inside the existing decision-center
// tab. Use text nodes throughout: imported reviewer text is untrusted.
(() => {
  const parent=document.getElementById('viewReview');
  if(!parent)return;
  const panel=document.createElement('section');
  panel.style.cssText='margin:18px;padding:18px;border:1px solid #64748b;border-radius:10px;overflow-wrap:anywhere';
  const heading=document.createElement('h2');heading.textContent='DSH＋开放第二方 · 独立审查材料';panel.append(heading);
  const link=document.createElement('a');link.href='/open-review';link.textContent='打开任务卡、回执与运行时间线';panel.append(link);
  const content=document.createElement('div');panel.append(content);parent.prepend(panel);
  async function refresh(){
    try{const response=await fetch('/api/open-review/state');if(!response.ok)throw Error('材料接口暂不可用');const state=await response.json();content.replaceChildren();
      const hint=document.createElement('p');hint.textContent='以下是已收齐的审查材料；FAIL、分歧与评分一并保留。批准仍由你在原卡作出。';content.append(hint);
      for(const packet of state.decision_packets){const detail=document.createElement('details');const summary=document.createElement('summary');summary.textContent=(packet.current?'当前待裁决':'历史已失效')+' · '+packet.data.subject.candidate_id+' · '+packet.data.reviews.map(r=>r.receipt.verdict).join(' / ');detail.append(summary);const body=document.createElement('pre');body.style.whiteSpace='pre-wrap';body.textContent=JSON.stringify(packet.data,null,2);detail.append(body);content.append(detail);}
      if(!state.decision_packets.length){const empty=document.createElement('p');empty.textContent='尚无齐备的双审材料。';content.append(empty);}
    }catch(error){content.textContent=String(error.message);}
  }
  refresh();setInterval(refresh,5000);
})();
