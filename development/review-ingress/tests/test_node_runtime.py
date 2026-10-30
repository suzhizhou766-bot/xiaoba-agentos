"""Real child-process protocol fixtures; no real model or human acceptance."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sys
import threading
import unittest
from pathlib import Path
import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool, node_state
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.store import canonical, Conflict


class RuntimeTests(fixtures.FixtureMixin,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.backends=BackendRegistry(self.store,resource_factory=json_stdio_factory)
        self.pool=AgentPool(self.store,self.backends);self.runtime=self.pool.runtime
        self.fixture('peer');self.fixture('dsh')

    def fixture(self,kind,wrong=False):
        path=self.root/('TEST-'+kind);marker=self.root/('calls-'+kind)
        code='#!'+sys.executable+'\n'+'''import json,sys
from pathlib import Path
v=json.load(sys.stdin)
p=Path(__file__).with_name('calls-'''+kind+'''')
with p.open('a') as out:out.write(v['operation']+'\\n')
if v['operation']=='test':
    r={'protocol':'xiaoba-resource-v1','challenge':v['challenge'],'output':v['challenge'],
       'model':v['resource_plan']['model'],'provider':'TEST child process, not a model','route_source':'TEST isolated stdio'}
else:
    c=v['card'];r={'protocol':'xiaoba-resource-v1','card_digest':c['card_digest'],'verdict':'PASS',
       'findings':'TEST protocol fixture only; no real model review or user acceptance.',
       'evidence_refs':[x['ref'] for x in c.get('evidence_refs',[]) if isinstance(x,dict)] or ['TEST subprocess'],
       'execution_evidence':{'provider':'TEST subprocess','model':c['resource_plan']['model'],'route_source':'TEST isolated stdio'}}
    if c.get('review_type'):r.update(review_type=c['review_type'],score=8,applicability='TEST 仅夹具适用条件')
if Path(__file__).with_name('wrong-'''+kind+'''').exists():r['model']='TEST incorrect model'
if Path(__file__).with_name('fail-'''+kind+'''').exists():raise RuntimeError('TEST execution failed')
print(json.dumps(r))
'''
        path.write_text(code);path.chmod(0o700)
        return path

    def add(self,kind='peer',node_id=None,entry=None):
        r=self.runtime.onboard({'node_id':node_id or 'TEST-node-'+kind,'name':'用户命名 '+kind,
            'interface':'cli','entry':str(entry or self.root/('TEST-'+kind)),'model':'TEST-user-model'},kind)
        with self.store.native.read_only() as db:source=self.store.source(db,r['node_id'])
        self.pool.change(source['reviewer_id'],source['node']['revision'],'toggle',True)
        return r['node_id']

    def node(self,node_id):
        return next(n for n in self.pool.state()['nodes'] if n['node_id']==node_id)

    def probe(self,node_id,request='TEST-test-1'):
        return self.runtime.test_run(node_id,self.node(node_id)['revision'],request)

    def calls(self,kind='peer'):
        p=self.root/('calls-'+kind)
        return p.read_text().splitlines() if p.exists() else []

    def test_conversation_only_asks_missing_model_without_writing(self):
        result=self.runtime.onboard({'name':'我的自定义Agent','interface':'desktop','entry':'用户指定入口'})
        self.assertEqual([q['field'] for q in result['questions']],['model'])
        self.assertEqual(len(self.pool.state()['nodes']),3)
        self.assertEqual(self.calls(),[])

    def test_complete_conversation_is_user_named_disabled_and_idempotent(self):
        draft={'name':'用户自定名','interface':'web','entry':'https://example.org/chat','model':'user-model'}
        one=self.runtime.onboard(draft);two=self.runtime.onboard(draft)
        self.assertEqual(one['node_id'],two['node_id'])
        node=self.node(one['node_id']);self.assertEqual(node['name'],'用户自定名');self.assertEqual(node['agent'],'用户自定名')
        self.assertFalse(node['enabled']);self.assertFalse(node['readiness']['ready'])

    def test_unconfigured_required_slot_asks_four_user_fields(self):
        with self.store.native.connect() as db:db.execute('DELETE FROM ri_sources')
        result=self.runtime.prepare(['peer'])
        self.assertEqual({q['field'] for q in result['questions']},{'name','interface','entry','model'})
        self.assertEqual(result['route'],'ask_user')

    def test_real_child_process_test_then_prepare_runs_without_questions(self):
        node=self.add();self.assertEqual(self.runtime.prepare(['peer'])['route'],'ask_user')
        self.assertEqual(self.probe(node)['status'],'passed');self.assertEqual(self.calls(),['test'])
        ready=self.runtime.prepare(['peer'])
        self.assertEqual(ready,{'route':'run','questions':[],'selections':{'peer':node}})
        self.assertTrue(self.node(node)['readiness']['ready'])
        evidence=self.node(node)['test']['execution_evidence']
        self.assertEqual(evidence['model'],'TEST-user-model');self.assertEqual(evidence['challenge'],evidence['output'])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_restart_retains_current_test_and_does_not_probe_again(self):
        node=self.add();self.probe(node)
        restarted=AgentPool(self.store,BackendRegistry(self.store,resource_factory=json_stdio_factory))
        self.assertEqual(restarted.runtime.prepare(['peer'])['route'],'run');self.assertEqual(self.calls(),['test'])

    def test_disabled_blocks_then_reenabled_same_configuration_keeps_test(self):
        node=self.add();self.probe(node)
        self.pool.change(node,self.node(node)['revision'],'toggle',False)
        self.assertEqual(self.runtime.prepare(['peer'])['route'],'ask_user')
        self.pool.change(node,self.node(node)['revision'],'toggle',True)
        self.assertEqual(self.runtime.prepare(['peer'])['route'],'run');self.assertEqual(self.calls(),['test'])

    def test_test_id_retry_does_not_rerun_and_stale_revision_rejected(self):
        node=self.add();before=self.node(node)['revision'];one=self.probe(node)
        self.assertEqual(self.runtime.test_run(node,before,'TEST-test-1'),one);self.assertEqual(self.calls(),['test'])
        with self.assertRaises(Conflict):self.runtime.test_run(node,before,'TEST-new-id')

    def test_program_change_invalidates_pass_and_original_retry(self):
        node=self.add();self.probe(node)
        with (self.root/'TEST-peer').open('a') as out:out.write('\n# TEST changed entry\n')
        self.assertFalse(self.node(node)['readiness']['ready']);self.assertEqual(self.runtime.prepare(['peer'])['route'],'ask_user')
        with self.assertRaises(Conflict):self.probe(node)
        self.assertEqual(self.calls(),['test'])

    def test_model_mismatch_is_failure_with_no_automatic_retry(self):
        node=self.add();(self.root/'wrong-peer').touch()
        self.assertEqual(self.probe(node)['status'],'failed');self.assertFalse(self.node(node)['readiness']['ready'])
        self.assertEqual(self.calls(),['test'])

    def test_unknown_desktop_and_missing_program_cannot_self_pass(self):
        node=self.runtime.onboard({'name':'TEST desktop','interface':'desktop','entry':'TEST app','model':'TEST model'})['node_id']
        self.pool.change(node,1,'toggle',True)
        self.assertEqual(self.probe(node)['reason'],'adapter_required')
        missing=self.add(node_id='TEST-missing',entry=self.root/'missing')
        self.assertEqual(self.probe(missing)['reason'],'entry_unavailable')
        self.assertEqual(self.calls(),[])

    def test_forged_pass_without_original_event_is_not_ready(self):
        node=self.add();self.probe(node)
        with self.store.native.connect() as db:
            source=self.store.source(db,node);source['node']['test']['evidence_digest']='f'*64
            db.execute('UPDATE ri_sources SET data=? WHERE id=?',(canonical(source),node))
        self.assertEqual(self.node(node)['readiness']['reason'],'test_evidence_invalid')
        self.assertEqual(self.runtime.prepare(['peer'])['route'],'ask_user')

    def test_explicit_bad_selection_never_falls_back_to_ready_other_node(self):
        node=self.add();self.probe(node)
        result=self.runtime.prepare(['peer'],{'peer':'TEST-no-such-node'})
        self.assertEqual(result['route'],'ask_user');self.assertEqual(result['selections'],{})

    def test_same_real_entry_cannot_supply_two_independent_slots(self):
        a=self.add('dsh',entry=self.root/'TEST-peer');b=self.add('peer')
        self.probe(a);self.probe(b)
        result=self.runtime.prepare(['dsh','peer'])
        self.assertEqual(result['route'],'ask_user');self.assertEqual(result['questions'][0]['field'],'independent_source')

    def test_multiple_ready_nodes_ask_selection_only(self):
        a=self.add();b=self.add(node_id='TEST-second',entry=self.root/'TEST-dsh')
        self.probe(a);self.probe(b)
        result=self.runtime.prepare(['peer'])
        self.assertEqual([q['field'] for q in result['questions']],['node_id'])
        self.assertEqual(self.runtime.prepare(['peer'],{'peer':a})['route'],'run')

    def test_broken_audit_chain_blocks_automatic_dispatch(self):
        node=self.add();self.probe(node)
        with self.native.connect() as db:db.execute("UPDATE events SET event_hash='TEST-broken' WHERE sequence=1")
        self.assertEqual(self.runtime.prepare(['peer'])['route'],'blocked')

    def test_direct_execute_uses_frozen_card_and_retains_legacy_behavior(self):
        a=self.add('dsh');b=self.add();self.probe(a);self.probe(b)
        request=self.store.create(self.candidate['task_id'],a,b,'TEST review',[{'ref':'TEST process','sha256':'a'*64}],dsh_channel='backend_adapter',peer_channel='backend_adapter')
        for assignment in request['assignments']:self.assertEqual(self.backends.execute(assignment['id'],require_test=True)['status'],'valid')
        self.assertEqual(self.calls(),['test','review']);self.assertEqual(self.calls('dsh'),['test','review'])
        self.assertEqual(self.count('memory_reviews'),1)
        self.assertEqual(len(self.store.get(request['id'])['receipts']),2)

    def test_actual_execution_failure_invalidates_pass_without_retry(self):
        a=self.add('dsh');b=self.add();self.probe(a);self.probe(b)
        request=self.store.create(self.candidate['task_id'],a,b,'TEST review',[{'ref':'TEST','sha256':'a'*64}],dsh_channel='backend_adapter',peer_channel='backend_adapter')
        (self.root/'fail-peer').touch();assignment=next(a for a in request['assignments'] if a['role']=='peer')
        with self.assertRaises(ValueError):self.backends.execute(assignment['id'],require_test=True)
        self.assertFalse(self.node(b)['readiness']['ready']);self.assertEqual(self.runtime.prepare(['peer'])['route'],'ask_user')
        self.assertEqual(self.calls(),['test','review'])

    def test_close_during_test_cannot_mark_pass(self):
        node=self.add();entered,finish=threading.Event(),threading.Event()
        class Adapter:
            supports_resource_plan=True;source_ref=self.node(node)['source_ref'];kind='peer'
            def resource_identity(self):return {'TEST':'blocking adapter'}
            def test_resource(self,plan,challenge):
                entered.set();finish.wait(5);return {'output':challenge,'model':plan['model'],'provider':'TEST','route_source':'TEST'}
        self.backends.register(node,Adapter())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.probe,node);self.assertTrue(entered.wait(3))
            self.pool.change(node,self.node(node)['revision'],'toggle',False);finish.set()
            self.assertEqual(future.result(5)['status'],'failed')
        self.assertFalse(self.node(node)['readiness']['ready'])

    def start_http(self):
        ctx=fixtures.Context(fixtures.config.load(self.root/'none.json',{'port':0}),fixtures.logs.setup(None),0)
        module=fixtures.OpenReviewModule(ctx,self.store,self.inbox,self.backends)
        app=fixtures.App(ctx);app.register(module);self.server=app.server();port=self.server.server_port
        ctx.port=port;ctx.hosts={'127.0.0.1:'+str(port)};ctx.origins={'http://127.0.0.1:'+str(port)};self.ctx=ctx
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    def test_http_new_actions_require_same_origin_and_token(self):
        self.start_http()
        for action in ('onboard','prepare','test','dispatch','interactive-confirm'):
            self.assertEqual(fixtures.HttpTest.fetch(self,'/api/agent-pool/'+action,{},False)[0],403)
        code,response=fixtures.HttpTest.fetch(self,'/api/agent-pool/onboard',{'draft':{'name':'用户名称','interface':'web','entry':'https://example.net'}})
        self.assertEqual(code,200,response)
        self.assertEqual([q['field'] for q in response['result']['questions']],['model'])

    def test_http_ready_pair_dispatches_without_questions_and_retry_is_once(self):
        a=self.add('dsh');b=self.add();self.probe(a);self.probe(b);self.start_http()
        payload={'candidate_id':self.candidate['task_id'],'scope':'TEST automatic review',
                 'evidence':[{'ref':'TEST process','sha256':'a'*64}],'request_id':'TEST-auto-1',
                 'review_type':'new_experience'}
        for _ in range(2):
            code,result=fixtures.HttpTest.fetch(self,'/api/agent-pool/dispatch',payload)
            self.assertEqual(code,200,result);self.assertEqual(result['result']['route'],'completed')
            self.assertEqual(result['result']['questions'],[])
        self.assertEqual(self.calls(),['test','review']);self.assertEqual(self.calls('dsh'),['test','review'])
        self.assertEqual(self.count('memory_reviews'),1);self.assertEqual(self.count('ri_requests'),1)

    def test_http_missing_backends_asks_without_creating_or_executing(self):
        self.start_http();payload={'candidate_id':self.candidate['task_id'],'scope':'TEST',
                 'evidence':[{'ref':'TEST','sha256':'a'*64}],'request_id':'TEST-no-nodes',
                 'review_type':'new_experience'}
        code,result=fixtures.HttpTest.fetch(self,'/api/agent-pool/dispatch',payload)
        self.assertEqual(code,200,result);self.assertEqual(result['result']['route'],'ask_user')
        self.assertEqual(self.count('ri_requests'),0);self.assertEqual(self.calls(),[])

    def count(self, table):
        with self.native.read_only() as db:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]
