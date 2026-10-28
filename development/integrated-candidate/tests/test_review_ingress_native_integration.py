"""Genuine sandbox executions joined to explicit TEST ingress identities and receipts."""
import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from fixtures_review import _config
from fixtures_validation import native_trial, bound_diff
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.experience_workflow import verify_final_qualification, complete_audit
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'review-ingress'))
import xiaoba_agentos
import xiaoba_review
from xiaoba_review.store import ReviewStore, DecisionInbox, digest

class ReviewIngressNativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve(); self.cfg=_config(self.root)
        self.store=EventStore(self.cfg.database_path); self.store.initialize(); self.store.apply_validation_extension()
        self.outbox=self.cfg.runtime_dir/'memory-outbox'; self.rs=ReviewStore(self.store,self.outbox); self.inbox=DecisionInbox()
        for name,kind in (('TEST-dsh','dsh'),('TEST-peer','peer')):
            self.rs.register_source({'reviewer_id':name,'kind':kind,'source_ref':'test://'+name,'provenance':'TEST synthetic independent source'})

    def candidate(self):
        c=self.store.create_authored_candidate('TEST方法','仅测试证据连接',self.outbox,author='test',experience_kind='method')
        self.store.review_memory(c['task_id'],'delivery','approve','TEST初审',c['evidence_sha256']); return c

    def gate(self,c):
        with self.store.read_only() as db: return verify_final_qualification(db,c['task_id'])

    def request(self,c,task=None):
        evidence=[{'ref':'test://execution','sha256':task['execution_digest'] if task else 'a'*64}]
        return self.rs.create(c['task_id'],'TEST-dsh','TEST-peer','TEST真实执行审查',evidence)

    def receipts(self,req,verdict='PASS'):
        receipts={}
        for assignment in req['assignments']:
            card=assignment['card']; data={k:card[k] for k in ('review_request_id','assignment_id','reviewer_role','plan_revision','reviewer_id','source_ref','candidate_sha256','input_digest','card_digest')}
            data.update(receipt_id=assignment['id']+'-first',verdict=verdict,findings='TEST证据独立核查',evidence_refs=card['evidence_refs'],started_at=time.time(),finished_at=time.time())
            received=self.rs.receive(data); self.rs.attest(received['id'],received['digest'],'TEST来源核对')
            receipts[assignment['role']]=data
        self.rs.deliver(self.inbox); return receipts

    def successful(self):
        c=self.candidate(); task=native_trial(self.store,c); req=self.request(c,task); receipts=self.receipts(req)
        self.assertTrue(self.gate(c)['qualified'],self.gate(c)); return c,task,req,receipts

    def test_loaded_source_paths_audit(self):
        expected=Path(__file__).resolve().parents[2]
        self.assertTrue(Path(xiaoba_agentos.__file__).is_relative_to(expected/'integrated-candidate'))
        self.assertTrue(Path(xiaoba_review.__file__).is_relative_to(expected/'review-ingress'))

    def test_end_to_end_receipt_ingress_to_native_qualification_and_publication(self):
        c,task,req,_=self.successful()
        self.assertEqual(req['subject']['validation_binding']['task_id'],task['task_id'])
        self.assertEqual(self.store.get_memory_candidate(c['task_id'])['status'],'pending_final_review')
        # TEST human decision only, never a real candidate approval.
        self.store.review_memory(c['task_id'],'final','approve','TEST人工终审',c['evidence_sha256'])
        with self.store.publication_transaction(c['task_id']) as window: self.assertEqual(window.evidence_sha256,c['evidence_sha256'])
        self.assertIsNone(self.store.get_memory_candidate(task['task_id']))

    def test_real_attested_receipts_never_replace_missing_execution_task(self):
        c=self.candidate(); req=self.request(c); self.receipts(req)
        self.assertFalse(self.gate(c)['qualified']); self.assertIsNone(self.store.get_validation_lineage(c['task_id']))

    def test_receipts_for_execution_b_cannot_qualify_failed_execution_a(self):
        c=self.candidate(); a=native_trial(self.store,c,output='TRIAL_VALIDATION: FAIL')
        b=native_trial(self.store,c); req=self.request(c,b); self.receipts(req)
        # Deliberately injected internal row corruption is a gate-boundary fixture.
        with self.store.connect() as db:
            db.execute("UPDATE experience_validation_records SET status='passed',task_id=?,execution_digest=?,dsh_verdict='PASS',peer_verdict='PASS',diff_explanation=? WHERE candidate_id=? AND round=2",(a['task_id'],a['execution_digest'],json.dumps(bound_diff(self.store,c['task_id'])),c['task_id']))
            db.execute("UPDATE experience_validation_lineages SET lineage_status='ready_for_final' WHERE candidate_id=?",(c['task_id'],))
        self.assertFalse(self.gate(c)['qualified'])

    def test_repeat_receipts_and_dispatch_are_idempotent(self):
        c,_,req,receipts=self.successful()
        before=self.rs.state()
        for data in receipts.values(): self.rs.receive(data)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool: list(pool.map(lambda _:self.rs.deliver(self.inbox),range(3)))
        self.assertTrue(self.gate(c)['qualified'])
        with self.store.read_only() as db: self.assertEqual(db.execute('SELECT count(*) FROM ri_inbox WHERE request_id=? AND current=1',(req['id'],)).fetchone()[0],1)

    def test_restart_preserves_qualification_and_bindings(self):
        c,_,_,_=self.successful(); self.store=EventStore(self.cfg.database_path); self.rs=ReviewStore(self.store,self.outbox)
        self.assertTrue(self.gate(c)['qualified']); self.assertTrue(self.store.verify_event_chain()['ok'])

    def test_latest_fail_receipt_revokes_existing_qualification(self):
        c,_,_,receipts=self.successful(); old=receipts['dsh']; newer=dict(old,receipt_id=old['receipt_id']+'-fail',verdict='FAIL',findings='TEST后续发现缺陷')
        received=self.rs.receive(newer); self.rs.attest(received['id'],received['digest'],'TEST来源复核'); self.rs.deliver(self.inbox)
        self.assertFalse(self.gate(c)['qualified'])

    def test_new_unattested_receipt_also_revokes_old_pass(self):
        c,_,_,receipts=self.successful(); old=receipts['peer']; self.rs.receive(dict(old,receipt_id=old['receipt_id']+'-new'))
        self.assertFalse(self.gate(c)['qualified'])

    def test_invalid_latest_receipt_does_not_leave_old_packet_current(self):
        c,_,_,receipts=self.successful(); old=receipts['peer']; self.rs.receive(dict(old,receipt_id=old['receipt_id']+'-wrong-role',reviewer_role='dsh'))
        self.assertFalse(self.gate(c)['qualified'])

    def test_wrong_explicit_receipt_ids_rejected(self):
        c,_,_,_=self.successful()
        with self.store.connect() as db: db.execute("UPDATE experience_validation_records SET dsh_receipt_id='missing',peer_receipt_id='missing' WHERE candidate_id=?",(c['task_id'],))
        self.assertFalse(self.gate(c)['qualified'])

    def test_review_card_digest_corruption_is_detected(self):
        c,_,req,_=self.successful()
        with self.store.connect() as db:
            row=db.execute('SELECT id,card FROM ri_assignments WHERE request_id=? LIMIT 1',(req['id'],)).fetchone(); card=json.loads(row['card']); card['scope']='篡改'
            db.execute('UPDATE ri_assignments SET card=? WHERE id=?',(json.dumps(card),row['id']))
        self.assertFalse(self.gate(c)['qualified'])

    def test_wrong_source_or_input_digest_is_rejected(self):
        c,_,_,receipts=self.successful(); old=receipts['dsh']; self.rs.receive(dict(old,receipt_id=old['receipt_id']+'-wrong-source',source_ref='test://other'))
        self.assertFalse(self.gate(c)['qualified'])

    def test_initial_approval_or_body_drift_invalidates_packet(self):
        for body_change in (False,True):
            c,_,_,_=self.successful()
            if body_change: Path(c['staged_path']).write_text('未审查正文')
            else: self.store.review_memory(c['task_id'],'delivery','approve','TEST重新初审',c['evidence_sha256'])
            self.rs.state(); self.assertFalse(self.gate(c)['qualified'])

    def test_new_request_waiting_receipts_revokes_previous_pass(self):
        c,task,_,_=self.successful(); self.request(c,task); self.assertFalse(self.gate(c)['qualified'])

    def test_evidence_before_execution_is_rejected(self):
        c=self.candidate(); task=native_trial(self.store,c); req=self.request(c,task)
        receipts=self.receipts(req); old=receipts['peer']
        received=self.rs.receive(dict(old,receipt_id=old['receipt_id']+'-old',started_at=task['execution']['started_at']-10,finished_at=task['execution']['started_at']-9))
        self.rs.attest(received['id'],received['digest'],'TEST旧回执'); self.rs.deliver(self.inbox)
        self.assertFalse(self.gate(c)['qualified'])
