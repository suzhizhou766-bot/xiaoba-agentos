"""LEAD TERMINAL ONLY: cases10/11 against switched scene, no live writes."""

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
import hashlib
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

R = _BIND.root
W = _BIND.work;O = _BIND.O
assert (_BIND.work/'BASE_READY.json').is_file()
scene=json.loads((O/'SCENE.json').read_text());root=Path(scene['root']).resolve()
assert root.is_relative_to(R/'rehearsal')
package=Path(scene['runtime'])/f'.runtime/releases/{VERSION}'
sys.path[:0]=[str(package/'agentos/src'),str(_BIND.source/'monitor')]
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore
from xiaoba_agentos import task_scores,review_ui_install
from xiaoba_review.store import ReviewStore,DecisionInbox
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.monitor import OpenReviewModule
from monitor_core.web import App,Context
from monitor_core import config as monitor_config

sha=lambda path:hashlib.sha256(Path(path).read_bytes()).hexdigest()
cfg=AppConfig.load(Path(scene['config']));assert cfg.database_path.resolve().is_relative_to(root)
native=EventStore(cfg.database_path)
store=ReviewStore(native,cfg.runtime_dir/'memory-outbox',root/'agent-workspaces')
backends=BackendRegistry(store)
ctx=Context(monitor_config.load(root/'unused-void-monitor.json',{'port':0}),logging.getLogger('card63-void'),0)
app=App(ctx);module=OpenReviewModule(ctx,store,DecisionInbox(False),backends);app.register(module)
server=app.server();port=server.server_address[1];base='http://127.0.0.1:'+str(port)
ctx.port=port;ctx.hosts={'127.0.0.1:'+str(port)};ctx.origins={base}
thread=threading.Thread(target=server.serve_forever);thread.start()
report={'ok':False,'sections':{},'live_writes':0}

def post(route,value):
    request=urllib.request.Request(base+route,json.dumps(value).encode(),headers={'Content-Type':'application/json','Origin':base,'X-Xiaoba-Token':ctx.page_token})
    try:
        with urllib.request.urlopen(request,timeout=60) as response:return response.status,json.loads(response.read())
    except urllib.error.HTTPError as exc:return exc.code,json.loads(exc.read())


def rows():
    with native.read_only() as db:
        return [dict(row,data=json.loads(row['data'])) for row in db.execute('SELECT * FROM ri_task_assessments')]


def assess(row,outcome,assessment_id,revision,reason='TEST isolated system defect'):
    value=row['data']
    return post('/api/agent-pool/task-assess',{'node_id':value['node_id'],'day':value['date'],'task_id':value['task_id'],
        'task_type':value['task_type'],'outcome':outcome,'role':value['role'],'assessment_id':assessment_id,
        'expected_revision':revision,'void_reason':reason,'evidence_refs':['TEST card63 scene: '+value['task_id']]})


def decorated(value):
    with native.read_only() as db:
        return task_scores.decorate_nodes(db,[{'node_id':value['node_id'],'source_ref':value['source_ref'],'model':value['model']}],
            day=value['date'],role=value['role'])[0]


try:
    # A real ordinary task previously completed via signed Runner/Worker + fake file-receipt CLIs.
    ordinary=json.loads((O/'35-card57-default-task.json').read_text())
    assert ordinary['ok'] and ordinary['task']['status']=='completed'
    good=next(row for row in rows() if row['data']['task_id']==ordinary['task']['id'] and row['data']['points']==1)
    value=good['data'];day=value['date']
    with native.read_only() as db:
        schema_before=[tuple(row) for row in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
    before=module.pool.runtime.scoring.weekly(day)
    selected_before=decorated(value)
    code,void_good=assess(good,'void','card63-v1614-r7-good-void',good['revision'])
    assert code==200 and void_good['result']['outcome']=='void'
    after=module.pool.runtime.scoring.weekly(day)
    selected_after=decorated(value)
    assert selected_after['weekly_score']['points']==selected_before['weekly_score']['points']-1
    assert selected_after['weekly_score']['tasks']==selected_before['weekly_score']['tasks']-1
    assert selected_after['recent_role_samples']==selected_before['recent_role_samples']-1
    assert sum(v['points'] for v in after['ranking'])==sum(v['points'] for v in before['ranking'])-1
    assert sum(v['tasks'] for v in after['ranking'])==sum(v['tasks'] for v in before['ranking'])-1
    code,restored_good=assess(good,'good','card63-v1614-r7-forbid-restore',good['revision']+1)
    assert code==400
    # Failed-task sample uses native run/settlement APIs, never SQL-inserted score rows.
    failed=native.submit('TEST card63 system-defect failure fixture',1)
    run=native.start_agent_run(failed,value['node_id'],{'purpose':value['role']})
    native.finish_agent_run(run,'permanent_error',None,'TEST isolated system defect',1)
    native.cancel_task(failed)
    bad=next(row for row in rows() if row['data']['task_id']==failed)
    assert bad['data']['points']==0 and bad['data']['task_completed'] is False
    failed_before=module.pool.runtime.scoring.weekly(day)
    failed_selected_before=decorated(bad['data'])
    code,bad_good=assess(bad,'good','card63-v1614-r7-no-upgrade',bad['revision'])
    assert code==400
    code,void_bad=assess(bad,'void','card63-v1614-r7-bad-void',bad['revision'])
    assert code==200 and void_bad['result']['outcome']=='void'
    failed_after=module.pool.runtime.scoring.weekly(day)
    failed_selected_after=decorated(bad['data'])
    assert failed_selected_after['recent_role_samples']==failed_selected_before['recent_role_samples']-1
    assert failed_selected_after['weekly_score']['tasks']==failed_selected_before['weekly_score']['tasks']-1
    assert failed_selected_after['weekly_score']['points']==failed_selected_before['weekly_score']['points']
    assert sum(v['points'] for v in failed_after['ranking'])==sum(v['points'] for v in failed_before['ranking'])
    assert sum(v['tasks'] for v in failed_after['ranking'])==sum(v['tasks'] for v in failed_before['ranking'])-1
    code,bad_restore=assess(bad,'good','card63-v1614-r7-bad-restore',bad['revision']+1)
    assert code==400
    with native.read_only() as db:
        audit=task_scores.audit_ledger_tx(db);assert audit['ok']
        counted=[row for row in audit['valid_rows'] if row.get('outcome')!='void' and row['week_start']==task_scores.week_of(day)[0]]
        expected=task_scores.weekly(counted,day)
        assert expected==failed_after
        assert all(row['success_rate']==row['good_tasks']/row['tasks'] for row in failed_after['ranking'])
        schema_after=[tuple(row) for row in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name')]
        assert schema_before==schema_after
        void_events=[json.loads(row[0]) for row in db.execute("SELECT payload_json FROM events WHERE event_type='task_scoring.assessed' AND task_id IN (?,?) ORDER BY sequence",(failed,value['task_id']))]
    assert {event['delta'] for event in void_events if event.get('void')}=={-1,0}
    report['sections']['10_void']={'ok':True,'before':before,'after_good_void':after,'failed_before':failed_before,'failed_after':failed_after,
        'void_events':void_events,'failed_upgrade_refused':bad_good,'void_restore_refused':bad_restore,'schema_unchanged':True,
        'selection_before':selected_before,'selection_after':selected_after,
        'failed_selection_before':failed_selected_before,'failed_selection_after':failed_selected_after,
        'ordinary_success_from_real_worker_fake_cli':True,'failure_fixture':'native run failure + cancel settlement API; no score-table injection'}
    # Immutable manifest baseline must already equal actual old live values.
    assets=package/'agentos/src/xiaoba_agentos/review_ui_assets';manifest_path=assets/'manifest.json'
    manifest_hash=sha(manifest_path);manifest=json.loads(manifest_path.read_text())
    previous=Path(scene['previous_package'])/'agentos/src/xiaoba_agentos/review_ui_assets'
    old={entry['asset']:entry for entry in json.loads((previous/'manifest.json').read_text())['files']}
    sandbox=root/'specials59-ui';project=sandbox/'project';assistant=sandbox/'assistant';runtime=sandbox/'runtime';runtime.mkdir(parents=True)
    roots={'project':project,'assistant':assistant};live_roots={'project':Path('/Users/suzhizhou/Documents/小八AgentOS'),'assistant':Path('/Users/suzhizhou/Documents/私人助理')}
    facts=[]
    for entry in manifest['files']:
        actual=live_roots[entry['base']]/entry['relative']
        assert sha(actual)==entry['before_sha256']==old[entry['asset']]['after_sha256']
        target=roots[entry['base']]/entry['relative'];target.parent.mkdir(parents=True,exist_ok=True)
        # Test input bytes are the preceding published asset, never a rebased manifest.
        data=(previous/entry['asset']).read_bytes();assert hashlib.sha256(data).hexdigest()==entry['before_sha256']
        with target.open('xb') as handle:handle.write(data)
        facts.append({'asset':entry['asset'],'live_sha256':sha(actual),'before_sha256':entry['before_sha256']})
    install_cfg=SimpleNamespace(project_dir=project,runtime_dir=runtime,memory={'obsidian_vault':str(assistant/'vault')})
    plan=review_ui_install.run(install_cfg,apply=False,assets=assets);assert plan['ok']
    applied=review_ui_install.run(install_cfg,apply=True,assets=assets);assert applied['ok']
    for entry in manifest['files']:
        assert sha(roots[entry['base']]/entry['relative'])==entry['after_sha256']
    assert sha(manifest_path)==manifest_hash
    report['sections']['11_ui_install']={'ok':True,'live_baseline':facts,'plan':plan,'apply':applied,'manifest_unchanged':True,'baseline_rewritten':False,'sandbox':str(sandbox)}
    assert native.verify_event_chain()['ok']
    report['ok']=True
finally:
    server.shutdown();server.server_close();thread.join();backends.close()
    (O/'48-v1614-new-cases.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'ok':report['ok'],'sections':list(report['sections']),'live_writes':0},ensure_ascii=False))
assert report['ok']
