"""Formal revision and inactivity decisions; only isolated TEST users/receipts."""
import hashlib
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_card52_reuse_scores as fixtures
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'review-ingress'))

class FormalReviewTests(unittest.TestCase):
    setUp=fixtures.Card52ReuseTests.setUp
    file=fixtures.Card52ReuseTests.file
    candidate=fixtures.Card52ReuseTests.candidate
    application=fixtures.Card52ReuseTests.application
    delivery=fixtures.Card52ReuseTests.delivery
    event_count=fixtures.Card52ReuseTests.event_count
    formal=fixtures.Card52ReuseTests.formal
    app=fixtures.Card52ReuseTests.app
    deliver=fixtures.Card52ReuseTests.deliver
    events=fixtures.Card52ReuseTests.events

    def request(self, draft=True, source=None):
        from xiaoba_agentos import formal_review as f
        c=source or self.formal(); p=Path(c['published_path'])
        p.write_text(p.read_text().replace('status: active','status: "active"'))
        c['source_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
        app=self.app(c,'modified')
        from xiaoba_agentos.experience_loop import record_application
        record_application(self.store,app); self.deliver(app,message_id=app['real_task']['id'])
        with self.store.read_only() as db:
            rid=db.execute("SELECT event_id FROM events WHERE event_type='memory.reuse_revision_requested' AND task_id=? ORDER BY sequence DESC",(c['task_id'],)).fetchone()[0]
        if draft: f.submit_draft(self.store,rid,self.file(rid+'.md','# TEST revised method\n\nTEST complete new steps and limits'))
        return c,rid

    def items(self, closed=False):
        from xiaoba_agentos.formal_review import items
        return {i['task_id']:i for i in items(self.store,closed)}

    def vault(self,*sources):
        cat=self.root/'vault/70_复利规则/经验索引.md';cat.parent.mkdir(parents=True,exist_ok=True)
        cat.write_text(''.join('- [['+str(Path(c['published_path']).relative_to(self.root/'vault').with_suffix(''))+']]\n' for c in sources))
        self.cfg.memory['publish_enabled']=True
        return cat

    def reviews(self,rid):
        from xiaoba_review.store import ReviewStore, DecisionInbox
        rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        for name,kind in [('TEST-a','dsh'),('TEST-b','peer')]:
            rs.register_source({'reviewer_id':name,'kind':kind,'source_ref':'test://'+name,'provenance':'TEST independent identity'})
        req=rs.create(rid,'TEST-a','TEST-b','TEST review',[{'ref':'TEST revision','sha256':self.items()[rid]['evidence_sha256']}],review_type='reuse_change')
        for a in req['assignments']:
            card=a['card']; data={k:card[k] for k in ('review_request_id','assignment_id','reviewer_role','plan_revision','reviewer_id','source_ref','candidate_sha256','input_digest','card_digest')}
            data.update(receipt_id=a['id'],verdict='PASS',findings='TEST checked complete revision',evidence_refs=card['evidence_refs'],started_at=time.time(),finished_at=time.time(),review_type='reuse_change',score=8,applicability='TEST constraints')
            r=rs.receive(data);rs.attest(r['id'],r['digest'],'TEST identity attestation')
        rs.deliver(DecisionInbox());return rs,req

    def decide(self,rid,decision,origin='user',now=None):
        from xiaoba_agentos.formal_review import decide
        return decide(self.cfg,self.store,rid,decision,self.items()[rid]['expected'],'TEST explicit user',origin=origin,now=now)

    def test_missing_body_visible_closable_not_reviewable(self):
        c,rid=self.request(False)
        self.assertEqual('待补正文',self.items()[rid]['status_label']);self.assertEqual(['reject'],self.items()[rid]['allowed_decisions'])
        self.decide(rid,'reject');self.assertNotIn(rid,self.items())
        self.assertEqual('published',self.store.get_memory_candidate(c['task_id'])['status'])

    def test_review_approve_new_version_keeps_old_without_trials(self):
        from xiaoba_agentos.memory import MemoryBridge
        c,rid=self.request();cat=self.vault(c);old=Path(c['published_path']).read_bytes();self.reviews(rid)
        self.assertEqual('待终审',self.items()[rid]['status_label'])
        self.assertEqual('published',self.store.get_memory_candidate(c['task_id'])['status'])
        with patch.object(MemoryBridge,'_rebuild_and_verify_index',return_value={'ok':True}):r=self.decide(rid,'approve')
        current=self.store.get_memory_candidate(c['task_id'])
        self.assertNotEqual(c['published_path'],current['published_path']);self.assertEqual(old,Path(r['archive_path']).read_bytes())
        self.assertIn('TEST complete new steps',Path(current['published_path']).read_text());self.assertNotIn(Path(c['published_path']).stem+']]',cat.read_text())
        self.assertTrue(self.store.verify_event_chain()['ok'])

    def test_reject_keeps_source_bytes_and_path(self):
        c,rid=self.request();old=Path(c['published_path']).read_bytes();self.decide(rid,'reject')
        self.assertEqual(old,Path(c['published_path']).read_bytes());self.assertEqual(c['published_path'],self.store.get_memory_candidate(c['task_id'])['published_path'])

    def test_draft_hash_drift_revokes_review_and_approval(self):
        _,rid=self.request();rs,req=self.reviews(rid);Path(self.items()[rid]['draft']['path']).write_text('TEST drift')
        with self.assertRaises(ValueError):self.decide(rid,'approve')
        with rs.transaction() as db:self.assertFalse(rs.fresh(db,db.execute('SELECT * FROM ri_requests WHERE id=?',(req['id'],)).fetchone()))

    def test_agent_cannot_decide_and_unreviewed_cannot_publish(self):
        _,rid=self.request()
        with self.assertRaises(ValueError):self.decide(rid,'approve')
        with self.assertRaises(ValueError):self.decide(rid,'reject',origin='agent')
        self.assertEqual(0,self.event_count('memory.formal_decided'))

    def test_old_version_requests_expire_after_another_approval(self):
        from xiaoba_agentos.memory import MemoryBridge
        c,a=self.request();_,b=self.request(source=c);self.vault(c);self.reviews(a)
        with patch.object(MemoryBridge,'_rebuild_and_verify_index',return_value={'ok':True}):self.decide(a,'approve')
        self.assertEqual('版本已过期',self.items()[b]['status_label'])
        with self.assertRaises(ValueError):self.decide(b,'approve')

    def test_inactivity_batches_and_keep_14_30_90_90(self):
        from xiaoba_agentos import formal_review as f
        self.formal();self.formal();self.assertEqual([],f.check_inactivity(self.store,now=100))
        self.assertEqual([],f.check_inactivity(self.store,now=100+14*86400))
        batch=f.check_inactivity(self.store,now=101+14*86400)[0];self.assertEqual(2,len(batch['entries']))
        self.assertEqual([],f.check_inactivity(self.store,now=102+14*86400))
        for n,days in enumerate((30,90,90)):
            stamp=2000000+n*10000000;self.decide(batch['task_id'],'approve',now=stamp)
            self.assertEqual('withdrawn',self.items(True)[batch['task_id']]['status'])
            self.assertEqual([],f.check_inactivity(self.store,now=stamp+days*86400))
            batch=f.check_inactivity(self.store,now=stamp+days*86400+1)[0]
            self.assertEqual(days,batch['entries'][0]['interval_days'])

    def test_retrieval_hit_resets_clock_without_scoring(self):
        from xiaoba_agentos import formal_review as f
        c=self.formal();f.check_inactivity(self.store,now=100);t=self.store.submit('TEST retrieval',1)
        self.store.record_memory_reuse(t,c['task_id'],c['published_path'])
        with self.store.connect() as db:db.execute('UPDATE memory_reuse SET created_at=2000000')
        self.assertEqual([],f.check_inactivity(self.store,now=2000001));self.assertEqual(1,len(f.check_inactivity(self.store,now=2000001+14*86400)))
        self.assertEqual(0,self.event_count('memory.reuse_scored'))

    def test_retirement_is_archive_and_recoverable(self):
        from xiaoba_agentos import formal_review as f
        from xiaoba_agentos.memory import MemoryBridge
        c=self.formal();cat=self.vault(c);old=Path(c['published_path']).read_bytes();f.check_inactivity(self.store,now=100)
        batch=f.check_inactivity(self.store,now=101+14*86400)[0]
        with patch.object(MemoryBridge,'_rebuild_and_verify_index',return_value={'ok':True}):r=self.decide(batch['task_id'],'reject')
        self.assertFalse(Path(c['published_path']).exists());self.assertEqual('',cat.read_text().strip());self.assertEqual(old,Path(r['archives'][0]).read_bytes())
        self.assertEqual('retired',self.store.get_memory_candidate(c['task_id'])['status'])

    def test_projection_readonly_and_no_schema_changes(self):
        from xiaoba_agentos import formal_review as f
        def schema():
            with self.store.read_only() as db:return [tuple(r) for r in db.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name')]
        before=schema();self.request();f.check_inactivity(self.store,now=100);count=self.event_count('memory.formal_enabled')
        self.items();self.items();self.assertEqual(count,self.event_count('memory.formal_enabled'));self.assertEqual(before,schema())

    def test_resubmission_revokes_previous_reviews_and_browser_snapshot(self):
        from xiaoba_agentos import formal_review as f
        _,rid=self.request();self.reviews(rid);old=self.items()[rid]['expected']
        f.submit_draft(self.store,rid,self.file('resubmitted.md','# TEST revised again\nTEST complete steps'))
        self.assertEqual('awaiting_review',self.items()[rid]['status'])
        with self.assertRaises(ValueError):f.decide(self.cfg,self.store,rid,'approve',old,'TEST stale browser',origin='user')

    def test_missing_body_cannot_dispatch_and_wrong_review_type_rejected(self):
        from xiaoba_review.store import ReviewStore
        _,rid=self.request(False)
        with self.assertRaises(ValueError):self.reviews(rid)
        _,rid=self.request();rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        with self.assertRaises(ValueError):rs.create(rid,'TEST-a','TEST-b','TEST scope',[{'ref':'TEST','sha256':self.items()[rid]['evidence_sha256']}],review_type='new_experience')

    def test_latest_unattested_receipt_revokes_approval(self):
        _,rid=self.request();rs,req=self.reviews(rid);card=req['assignments'][0]['card']
        data={k:card[k] for k in ('review_request_id','assignment_id','reviewer_role','plan_revision','reviewer_id','source_ref','candidate_sha256','input_digest','card_digest')}
        data.update(receipt_id='TEST-newer',verdict='FAIL',findings='TEST new findings',evidence_refs=card['evidence_refs'],started_at=time.time(),finished_at=time.time(),review_type='reuse_change',score=2,applicability='TEST limits')
        rs.receive(data);self.assertEqual('awaiting_review',self.items()[rid]['status'])
        with self.assertRaises(ValueError):self.decide(rid,'approve')

    def test_reviewer_who_authored_is_excluded(self):
        from xiaoba_agentos.task_participants import record_participant
        from xiaoba_review.store import ReviewStore
        c,rid=self.request();self.reviews(rid);record_participant(self.store,c['task_id'],'TEST-a','author')
        rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        with self.assertRaisesRegex(ValueError,'作者|干过'):rs.create(rid,'TEST-a','TEST-b','TEST scope',[{'ref':'TEST','sha256':self.items()[rid]['evidence_sha256']}],review_type='reuse_change')

    def test_start_uses_later_publication_and_missing_timestamp_enable(self):
        from xiaoba_agentos import formal_review as f
        a=self.formal();b=self.formal()
        with self.store.connect() as db:
            db.execute('UPDATE memory_candidates SET published_at=2000000 WHERE task_id=?',(a['task_id'],))
            db.execute('UPDATE memory_candidates SET published_at=NULL WHERE task_id=?',(b['task_id'],))
        f.check_inactivity(self.store,now=100);batch=f.check_inactivity(self.store,now=101+14*86400)[0]
        self.assertEqual([b['task_id']],[e['source_task_id'] for e in batch['entries']])
        later=f.check_inactivity(self.store,now=2000001+14*86400)[0]
        self.assertEqual([a['task_id']],[e['source_task_id'] for e in later['entries']])

    def test_publish_failure_restores_source_catalog_and_database(self):
        from xiaoba_agentos.memory import MemoryBridge
        c,rid=self.request();cat=self.vault(c);self.reviews(rid);old=Path(c['published_path']).read_bytes();catalog=cat.read_bytes()
        with patch.object(MemoryBridge,'_rebuild_and_verify_index',side_effect=[RuntimeError('TEST index failed'),{'ok':True}]):
            with self.assertRaisesRegex(RuntimeError,'TEST index failed'):self.decide(rid,'approve')
        self.assertEqual(old,Path(c['published_path']).read_bytes());self.assertEqual(catalog,cat.read_bytes())
        self.assertEqual(c['published_path'],self.store.get_memory_candidate(c['task_id'])['published_path'])
        self.assertEqual('pending',self.items()[rid]['status']);self.assertTrue(list((self.cfg.runtime_dir/'_待删').glob('*.md')))

    def test_browser_action_closes_same_formal_object(self):
        from test_trial_review_center import module
        from xiaoba_agentos import cli
        _,rid=self.request(False);action=module('review_action');old=cli.EventStore
        try:
            packet=dict(task_id=rid,stage='final',decision='reject',note='TEST browser closes missing body',expected=self.items()[rid]['expected'],entry_type='reuse_change')
            r=action.execute(packet,self.root/'config.json',Path(__file__).resolve().parents[1]/'src')
            self.assertEqual(0,r['exit_code']);self.assertNotIn(rid,self.items())
        finally:cli.EventStore=old

    def test_host_fallback_requires_user_choice_marks_no_independent_review(self):
        from xiaoba_agentos import formal_review as f
        from xiaoba_agentos.task_participants import bind_host
        _,rid=self.request();item=self.items()[rid]
        bind_host(self.store,item['real_task_id'],{'id':'TEST-host','entry':'/TEST/host','model':'TEST-model'})
        receipt={'reviewer_id':'TEST-host','request_id':rid,'candidate_sha256':item['evidence_sha256'],'receipt_id':'TEST-host-receipt','started_at':time.time(),'finished_at':time.time(),'verdict':'PASS','findings':'TEST checked full revision','review_type':'reuse_change','score':8,'applicability':'TEST limits','evidence_refs':[item['draft']]}
        with self.assertRaises(ValueError):f.submit_host_review(self.store,rid,receipt)
        r=f.submit_host_review(self.store,rid,receipt,user_chose_host=True)
        self.assertEqual('无独立审查',r['label']);self.assertIn('无独立审查',self.items()[rid]['body'])
        self.assertEqual('pending',self.items()[rid]['status']);self.assertEqual(0,self.event_count('memory.formal_decided'))

    def test_host_author_cannot_supply_independent_vote(self):
        from xiaoba_agentos import formal_review as f
        from xiaoba_agentos.task_participants import bind_host,record_participant
        c,rid=self.request();item=self.items()[rid]
        bind_host(self.store,item['real_task_id'],{'id':'TEST-host','entry':'/TEST/host','model':'TEST-model'})
        record_participant(self.store,c['task_id'],'TEST-host','author')
        with self.assertRaises(ValueError):f.submit_host_review(self.store,rid,{'reviewer_id':'TEST-host'},user_chose_host=True)

    def test_explicit_user_call_resets_timer_without_score(self):
        from xiaoba_agentos import formal_review as f
        c=self.formal();f.check_inactivity(self.store,now=100)
        with patch('xiaoba_agentos.formal_review.time.time',return_value=2000000):f.record_call(self.store,c['task_id'],c['source_sha256'])
        self.assertEqual([],f.check_inactivity(self.store,now=2000001));self.assertEqual(0,self.event_count('memory.reuse_scored'))

    def test_real_index_and_retrieval_use_new_version_only(self):
        from xiaoba_agentos.memory import MemoryBridge
        c,rid=self.request();cat=self.vault(c);vault=self.root/'vault';old=Path(c['published_path'])
        old.write_text('---\nid: "experience-'+c['task_id']+'"\ntype: "experience"\nparent: "experiences"\nstatus: "active"\nauthority: false\nsource_task_id: "'+c['task_id']+'"\n---\n\n# TEST original method\nTEST old complete method')
        # Rebind a fresh request to this well-formed tree fixture, rather than alter an existing request's source hash.
        self.decide(rid,'reject');c['source_sha256']=hashlib.sha256(old.read_bytes()).hexdigest();_,rid=self.request(source=c)
        (vault/'root.md').write_text('---\nid: "root"\ntype: "root"\nparent: ""\nstatus: "active"\nauthority: true\n---\n\n# Root\n- [[70_复利规则/经验索引]]\n')
        cat.write_text('---\nid: "experiences"\ntype: "branch"\nparent: "root"\nstatus: "active"\nauthority: true\n---\n\n# Experiences\n'+cat.read_text())
        self.cfg.raw['project_dir']=str(Path(__file__).resolve().parents[1]);self.cfg.memory['knowlp_graph_dir']=str(self.root/'graph')
        self.reviews(rid);result=self.decide(rid,'approve')
        self.assertTrue(result['index']['ok'])
        graph=json.loads((self.root/'graph/meta_index.json').read_text())
        self.assertIn(Path(result['path']).name,json.dumps(graph));self.assertNotIn(old.name,json.dumps(graph))
        self.assertTrue(Path(result['archive_path']).exists());self.assertFalse(old.exists())
        bridge=MemoryBridge(self.cfg,self.store)
        self.assertIsNone(bridge._verified_source({'path':str(old)}))
        self.assertIsNotNone(bridge._verified_source({'path':result['path']}))

    def test_one_pool_vote_and_eligible_host_form_two_votes(self):
        from xiaoba_agentos import formal_review as f
        from xiaoba_agentos.task_participants import bind_host
        from xiaoba_review.store import ReviewStore
        _,rid=self.request();item=self.items()[rid]
        bind_host(self.store,item['real_task_id'],{'id':'TEST-host','entry':'/TEST/host','model':'TEST-model'})
        rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        rs.register_source({'reviewer_id':'TEST-a','kind':'dsh','source_ref':'test://TEST-a','provenance':'TEST independent'})
        nodes=[{'node_id':'TEST-a','role':'review','ready_review':True,'independence':('test://TEST-a',None)}]
        with patch('xiaoba_agentos.pool_dispatch.load_nodes',return_value=nodes):
            req=rs.create_host_plan(rid,'TEST-host-plan')
            self.assertEqual(1,len(req['assignments']));card=req['assignments'][0]['card']
            data={k:card[k] for k in ('review_request_id','assignment_id','reviewer_role','plan_revision','reviewer_id','source_ref','candidate_sha256','input_digest','card_digest')}
            data.update(receipt_id='TEST-pool-one',verdict='FAIL',findings='TEST independent discrepancy',evidence_refs=card['evidence_refs'],started_at=time.time(),finished_at=time.time(),review_type='reuse_change',score=3,applicability='TEST limits')
            r=rs.receive(data);rs.attest(r['id'],r['digest'],'TEST identity')
            receipt={'reviewer_id':'TEST-host','request_id':rid,'candidate_sha256':item['evidence_sha256'],'receipt_id':'TEST-host-receipt','started_at':time.time(),'finished_at':time.time(),'verdict':'PASS','findings':'TEST host judgement','review_type':'reuse_change','score':8,'applicability':'TEST limits','evidence_refs':[item['draft']]}
            f.submit_host_review(self.store,rid,receipt)
        self.assertEqual('pending',self.items()[rid]['status']);self.assertEqual(2,len(self.items()[rid]['reviews']))
        self.assertEqual(0,self.event_count('memory.formal_decided'))

    def test_participant_change_revokes_previously_complete_packet(self):
        from xiaoba_agentos.task_participants import record_participant
        c,rid=self.request();self.reviews(rid)
        self.assertEqual('pending',self.items()[rid]['status'])
        record_participant(self.store,c['task_id'],'TEST-a','author')
        self.assertEqual('awaiting_review',self.items()[rid]['status'])

    def test_inline_draft_becomes_bound_request_in_delivery_transaction(self):
        from xiaoba_agentos.experience_loop import record_application
        c=self.formal();app=self.app(c,'inapplicable');ref=self.file('inline.md','# TEST conditional method\nTEST added limits')
        app['reuse_assessment']['draft']=ref;record_application(self.store,app);self.deliver(app)
        item=list(self.items().values())[0]
        self.assertEqual(ref,item['draft']);self.assertEqual('awaiting_review',item['status'])

    def test_approved_prose_is_not_rewritten_even_around_horizontal_dividers(self):
        from xiaoba_agentos import formal_review as f
        body='# TEST title\n\nTEST first section\n---\nTEST later section  '
        rendered=f._published_revision({'source_task_id':'TEST','source_sha256':'a'*64},body,'b'*64,'TEST-request')
        self.assertEqual(body,rendered.split('\n---\n',1)[1])

    def test_queue_server_detail_and_idempotent_user_rejection(self):
        from test_trial_review_center import module
        from xiaoba_agentos import formal_review as f
        from xiaoba_agentos import cli
        import types,uuid
        _,rid=self.request();Path(self.items()[rid]['draft']['path']).write_text('TEST stale draft')
        raw=json.loads((self.root/'config.json').read_text())
        raw['memory']['knowlp_graph_dir']=str(self.root/'graph');(self.root/'config.json').write_text(json.dumps(raw))
        server=module('server');center=server.Center(root=self.root,config_path=self.root/'config.json')
        center.workflow_views=lambda task_id=None:{'_formal':f.items(self.store,True)}
        center.verify=lambda:{'files_manifest_sha256':'TEST-binding'};center.sync=lambda **kw:None
        self.assertIn(rid,[x['task_id'] for x in center.state()['items']]);detail=center.detail(rid)
        req={'id':str(uuid.uuid4()),'items':[{'task_id':rid,'stage':'final','decision':'reject','note':'TEST user closes','expected':detail['expected']}]}
        action=module('review_action');old=cli.EventStore
        def execute(*args,**kwargs):
            request=json.loads(kwargs['input']);result=action.execute(request['packet'],self.root/'config.json',Path(__file__).resolve().parents[1]/'src')
            return types.SimpleNamespace(stdout=json.dumps(result),returncode=0)
        try:
            with patch.object(server.subprocess,'run',side_effect=execute):r=center.decide(req)
            self.assertTrue(r['ok'],r);self.assertTrue(r['items'][0]['recorded'])
            self.assertTrue(center.decide(req)['replayed']);self.assertEqual(1,self.event_count('memory.formal_decided'))
        finally:cli.EventStore=old

    def test_idle_worker_creates_timer_event_without_model_calls(self):
        from xiaoba_agentos.orchestrator import Orchestrator
        worker=Orchestrator(self.cfg,self.store)
        with patch.object(self.store,'has_runnable_work',return_value=False),patch.object(worker,'_process_task',create=True,side_effect=AssertionError('TEST model call forbidden')):
            self.assertEqual('idle',worker.run_once(preflight_when_idle=False)['status'])
            self.assertEqual('idle',worker.run_once(preflight_when_idle=False)['status'])
        self.assertEqual(1,self.event_count('memory.formal_enabled'))

    def test_index_call_receipt_resets_clock_but_never_scores(self):
        from xiaoba_agentos import formal_review as f
        c=self.formal();f.check_inactivity(self.store,now=100)
        with patch('xiaoba_agentos.formal_review.time.time',return_value=2000000):
            f.record_hits(self.store,[{'source_task_id':c['task_id'],'source_sha256':c['source_sha256']}])
        self.assertEqual([],f.check_inactivity(self.store,now=2000001));self.assertEqual(0,self.event_count('memory.reuse_scored'))
        self.assertEqual(1,self.event_count('memory.formal_retrieved'))

    def test_expired_request_resubmission_binds_current_application_no_rescore(self):
        from xiaoba_agentos import formal_review as f
        from xiaoba_agentos.memory import MemoryBridge
        from xiaoba_agentos.experience_loop import record_application
        c,a=self.request();_,b=self.request(source=c);old=self.items()[b];self.vault(c);self.reviews(a)
        with patch.object(MemoryBridge,'_rebuild_and_verify_index',return_value={'ok':True}):self.decide(a,'approve')
        before=self.event_count('memory.reuse_scored')
        with self.assertRaises(ValueError):f.resubmit(self.store,b,old['application_ids'][0],old['draft'])
        current=self.store.get_memory_candidate(c['task_id']);current['source_sha256']=hashlib.sha256(Path(current['published_path']).read_bytes()).hexdigest()
        app=self.app(current,'modified',task_id=old['real_task_id']);registered=record_application(self.store,app)
        self.deliver(app,message_id='TEST-new-version-delivery')
        new=f.resubmit(self.store,b,registered['application_id'],old['draft'])
        self.assertNotEqual(b,new['request_id']);self.assertEqual('awaiting_review',self.items()[new['request_id']]['status'])
        self.assertEqual(before,self.event_count('memory.reuse_scored'))

    def test_keep_preserves_nonzero_permanent_score(self):
        from xiaoba_agentos import formal_review as f
        from xiaoba_agentos.experience_loop import record_application
        from xiaoba_agentos.reuse_scores import projection_tx
        c=self.formal();app=self.app(c);record_application(self.store,app);self.deliver(app)
        now=time.time()+100;f.check_inactivity(self.store,now=now)
        batch=f.check_inactivity(self.store,now=now+14*86400+1)[0]
        self.assertEqual(1,batch['entries'][0]['score']);self.decide(batch['task_id'],'approve',now=now+14*86400+2)
        with self.store.read_only() as db:self.assertEqual(1,projection_tx(db)[c['task_id']]['score'])
