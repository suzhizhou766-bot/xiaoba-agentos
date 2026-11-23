"""Cards52/52b in a scratch scene DB/vault, using sealed package APIs and real Center state."""

# --- rebound by stage75q ---
"""stage75q helper — rebound from mature v1614-r7 source; paths via STAGE75Q_BIND_JSON."""
from pathlib import Path
import os, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bind import load_bind
_BIND = load_bind()
_BIND.require_ready()
R = _BIND.root
O = _BIND.O
A = _BIND.A
W = _BIND.W
C = _BIND.C
PKG = _BIND.PKG
VERSION = _BIND.version
LIVE = _BIND.live
P = LIVE
os.chdir(R)
# --- end bind header; original body follows with path overrides ---
from pathlib import Path
# Pending v1614-r7 must never consume historical evidence or run before new-BASE preparation.
assert (_BIND.work / 'BASE_READY.json').is_file(), 'STOP: BASE_READY missing'
import os,sys,json,time,hashlib,importlib.util
R = _BIND.root;os.chdir(R);O = _BIND.O
scene=json.loads((O/'SCENE.json').read_text());root=Path(scene['root']).resolve();assert root.is_relative_to(R/'rehearsal')
PKG=Path(scene['runtime'])/f'.runtime/releases/{VERSION}';sys.path.insert(0,str(PKG/'agentos/src'))
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.experience_loop import record_application,record_delivery
from xiaoba_agentos.health import doctor
from xiaoba_agentos.formal_review import items,due
scratch=root/'specials52';scratch.mkdir();vault=scratch/'vault';vault.mkdir();project=scratch/'evidence';project.mkdir()
raw=json.loads(Path(scene['config']).read_text());raw['runtime_dir']=str(scratch/'runtime')
for k in ('memory','paths'):raw[k].update(obsidian_vault=str(vault),knowlp_vault=str(vault),knowlp_graph_dir=str(scratch/'graph'))
raw['memory'].update(publish_enabled=False,knowlp_enabled=False)
cp=scratch/'config.json';cp.write_text(json.dumps(raw,ensure_ascii=False,indent=2)+'\n')
cfg=AppConfig.load(cp);store=EventStore(cfg.database_path);store.initialize();store.apply_validation_extension()
report={'ok':False,'root':str(root),'scratch':str(scratch),'sections':{}}
def dump(): (O/'42-card52-52b.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
def events(kind):
 with store.read_only() as db:return [dict(r) for r in db.execute('SELECT * FROM events WHERE event_type=? ORDER BY sequence',(kind,))]
def file(name,text):
 p=project/name
 with p.open('x') as f:f.write(text)
 return {'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
def formal(label):
 c=store.create_authored_candidate('TEST card54 '+label,'TEST complete reusable method',cfg.runtime_dir/'memory-outbox',author='TEST-rehearsal',experience_kind='method',problem_key='card54-fixture')
 p=vault/(c['task_id']+'.md');p.write_text('---\nstatus: "active"\nsource_task_id: "'+c['task_id']+'"\n---\n\n# TEST complete method\nTEST fixture, scene only.\n')
 with store.connect() as db:
  db.execute('BEGIN IMMEDIATE');db.execute("UPDATE memory_candidates SET status='published',published_path=?,published_at=? WHERE task_id=?",(str(p),time.time(),c['task_id']))
  store._append_event_tx(db,c['task_id'],'rehearsal.formal_source_fixture',{'fake':True,'path':str(p)})
 return c,p
def use(c,p,label,outcome,draft=None):
 task=store.submit('TEST card54 actual reuse '+label,1)
 with store.connect() as db:db.execute('UPDATE tasks SET result=? WHERE id=?',('TEST result cites '+c['task_id'],task))
 art=dict(file(label+'-product.txt','TEST complete artifact'),version='v1');e=file(label+'-execution.txt','TEST actual scene execution evidence');d=file(label+'-delivery.md','TEST user delivery fixture authorized by card54')
 assess={'outcome':outcome,'changed':'TEST revised complete step' if outcome!='unchanged' else '', 'added':'','discarded':''}
 if draft:assess['draft']=draft
 app={'candidate_id':c['task_id'],'candidate_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'real_task':{'id':task,'problem_key':'card54-fixture','task_type':'TEST scene reuse','description':'TEST applied method in rehearsal','workspace':str(project),'authorization_ref':'TEST card54 user-authorized rehearsal'},'conditions_checked':{'applicability':'TEST matched','exclusions':'TEST none','environment':'TEST isolated scene'},'artifact':art,'application_location':'TEST scene step','steps_applied':'TEST complete method used','deviations':'TEST none' if outcome=='unchanged' else 'TEST modified step','execution_evidence':[e],'reuse_assessment':assess}
 record_application(store,app)
 delivery=record_delivery(store,{'origin':'user','user_message':'交付','message_id':'TEST-card54-'+task,'source_ref':'TEST user identity, rehearsal fixture only','real_task_id':task,'artifact':art,'delivery_record':d})
 return task,delivery
try:
 # Real Worker idle path enables the inactivity clock, without dispatching any node.
 c,p=formal('unchanged')
 assert not events('memory.formal_enabled')
 idle=Orchestrator(cfg,store).run_once(preflight_when_idle=False)
 assert idle['status']=='idle',idle
 enabled=events('memory.formal_enabled');assert len(enabled)==1,enabled
 assert not events('memory.formal_batch_created')
 report['sections']['card52b_worker_idle']={'ok':True,'worker_result':idle,'enabled_events':enabled,'today_due_batches':0};dump()
 before=len(events('memory.reuse_scored'));task,d=use(c,p,'unchanged','unchanged')
 scored=events('memory.reuse_scored');new=scored[before:];assert len(new)==1,new
 v=json.loads(new[0]['payload_json']);assert v['source_task_id']==c['task_id'] and v['real_task_id']==task and v['score']==1,v
 health=doctor(cfg,store);assert health['reuse_scores']['ok'] is True,health['reuse_scores']
 report['sections']['card52_reuse_plus_one']={'ok':True,'task_id':task,'event':new[0],'reuse_scores':health['reuse_scores'],'user_delivery_fixture':True};dump()
 c2,p2=formal('modified');draft=file('complete-revision.md','# TEST revised complete method\n\nTEST new full steps, conditions and exclusions.')
 task2,_=use(c2,p2,'modified','modified',draft)
 matches=[i for i in items(store) if i.get('source_task_id')==c2['task_id'] and i.get('real_task_id')==task2]
 assert len(matches)==1,matches
 request=matches[0]
 assert request['source_sha256']==hashlib.sha256(p2.read_bytes()).hexdigest(),request
 assert request['entry_type']=='reuse_change' and request['status']=='awaiting_review' and request['draft']==draft,request
 assert 'TEST new full steps' in request['body'] and 'approve' not in request['allowed_decisions'],request
 # Use the actual adjudication Center query path, with scene verifier/config; no mocks or approval.
 spec=importlib.util.spec_from_file_location('card54_scene_center',PKG/'agentos/src/xiaoba_agentos/review_ui_assets/server.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
 center=mod.Center(root=scratch,port=0,config_path=cp);center.verifier=Path(scene['entries'][0])/'scripts/verify-release'
 state=center.state();listed=next(i for i in state['items'] if i['task_id']==request['task_id'])
 assert listed['entry_type']=='reuse_change' and listed['status']=='awaiting_review',listed
 assert not events('memory.formal_decided') and store.get_memory_candidate(c2['task_id'])['status']=='published'
 report['sections']['card52b_revision_list']={'ok':True,'request':listed,'center_state_ok':state['ok'],'draft':draft,'auto_approved':False,'formal_decided_events':0};dump()
 with store.read_only() as db:assert not due(db,time.time())
 assert len(events('memory.formal_enabled'))==1 and not events('memory.formal_batch_created')
 assert store.verify_event_chain()['ok']
 report['ok']=True
finally:
 dump();print(json.dumps({'ok':report['ok'],'sections':list(report['sections'])},ensure_ascii=False))
