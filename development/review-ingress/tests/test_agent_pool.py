"""Persistent Agent lifecycle and actual selection/call boundaries; TEST data only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import unittest

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool, node_state
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.store import ReviewStore, Conflict
from xiaoba_agentos.store import EventStore


class PoolTests(fixtures.FixtureMixin,unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.backends=BackendRegistry(self.store)
        self.pool=AgentPool(self.store,self.backends)

    def value(self, node_id='TEST-pool-peer', **changes):
        return dict({'node_id':node_id,'name':'TEST 文案节点','agent':'TEST Arbitrary Agent','kind':'peer',
                     'interface':'cli','entry':'/TEST/bin/arbitrary-agent','model':'TEST-model-1'},**changes)

    def add(self, **changes):
        return self.pool.add(self.value(**changes))

    def toggle(self, source, enabled):
        return self.pool.change(source['reviewer_id'],node_state(source)['revision'],'toggle',enabled)

    def request_node(self, source, **changes):
        return self.store.create(self.candidate['task_id'],'fixture-dsh',source['reviewer_id'],
                                 'TEST frozen node plan',[{'ref':'TEST evidence','sha256':'a'*64}],**changes)

    def count(self, table):
        with self.native.read_only() as db:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_add_starts_disabled_and_survives_restart_without_approving_anything(self):
        source=self.add(); self.assertFalse(source['node']['enabled'])
        restarted=AgentPool(ReviewStore(EventStore(self.native.path),self.root/'memory-outbox'),BackendRegistry(self.store))
        node=next(n for n in restarted.state()['nodes'] if n['node_id']==source['reviewer_id'])
        self.assertEqual(node['model'],'TEST-model-1');self.assertFalse(node['backend_available'])
        self.assertEqual(self.count('memory_reviews'),1)

    def test_node_state_reads_do_not_write_or_generate_mock_entries(self):
        before=self.native.path.read_bytes(); events=self.count('events')
        state=self.pool.state()
        self.assertEqual(state['source'],'local_database');self.assertEqual(len(state['nodes']),3)
        self.assertEqual(events,self.count('events'));self.assertEqual(before,self.native.path.read_bytes())

    def test_disabled_node_cannot_create_task_or_backend_call(self):
        source=self.add();before=self.count('ri_requests')
        with self.assertRaisesRegex(ValueError,'停用'):self.request_node(source)
        self.assertEqual(self.count('ri_requests'),before)
        source=self.toggle(source,True);req=self.request_node(source,peer_channel='backend_adapter')
        assignment=next(a for a in req['assignments'] if a['role']=='peer')
        self.toggle(source,False)
        with self.assertRaisesRegex(ValueError,'停用'):self.backends.execute(assignment['id'])
        self.assertFalse(any(a['running'] for a in self.store.get(req['id'])['assignments']))

    def test_current_task_freezes_entry_model_and_node_revision(self):
        source=self.toggle(self.add(),True);req=self.request_node(source)
        card=next(a['card'] for a in req['assignments'] if a['role']=='peer')
        self.assertEqual(card['resource_plan']['model'],'TEST-model-1')
        self.assertEqual(card['resource_plan']['entry'],'/TEST/bin/arbitrary-agent')
        before=json.dumps(card,sort_keys=True)
        self.toggle(source,False)
        current=next(a['card'] for a in self.store.get(req['id'])['assignments'] if a['role']=='peer')
        self.assertEqual(before,json.dumps(current,sort_keys=True))

    def test_same_entry_alias_or_other_model_is_not_an_independent_second_source(self):
        dsh=self.toggle(self.add(node_id='TEST-source-dsh',kind='dsh'),True)
        peer=self.toggle(self.add(node_id='TEST-alias',agent='TEST other label',model='TEST-model-2'),True)
        with self.assertRaisesRegex(ValueError,'来源相同'):
            self.store.create(self.candidate['task_id'],dsh['reviewer_id'],peer['reviewer_id'],'TEST scope',[{'ref':'TEST','sha256':'a'*64}])

    def test_remove_and_restore_retain_task_receipts_and_identity(self):
        source=self.toggle(self.add(),True);req=self.request_node(source);self.complete(req)
        before=self.store.get(req['id']);count=self.count('ri_receipts')
        removed=self.pool.change(source['reviewer_id'],source['node']['revision'],'remove')
        self.assertTrue(removed['node']['removed'])
        self.assertEqual(self.store.get(req['id'])['receipts'],before['receipts'])
        self.assertEqual(self.count('ri_receipts'),count)
        with self.assertRaises(ValueError):self.request_node(source)
        restored=self.pool.change(source['reviewer_id'],removed['node']['revision'],'restore')
        self.assertFalse(restored['node']['enabled']);self.assertEqual(restored['source_ref'],source['source_ref'])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_parallel_add_and_toggle_retry_are_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:values=list(pool.map(lambda _:self.add(),range(4)))
        self.assertEqual(self.count('ri_sources'),4)
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.toggle(values[0],True),range(4)))
        with self.native.read_only() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE event_type='agent_pool.toggle'").fetchone()[0],1)
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_stale_opposite_write_cannot_overwrite_latest_switch(self):
        source=self.add();self.toggle(source,True)
        with self.assertRaises(Conflict):self.toggle(source,False)

    def test_duplicate_add_never_reenables_or_restores_a_removed_node(self):
        source=self.add();removed=self.pool.change(source['reviewer_id'],1,'remove')
        retry=self.add();self.assertEqual(retry,removed)
        with self.assertRaises(Conflict):self.add(model='TEST changed model')

    def test_replaced_peer_must_be_enabled_and_original_plan_survives_refusal(self):
        req=self.request();source=self.add()
        with self.assertRaises(ValueError):self.store.replace_peer(req['id'],source['reviewer_id'],'TEST requested replacement')
        current=self.store.get(req['id']);self.assertEqual(current['revision'],req['revision'])
        self.assertEqual(len(current['assignments']),2)

    def test_old_sources_keep_existing_behavior_and_can_be_switched_off(self):
        with self.store.transaction() as db:source=self.store.source(db,'fixture-peer')
        self.assertTrue(node_state(source)['enabled']);self.request()
        off=self.toggle(source,False)
        self.store.register_source({k:source[k] for k in ('reviewer_id','source_ref','provenance','kind')})
        with self.store.transaction() as db:self.assertFalse(node_state(self.store.source(db,'fixture-peer'))['enabled'])
        with self.assertRaises(ValueError):self.request()

    def test_web_conversation_urls_of_one_service_share_actual_source(self):
        one=self.add(node_id='TEST-web1',interface='web',entry='https://example.com/chat/1')
        two=self.add(node_id='TEST-web2',interface='web',entry='https://example.com/chat/2')
        self.assertEqual(one['source_ref'],two['source_ref'])
        with self.assertRaises(ValueError):self.add(node_id='TEST-bad',interface='web',entry='https://user:fake@example.com')

    def test_unknown_fields_or_missing_model_never_enter_database(self):
        for value in (dict(self.value(),unexpected='anything'),self.value(model=''),self.value(entry='agent --run')):
            with self.assertRaises(ValueError):self.pool.add(value)
        self.assertEqual(self.count('ri_sources'),3)

    def test_pool_plan_requires_a_supporting_adapter_and_actual_returned_model(self):
        source=self.toggle(self.add(),True);req=self.request_node(source,peer_channel='backend_adapter')
        assignment=next(a for a in req['assignments'] if a['role']=='peer')
        class Adapter:
            kind='peer';source_ref=source['source_ref'];supports_resource_plan=False
            def run(self,card):return {'verdict':'PASS','findings':'TEST executed callback','evidence_refs':['TEST'],'execution_evidence':{'model':'TEST wrong model'}}
        adapter=Adapter();self.backends.register(source['reviewer_id'],adapter)
        with self.assertRaisesRegex(ValueError,'调用计划'):self.backends.execute(assignment['id'])
        adapter.supports_resource_plan=True
        with self.assertRaisesRegex(ValueError,'模型'):self.backends.execute(assignment['id'])
        self.assertEqual(self.count('ri_receipts'),0)

    def test_disable_during_running_call_keeps_receipt_but_blocks_next_call(self):
        source=self.toggle(self.add(),True);req=self.request_node(source,peer_channel='backend_adapter')
        assignment=next(a for a in req['assignments'] if a['role']=='peer')
        entered,finish=threading.Event(),threading.Event()
        class Adapter:
            kind='peer';source_ref=source['source_ref'];supports_resource_plan=True
            def run(self,card):
                entered.set();finish.wait(5)
                return {'verdict':'PASS','findings':'TEST authorized in-flight callback','evidence_refs':['TEST'],
                        'execution_evidence':{'provider':'TEST','model':card['resource_plan']['model']}}
        self.backends.register(source['reviewer_id'],Adapter())
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.backends.execute,assignment['id']);self.assertTrue(entered.wait(5))
            self.toggle(source,False);finish.set();receipt=future.result(5)
        self.assertEqual(receipt['status'],'valid')
        with self.assertRaises(ValueError):self.backends.execute(assignment['id'])
        self.assertEqual(self.count('memory_reviews'),1)

    def test_broken_chain_refuses_pool_mutations(self):
        with self.native.connect() as db:db.execute("UPDATE events SET event_hash='TEST broken' WHERE sequence=1")
        self.assertFalse(self.pool.state()['event_chain']['ok'])
        with self.assertRaisesRegex(ValueError,'事件链'):self.add()


class PoolHttpTests(fixtures.FixtureMixin,unittest.TestCase):
    def test_inline_mount_keeps_original_topology_without_a_new_page_or_tab(self):
        from monitor_core.web import Response
        response=Response(200,b'<html><body><svg id="topo"></svg><aside id="detail"></aside></body></html>','text/html')
        mounted=fixtures.OpenReviewModule.mount_pool(response,'TEST page token')
        self.assertIn(b'<svg id="topo">',mounted.body)
        self.assertIn(b'<aside id="detail">',mounted.body)
        self.assertIn(b'data-page-token="TEST page token"',mounted.body)
        self.assertNotIn(b'__TOKEN__',mounted.body)
        self.assertNotIn(b'<iframe',mounted.body)
        self.assertNotIn(b'tabAgents',mounted.body)
        self.assertEqual(mounted.body.count(b'id="apDrawer"'),1)

    def setUp(self):
        super().setUp()
        ctx=fixtures.Context(fixtures.config.load(self.root/'none.json',{'port':0}),fixtures.logs.setup(None),0)
        module=fixtures.OpenReviewModule(ctx,self.store,self.inbox,BackendRegistry(self.store))
        app=fixtures.App(ctx);app.register(module)
        self.server=app.server();port=self.server.server_port
        ctx.port=port;ctx.hosts={'127.0.0.1:'+str(port)};ctx.origins={'http://127.0.0.1:'+str(port)};self.ctx=ctx
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    fetch=fixtures.HttpTest.fetch

    def test_pool_endpoints_require_same_origin_csrf_and_persist_real_nodes(self):
        for action in ('nodes','toggle','remove','restore'):
            self.assertEqual(self.fetch('/api/agent-pool/'+action,{},False)[0],403)
        value={'node_id':'TEST-HTTP-node','name':'TEST HTTP Agent','agent':'TEST peer','kind':'peer',
               'interface':'desktop','entry':'TEST owned application','model':'TEST desktop model'}
        code,added=self.fetch('/api/agent-pool/nodes',value);self.assertEqual(code,200,added)
        for action,revision,extra in (('toggle',1,{'enabled':True}),('remove',2,{}),('restore',3,{})):
            code,result=self.fetch('/api/agent-pool/'+action,{'node_id':value['node_id'],'expected_revision':revision,**extra})
            self.assertEqual(code,200,result)
        code,state=self.fetch('/api/agent-pool/state');self.assertEqual(code,200)
        node=next(n for n in state['nodes'] if n['node_id']==value['node_id'])
        self.assertFalse(node['removed']);self.assertFalse(node['enabled'])
        self.assertEqual(self.count('memory_reviews'),1)
        self.assertEqual(self.fetch('/agent-pool')[0],404)  # Controls live in the original topology.
        for path in ('/agent-pool.js','/agent-pool.css','/agent-pool-native.js'):
            self.assertEqual(self.fetch(path)[0],200)
