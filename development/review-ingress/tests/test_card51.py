"""Card 51: role selection, corrections and initial value admission."""
from datetime import datetime
import json
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_review as fx
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.store import ReviewStore


class Card51IngressTests(fx.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.pool=AgentPool(self.store,BackendRegistry(self.store))

    def add_node(self,node,kind,model=None):
        source=self.pool.add({'node_id':node,'name':node,'agent':node,'kind':kind,
            'interface':'cli','entry':'/TEST/'+node,'model':model or 'TEST '+node,'agent_type':'other'})
        self.pool.change(node,source['node']['revision'],'toggle',True)
        return node

    def score(self,node,role='review',success=True):
        task=self.native.submit('TEST actual task',2)
        run=self.native.start_agent_run(task,node,{'purpose':role})
        self.native.finish_agent_run(run,'completed' if success else 'permanent_error',
            '审查结论：PASS\nTEST receipt' if success else None,None,1)
        self.native.complete_manually(task,'TEST settled')
        return task

    def prepare(self,kinds,**kw):
        # Four-field historical fixtures in FixtureMixin have no runnable entry.
        with patch.object(self.pool.runtime,'readiness',side_effect=lambda db,s:{'ready':bool(s.get('node'))}):
            return self.pool.runtime.prepare(kinds,**kw)

    def test_legacy_labels_are_not_exact_vendor_filters_and_both_uses_review_score(self):
        self.add_node('old-dsh','dsh');self.add_node('modern','review');self.add_node('new','both')
        for _ in range(4):self.score('modern')
        for _ in range(3):self.score('old-dsh')
        for _ in range(5):self.score('new','collaboration')
        result=self.prepare(['dsh','peer'])
        self.assertEqual('run',result['route'],result)
        self.assertEqual({'dsh':'modern','peer':'new'},result['selections'])
        result=self.prepare(['review','review_2'])
        self.assertEqual({'review':'modern','review_2':'new'},result['selections'])

    def test_collaboration_selects_by_its_role_and_reserves_second_seat(self):
        for node,kind in [('work','collaborate'),('both','both'),('review-only','review')]:self.add_node(node,kind)
        for _ in range(4):self.score('both','collaboration')
        for _ in range(9):self.score('work','review')
        result=self.prepare(['collaborate','collaborate_2'])
        self.assertEqual({'collaborate':'both','collaborate_2':'work'},result['selections'])

    def test_recent_dispatch_rotates_low_sample_nodes(self):
        for node in ['winner','new-a','new-b']:self.add_node(node,'review')
        for _ in range(3):self.score('winner')
        first=self.prepare(['review','review_2'])
        self.assertEqual('new-a',first['selections']['review_2'])
        task=self.native.submit('TEST assigned not settled',2)
        self.native.start_agent_run(task,'new-a',{'purpose':'review'})
        second=self.prepare(['review','review_2'])
        self.assertEqual('new-b',second['selections']['review_2'])

    def test_correction_preserves_role_week_and_one_sample_with_compare_and_swap(self):
        node=self.add_node('correct','both');task=self.score(node)
        scorer=self.pool.runtime.scoring;day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        args=dict(node_id=node,day=day,task_id=task,task_type='ignored correction label',
            outcome='not_good',evidence_refs=['TEST corrected verified receipt'],expected_revision=1,
            assessment_id='TEST-correction',role='review')
        self.assertEqual(0,scorer.assess(**args)['points'])
        self.assertEqual(2,scorer.assess(**args)['revision'])
        row=scorer.weekly(day)['ranking'][0]
        self.assertEqual(('review',1,0),(row['role'],row['tasks'],row['success_rate']))
        with self.assertRaises(ValueError):scorer.assess(**dict(args,assessment_id='stale'))

    def unapproved(self):
        task_id = self.native.submit('TEST historical candidate fixture', 2)
        self.native.complete_manually(task_id, 'TEST completed legacy source task')
        path = self.root / 'memory-outbox' / f'{task_id}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('TEST full draft to discard', encoding='utf-8')
        candidate_sha256 = __import__('hashlib').sha256(path.read_bytes()).hexdigest()
        self.native.stage_memory_candidate(task_id, str(path), candidate_sha256, [])
        return {'task_id': task_id, 'staged_path': str(path), 'evidence_sha256': candidate_sha256}

    def rate(self,candidate,scores,duplicate=None):
        req=self.store.create(candidate['task_id'],'fixture-dsh','fixture-peer','TEST new value',
            [{'ref':'TEST evidence','sha256':'a'*64}],review_type='new_experience')
        for assignment,score in zip(req['assignments'],scores):
            received=self.store.receive(self.receipt(assignment,verdict='FAIL',score=score,duplicate_of=duplicate))
            self.store.attest(received['id'],received['digest'],'TEST actual source verified')
        self.store.deliver(self.inbox)
        return self.store.get(req['id'])

    def value_review_request(self, candidate, scope='TEST prepared value review'):
        suffix = candidate['task_id'][:8]
        review = self.add_node('TEST-value-review-' + suffix, 'review', model='TEST review model')
        both = self.add_node('TEST-value-both-' + suffix, 'both', model='TEST both model')
        return self.store.create(
            candidate['task_id'], review, both, scope,
            [{'ref': candidate['staged_path'], 'sha256': candidate['evidence_sha256']}],
            review_type='new_experience', value_review=True,
            dsh_channel='external_handoff', peer_channel='external_handoff',
        )

    def rate_prepared(self, candidate, scores, duplicate=None):
        req = self.value_review_request(candidate)
        for assignment, score in zip(req['assignments'], scores):
            received = self.store.receive(self.receipt(
                assignment, verdict='FAIL', score=score, duplicate_of=duplicate,
            ))
            self.store.attest(received['id'], received['digest'], 'TEST actual source verified')
        self.store.deliver(self.inbox)
        return self.store.get(req['id'])

    def prepared_candidate(self, *, complete=True):
        from xiaoba_agentos.value_queue import defer_tx
        task = self.native.submit('TEST prepared experience', 2, snapshot={
            'experience_policy': {'version': 1, 'kind': 'method', 'problem_key': 'TEST-prepared'},
        })
        if complete:
            self.native.complete_manually(task, 'TEST completed source task')
        path = self.root / 'memory-outbox' / f'{task}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('TEST prepared candidate body', encoding='utf-8')
        sha = __import__('hashlib').sha256(path.read_bytes()).hexdigest()
        with self.native.connect() as db:
            defer_tx(db, task, {
                'version': 1,
                'staged_path': str(path),
                'candidate_sha256': sha,
                'source_task_ids': [],
                'reason': 'no_scoring_source',
            })
        return {'task_id': task, 'staged_path': str(path), 'evidence_sha256': sha}

    def test_low_value_retires_unapproved_candidate_not_just_packet_field(self):
        c=self.unapproved()
        original_result=self.native.get_task(c['task_id'])['result']
        original_body=Path(c['staged_path']).read_bytes()
        req=self.rate(c,[5,5])
        self.assertEqual('value_discarded',req['state'])
        current=self.native.get_memory_candidate(c['task_id'])
        self.assertEqual('retired',current['status'])
        summary_path=Path(current['staged_path'])
        self.assertNotEqual(c['staged_path'],str(summary_path))
        self.assertNotIn('TEST full draft to discard',summary_path.read_text())
        self.assertEqual(current['evidence_sha256'],__import__('hashlib').sha256(summary_path.read_bytes()).hexdigest())
        self.assertFalse(Path(c['staged_path']).exists())
        self.assertNotIn(c['task_id'],{r['task_id'] for r in self.native.review_queue('delivery')})
        self.assertEqual(0,len(req['deliveries']))
        self.assertEqual(original_result,self.native.get_task(c['task_id'])['result'])
        with self.native.read_only() as db:
            event=db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='open_review.value_discarded' ORDER BY sequence DESC LIMIT 1",(c['task_id'],)).fetchone()
            payload=json.loads(event['payload_json'])
            self.assertEqual(c['evidence_sha256'],payload['reviewed_candidate_sha256'])
            self.assertEqual(current['evidence_sha256'],payload['summary_sha256'])
            self.assertEqual(0,db.execute('SELECT count(*) FROM ri_value_discard_cleanup WHERE task_id=?',(c['task_id'],)).fetchone()[0])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def _crash_value_discard(self,candidate,request_id,phase):
        source=Path(__file__).resolve().parents[3]
        script='''
import os,sys
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from xiaoba_agentos.store import EventStore
from xiaoba_review.store import ReviewStore
native=EventStore(sys.argv[3])
review=ReviewStore(native,sys.argv[4])
phase=sys.argv[8]
if phase=='before_commit':
    from xiaoba_agentos.store import EventStore
    original=EventStore._write_candidate_status_sidecar_values
    def crash_after_summary_status(*args,**kwargs):
        original(*args,**kwargs)
        os._exit(9)
    EventStore._write_candidate_status_sidecar_values=staticmethod(crash_after_summary_status)
else:
    review.recover_value_discard_cleanup=lambda *args,**kwargs: os._exit(9)
assessment={'version':2,'candidate_sha256':sys.argv[7],'decision':'discard',
            'reason':'below_threshold','scores':{'TEST-a':5,'TEST-b':5}}
with review.transaction() as db:
    review.discard_value(db,{'id':sys.argv[5],'candidate_id':sys.argv[6]},assessment)
'''
        env=dict(__import__('os').environ,PYTHONDONTWRITEBYTECODE='1')
        return subprocess.run([
            sys.executable,'-B','-c',script,
            str(source/'development/integrated-candidate/src'),
            str(source/'development/review-ingress'),
            str(self.native.path),str(self.root/'memory-outbox'),request_id,
            candidate['task_id'],candidate['evidence_sha256'],phase,
        ],capture_output=True,text=True,env=env)

    def _discard_request(self,candidate):
        request=self.store.create(
            candidate['task_id'],'fixture-dsh','fixture-peer','TEST crash-safe discard',
            [{'ref':'TEST evidence','sha256':'a'*64}],review_type='new_experience',
        )
        return request['id']

    def test_discard_crash_before_commit_keeps_database_bound_original_body(self):
        candidate=self.unapproved()
        request_id=self._discard_request(candidate)
        old_path=Path(candidate['staged_path'])
        old_body=old_path.read_bytes()
        result=self._crash_value_discard(candidate,request_id,'before_commit')
        self.assertEqual(9,result.returncode,result.stderr)
        current=self.native.get_memory_candidate(candidate['task_id'])
        self.assertEqual('pending_delivery_review',current['status'])
        self.assertEqual(candidate['evidence_sha256'],current['evidence_sha256'])
        self.assertEqual(old_body,old_path.read_bytes())
        with self.native.read_only() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM ri_value_discard_cleanup WHERE task_id=?',(candidate['task_id'],)).fetchone()[0])
            self.assertEqual(0,db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type='open_review.value_discarded'",(candidate['task_id'],)).fetchone()[0])

    def test_discard_restart_recovers_postcommit_cleanup_by_hash(self):
        candidate=self.unapproved()
        request_id=self._discard_request(candidate)
        old_path=Path(candidate['staged_path'])
        result=self._crash_value_discard(candidate,request_id,'after_commit')
        self.assertEqual(9,result.returncode,result.stderr)
        current=self.native.get_memory_candidate(candidate['task_id'])
        summary_path=Path(current['staged_path'])
        self.assertEqual('retired',current['status'])
        self.assertTrue(old_path.is_file())
        self.assertEqual(current['evidence_sha256'],__import__('hashlib').sha256(summary_path.read_bytes()).hexdigest())
        with self.native.read_only() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM ri_value_discard_cleanup WHERE task_id=?',(candidate['task_id'],)).fetchone()[0])
        ReviewStore(self.native,self.root/'memory-outbox')
        self.assertFalse(old_path.exists())
        self.assertTrue(summary_path.is_file())
        with self.native.read_only() as db:
            self.assertEqual(0,db.execute('SELECT count(*) FROM ri_value_discard_cleanup WHERE task_id=?',(candidate['task_id'],)).fetchone()[0])

    def test_discard_recovery_keeps_body_when_old_hash_has_drifted(self):
        candidate=self.unapproved()
        request_id=self._discard_request(candidate)
        old_path=Path(candidate['staged_path'])
        result=self._crash_value_discard(candidate,request_id,'after_commit')
        self.assertEqual(9,result.returncode,result.stderr)
        old_path.write_text('TEST changed after committed discard',encoding='utf-8')
        ReviewStore(self.native,self.root/'memory-outbox')
        self.assertEqual('TEST changed after committed discard',old_path.read_text())
        with self.native.read_only() as db:
            self.assertEqual(1,db.execute('SELECT count(*) FROM ri_value_discard_cleanup WHERE task_id=?',(candidate['task_id'],)).fetchone()[0])

    def test_value_disagreement_and_duplicate_reach_initial_review_with_scores(self):
        for scores,duplicate,reason in [([6,6],None,'average'),([1,5],None,'disagreement'),([1,1],'TEST-known','duplicate')]:
            with self.subTest(reason=reason):
                c=self.unapproved();req=self.rate(c,scores,duplicate)
                self.assertEqual('submitted',req['state'])
                item=next(r for r in self.native.review_queue('delivery') if r['task_id']==c['task_id'])
                self.assertEqual(reason,item['value_assessment']['reason'])
                self.assertEqual(scores, list(item['value_assessment']['scores'].values()))

    def test_prepared_subject_reads_only_hash_bound_outbox_file(self):
        from pathlib import Path
        c = self.prepared_candidate()
        with self.native.read_only() as db:
            subject = self.store.subject(db, c['task_id'])
        self.assertEqual(c['evidence_sha256'], subject['candidate_sha256'])
        self.assertEqual('TEST prepared candidate body', subject['body'])
        self.assertIsNone(self.native.get_memory_candidate(c['task_id']))

        Path(c['staged_path']).write_text('TEST changed body', encoding='utf-8')
        with self.native.read_only() as db, self.assertRaisesRegex(ValueError, '哈希不一致'):
            self.store.subject(db, c['task_id'])

    def test_prepared_subject_rejects_symlinked_candidate(self):
        from pathlib import Path
        c = self.prepared_candidate()
        path = Path(c['staged_path'])
        target = self.root / 'outside.md'
        target.write_text('TEST prepared candidate body', encoding='utf-8')
        path.unlink()
        path.symlink_to(target)
        with self.native.read_only() as db, self.assertRaisesRegex(ValueError, '符号链接'):
            self.store.subject(db, c['task_id'])

    def test_prepared_subject_requires_completed_method_or_rule_task(self):
        c = self.prepared_candidate(complete=False)
        with self.native.read_only() as db, self.assertRaisesRegex(ValueError, '已完成任务'):
            self.store.subject(db, c['task_id'])

    def test_prepared_value_admission_waits_for_both_independent_receipts(self):
        c = self.prepared_candidate()
        req = self.value_review_request(c)
        assignment = req['assignments'][0]
        received = self.store.receive(self.receipt(assignment, verdict='FAIL', score=8))
        self.store.attest(received['id'], received['digest'], 'TEST source checked')
        self.assertIsNone(self.native.get_memory_candidate(c['task_id']))

        second = req['assignments'][1]
        received = self.store.receive(self.receipt(second, verdict='FAIL', score=8))
        settled = self.store.attest(received['id'], received['digest'], 'TEST source checked')
        self.assertEqual('pending_center', settled['state'])
        candidate = self.native.get_memory_candidate(c['task_id'])
        self.assertEqual(('pending_delivery_review', c['evidence_sha256']),
            (candidate['status'], candidate['evidence_sha256']))
        with self.native.read_only() as db:
            self.assertEqual(0, db.execute(
                'SELECT count(*) FROM memory_reviews WHERE task_id=?', (c['task_id'],),
            ).fetchone()[0])
            assessment = json.loads(db.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (c['task_id'],),
            ).fetchone()[0])
        self.assertEqual(2, assessment['version'])
        self.assertEqual(req['id'], assessment['review_request_id'])
        self.assertEqual(c['evidence_sha256'], assessment['candidate_sha256'])
        self.assertEqual('initial_review', assessment['decision'])

    def test_prepared_low_value_is_recorded_without_candidate_or_user_packet(self):
        c = self.prepared_candidate()
        original_result = self.native.get_task(c['task_id'])['result']
        req = self.rate_prepared(c, [5, 5])
        self.assertEqual('value_discarded', req['state'])
        self.assertIsNone(self.native.get_memory_candidate(c['task_id']))
        self.assertNotIn(c['task_id'], {item['task_id'] for item in self.native.review_queue('delivery')})
        self.assertEqual(0, self.count('ri_inbox'))
        with self.native.read_only() as db:
            assessment = json.loads(db.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (c['task_id'],),
            ).fetchone()[0])
        self.assertEqual(c['evidence_sha256'], assessment['candidate_sha256'])
        self.assertEqual('discard', assessment['decision'])
        self.assertEqual(original_result, self.native.get_task(c['task_id'])['result'])
        self.assertFalse(Path(c['staged_path']).exists())
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_prepared_discard_cleanup_recovers_after_commit_crash(self):
        c = self.prepared_candidate()
        original_result = self.native.get_task(c['task_id'])['result']
        req = self.value_review_request(c)
        first, second = req['assignments']
        received = self.store.receive(self.receipt(first, verdict='FAIL', score=5))
        self.store.attest(received['id'], received['digest'], 'TEST source checked')
        received = self.store.receive(self.receipt(second, verdict='FAIL', score=5))

        script = '''
import os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from xiaoba_agentos.store import EventStore
from xiaoba_review.store import ReviewStore
native=EventStore(Path(sys.argv[3]))
review=ReviewStore(native,Path(sys.argv[4]))
native.cleanup_discarded_candidate=lambda *args,**kwargs: os._exit(9)
review.attest(sys.argv[5],sys.argv[6],'TEST second source checked')
'''
        source = Path(__file__).resolve().parents[3]
        env = dict(__import__('os').environ, PYTHONDONTWRITEBYTECODE='1')
        crashed = subprocess.run([
            sys.executable, '-B', '-c', script,
            str(source / 'development/integrated-candidate/src'),
            str(source / 'development/review-ingress'),
            str(self.native.path), str(self.root / 'memory-outbox'),
            received['id'], received['digest'],
        ], capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(9, crashed.returncode, crashed.stderr)
        self.assertTrue(Path(c['staged_path']).is_file())
        self.assertEqual(original_result, self.native.get_task(c['task_id'])['result'])
        with self.native.read_only() as db:
            assessment = json.loads(db.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (c['task_id'],),
            ).fetchone()[0])
            self.assertEqual('discard', assessment['decision'])

        ReviewStore(self.native, self.root / 'memory-outbox')

        self.assertFalse(Path(c['staged_path']).exists())
        self.assertEqual(original_result, self.native.get_task(c['task_id'])['result'])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_prepared_value_admission_fails_clearly_without_core_api(self):
        from unittest.mock import patch
        c = self.prepared_candidate()
        req = self.value_review_request(c)
        assignments = req['assignments']
        received = self.store.receive(self.receipt(assignments[0], verdict='FAIL', score=8))
        self.store.attest(received['id'], received['digest'], 'TEST source checked')
        received = self.store.receive(self.receipt(assignments[1], verdict='FAIL', score=8))
        with patch.object(self.native, 'assess_value_candidate', None, create=True):
            with self.assertRaisesRegex(RuntimeError, 'EventStore.assess_value_candidate'):
                self.store.attest(received['id'], received['digest'], 'TEST source checked')
        self.assertIsNone(self.native.get_memory_candidate(c['task_id']))

    def test_prepared_admission_rolls_back_candidate_and_sidecar_if_packet_write_fails(self):
        from pathlib import Path
        c = self.prepared_candidate()
        req = self.value_review_request(c)
        for assignment in req['assignments'][:1]:
            received = self.store.receive(self.receipt(assignment, verdict='FAIL', score=8))
            self.store.attest(received['id'], received['digest'], 'TEST source checked')
        second = req['assignments'][1]
        received = self.store.receive(self.receipt(second, verdict='FAIL', score=8))
        staged = Path(c['staged_path'])
        original = staged.read_bytes()
        sidecar = staged.with_suffix(staged.suffix + '.status.json')
        self.assertFalse(sidecar.exists())
        with self.native.connect() as db:
            db.execute("CREATE TRIGGER fail_prepared_packet BEFORE INSERT ON ri_outbox "
                       "BEGIN SELECT RAISE(ABORT,'TEST packet failure'); END")
        with self.assertRaisesRegex(Exception, 'TEST packet failure'):
            self.store.attest(received['id'], received['digest'], 'TEST source checked')
        self.assertEqual(original, staged.read_bytes())
        self.assertFalse(sidecar.exists())
        self.assertIsNone(self.native.get_memory_candidate(c['task_id']))
        with self.native.read_only() as db:
            self.assertEqual(0, db.execute(
                "SELECT count(*) FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (c['task_id'],),
            ).fetchone()[0])

    def test_value_discard_failure_rolls_back_note_hash_candidate_and_event(self):
        c=self.unapproved()
        from pathlib import Path
        before=Path(c['staged_path']).read_bytes()
        with self.native.connect() as db:
            db.execute("CREATE TRIGGER fail_value BEFORE INSERT ON events WHEN NEW.event_type='open_review.value_discarded' BEGIN SELECT RAISE(ABORT,'TEST rollback'); END")
        with self.assertRaises(Exception):self.rate(c,[4,4])
        self.assertEqual(before,Path(c['staged_path']).read_bytes())
        current=self.native.get_memory_candidate(c['task_id'])
        self.assertEqual(c['evidence_sha256'],current['evidence_sha256'])
        self.assertEqual('pending_delivery_review',current['status'])

    def test_public_value_discard_rejects_body_or_sidecar_drift_without_mutation(self):
        from pathlib import Path

        def snapshot():
            tables=('tasks','memory_candidates','events','ri_requests','ri_assignments',
                    'ri_receipts','ri_inbox','ri_outbox')
            with self.native.read_only() as db:
                return {table:[tuple(row) for row in db.execute(
                    'SELECT * FROM '+table+' ORDER BY rowid')] for table in tables}

        for drift in ('body_hash','sidecar_hash'):
            with self.subTest(drift=drift):
                candidate=self.unapproved()
                request=self.store.create(candidate['task_id'],'fixture-dsh','fixture-peer',
                    'TEST discard drift guard',[{'ref':'TEST evidence','sha256':'a'*64}],
                    review_type='new_experience')
                assignments=request['assignments']
                received=[]
                for assignment in assignments:
                    receipt=self.store.receive(self.receipt(assignment,verdict='FAIL',score=4))
                    self.assertEqual('identity_pending',receipt['status'])
                    received.append((receipt,assignment))
                self.store.attest(received[0][0]['id'],received[0][0]['digest'],
                                  'TEST first source checked')
                staged=Path(candidate['staged_path'])
                sidecar=staged.with_suffix(staged.suffix+'.status.json')
                db_state=snapshot()
                original_refresh=self.store.refresh
                file_state=[]
                refresh_calls=0

                def refresh_then_drift(db,request):
                    nonlocal refresh_calls
                    result=original_refresh(db,request)
                    refresh_calls+=1
                    if result and refresh_calls==2:
                        if drift=='body_hash':
                            staged.write_text('TEST changed after review card freeze',encoding='utf-8')
                        else:
                            sidecar.write_text(json.dumps({
                                'task_id':candidate['task_id'],
                                'status':'pending_delivery_review',
                                'evidence_sha256':'0'*64,
                            }),encoding='utf-8')
                        file_state.append((staged.read_bytes(),sidecar.read_bytes() if sidecar.exists() else None))
                    return result

                with self.assertRaisesRegex(ValueError,'与登记 evidence_sha256 不匹配|状态文件与当前候选版本'):
                    with patch.object(self.store,'refresh',side_effect=refresh_then_drift):
                        self.store.attest(received[1][0]['id'],received[1][0]['digest'],
                                          'TEST second source checked')
                self.assertEqual(1,len(file_state))
                self.assertEqual(file_state[0],(staged.read_bytes(),sidecar.read_bytes() if sidecar.exists() else None))
                self.assertEqual(db_state,snapshot())

    def value_dispatch_module(self):
        backends = BackendRegistry(self.store)
        module = fx.OpenReviewModule(types.SimpleNamespace(page_token='TEST-page-token'),
                                     self.store, self.inbox, backends)
        return module

    def dispatch_prepared_value(self, module, candidate, scores, fail_once=None, calls=None):
        scores = list(scores)
        calls = calls if calls is not None else {}

        def execute(assignment_id, require_test=False):
            calls[assignment_id] = calls.get(assignment_id, 0) + 1
            attempt = f'TEST-attempt-{assignment_id}-{calls[assignment_id]}'
            with self.store.transaction() as db:
                row = db.execute('SELECT * FROM ri_assignments WHERE id=?', (assignment_id,)).fetchone()
                request_id = row['request_id']
                self.store.event(db, request_id, 'backend_started', {
                    'assignment_id': assignment_id, 'attempt': attempt,
                })
                assignment = dict(row)
                assignment['card'] = json.loads(assignment['card'])
            if fail_once == assignment_id or fail_once == len(calls):
                with self.store.transaction() as db:
                    self.store.event(db, request_id, 'backend_failed', {
                        'assignment_id': assignment_id, 'attempt': attempt, 'error_type': 'RuntimeError',
                    })
                raise RuntimeError('TEST backend unavailable')
            payload = self.receipt(assignment, verdict='FAIL', score=scores[len(calls) - 1])
            payload['receipt_id'] = attempt
            receipt = self.store.receive(payload)
            return self.store.attest(
                attempt, receipt['digest'], '后台适配器实际执行；attempt=' + attempt,
                backend_attempt=attempt,
            )

        module.pool.runtime.readiness = lambda db, source: {'ready': True}
        module.backends.execute = execute
        response = module.value_review_dispatch(types.SimpleNamespace(json=lambda: {
            'candidate_id': candidate['task_id'],
            'candidate_sha256': candidate['evidence_sha256'],
        }))
        return response, calls

    def add_value_review_nodes(self, suffix=''):
        self.add_node('TEST-value-review' + suffix, 'review', model='TEST review model')
        self.add_node('TEST-value-both' + suffix, 'both', model='TEST both model')

    def test_value_review_route_uses_review_and_both_receipts_before_initial_review(self):
        self.add_value_review_nodes()
        candidate = self.prepared_candidate()
        module = self.value_dispatch_module()
        response, _ = self.dispatch_prepared_value(module, candidate, [8, 8])

        self.assertEqual(200, response[0])
        result = response[1]['result']
        self.assertEqual('completed', result['route'])
        self.assertNotIn(candidate['task_id'], {
            item['task_id'] for item in self.native.value_review_queue()
        })
        admitted = self.native.get_memory_candidate(candidate['task_id'])
        self.assertEqual(('pending_delivery_review', candidate['evidence_sha256']),
                         (admitted['status'], admitted['evidence_sha256']))
        with self.native.read_only() as db:
            rows = db.execute(
                'SELECT a.source_id,s.data FROM ri_assignments a JOIN ri_sources s ON s.id=a.source_id '
                'WHERE a.request_id=? AND a.active=1 ORDER BY a.role', (result['request_id'],),
            ).fetchall()
            assessment = json.loads(db.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (candidate['task_id'],),
            ).fetchone()[0])
            self.assertEqual(0, db.execute(
                'SELECT count(*) FROM memory_reviews WHERE task_id=?', (candidate['task_id'],),
            ).fetchone()[0])
        self.assertEqual({'TEST-value-review', 'TEST-value-both'}, {row['source_id'] for row in rows})
        self.assertEqual({'review', 'both'}, {json.loads(row['data'])['kind'] for row in rows})
        self.assertEqual(2, assessment['version'])
        self.assertEqual(result['request_id'], assessment['review_request_id'])
        self.assertEqual('initial_review', assessment['decision'])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_value_review_queue_keeps_missing_nodes_visible_without_request(self):
        candidate = self.prepared_candidate()
        module = self.value_dispatch_module()
        response = module.value_review_dispatch(types.SimpleNamespace(json=lambda: {
            'candidate_id': candidate['task_id'],
            'candidate_sha256': candidate['evidence_sha256'],
        }))

        self.assertEqual(200, response[0])
        self.assertEqual('ask_user', response[1]['result']['route'])
        items_response = module.value_review_queue(None)
        self.assertEqual(200, items_response[0])
        item = next(row for row in items_response[1]['items'] if row['task_id'] == candidate['task_id'])
        self.assertEqual('blocked', item['dispatch']['status'])
        self.assertIsNone(self.native.get_memory_candidate(candidate['task_id']))
        with self.native.read_only() as db:
            self.assertEqual(0, db.execute(
                'SELECT count(*) FROM ri_requests WHERE candidate_id=?', (candidate['task_id'],),
            ).fetchone()[0])

    def test_general_new_experience_review_cannot_bypass_pending_value_score(self):
        candidate = self.prepared_candidate()
        with self.assertRaisesRegex(ValueError, '专用价值评分双审入口'):
            self.store.create(
                candidate['task_id'], 'fixture-dsh', 'fixture-peer', 'TEST ordinary review',
                [{'ref': candidate['staged_path'], 'sha256': candidate['evidence_sha256']}],
                review_type='new_experience',
            )

        self.assertIsNone(self.native.get_memory_candidate(candidate['task_id']))
        queued = next(row for row in self.native.value_review_queue()
                      if row['task_id'] == candidate['task_id'])
        self.assertEqual(candidate['evidence_sha256'], queued['candidate_sha256'])
        with self.native.read_only() as db:
            self.assertEqual(0, db.execute(
                'SELECT count(*) FROM ri_requests WHERE candidate_id=?', (candidate['task_id'],),
            ).fetchone()[0])

    def test_value_review_failure_stays_queued_and_retry_only_runs_missing_side(self):
        self.add_value_review_nodes()
        candidate = self.prepared_candidate()
        module = self.value_dispatch_module()
        counts = {}
        first, counts = self.dispatch_prepared_value(
            module, candidate, [8, 8], fail_once=2, calls=counts,
        )
        self.assertEqual('failed', first[1]['result']['route'])
        request_id = first[1]['result']['request_id']
        waiting = next(row for row in self.native.value_review_queue()
                       if row['task_id'] == candidate['task_id'])
        self.assertEqual('failed', waiting['dispatch']['status'])
        self.assertIsNone(self.native.get_memory_candidate(candidate['task_id']))
        with self.native.read_only() as db:
            assignments = [row['id'] for row in db.execute(
                'SELECT id FROM ri_assignments WHERE request_id=? ORDER BY role', (request_id,),
            )]
        self.assertEqual(2, len(assignments))
        # Only the first source was successful; the failed assignment is the other call.
        self.assertEqual(2, len(counts))
        failed_assignment = next(aid for aid, count in counts.items() if count == 1
                                 and not any(r['assignment_id'] == aid and r['status'] == 'valid'
                                             for r in self.store.get(request_id)['receipts']))
        counts.clear()
        retried, counts = self.dispatch_prepared_value(module, candidate, [8, 8], calls=counts)
        self.assertEqual('completed', retried[1]['result']['route'])
        self.assertEqual({failed_assignment: 1}, counts)
        self.assertEqual('pending_delivery_review',
                         self.native.get_memory_candidate(candidate['task_id'])['status'])

    def test_value_review_applies_disagreement_and_below_six_rules(self):
        for index, (scores, expected_reason, expected_decision) in enumerate((
            ([2, 6], 'disagreement', 'initial_review'),
            ([5, 5], 'below_threshold', 'discard'),
        )):
            with self.subTest(scores=scores):
                self.add_value_review_nodes(str(index))
                candidate = self.prepared_candidate()
                module = self.value_dispatch_module()
                response, _ = self.dispatch_prepared_value(module, candidate, scores)
                self.assertEqual(200, response[0])
                self.assertEqual('completed', response[1]['result']['route'])
                if expected_decision == 'discard':
                    self.assertIsNone(self.native.get_memory_candidate(candidate['task_id']))
                    self.assertNotIn(candidate['task_id'], {
                        item['task_id'] for item in self.native.review_queue('delivery')
                    })
                else:
                    self.assertEqual('pending_delivery_review',
                                     self.native.get_memory_candidate(candidate['task_id'])['status'])
                with self.native.read_only() as db:
                    payload = json.loads(db.execute(
                        "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                        (candidate['task_id'],),
                    ).fetchone()[0])
                self.assertEqual(expected_reason, payload['reason'])
                self.assertEqual(expected_decision, payload['decision'])


if __name__=='__main__':unittest.main()
