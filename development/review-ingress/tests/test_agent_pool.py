"""Persistent Agent lifecycle and actual selection/call boundaries; TEST data only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import unittest

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool, node_state
from xiaoba_review.agent_workspace import AgentWorkspace
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.store import ReviewStore, Conflict
from xiaoba_agentos.store import EventStore


class PoolFixture(fixtures.FixtureMixin):
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

    def edit(self, source, expected_revision=None, **changes):
        revision=node_state(source)['revision'] if expected_revision is None else expected_revision
        return self.pool.edit(dict(changes,node_id=source['reviewer_id'],expected_revision=revision))

    def node(self, node_id):
        return next(n for n in self.pool.state()['nodes'] if n['node_id']==node_id)

    def passed_node(self, source, test_id='TEST-edit-gate'):
        """A node with a passed test, so invalidation has something to invalidate."""
        class Adapter:
            kind=source['kind'];source_ref=source['source_ref'];supports_resource_plan=True
            def resource_identity(self):return {'TEST':'edit fixture adapter'}
            def test_resource(self,plan,challenge):
                return {'output':challenge,'model':plan['model'],'provider':'TEST','route_source':'TEST'}
        self.backends.register(source['reviewer_id'],Adapter())
        result=self.pool.runtime.test_run(source['reviewer_id'],node_state(source)['revision'],test_id)
        self.assertEqual(result['status'],'passed',result)
        return self.reload(source['reviewer_id'])

    def reload(self, node_id):
        with self.native.read_only() as db:return self.store.source(db,node_id)

    def events(self, event_type):
        with self.native.read_only() as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT payload_json FROM events WHERE event_type=? ORDER BY sequence',(event_type,))]

    def request_node(self, source, **changes):
        return self.store.create(self.candidate['task_id'],'fixture-dsh',source['reviewer_id'],
                                 'TEST frozen node plan',[{'ref':'TEST evidence','sha256':'a'*64}],**changes)

    def count(self, table):
        with self.native.read_only() as db:return db.execute('SELECT count(*) FROM '+table).fetchone()[0]


class PoolTests(PoolFixture,unittest.TestCase):
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

    def test_same_entry_same_model_alias_is_not_an_independent_second_source(self):
        # 用户裁决 6：独立性看“入口程序＋模型”。同一入口同一模型，换 Agent 名字仍只算一个。
        dsh=self.toggle(self.add(node_id='TEST-source-dsh',kind='dsh'),True)
        peer=self.toggle(self.add(node_id='TEST-alias',agent='TEST other label'),True)
        with self.assertRaisesRegex(ValueError,'来源'):
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

    def test_pool_plan_requires_a_supporting_adapter_and_records_the_configured_model(self):
        source=self.toggle(self.add(),True);req=self.request_node(source,peer_channel='backend_adapter')
        assignment=next(a for a in req['assignments'] if a['role']=='peer')
        class Adapter:
            kind='peer';source_ref=source['source_ref'];supports_resource_plan=True
            def resource_identity(self):return {'TEST':'plan adapter'}
            def test_resource(self,plan,challenge):
                return {'output':challenge,'model':plan['model'],'provider':'TEST','route_source':'TEST'}
            def run(self,card):return {'verdict':'PASS','findings':'TEST executed callback','evidence_refs':['TEST'],'execution_evidence':{'model':'TEST wrong model'}}
        adapter=Adapter();self.backends.register(source['reviewer_id'],adapter)
        # 带节点计划的任务卡先过测试资格；未测试时不会调用适配器。
        with self.assertRaisesRegex(ValueError,'测试'):self.backends.execute(assignment['id'])
        with self.native.read_only() as db:revision=node_state(self.store.source(db,source['reviewer_id']))['revision']
        self.assertEqual(self.pool.runtime.test_run(source['reviewer_id'],revision,'TEST-plan-gate')['status'],'passed')
        # 用户 2026-11-01 裁决：适配器回报的模型与节点计划不同不再判失败，
        # 回执照常结算，证据里保留适配器回报的原值。
        result=self.backends.execute(assignment['id'])
        self.assertEqual(result['status'],'valid')
        data=self.store.get(req['id'])['receipts'][0]['data']
        self.assertEqual('PASS',data['verdict'])
        self.assertEqual('TEST wrong model',data['execution_evidence']['model'])
        self.assertEqual(1,self.count('ri_receipts'))

    def test_disable_during_running_call_keeps_receipt_but_blocks_next_call(self):
        source=self.toggle(self.add(),True);req=self.request_node(source,peer_channel='backend_adapter')
        assignment=next(a for a in req['assignments'] if a['role']=='peer')
        entered,finish=threading.Event(),threading.Event()
        class Adapter:
            kind='peer';source_ref=source['source_ref'];supports_resource_plan=True
            def resource_identity(self):return {'TEST':'in-flight adapter'}
            def test_resource(self,plan,challenge):
                return {'output':challenge,'model':plan['model'],'provider':'TEST','route_source':'TEST'}
            def run(self,card):
                entered.set();finish.wait(5)
                return {'verdict':'PASS','findings':'TEST authorized in-flight callback','evidence_refs':['TEST'],
                        'execution_evidence':{'provider':'TEST','model':card['resource_plan']['model']}}
        self.backends.register(source['reviewer_id'],Adapter())
        # 带节点计划的任务卡要先有测试资格，之后才谈执行中停用。
        with self.native.read_only() as db:revision=node_state(self.store.source(db,source['reviewer_id']))['revision']
        self.assertEqual(self.pool.runtime.test_run(source['reviewer_id'],revision,'TEST-in-flight-gate')['status'],'passed')
        with self.native.read_only() as db:source=self.store.source(db,source['reviewer_id'])
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


class PoolEditTests(PoolFixture,unittest.TestCase):
    """用户 2026-11-01 要求：节点添加后可以修改，不必移除再添加（卡 32）。"""

    def readiness(self,node_id):
        with self.native.read_only() as db:return self.pool.runtime.readiness(db,self.store.source(db,node_id))

    def test_a_corrected_model_name_requires_a_new_test_before_review(self):
        source=self.toggle(self.add(),True);source=self.passed_node(source)
        self.assertTrue(self.readiness(source['reviewer_id'])['ready'])
        edited=self.edit(source,model='TEST-model-2')
        node=node_state(edited)
        self.assertEqual(node['model'],'TEST-model-2')
        self.assertEqual(node['revision'],node_state(source)['revision']+1)
        self.assertTrue(node['enabled'])  # 开关状态保持
        self.assertEqual(edited['source_ref'],source['source_ref'])  # 模型不在 source_ref 里
        # 资格靠 readiness 的摘要绑定失效，不是靠清字段。
        state=self.node(source['reviewer_id'])
        self.assertFalse(state['readiness']['ready'])
        self.assertEqual(state['readiness']['reason'],'test_required')
        self.assertEqual(state['test']['status'],'invalidated')
        self.assertEqual(state['test']['reason'],'node_edited')
        # 旧测试结果没有被改写：原样留在 agent_pool.edited 的 before 和原事件里。
        self.assertEqual(self.events('agent_pool.edited')[-1]['before']['test']['status'],'passed')
        req=self.request_node(edited,peer_channel='backend_adapter')
        assignment=next(a for a in req['assignments'] if a['role']=='peer')
        self.assertEqual(assignment['card']['resource_plan']['model'],'TEST-model-2')
        with self.assertRaisesRegex(ValueError,'测试'):self.backends.execute(assignment['id'])
        # 重新测试通过后恢复资格。
        self.assertEqual(self.pool.runtime.test_run(edited['reviewer_id'],node['revision'],
            'TEST-after-edit')['status'],'passed')
        self.assertTrue(self.readiness(edited['reviewer_id'])['ready'])

    def test_a_renamed_node_keeps_its_source_ref_and_its_run_qualification(self):
        source=self.toggle(self.add(),True);source=self.passed_node(source)
        passed=node_state(source)['test']
        edited=self.edit(source,name='TEST 改名节点')
        self.assertEqual(edited['source_ref'],source['source_ref'])
        self.assertEqual(node_state(edited)['name'],'TEST 改名节点')
        # 卡 32 验收：只改名称不作废。名称既不在 spec() 里，也不在适配器缓存键里，
        # 所以两个摘要都没动，资格还是原来那一次测试给的。
        self.assertEqual(self.readiness(edited['reviewer_id'])['reason'],'tested')
        self.assertTrue(self.node(edited['reviewer_id'])['readiness']['ready'])
        # 旧结果原样留着，连 event_id 都在：不是清字段换来的“不作废”。
        self.assertEqual(node_state(edited)['test'],passed)
        self.assertEqual(node_state(edited)['workspace_name'],'TEST 文案节点')  # 目录名固定，不搬

    def test_a_rename_keeps_the_cached_adapter_that_produced_the_test(self):
        built=[]
        class Adapter:
            kind='peer';supports_resource_plan=True;persistent_resource=True
            def __init__(self,source):
                built.append(source['node']['name'])
                self.identity={'TEST':'adapter-%d'%len(built)}
            def resource_identity(self):return self.identity
            def retire(self):built.append('retired')
            def close(self):pass
        self.backends.resource_factory=Adapter
        source=self.toggle(self.add(),True)
        first=self.backends.adapter_for(source)
        edited=self.edit(source,name='TEST 改名节点')
        self.assertIs(self.backends.adapter_for(edited),first)
        self.assertNotIn('retired',built)  # 重建会换 instance，readiness 会当成没测过
        edited=self.edit(edited,model='TEST-model-2')
        self.assertIsNot(self.backends.adapter_for(edited),first)
        self.assertIn('retired',built)

    def test_an_entry_change_recomputes_source_ref_and_reuses_add_validation(self):
        source=self.toggle(self.add(),True)
        edited=self.edit(source,entry='/TEST/bin/other-agent')
        self.assertNotEqual(edited['source_ref'],source['source_ref'])
        self.assertEqual(node_state(edited)['entry'],'/TEST/bin/other-agent')
        for bad,message in (({'entry':'agent --run'},'绝对路径'),
                            ({'entry':''},'格式异常'),
                            ({'model':'sk-TEST-1234567890abcdefghijklmnopqrstuv'},'密钥'),
                            ({'name':''},'格式异常'),
                            ({},'可修改')):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError,message):self.edit(edited,**bad)
                self.assertEqual(self.reload(edited['reviewer_id']),edited)  # 拒绝时不写入
        self.assertEqual(len(self.events('agent_pool.edited')),1)

    def test_identity_fields_are_not_editable(self):
        source=self.toggle(self.add(),True)
        for bad in ({'kind':'dsh'},{'interface':'web'},{'removed':True},{'enabled':False},{'unexpected':'x'}):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError,'不能改'):self.edit(source,**bad)
        # 节点ID就是身份：指向别的节点只会找不到来源，绝不会改写原节点。
        with self.assertRaisesRegex(ValueError,'未知审查来源'):
            self.pool.edit({'node_id':'TEST-other','expected_revision':1,'model':'TEST-model-9'})
        self.assertEqual(self.reload(source['reviewer_id']),source)
        self.assertEqual(self.events('agent_pool.edited'),[])

    def test_a_stale_revision_is_refused_and_an_identical_retry_writes_no_second_event(self):
        source=self.toggle(self.add(),True)
        with self.assertRaisesRegex(Conflict,'节点已变化，请刷新后重试'):
            self.edit(source,expected_revision=node_state(source)['revision']+5,model='TEST-model-9')
        edited=self.edit(source,model='TEST-model-2')
        self.assertEqual(len(self.events('agent_pool.edited')),1)
        # 完全相同的重试：旧修订号也算已落地的那一次，不再产生第二条事件。
        retry=self.edit(source,model='TEST-model-2')
        self.assertEqual(retry,edited)
        self.assertEqual(len(self.events('agent_pool.edited')),1)
        # 内容相同、修订号正确：同样不是新变更。
        self.assertEqual(self.edit(edited,model='TEST-model-2'),edited)
        self.assertEqual(node_state(self.reload(edited['reviewer_id']))['revision'],
                         node_state(edited)['revision'])
        self.assertEqual(len(self.events('agent_pool.edited')),1)

    def test_the_event_carries_before_and_after_and_the_chain_still_verifies(self):
        source=self.toggle(self.add(),True)
        self.edit(source,model='TEST-model-2',name='TEST 改名节点')
        event=self.events('agent_pool.edited')[-1]
        self.assertEqual(event['node_id'],source['reviewer_id'])
        self.assertEqual(event['before']['model'],'TEST-model-1')
        self.assertEqual(event['after']['model'],'TEST-model-2')
        self.assertEqual(event['before']['name'],'TEST 文案节点')
        self.assertEqual(event['after']['name'],'TEST 改名节点')
        self.assertEqual(event['before_source_ref'],source['source_ref'])
        self.assertEqual(event['after_source_ref'],source['source_ref'])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_removed_in_flight_and_unsettled_nodes_refuse_an_edit(self):
        source=self.toggle(self.add(),True)
        removed=self.pool.change(source['reviewer_id'],node_state(source)['revision'],'remove')
        with self.assertRaisesRegex(ValueError,'先恢复'):self.edit(removed,model='TEST-model-2')
        restored=self.pool.change(source['reviewer_id'],node_state(removed)['revision'],'restore')
        self.assertEqual(self.edit(restored,model='TEST-model-2')['node']['model'],'TEST-model-2')

        source=self.toggle(self.add(node_id='TEST-busy'),True)
        req=self.request_node(source,peer_channel='backend_adapter')
        assignment=next(a for a in req['assignments'] if a['role']=='peer')
        with self.assertRaisesRegex(Conflict,'未结算'):self.edit(source,model='TEST-model-3')
        with self.store.transaction() as db:
            db.execute('UPDATE ri_assignments SET running=? WHERE id=?',('TEST-token',assignment['id']))
        with self.assertRaisesRegex(Conflict,'进行中'):self.edit(source,model='TEST-model-3')
        with self.store.transaction() as db:
            db.execute('UPDATE ri_assignments SET running=NULL WHERE id=?',(assignment['id'],))
        self.complete(req)
        # 已结算的指派一直保留 active=1；否则用过的节点永远改不了。
        self.assertNotEqual(self.store.get(req['id'])['state'],'waiting_receipts')
        receipts=self.store.get(req['id'])['receipts']
        edited=self.edit(source,model='TEST-model-3')
        self.assertEqual(node_state(edited)['model'],'TEST-model-3')
        current=self.store.get(req['id'])
        self.assertEqual(current['receipts'],receipts)  # 旧回执不改写
        self.assertEqual(next(a['card'] for a in current['assignments']
            if a['role']=='peer')['resource_plan']['model'],'TEST-model-1')  # 冻结卡不改写
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_a_stale_request_does_not_lock_its_node_forever(self):
        source=self.toggle(self.add(),True)
        req=self.request_node(source,peer_channel='backend_adapter')
        with self.assertRaisesRegex(Conflict,'未结算'):self.edit(source,model='TEST-model-2')
        original=self.store.fresh
        self.store.fresh=lambda db,request:False
        try:edited=self.edit(source,model='TEST-model-2')
        finally:self.store.fresh=original
        self.assertEqual(node_state(edited)['model'],'TEST-model-2')
        self.assertEqual(self.store.get(req['id'])['id'],req['id'])

    def workspace(self,source):
        return AgentWorkspace(self.root/'Agent工作区',source)

    def test_an_edited_node_keeps_its_workspace_and_records(self):
        (self.root/'Agent工作区').mkdir()
        source=self.toggle(self.add(),True)
        original=self.workspace(source)
        path=original.ensure()
        record=original.begin('test','TEST-record',{'TEST':'before edit'})
        original.finish(record,'failed',reason='TEST')
        marker=(path/'.xiaoba-agent-workspace.json').read_bytes()
        current=source
        for change in ({'entry':'/TEST/bin/other-agent'},{'name':'TEST 改名节点'},
                       {'name':'test 改名节点'},{'model':'TEST-model-2'}):
            with self.subTest(change=change):
                current=self.edit(current,**change)
                workspace=self.workspace(current)
                self.assertEqual(workspace.ensure(),path)
                self.assertEqual(workspace.verify()[1],2)  # 原任务记录仍在、仍可校验
        self.assertEqual(sorted(p.name for p in (self.root/'Agent工作区').iterdir()),[path.name])
        self.assertEqual((path/'.xiaoba-agent-workspace.json').read_bytes(),marker)  # 旧标记不改写

    def test_a_directory_held_by_another_node_is_refused_and_names_it(self):
        (self.root/'Agent工作区').mkdir()
        holder=self.add(node_id='TEST-holder',name='TEST 占用',entry='/TEST/bin/holder')
        path=self.workspace(holder).ensure()
        holder=self.pool.change('TEST-holder',node_state(holder)['revision'],'remove')
        source=self.toggle(self.add(node_id='TEST-new',name='TEST 占用'),True)
        test=self
        class Adapter:
            kind=source['kind'];source_ref=source['source_ref'];supports_resource_plan=True
            def resource_identity(self):return {'TEST':'workspace conflict fixture'}
            def test_resource(self,plan,challenge):
                test.workspace(source).ensure()
        self.backends.register('TEST-new',Adapter())
        result=self.pool.runtime.test_run('TEST-new',node_state(source)['revision'],'TEST-occupied')
        self.assertEqual(result['status'],'failed')
        node=self.node('TEST-new')
        self.assertEqual(node['test']['reason'],'workspace_conflict')
        self.assertIn('“TEST 占用”（已移除，材料保留）',node['test']['message'])
        self.assertTrue(path.is_dir())  # 不接管、不覆盖、不删除
        self.assertEqual(json.loads((path/'.xiaoba-agent-workspace.json').read_text())['node_id'],'TEST-holder')

    def test_a_waiting_attended_job_blocks_an_edit_of_its_node(self):
        directory=(self.root/'attended');directory.mkdir()
        source=self.pool.add(self.value(node_id='TEST-tmux-edit',kind='collaborator',interface='tmux',
            entry='/TEST/bin/attended',tmux_session='TEST-session',working_directory=str(directory.resolve())))
        source=self.pool.change(source['reviewer_id'],1,'toggle',True)
        edited=self.edit(source,tmux_session='TEST-renamed',working_directory=str(directory.resolve()))
        self.assertEqual(node_state(edited)['tmux_session'],'TEST-renamed')
        with self.store.transaction() as db:
            self.pool.tmux._save(db,{'job_id':'TEST-job','node_id':source['reviewer_id'],
                'session':'TEST-renamed','status':'waiting_user_agent'})
        with self.assertRaisesRegex(Conflict,'等待中'):self.edit(edited,name='TEST 改名')
        self.assertEqual(node_state(self.reload(edited['reviewer_id']))['name'],'TEST 文案节点')
        with self.assertRaises(ValueError):self.edit(edited,tmux_session='../escape')

    def test_a_legacy_source_without_node_data_cannot_be_edited(self):
        with self.native.read_only() as db:source=self.store.source(db,'fixture-peer')
        with self.assertRaisesRegex(ValueError,'旧四字段'):self.edit(source,name='TEST 改名')


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
        # 派工审查在抽屉里选审查类型：不选不派工，接口也不再接受第 1 版。
        for marker in (b'id="apReviewType"',b'value="new_experience"',b'value="revalidation"',
                       b'value="reuse_change"',b'id="apDispatchGo"'):
            self.assertIn(marker,mounted.body)

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
        for action in ('nodes','edit','toggle','remove','restore'):
            self.assertEqual(self.fetch('/api/agent-pool/'+action,{},False)[0],403)
        value={'node_id':'TEST-HTTP-node','name':'TEST HTTP Agent','agent':'TEST peer','kind':'peer',
               'interface':'desktop','entry':'TEST owned application','model':'TEST desktop model'}
        code,added=self.fetch('/api/agent-pool/nodes',value);self.assertEqual(code,200,added)
        for action,revision,extra in (('toggle',1,{'enabled':True}),('remove',2,{}),('restore',3,{})):
            code,result=self.fetch('/api/agent-pool/'+action,{'node_id':value['node_id'],'expected_revision':revision,**extra})
            self.assertEqual(code,200,result)
        code,result=self.fetch('/api/agent-pool/edit',{'node_id':value['node_id'],
            'expected_revision':9,'model':'TEST desktop model 2'})
        self.assertEqual(code,409,result);self.assertEqual(result['error'],'节点已变化，请刷新后重试')
        code,result=self.fetch('/api/agent-pool/edit',{'node_id':value['node_id'],
            'expected_revision':4,'model':'TEST desktop model 2'})
        self.assertEqual(code,200,result)
        self.assertEqual(result['result']['node']['model'],'TEST desktop model 2')
        self.assertEqual(result['result']['node']['revision'],5)
        code,result=self.fetch('/api/agent-pool/edit',{'node_id':value['node_id'],
            'expected_revision':5,'kind':'dsh'})
        self.assertEqual(code,400,result);self.assertIn('不能改',result['error'])
        code,state=self.fetch('/api/agent-pool/state');self.assertEqual(code,200)
        node=next(n for n in state['nodes'] if n['node_id']==value['node_id'])
        self.assertFalse(node['removed']);self.assertFalse(node['enabled'])
        self.assertEqual(node['model'],'TEST desktop model 2')
        self.assertEqual(node['kind'],'peer')
        self.assertEqual(node['readiness']['reason'],'disabled')  # restore 后开关仍是关的
        self.assertFalse(node['readiness']['ready'])
        self.assertEqual(self.count('memory_reviews'),1)
        self.assertEqual(self.fetch('/agent-pool')[0],404)  # Controls live in the original topology.
        for path in ('/agent-pool.js','/agent-pool.css','/agent-pool-native.js'):
            self.assertEqual(self.fetch(path)[0],200)
