// Mount the local architecture page as a same-origin monitor panel.
(() => {
  const tabs=document.querySelector('nav.tabs');
  if(!tabs||document.getElementById('tabArchitecture'))return;
  const button=document.createElement('button');button.id='tabArchitecture';button.className='tab';button.type='button';button.setAttribute('role','tab');button.textContent='架构与升级发布';button.setAttribute('aria-selected','false');tabs.append(button);
  const panel=document.createElement('section');panel.id='viewArchitecture';panel.hidden=true;panel.setAttribute('role','tabpanel');panel.style.cssText='height:calc(100dvh - 125px);min-height:500px';
  const frame=document.createElement('iframe');frame.title='小八架构与升级发布中心';frame.src='/architecture/';frame.style.cssText='width:100%;height:100%;border:0';panel.append(frame);tabs.parentNode.insertBefore(panel,tabs.nextSibling);
  let active=false;
  function hide(){active=false;sessionStorage.removeItem('xiaoba-architecture-tab');panel.hidden=true;button.setAttribute('aria-selected','false');}
  button.addEventListener('click',()=>{
    if(typeof Tabs!=='undefined')Tabs.show('overview',false);
    document.querySelectorAll('.view').forEach(view=>view.hidden=true);
    document.querySelectorAll('.tab').forEach(tab=>tab.setAttribute('aria-selected','false'));
    active=true;sessionStorage.setItem('xiaoba-architecture-tab','1');panel.hidden=false;button.setAttribute('aria-selected','true');history.replaceState(null,'','#architecture');
  });
  if(typeof Tabs!=='undefined')Tabs.onChange(()=>{if(active)hide();});
  for(const tab of tabs.querySelectorAll('.tab'))if(tab!==button)tab.addEventListener('click',hide);
  if(location.hash==='#architecture'||sessionStorage.getItem('xiaoba-architecture-tab'))button.click();
})();
