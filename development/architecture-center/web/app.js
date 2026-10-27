'use strict';
(() => {
  const data=window.XIAOBA_ARCHITECTURE;
  const $=id=>document.getElementById(id);
  const viewport=$('viewport'),world=$('world'),canvas=$('wires'),ctx=canvas.getContext('2d');
  const saved=(()=>{try{return JSON.parse(localStorage.getItem('xiaoba-architecture-view')||'{}');}catch{return {};}})();
  let view,selected=null,scale=1,tx=20,ty=20,drag=null,reading=saved.reading??(innerWidth<650),frame;
  const remembered={};
  const text=(tag,value,parent,className)=>{const e=document.createElement(tag);if(value!==undefined)e.textContent=value;if(className)e.className=className;if(parent)parent.append(e);return e;};
  const button=(title,fn,parent)=>{const b=text('button',title,parent);b.type='button';b.addEventListener('click',fn);return b;};
  function remember(){try{localStorage.setItem('xiaoba-architecture-view',JSON.stringify({view:view.id,reading}));}catch{}}
  function announce(value){$('announcement').textContent=value;}
  function transform(){world.style.transform=`translate(${tx}px,${ty}px) scale(${scale})`;$('zoomLevel').value=Math.round(scale*100)+'%';remembered[view.id]={scale,tx,ty};}
  function fit(){const padding=35;scale=Math.min((viewport.clientWidth-padding*2)/view.w,(viewport.clientHeight-padding*2)/view.h,1.2);scale=Math.max(.18,scale);tx=(viewport.clientWidth-view.w*scale)/2;ty=(viewport.clientHeight-view.h*scale)/2;transform();}
  function zoom(next,cx=viewport.clientWidth/2,cy=viewport.clientHeight/2){next=Math.max(.18,Math.min(2.2,next));const ratio=next/scale;tx=cx-(cx-tx)*ratio;ty=cy-(cy-ty)*ratio;scale=next;transform();}
  function focusNode(id){const node=view.nodes.find(n=>n.id===id);if(!node||reading)return;scale=Math.max(scale,.9);tx=viewport.clientWidth/2-(node.x+node.w/2)*scale;ty=viewport.clientHeight/2-(node.y+node.h/2)*scale;transform();}
  function closeDetail(){selected=null;$('detail').hidden=true;highlight();}
  function selectedSet(){if(!selected)return null;const ids=new Set([selected]);for(const edge of view.edges){if(edge.from===selected)ids.add(edge.to);if(edge.to===selected)ids.add(edge.from);}return ids;}
  function highlight(){const related=selectedSet();for(const node of $('nodes').children){node.classList.toggle('selected',node.dataset.id===selected);node.classList.toggle('dim',!!related&&!related.has(node.dataset.id));node.setAttribute('aria-pressed',String(node.dataset.id===selected));}draw();}
  function selectNode(id,{focus=false}={}){
    if(!data.facts[id])return;
    selected=id;const fact=data.facts[id];$('detail').hidden=false;$('detailEyebrow').textContent=view.title;$('detailTitle').textContent=fact.title;
    $('detailStatus').replaceChildren();text('span',fact.status,$('detailStatus'),'badge '+fact.kind);
    $('detailDescription').textContent=fact.summary;$('detailSections').replaceChildren();
    if(fact.rules.length){text('h3','节点规则',$('detailSections'));const ul=text('ul',undefined,$('detailSections'));for(const rule of fact.rules)text('li',rule,ul);}
    const next=view.edges.filter(edge=>edge.from===id),previous=view.edges.filter(edge=>edge.to===id);
    for(const [title,edges,key] of [['下一步',next,'to'],['来自',previous,'from']]){if(!edges.length)continue;text('h3',title,$('detailSections'));const box=text('div',undefined,$('detailSections'),'detail-links');for(const edge of edges){button((edge.label?edge.label+' → ':'')+data.facts[edge[key]].title,()=>selectNode(edge[key],{focus:true}),box);}}
    $('detailLinks').replaceChildren();if(fact.links.length){text('h3','展开相关模块',$('detailLinks'));const box=text('div',undefined,$('detailLinks'),'detail-links');for(const id of fact.links){const target=data.views.find(v=>v.id===id);if(target)button(target.title,()=>setView(id),box);}}
    highlight();if(focus)requestAnimationFrame(()=>focusNode(id));announce('已打开节点：'+fact.title);window.dispatchEvent(new CustomEvent('xiaoba:node-selected',{detail:{id,view:view.id}}));
  }
  function point(node,side){return side==='L'?[node.x,node.y+node.h/2]:side==='R'?[node.x+node.w,node.y+node.h/2]:side==='T'?[node.x+node.w/2,node.y]:[node.x+node.w/2,node.y+node.h];}
  function edgePoints(edge){
    const a=view.nodes.find(n=>n.id===edge.from),b=view.nodes.find(n=>n.id===edge.to);
    if(edge.via?.length){const first=edge.via[0],last=edge.via.at(-1);const aside=first[0]>a.x+a.w?'R':first[0]<a.x?'L':first[1]>a.y+a.h?'B':'T';const bside=last[1]<b.y?'T':last[1]>b.y+b.h?'B':last[0]<b.x?'L':'R';return [point(a,aside),...edge.via,point(b,bside)];}
    const dx=(b.x+b.w/2)-(a.x+a.w/2),dy=(b.y+b.h/2)-(a.y+a.h/2);
    if(Math.abs(dx)>Math.abs(dy)*1.3){const s=point(a,dx>0?'R':'L'),t=point(b,dx>0?'L':'R'),mid=(s[0]+t[0])/2;return [s,[mid,s[1]],[mid,t[1]],t];}
    const s=point(a,dy>0?'B':'T'),t=point(b,dy>0?'T':'B'),mid=(s[1]+t[1])/2;return [s,[s[0],mid],[t[0],mid],t];
  }
  function draw(){
    const ratio=Math.min(devicePixelRatio||1,2);canvas.width=view.w*ratio;canvas.height=view.h*ratio;canvas.style.width=view.w+'px';canvas.style.height=view.h+'px';ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,view.w,view.h);
    for(const edge of view.edges){const related=!selected||edge.from===selected||edge.to===selected;const color=edge.tone==='pass'?'#69a880':edge.tone==='fail'?'#d4867c':edge.tone==='feedback'?'#84a5c1':'#91a8bc';ctx.globalAlpha=related?1:.14;ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=selected&&related?2.5:1.5;ctx.setLineDash(edge.tone==='feedback'?[5,5]:[]);const points=edgePoints(edge).filter((p,i,a)=>i===0||p[0]!==a[i-1][0]||p[1]!==a[i-1][1]);ctx.beginPath();points.forEach((p,i)=>i?ctx.lineTo(...p):ctx.moveTo(...p));ctx.stroke();ctx.setLineDash([]);const end=points.at(-1),last=points.at(-2);if(last){const angle=Math.atan2(end[1]-last[1],end[0]-last[0]);ctx.beginPath();ctx.moveTo(...end);ctx.lineTo(end[0]-8*Math.cos(angle-.45),end[1]-8*Math.sin(angle-.45));ctx.lineTo(end[0]-8*Math.cos(angle+.45),end[1]-8*Math.sin(angle+.45));ctx.closePath();ctx.fill();}
      if(edge.label){let longest=0,position;for(let i=1;i<points.length;i++){const a=points[i-1],b=points[i],length=Math.hypot(b[0]-a[0],b[1]-a[1]);if(length>longest){longest=length;position=[(a[0]+b[0])/2,(a[1]+b[1])/2];}}if(position){ctx.font='10px system-ui,sans-serif';ctx.textAlign='center';const width=ctx.measureText(edge.label).width;ctx.fillStyle='#f9fbfd';ctx.fillRect(position[0]-width/2-4,position[1]-14,width+8,16);ctx.fillStyle=color;ctx.fillText(edge.label,position[0],position[1]-3);}}
    }ctx.globalAlpha=1;
  }
  function renderReading(){const root=$('readingView');root.replaceChildren();text('p',view.note,root,'reading-heading');for(const group of view.groups){const section=text('section',undefined,root,'reading-group');text('h3',group.title,section);const items=view.nodes.filter(n=>n.x>=group.x&&n.x<group.x+group.w&&n.y>=group.y&&n.y<group.y+group.h).sort((a,b)=>a.y-b.y||a.x-b.x);for(const node of items){const fact=data.facts[node.id],card=text('article',undefined,section,'reading-card');button(fact.title,()=>selectNode(node.id),card);text('p',fact.summary,card);text('span',fact.status,card,'badge '+fact.kind);const paths=view.edges.filter(e=>e.from===node.id);if(paths.length){const nav=text('div',undefined,card,'reading-paths');for(const edge of paths)button((edge.label||'下一步')+' → '+data.facts[edge.to].title,()=>selectNode(edge.to),nav);}}}}
  function mode(){viewport.hidden=reading;$('readingView').hidden=!reading;$('readingToggle').setAttribute('aria-pressed',String(reading));$('readingToggle').textContent=reading?'拓扑模式':'阅读模式';$('hint').textContent=reading?'按模块阅读 · 点击标题查看细节':'拖动画布 · 滚轮缩放 · 点击节点查看规则';remember();if(!reading)requestAnimationFrame(fit);}
  function setView(id){
    const next=data.views.find(v=>v.id===id);if(!next)return;view=next;closeDetail();$('viewTitle').textContent=view.title;$('viewSubtitle').textContent=view.subtitle;world.style.width=view.w+'px';world.style.height=view.h+'px';
    for(const tab of $('viewTabs').children){const active=tab.dataset.view===id;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;}
    $('groups').replaceChildren();for(const group of view.groups){const node=text('div',undefined,$('groups'),'group');Object.assign(node.style,{left:group.x+'px',top:group.y+'px',width:group.w+'px',height:group.h+'px'});text('span',group.title,node,'group-title');}
    $('nodes').replaceChildren();for(const node of view.nodes){const fact=data.facts[node.id],b=button('',()=>selectNode(node.id),$('nodes'));b.className='node '+fact.kind;b.dataset.id=node.id;b.setAttribute('aria-pressed','false');b.setAttribute('aria-label',fact.title+'，'+fact.status);Object.assign(b.style,{left:node.x+'px',top:node.y+'px',width:node.w+'px',height:node.h+'px'});text('strong',fact.title,b);text('small',fact.summary,b);}
    renderReading();draw();if(remembered[id]){({scale,tx,ty}=remembered[id]);transform();}else requestAnimationFrame(fit);try{history.replaceState(null,'','#'+id);}catch{}remember();announce('当前视图：'+view.title);
  }
  for(const v of data.views){const b=button(v.title.replace(/^[一二三四五六]、/,''),()=>setView(v.id),$('viewTabs'));b.dataset.view=v.id;b.setAttribute('role','tab');}
  $('viewTabs').addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const index=data.views.indexOf(view);const next=event.key==='Home'?0:event.key==='End'?data.views.length-1:(index+(event.key==='ArrowRight'?1:-1)+data.views.length)%data.views.length;setView(data.views[next].id);$('viewTabs').children[next].focus();});
  $('closeDetail').addEventListener('click',closeDetail);
  $('zoomIn').addEventListener('click',()=>zoom(scale*1.2));$('zoomOut').addEventListener('click',()=>zoom(scale/1.2));$('fit').addEventListener('click',()=>{if(reading){reading=false;mode();}else fit();});$('actual').addEventListener('click',()=>{if(reading){reading=false;mode();}requestAnimationFrame(()=>zoom(1));});
  $('readingToggle').addEventListener('click',()=>{reading=!reading;mode();});$('statusToggle').addEventListener('click',()=>{$('statusStrip').hidden=!$('statusStrip').hidden;requestAnimationFrame(()=>{if(!reading)fit();});});
  viewport.addEventListener('pointerdown',event=>{if(event.button!==0||event.target.closest('.node'))return;drag={id:event.pointerId,x:event.clientX,y:event.clientY,tx,ty};viewport.setPointerCapture(event.pointerId);viewport.classList.add('dragging');});
  viewport.addEventListener('pointermove',event=>{if(!drag||drag.id!==event.pointerId)return;tx=drag.tx+event.clientX-drag.x;ty=drag.ty+event.clientY-drag.y;transform();});
  const endDrag=()=>{drag=null;viewport.classList.remove('dragging');};viewport.addEventListener('pointerup',endDrag);viewport.addEventListener('pointercancel',endDrag);
  viewport.addEventListener('wheel',event=>{event.preventDefault();const bounds=viewport.getBoundingClientRect();zoom(scale*Math.exp(-event.deltaY*.0015),event.clientX-bounds.left,event.clientY-bounds.top);},{passive:false});
  viewport.addEventListener('keydown',event=>{if(event.target!==viewport)return;const offset=35;if(event.key==='ArrowLeft')tx+=offset;else if(event.key==='ArrowRight')tx-=offset;else if(event.key==='ArrowUp')ty+=offset;else if(event.key==='ArrowDown')ty-=offset;else return;event.preventDefault();transform();});
  $('search').addEventListener('input',()=>{const query=$('search').value.trim().toLowerCase(),root=$('searchResults');root.replaceChildren();root.hidden=!query;if(!query)return;let count=0;for(const [id,fact] of Object.entries(data.facts)){if(!(fact.title+' '+fact.summary+' '+fact.rules.join(' ')).toLowerCase().includes(query))continue;const target=data.views.find(v=>v.id!== 'overview'&&v.nodes.some(n=>n.id===id))||data.views.find(v=>v.nodes.some(n=>n.id===id));if(!target)continue;const b=button(fact.title,()=>{setView(target.id);root.hidden=true;$('search').value='';selectNode(id,{focus:true});},root);text('small',target.title,b);if(++count>=12)break;}if(!count)text('p','没有匹配节点',root);});
  document.addEventListener('click',event=>{if(!event.target.closest('.search-wrap'))$('searchResults').hidden=true;});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){closeDetail();$('searchResults').hidden=true;}if(event.key==='/'&&!['INPUT','TEXTAREA'].includes(document.activeElement.tagName)){event.preventDefault();$('search').focus();}});
  new ResizeObserver(()=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{if(view&&!reading)draw();});}).observe(viewport);
  setView(data.views.some(v=>v.id===location.hash.slice(1))?location.hash.slice(1):saved.view||'overview');if(!view)setView('overview');mode();
})();
