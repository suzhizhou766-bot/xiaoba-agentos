"""Isolated native page endpoints. Exactly one test per node; stop on failure."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bind import load_bind
_BIND = load_bind()
_BIND.require_ready()
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'
os.chdir(_BIND.root)
import hashlib,json,logging,re,threading,urllib.request,urllib.error
from pathlib import Path
from leaf_contracts import resolve_post_switch_package, verify_same_package_modules
R=_BIND.root;A=_BIND.A;report=_BIND.O
report.mkdir(parents=True, exist_ok=True)
s=json.loads((A/'STATE.json').read_text());scene=json.loads((report/'SCENE.json').read_text());s.update(config=scene['config'],workspace_root=str(Path(scene['root'])/'agent-workspaces'))
# Post-switch: same new package for core + review (never previous_package 14 core + 15 review mix).
_switched=resolve_post_switch_package(_BIND, s, scene)
PKG=Path(_switched['package']);SRC=Path(_switched['agentos_src']);_ingress=Path(_switched['review_ingress_root'])
_monitor = _BIND.source / 'monitor'
sys.path[:0]=[str(SRC), str(_ingress), str(_monitor)]
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos import store as event_store_mod
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.pool_dispatch import REVIEW_HEADER_FIELDS
from xiaoba_agentos import workday_ledger as workday_ledger_mod
from xiaoba_agentos.release import release_binding_status
from xiaoba_review.store import ReviewStore,DecisionInbox
from xiaoba_review import store as review_store_mod
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.monitor import OpenReviewModule
from monitor_core.web import App,Context
from monitor_core import config
def dump(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
_module_origins=verify_same_package_modules(
    SRC, _ingress, _BIND.previous_package,
    event_store_mod=event_store_mod, review_store_mod=review_store_mod, workday_ledger_mod=workday_ledger_mod)
_module_origins['package']=str(PKG);_module_origins['version']=_switched['version']
_module_origins['release_sha256']=_switched['release_sha256']
dump(report/'31-post-switch-module-origins.json',_module_origins)
cfg=AppConfig.load(Path(s['config']));binding=release_binding_status(cfg)
assert binding['ok'],binding
native=EventStore(cfg.database_path);native.initialize()
store=ReviewStore(native,cfg.runtime_dir/'memory-outbox',Path(s['workspace_root']))
backends=BackendRegistry(store,resource_factory=lambda source:json_stdio_factory(source,store.workspace_root))
ctx=Context(config.load(cfg.runtime_dir/'unused-monitor.json',{'port':0}),logging.getLogger('card49'),0)
app=App(ctx);module=OpenReviewModule(ctx,store,DecisionInbox(False),backends);app.register(module)
server=app.server();port=server.server_address[1];base='http://127.0.0.1:'+str(port)
ctx.port=port;ctx.hosts={'127.0.0.1:'+str(port)};ctx.origins={base}
t=threading.Thread(target=server.serve_forever);t.start()
def post(path,value):
 req=urllib.request.Request(base+path,json.dumps(value).encode(),headers={'Content-Type':'application/json','Origin':base,'X-Xiaoba-Token':ctx.page_token})
 try:
  with urllib.request.urlopen(req,timeout=180) as response:return {'code':response.status,'body':json.loads(response.read())}
 except urllib.error.HTTPError as e:return {'code':e.code,'body':json.loads(e.read())}
fake_root=Path(scene['root'])/'fake-cli';fake_root.mkdir()
FAKE='\nimport json, os, re, sys\nfrom pathlib import Path\nroot = Path(__file__).resolve().parent\nstdin = sys.stdin.read()\ncalls = root / (Path(__file__).name + ".calls")\nwith calls.open("a") as out:\n    out.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin[:800]}) + "\\n")\nif (root / (Path(__file__).name + ".no-receipt")).exists():\n    sys.exit(0)\nif "XIAOBA_TASK_CARD=" not in stdin:\n    sys.exit(2)\npath = json.loads(stdin.split("XIAOBA_TASK_CARD=", 1)[1].split("\\n", 1)[0])\ncard = Path(path).read_text()\nlogpath = Path(re.search(r"^工作回执文件: (.+)$", card, re.M)[1])\ntaskid = re.search(r"^任务ID: (.+)$", card, re.M)[1]\nstep = re.search(r"^步骤码: (.+)$", card, re.M)[1]\nnonce = re.search(r"^本轮标识: (.+)$", card, re.M)[1]\nctx = json.loads(card.split("## 材料\\n", 1)[1].split("\\n\\n## 范围与回执", 1)[0])\nbody = ctx["challenge"] if "challenge" in ctx else "RECEIPT_BODY_FROM_" + Path(__file__).name\nif not logpath.exists():\n    header = card.split("先写以下文件头：\\n\\n", 1)[1].split("\\n文件已存在时", 1)[0]\n    logpath.write_text(header)\nmeta = "任务ID: " + taskid + "\\n步骤码: " + step + "\\n状态: completed\\n"\nordinary_mode = (root / (Path(__file__).name + ".ordinary-answer")).exists()\nif ordinary_mode:\n    body = "TEST ordinary answer: A clear checklist breaks work into concrete steps, exposes missing inputs, and makes completion easy to verify."\nif step == "REVIEW-01" and not ordinary_mode:\n    meta += ("审查结论: PASS\\n证据引用: TEST fake cli receipt\\n审查类型: new_experience\\n"\n             "评分: 8\\n适用条件: 仅测试夹具\\n重复经验: \\n")\nwith logpath.open("a") as out:\n    out.write("\\n<!-- task:" + nonce + " -->\\n" + meta + "---\\n" + body + "\\n<!-- done:" + nonce + " -->\\n")\nprint(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))\n'
for name in ('codex','claude'):
 p=fake_root/name;p.write_text('#!'+sys.executable+'\n'+FAKE);p.chmod(0o700)
results={'binding':binding,'nodes':[],'ok':False}
try:
 for kind in ('codex','claude'):
  entry=str(fake_root/kind)
  nid='card49-fake-'+kind
  node={'node_id':nid,'name':nid,'agent':kind.upper(),'kind':'review','interface':'cli','entry':entry,'model':'','agent_type':kind,'no_progress_seconds':1800}
  rec={'node':node};results['nodes'].append(rec)
  rec['add']=post('/api/agent-pool/nodes',node);dump(report/'31-fake-node-tests.json',results)
  assert rec['add']['code']==200,rec
  revision=rec['add']['body']['result']['node']['revision']
  rec['enable']=post('/api/agent-pool/toggle',{'node_id':nid,'expected_revision':revision,'enabled':True})
  assert rec['enable']['code']==200,rec
  revision=rec['enable']['body']['result']['node']['revision']
  print(json.dumps({'phase':'node_test_started','node':nid,'no_progress_seconds':1800}),flush=True)
  rec['test']=post('/api/agent-pool/test',{'node_id':nid,'expected_revision':revision,'test_id':'card63-v1614-r7-fake-'+kind+'-once-v1614-r7'})
  dump(report/'31-fake-node-tests.json',results)
  print(json.dumps({'phase':'node_test_finished','node':nid,'test':rec['test']},ensure_ascii=False),flush=True)
  assert rec['test']['code']==200 and rec['test']['body']['result']['status']=='passed',rec['test']
 results['ok']=True
finally:
 results['pool_state']=module.pool.state()
 server.shutdown();server.server_close();t.join();backends.close()
 results['http_closed']=True;dump(report/'31-fake-node-tests.json',results)


import subprocess,os,time
assert results['ok']
with native.read_only() as db:
 score_total_before=db.execute("SELECT COUNT(*) FROM ri_task_assessments").fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='ri_task_assessments'").fetchone() else 0
q=subprocess.run([sys.executable,'-B','-m','xiaoba_agentos.cli','--config',str(scene['config']),'submit','仅演练假CLI文件回执，不调用真实模型','--memory-kind','audit'],cwd=Path(scene['runtime']),env=dict(os.environ,PYTHONPATH=str(SRC),PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True)
assert q.returncode==0,q.stdout+q.stderr
task=json.loads(q.stdout)['task_id']
end=time.monotonic()+90
while time.monotonic()<end:
 row=native.get_task(task)
 if row['status'] in ('completed','failed','frozen_paused'):break
 time.sleep(1)
runs=native.task_runs(task)
checks={'task':row,'runs':runs,'bound_root':str(store.workspace_root),'calls':{name:[json.loads(x) for x in (fake_root/(name+'.calls')).read_text().splitlines()] for name in ('codex','claude')}}
dump(report/'32-fake-dispatch.json',checks)
assert row['status']=='completed',checks
assert {r['agent_name'] for r in runs}=={'card49-fake-codex','card49-fake-claude'},checks
assert all('RECEIPT_BODY_FROM_' in r['output'] for r in runs),checks
assert all(len(v)>=2 and all(Path(c['cwd']).is_relative_to(store.workspace_root) for c in v) for v in checks['calls'].values()),checks
# Card51: same actual Runner/Worker task must acquire automatic score rows.
from xiaoba_agentos.health import doctor
with native.read_only() as db:
    scores=[dict(r) for r in db.execute("SELECT * FROM ri_task_assessments WHERE json_extract(data,'$.task_id')=?",(task,))]
values=[json.loads(r['data']) for r in scores]
health=doctor(cfg,native)
assessment={'ok':False,'task_id':task,'ledger_total_before_submit':score_total_before,'task_score_rows_after':len(scores),'rows':scores,'task_scores':health.get('task_scores')}
assert len(scores)==2,assessment
assert {v['node_id'] for v in values}=={'card49-fake-codex','card49-fake-claude'},assessment
assert all(v['points']==1 and v['receipt_accepted'] and v['task_completed'] for v in values),assessment
assert health['task_scores']['ok'] is True,assessment
assessment['ok']=True;dump(report/'34-card51-worker-scores.json',assessment)
print(json.dumps({'ok':True,'task_id':task,'reply_source':'daily_worklog','bound_root':str(store.workspace_root),'worker_dispatched':True}))

# Card57: a default submit, ordinary answers only, executed by the still-running Runner Worker.
for name in ('codex','claude'):
 (fake_root/(name+'.ordinary-answer')).open('x').close()
plain_prompt='TEST card58 default ordinary question: explain why a clear checklist helps finish work.'
plain_args=[sys.executable,'-B','-m','xiaoba_agentos.cli','--config',str(scene['config']),'submit',plain_prompt]
plain=subprocess.run(plain_args,cwd=Path(scene['runtime']),env=dict(os.environ,PYTHONPATH=str(SRC),PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True)
assert plain.returncode==0,plain.stdout+plain.stderr
plain_id=json.loads(plain.stdout)['task_id'];end=time.monotonic()+90
while time.monotonic()<end:
 plain_task=native.get_task(plain_id)
 if plain_task['status'] in ('completed','failed','frozen_paused'):break
 time.sleep(1)
plain_runs=native.task_runs(plain_id);candidate=native.get_memory_candidate(plain_id)
with native.read_only() as db:
 unscored=[dict(r) for r in db.execute("SELECT * FROM events WHERE task_id=? AND event_type='memory.value_unscored'",(plain_id,))]
 plain_scores=[json.loads(r[0]) for r in db.execute("SELECT data FROM ri_task_assessments WHERE json_extract(data,'$.task_id')=?",(plain_id,))]
plain_report={'ok':False,'argv':plain_args,'task':plain_task,'runs':plain_runs,'candidate':candidate,'value_unscored_events':unscored,'score_rows':plain_scores,'worker_task_id':plain_id}
dump(report/'35-card57-default-task.json',plain_report)
assert plain_task['status']=='completed' and candidate['status']=='pending_delivery_review',plain_report
assert len(unscored)==1,plain_report
assert len(plain_runs)==2 and {r['agent_name'] for r in plain_runs}=={'card49-fake-codex','card49-fake-claude'},plain_report
assert all(r['status']=='completed' for r in plain_runs),plain_report
review_header_pattern = re.compile(r'^\s*(?:' + '|'.join(re.escape(field) for field in REVIEW_HEADER_FIELDS) + r')\s*[:：]', re.I | re.M)
assert not any(review_header_pattern.search(run.get('output') or '') for run in plain_runs),plain_report
assert len(plain_scores)==2 and {v['node_id'] for v in plain_scores}=={'card49-fake-codex','card49-fake-claude'} and all(v['points']==1 and v['receipt_accepted'] and v['task_completed'] for v in plain_scores),plain_report
plain_report['ok']=True;dump(report/'35-card57-default-task.json',plain_report)
