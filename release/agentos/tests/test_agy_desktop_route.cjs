const test=require('node:test');
const assert=require('node:assert/strict');
const {selectModel,localUrl,failureReason}=require('../scripts/agy_desktop_route.cjs');
const models=[
  {label:'Gemini 3.8 Flash (High)',choice:{case:'model',value:1318},disabled:false},
  {label:'Claude Opus 4.6 (Thinking)',choice:{case:'model',value:1026},disabled:false},
];
const listing='Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n';
test('forwards backend display names without an upstream availability query',()=>{
  assert.equal(selectModel([{selected:1318,models}]).model,'Gemini 3.8 Flash (High)');
  assert.equal(selectModel([{selected:1026,models}]).model,'Claude Opus 4.6 (Thinking)');
  const future=[{label:'Future backend model',choice:{case:'model',value:9999}}];
  assert.equal(selectModel([{selected:9999,models:future}]).model,'Future backend model');
});
test('rejects missing, unsupported, disabled, alias and ambiguous selections',()=>{
  for(const states of [[],[{},{}],[{}],[{selected:999,models}],
    [{selected:1318,models:[{...models[0],disabled:true}]}],
    [{selected:1318,models:[{...models[0],choice:{case:'alias',value:1318}}]}]]){
    assert.throws(()=>selectModel(states,listing));
  }
  assert.throws(()=>selectModel([{selected:1318,models:[models[0],models[0]]}]));
});
test('debugger connections must stay on the discovered loopback port',()=>{
  assert.equal(localUrl('ws://127.0.0.1:1234/devtools/page/x',1234,'ws:').hostname,'127.0.0.1');
  for(const url of ['ws://example.com:1234/x','ws://127.0.0.1:2345/x','ws://u:p@127.0.0.1:1234/x','https://127.0.0.1:1234/x'])
    assert.throws(()=>localUrl(url,1234,'ws:'));
});
test('failures map to fixed reason codes and never echo raw errors',()=>{
  assert.equal(failureReason(new Error('Ambiguous desktop window')),'ambiguous_window');
  assert.equal(failureReason(new Error('Desktop debugger unavailable')),'desktop_not_running');
  assert.equal(failureReason(Object.assign(new Error('no such file /Users/secret/path'),{code:'ENOENT'})),'desktop_not_running');
  assert.equal(failureReason(new TypeError('fetch failed')),'desktop_not_running');
  assert.equal(failureReason(new Error('Missing desktop selection')),'desktop_state_unreadable');
  assert.equal(failureReason(new Error('Unavailable desktop model')),'model_unavailable');
  assert.equal(failureReason(new Error('something with /private/data')),'unknown');
});
