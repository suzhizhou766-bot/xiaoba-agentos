// Read only Antigravity's model selection; never change UI or CLI settings.
const fs = require('node:fs');

const expression = `(()=>{
  let root;
  for (const el of document.querySelectorAll('body *')) {
    const key=Object.keys(el).find(k=>k.startsWith('__reactFiber$'));
    if(key){root=el[key];while(root.return)root=root.return;break;}
  }
  const seen=new Set(), values=[];
  function walk(f){
    if(!f||seen.has(f))return;seen.add(f);
    const v=f.memoizedProps?.value;
    if(v?.userStatusProvider&&v?.modelPreferencesProvider){
      const prefs=v.modelPreferencesProvider.getState();
      const status=v.userStatusProvider.getState();
      values.push({selected:prefs.lastSelectedAgentModel,
        models:status.cascadeModelConfigData?.clientModelConfigs?.map(m=>({
          label:m.label, choice:m.modelOrAlias?.choice, disabled:m.disabled
        }))});
    }
    walk(f.child);walk(f.sibling);
  }
  walk(root);return values;
})()`;

function localUrl(value, port, protocol) {
  const u=new URL(value);
  if(u.protocol!==protocol || u.hostname!=='127.0.0.1' ||
     Number(u.port)!==port || u.username || u.password) throw new Error('Non-local debugger');
  return u;
}

function selectModel(states) {
  if(!Array.isArray(states)||states.length!==1) throw new Error('Ambiguous desktop state');
  const {selected,models}=states[0];
  if(!Number.isInteger(selected)||!Array.isArray(models)) throw new Error('Missing desktop selection');
  const selectedModels=models.filter(m=>!m.disabled&&m.choice?.case==='model'&&m.choice.value===selected);
  if(selectedModels.length!==1) throw new Error('Unavailable desktop model');
  const label=selectedModels[0].label;
  if(typeof label!=='string'||!label.trim()||label.length>160||/[\r\n\x00-\x1f]/.test(label)) throw new Error('Invalid desktop model name');
  // Forward the name selected by the desktop. AGY owns name resolution.
  // Do not fetch upstream availability, choose an ID, or create a fallback pool.
  return {provider:'Antigravity',model:label,model_label:label,
    model_source:'antigravity_desktop',desktop_model_value:selected};
}

async function readDesktop(debugPortFile) {
  const port=Number(fs.readFileSync(debugPortFile,'utf8').split('\n')[0]);
  if(!Number.isInteger(port)||port<1||port>65535) throw new Error('Invalid debugger port');
  const response=await fetch(`http://127.0.0.1:${port}/json/list`, {signal:AbortSignal.timeout(2500)});
  if(!response.ok) throw new Error('Desktop debugger unavailable');
  const targets=await response.json();
  const pages=targets.filter(t=>{
    try {const u=new URL(t.url);return t.type==='page'&&u.protocol==='https:'&&u.hostname==='127.0.0.1';}
    catch{return false;}
  });
  if(pages.length!==1) throw new Error('Ambiguous desktop window');
  const url=localUrl(pages[0].webSocketDebuggerUrl,port,'ws:');
  return new Promise((resolve,reject)=>{
    const ws=new WebSocket(url);
    const timer=setTimeout(()=>finish(new Error('Desktop read timeout')),2500);
    let done=false;
    function finish(error,value){
      if(done)return;done=true;clearTimeout(timer);ws.close();
      if(error)reject(error);else resolve(value);
    }
    ws.onopen=()=>ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression,returnByValue:true}}));
    ws.onerror=()=>finish(new Error('Desktop debugger unavailable'));
    ws.onclose=()=>{if(!done)finish(new Error('Desktop debugger closed'));};
    ws.onmessage=event=>{
      try {
        const reply=JSON.parse(event.data);
        if(reply.id!==1)return;
        if(reply.error||reply.result?.exceptionDetails)throw new Error('Desktop state read failed');
        finish(null,reply.result?.result?.value);
      } catch {finish(new Error('Invalid desktop response'));}
    };
  });
}

async function main() {
  const [debugPortFile]=process.argv.slice(2);
  if(!debugPortFile) throw new Error('Missing arguments');
  const states=await readDesktop(debugPortFile);
  process.stdout.write(JSON.stringify(selectModel(states))+'\n');
}

// Fixed, non-sensitive reason codes only: raw errors may carry local paths or UI data.
const REASONS={
  'Desktop debugger unavailable':'desktop_not_running','Desktop debugger closed':'desktop_not_running',
  'Invalid debugger port':'desktop_not_running','Desktop read timeout':'desktop_unresponsive',
  'Ambiguous desktop window':'ambiguous_window','Ambiguous desktop state':'desktop_state_unreadable',
  'Missing desktop selection':'desktop_state_unreadable','Desktop state read failed':'desktop_state_unreadable',
  'Invalid desktop response':'desktop_state_unreadable','Unavailable desktop model':'model_unavailable',
  'Invalid desktop model name':'model_unavailable','Non-local debugger':'non_local_debugger',
  'Missing arguments':'missing_arguments',
};
function failureReason(error){
  if(error&&Object.prototype.hasOwnProperty.call(REASONS,error.message))return REASONS[error.message];
  // No port file, connection refused, or a fetch timeout all mean nothing is listening.
  if(error&&(error.code==='ENOENT'||error.name==='TypeError'||error.name==='TimeoutError'||error.name==='AbortError'))return 'desktop_not_running';
  return 'unknown';
}
module.exports={selectModel,localUrl,readDesktop,failureReason};
if(require.main===module)main().catch(error=>{
  process.stderr.write('Antigravity desktop model could not be resolved; refusing fallback.\n');
  process.stderr.write('reason='+failureReason(error)+'\n');
  process.exitCode=1;
});
