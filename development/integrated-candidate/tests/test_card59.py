"""Card59 regression cases: isolated databases, no production calls."""
import ast
import hashlib
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock
import test_card51_scoring as fixtures
import test_card52b_formal_review as formal_fixtures
import test_card46_revalidation_recovery as recovery_fixtures
from test_release import BUILD_RELEASE_MODULE
from xiaoba_agentos import task_scores
from xiaoba_agentos.store import EventStore

REPO=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(REPO/'development/review-ingress'))
sys.path.insert(0,str(REPO/'architecture-package-20261027/shared/工具/小八监看'))  # 与接入口测试同源；仓库根 monitor/ 不在发版导出里
PREVIOUS=Path(__file__).with_name('fixtures')/'review-ui-v1.6.10.json'


class Card59ScoringTests(unittest.TestCase):
    setUp=fixtures.Card51ScoringTests.setUp
    run_node=fixtures.Card51ScoringTests.run_node
    settle=fixtures.Card51ScoringTests.settle
    rows=fixtures.Card51ScoringTests.rows

    def scoring(self):
        from xiaoba_review.store import ReviewStore
        from xiaoba_review.task_scoring import TaskScoring
        return TaskScoring(SimpleNamespace(store=ReviewStore(self.store,self.config.runtime_dir/'memory-outbox')))

    def correct(self,task,**kwargs):
        row=next(v for v in self.rows() if v['task_id']==task)
        args=dict(node_id='A',day=row['date'],task_id=task,task_type='review',outcome='void',
                  evidence_refs=['TEST verified defect'],expected_revision=1,assessment_id='TEST-void-'+task,
                  role='review',void_reason='TEST system defect')
        args.update(kwargs)
        return self.scoring().assess(**args)

    def test_A2_void_removes_sample_and_is_irreversible_idempotent(self):
        for status in ('completed','permanent_error'):
            task=self.settle(status=status);old=self.rows()[-1]
            result=self.correct(task)
            self.assertEqual(('void',0,0), (result['outcome'],result['points'],result['tasks']))
            self.assertEqual(result,self.correct(task))
            with self.store.read_only() as db:
                self.assertTrue(task_scores.audit_ledger_tx(db)['ok'])
                payload=json.loads(db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task_scoring.assessed' ORDER BY sequence DESC LIMIT 1",(task,)).fetchone()[0])
                self.assertEqual(-old['points'],payload['delta']);self.assertTrue(payload['void'])
                nodes=task_scores.decorate_nodes(db,[dict(node_id='A',source_ref=old['source_ref'],model=old['model'])],day=old['date'])
            self.assertEqual([],self.scoring().weekly(old['date'])['ranking'])
            self.assertEqual(0,nodes[0]['recent_role_samples'])
            for outcome in ('good','not_good'):
                with self.assertRaises(ValueError):self.correct(task,outcome=outcome,expected_revision=2,assessment_id='TEST-restore')

    def test_A2_requires_reason_evidence_and_existing_automatic_sample(self):
        task=self.settle()
        for reason in ('', '   ', None):
            with self.assertRaises(ValueError):self.correct(task,void_reason=reason)
        with self.assertRaises(ValueError):self.correct(task,evidence_refs=[])
        failed=self.settle(status='permanent_error')
        with self.assertRaises(ValueError):self.correct(failed,outcome='good')

    def test_B3_explicit_marker_and_legacy_only_when_absent(self):
        prompt='FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL'
        for snapshot,expected in (({'review_context':{'release_acceptance':True}},True),
                ({'review_context':{'release_acceptance':False}},False),({},True)):
            task=self.store.submit(prompt,2,snapshot=snapshot)
            with self.store.read_only() as db:
                row=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
                self.assertEqual(expected,task_scores.requires_review_vote(db,row,snapshot))
        task=self.store.submit('TEST no magic prompt',2,snapshot={'review_context':{'release_acceptance':True}})
        with self.store.read_only() as db:
            row=db.execute('SELECT * FROM tasks WHERE id=?',(task,)).fetchone()
            self.assertTrue(task_scores.requires_review_vote(db,row,{'review_context':{'release_acceptance':True}}))

    def test_B3_unscored_reason_and_B4_repair_discard_preserves_completed_hash(self):
        task=self.settle();before=self.store.get_task(task)['result']
        path=self.root/'TEST.md';path.write_text('TEST body')
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.read_only() as db:
            last=json.loads(db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.completed' ORDER BY sequence DESC LIMIT 1",(task,)).fetchone()[0])
        assessment={'decision':'discard','scores':{'TEST':1}}
        with patch('xiaoba_agentos.value_scoring.native_value',return_value=assessment):
            self.store.complete_with_memory_candidate(task,'TEST body',str(path),sha,[],expected_attempt=0)
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertEqual(before,self.store.get_task(task)['result'])
        self.assertEqual(last['result_sha256'],hashlib.sha256(before.encode()).hexdigest())
        with self.store.read_only() as db:
            self.assertFalse(db.execute("SELECT 1 FROM events WHERE task_id=? AND event_type='memory.candidate_staged'",(task,)).fetchone())

    def test_B3_reasons_distinguish_kind_mock_host_and_no_source(self):
        from xiaoba_agentos.value_scoring import unscored_reason
        cases=[({'experience_policy':{'version':1,'kind':'audit'}},'kind_not_method_rule'),
               ({'experience_policy':{'version':1,'kind':'method'}},'mock_or_non_pool')]
        for snapshot,expected in cases:
            task=self.store.submit('TEST reason',2,snapshot=snapshot)
            with self.store.read_only() as db:self.assertEqual(expected,unscored_reason(db,task))
        task=self.store.submit('TEST pool no scoring source',2,snapshot={'experience_policy':{'version':1,'kind':'method'}})
        self.run_node(task)
        with self.store.read_only() as db:self.assertEqual('no_scoring_source',unscored_reason(db,task))
        authored=self.store.create_authored_candidate('TEST draft','TEST body',self.config.runtime_dir/'memory-outbox',author='TEST',experience_kind='method')
        with self.store.read_only() as db:self.assertEqual('host_draft',unscored_reason(db,authored['task_id']))

    def test_B7_cancel_hides_only_recovery_notifications(self):
        task=self.store.submit('TEST cancel',2)
        self.store.create_notification('task.recovery_blocked',{'TEST':1},task_id=task)
        self.store.create_notification('agent.route_changed',{'TEST':1},task_id=task)
        self.store.cancel_task(task)
        self.assertEqual(['agent.route_changed'],[v['kind'] for v in self.store.pending_notifications()])

    def test_B3_submission_freezes_explicit_marker_even_without_magic_words(self):
        from xiaoba_agentos.cli import _submit_task
        task=_submit_task(self.config,self.store,'TEST release review',experience_kind='audit',release_acceptance=True,no_work_task=True)
        snapshot=json.loads(self.store.get_task(task)['execution_snapshot_json'])
        self.assertIs(True,snapshot['review_context']['release_acceptance'])
        task=_submit_task(self.config,self.store,'TEST ordinary review',experience_kind='audit')
        self.assertIs(False,json.loads(self.store.get_task(task)['execution_snapshot_json'])['review_context']['release_acceptance'])

    def test_B7_abandon_hides_recovery_but_not_other_tasks(self):
        task=self.store.submit('TEST abandon',2)
        self.store.create_notification('task.recovery_blocked',{'TEST':1},task_id=task)
        other=self.store.submit('TEST still active',2)
        self.store.create_notification('task.recovery_blocked',{'TEST':1},task_id=other)
        frozen=self.store.freeze_task(task,failure_class='external_upstream',failures=[])
        self.store.abandon_task(task,frozen['freeze_hash'])
        self.assertEqual([other],[v['task_id'] for v in self.store.pending_notifications()])

    def test_A2_void_conflicting_id_revision_and_forged_restore_rejected(self):
        from xiaoba_review.store import Conflict
        task=self.settle();result=self.correct(task)
        with self.assertRaises(Conflict):self.correct(task,void_reason='TEST changed reason')
        with self.assertRaises(ValueError):self.correct(task,expected_revision=2,assessment_id='TEST-again')
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM ri_task_assessments').fetchone()
            previous=json.loads(row['data']);value=dict(previous,outcome='good',points=1,tasks=1,success_rate=1)
            db.execute('UPDATE ri_task_assessments SET data=?,revision=3,assessment_id=? WHERE id=?',(json.dumps(value),'TEST-forged',row['id']))
            self.store._append_event_tx(db,task,'task_scoring.assessed',{'key':row['id'],'revision':3,'assessment_id':'TEST-forged','previous':previous,'current':value,'delta':1})
        with self.store.read_only() as db:self.assertFalse(task_scores.audit_ledger_tx(db)['ok'])


class Card59RecoveryTests(unittest.TestCase):
    setUp=recovery_fixtures.Card46Fixture.setUp
    tearDown=recovery_fixtures.Card46Fixture.tearDown

    def test_B6_link_in_prose_is_not_index_entry(self):
        target=self.vault/'TEST.md';body='---\nstatus: "active"\n---\nTEST'
        self.bridge.outbox.mkdir(parents=True,exist_ok=True)
        target.write_text(body);original='TEST mentions [[TEST]] in prose\n'
        self.catalog.write_text(original)
        with patch.object(self.bridge,'_rebuild_and_verify_index',return_value={'ok':True}):
            self.bridge._recover_revalidation_unlocked('TEST',target,body,self.catalog,original,'[[TEST]]',self.root/'TEST-journal.json','a'*64)
        self.assertIn('- [[TEST]]',self.catalog.read_text().splitlines())


class Card59FormalTests(unittest.TestCase):
    setUp=formal_fixtures.FormalReviewTests.setUp
    file=formal_fixtures.FormalReviewTests.file
    candidate=formal_fixtures.FormalReviewTests.candidate
    application=formal_fixtures.FormalReviewTests.application
    delivery=formal_fixtures.FormalReviewTests.delivery
    event_count=formal_fixtures.FormalReviewTests.event_count
    formal=formal_fixtures.FormalReviewTests.formal
    app=formal_fixtures.FormalReviewTests.app
    deliver=formal_fixtures.FormalReviewTests.deliver
    events=formal_fixtures.FormalReviewTests.events
    request=formal_fixtures.FormalReviewTests.request
    items=formal_fixtures.FormalReviewTests.items

    def test_B8_soft_failure_one_retry_then_manual_visible_across_restart(self):
        from xiaoba_review.monitor import OpenReviewModule
        from xiaoba_review.store import ReviewStore
        from xiaoba_agentos import formal_review
        _,rid=self.request()
        rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        for name,kind in [('TEST-a','dsh'),('TEST-b','peer')]:
            rs.register_source({'reviewer_id':name,'kind':kind,'source_ref':'test://'+name,'provenance':'TEST independent'})
        module=object.__new__(OpenReviewModule);module.store=rs
        module.pool=SimpleNamespace(runtime=SimpleNamespace(prepare=Mock(return_value={'route':'run','selections':{'review':'TEST-a','review_2':'TEST-b'}})))
        module.backends=SimpleNamespace(execute=Mock(return_value={'status':'invalid','reason':'TEST soft failure'}))
        for _ in range(5):module.prepare_revisions()
        self.assertEqual(4,module.backends.execute.call_count)
        item=self.items()[rid]
        self.assertEqual('manual_required',item['dispatch_status'])
        self.assertIn('人工',item['body'])
        with self.store.read_only() as db:
            requests=db.execute('SELECT count(*) FROM ri_requests WHERE candidate_id=?',(rid,)).fetchone()[0]
        self.assertEqual(1,requests)
        # Reconstructed module reads persisted attempts rather than an in-memory counter.
        other=object.__new__(OpenReviewModule);other.store=rs;other.pool=module.pool;other.backends=module.backends
        other.prepare_revisions();self.assertEqual(4,module.backends.execute.call_count)

    def test_B8_retry_only_unqualified_assignment_then_stops_after_success(self):
        from xiaoba_review.monitor import OpenReviewModule
        from xiaoba_review.store import ReviewStore,DecisionInbox
        _,rid=self.request();rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        for name,kind in [('TEST-a','dsh'),('TEST-b','peer')]:
            rs.register_source({'reviewer_id':name,'kind':kind,'source_ref':'test://'+name,'provenance':'TEST independent'})
        module=object.__new__(OpenReviewModule);module.store=rs
        module.pool=SimpleNamespace(runtime=SimpleNamespace(prepare=Mock(return_value={'route':'run','selections':{'review':'TEST-a','review_2':'TEST-b'}})))
        calls=[]
        def execute(aid,require_test):
            calls.append(aid)
            with self.store.read_only() as db:
                a=db.execute('SELECT * FROM ri_assignments WHERE id=?',(aid,)).fetchone();card=json.loads(a['card'])
            if a['source_id']=='TEST-b' and calls.count(aid)==1:return {'status':'invalid'}
            data={k:card[k] for k in ('review_request_id','assignment_id','reviewer_role','plan_revision','reviewer_id','source_ref','candidate_sha256','input_digest','card_digest')}
            data.update(receipt_id='TEST-'+aid,verdict='PASS',findings='TEST checked full version',evidence_refs=card['evidence_refs'],started_at=time.time(),finished_at=time.time(),review_type='reuse_change',score=8,applicability='TEST limits')
            receipt=rs.receive(data);rs.attest(receipt['id'],receipt['digest'],'TEST isolated identity')
        module.backends=SimpleNamespace(execute=execute)
        module.prepare_revisions();self.assertEqual('retry_pending',self.items()[rid]['dispatch_status'])
        module.prepare_revisions();self.assertEqual(3,len(calls));self.assertEqual([1,2],sorted(calls.count(a) for a in set(calls)))
        rs.deliver(DecisionInbox())
        self.assertEqual('pending',self.items()[rid]['status'])
        for _ in range(3):module.prepare_revisions()
        self.assertEqual(3,len(calls))

    def test_B8_hard_failure_does_not_automatically_retry(self):
        from xiaoba_review.monitor import OpenReviewModule
        from xiaoba_review.store import ReviewStore
        _,rid=self.request();rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        for name,kind in [('TEST-a','dsh'),('TEST-b','peer')]:
            rs.register_source({'reviewer_id':name,'kind':kind,'source_ref':'test://'+name,'provenance':'TEST independent'})
        module=object.__new__(OpenReviewModule);module.store=rs
        module.pool=SimpleNamespace(runtime=SimpleNamespace(prepare=Mock(return_value={'route':'run','selections':{'review':'TEST-a','review_2':'TEST-b'}})))
        module.backends=SimpleNamespace(execute=Mock(side_effect=ValueError('TEST hard failure')))
        for _ in range(4):module.prepare_revisions()
        self.assertEqual(2,module.backends.execute.call_count)
        self.assertEqual('manual_required',self.items()[rid]['dispatch_status'])

    def test_B8_legacy_request_without_soft_failure_evidence_stays_manual(self):
        import uuid
        from xiaoba_review.monitor import OpenReviewModule
        from xiaoba_review.store import ReviewStore
        _,rid=self.request();item=self.items()[rid]
        rs=ReviewStore(self.store,self.cfg.runtime_dir/'memory-outbox')
        for name,kind in [('TEST-a','dsh'),('TEST-b','peer')]:
            rs.register_source({'reviewer_id':name,'kind':kind,'source_ref':'test://'+name,'provenance':'TEST independent'})
        version=str(uuid.uuid5(uuid.NAMESPACE_URL,rid+item['evidence_sha256']))
        rs.create(rid,'TEST-a','TEST-b','TEST previous interrupted dispatch',[{'ref':item['draft']['path'],'sha256':item['evidence_sha256']}],request_id=version,dsh_channel='backend_adapter',peer_channel='backend_adapter',review_type='reuse_change')
        module=object.__new__(OpenReviewModule);module.store=rs
        module.pool=SimpleNamespace(runtime=SimpleNamespace(prepare=Mock()))
        module.backends=SimpleNamespace(execute=Mock())
        for _ in range(3):module.prepare_revisions()
        self.assertEqual(0,module.backends.execute.call_count)
        self.assertEqual('manual_required',self.items()[rid]['dispatch_status'])


class Card59PolicyTests(unittest.TestCase):
    def test_B5_pass_plus_needs_info_is_not_pass(self):
        with patch('xiaoba_agentos.pool_dispatch.review_vote_problem',return_value=''):
            self.assertFalse(BUILD_RELEASE_MODULE._passed_verdict('FINAL_ACCEPTANCE: PASS\nFINAL_ACCEPTANCE: NEEDS_INFO'))
        self.assertFalse(BUILD_RELEASE_MODULE._passed_verdict('FINAL_ACCEPTANCE: PASS\nFINAL_ACCEPTANCE: NEEDS_INFO'))
        self.assertTrue(BUILD_RELEASE_MODULE._passed_verdict('FINAL_ACCEPTANCE: PASS'))

    def test_B9_before_equals_previous_published_after(self):
        self.assertEqual('cc41e0e5749d06be7c7020b5733e7fd04103ee38398288b245b8ec0eef76a00b',hashlib.sha256(PREVIOUS.read_bytes()).hexdigest())
        previous=json.loads(PREVIOUS.read_text())
        current=json.loads((REPO/'development/integrated-candidate/src/xiaoba_agentos/review_ui_assets/manifest.json').read_text())
        self.assertEqual({(v['base'],v['relative']):v['after_sha256'] for v in previous['files']},
                         {(v['base'],v['relative']):v['before_sha256'] for v in current['files']})

    def test_B3_release_tool_declares_marker(self):
        import importlib.util
        path=REPO/'development/integrated-candidate/scripts/submit_release_acceptance.py'
        spec=importlib.util.spec_from_file_location('TEST_release_submit',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        args=module.arguments(['--config','TEST-config','TEST prompt','--no-work-task','--memory-kind','audit'])
        self.assertIn('--release-acceptance',args)
        self.assertEqual(['--config','TEST-config','submit','TEST prompt'],args[:4])

    def test_B10_all_repository_tests_main_guard_last(self):
        from test_repository_test_layout import violations, EXCLUSIONS
        self.assertEqual({},EXCLUSIONS)  # 卡 60 已修好监看副本，临时排除已移除
        self.assertEqual([],violations(REPO))
