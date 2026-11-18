"""Both host receipt settlement and Worker value admission use the same rules."""
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_card37 as fixture
from xiaoba_agentos.host_completion import complete_host
from xiaoba_agentos.orchestrator import Orchestrator
from test_pool_dispatch import _FakePoolResource


class Card51HostTests(unittest.TestCase):
    setUp=fixture.Card37Tests.setUp
    seed=fixture.Card37Tests.seed
    submit=fixture.Card37Tests.submit
    host=fixture.Card37Tests.host
    receipt=fixture.Card37Tests.receipt

    def scores(self):
        with self.store.read_only() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM ri_task_assessments')]

    def test_host_second_vote_scores_pool_once_and_never_scores_host(self):
        self.seed();task=self.submit(host=self.host())
        self.assertEqual('awaiting_host',Orchestrator(self.config,self.store).run_once()['status'])
        receipt=self.receipt(task)
        complete_host(self.config,self.store,task,receipt)
        complete_host(self.config,self.store,task,receipt)
        self.assertEqual([('0',1)],[(r['node_id'],r['points']) for r in self.scores()])

    def test_host_and_worker_discard_low_value_but_still_score_accepted_work(self):
        self.seed(2)
        def low(resource,task_id,step_code,instruction,materials):
            return {'status':'completed','body':'审查结论：FAIL\n评分：4\n审查类型：new_experience\n适用条件：TEST only\n证据引用：task:'+task_id+'\nTEST low value evidence','execution_evidence':{}}
        with patch.object(_FakePoolResource,'run_task',low):
            task=self.submit('method',host=self.host())
            # With two reviewers this is the ordinary Worker path.
            result=Orchestrator(self.config,self.store).run_once()
        self.assertEqual('completed',result['status'],result)
        self.assertEqual('discarded',result['delivery_review']['status'])
        self.assertIsNone(self.store.get_memory_candidate(task))
        settled = self.store.get_task(task)
        self.assertIn('TEST low value evidence', settled['result'])
        self.assertEqual(result['result'], settled['result'])
        outbox = self.config.runtime_dir / 'memory-outbox'
        self.assertFalse((outbox / f'{task}.md').exists())
        self.assertFalse((outbox / f'{task}.md.status.json').exists())
        self.assertEqual('value_discarded', json.loads(settled['provenance_json'])['memory_disposition'])
        self.assertEqual(2,len(self.scores()))
        self.assertTrue(all(r['points']==1 for r in self.scores()))

    def test_legacy_host_value_queue_failure_rolls_back_and_retry_stays_queued(self):
        self.seed()
        def low(resource,task_id,step_code,instruction,materials):
            return {'status':'completed','body':'审查结论：PASS\nTEST accepted low value','execution_evidence':{}}
        with patch.object(_FakePoolResource,'run_task',low):
            # An already frozen v1 task retains its original receipt contract.
            task=self.store.submit('TEST legacy value review', 2, snapshot={
                'needs_review': True, 'review_context': {'host': self.host()},
                'experience_policy': {'version': 1, 'kind': 'method'},
            })
            self.assertEqual('awaiting_host',Orchestrator(self.config,self.store).run_once()['status'])
        receipt=dict(self.receipt(task),score=4)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_score BEFORE INSERT ON events WHEN NEW.event_type='memory.value_unscored' BEGIN SELECT RAISE(ABORT,'TEST rollback'); END")
        with self.assertRaises(Exception): complete_host(self.config,self.store,task,receipt)
        self.assertEqual('pending',self.store.get_task(task)['status'])
        with self.store.connect() as db:db.execute('DROP TRIGGER fail_score')
        result=complete_host(self.config,self.store,task,receipt)
        self.assertIsNone(result['value_assessment'])
        self.assertEqual('completed', self.store.get_task(task)['status'])
        self.assertIsNone(self.store.get_memory_candidate(task))
        queued=next(item for item in self.store.value_review_queue() if item['task_id']==task)
        self.assertEqual('awaiting_value_review', self.store.value_candidate_status(task, queued['candidate_sha256']))
        self.assertEqual(1,self.scores()[0]['points'])

    def test_host_completion_preserves_candidate_in_value_queue_until_pool_review(self):
        self.seed()
        def low(resource,task_id,step_code,instruction,materials):
            return {'status':'completed','body':'审查结论：PASS\n评分：4\n审查类型：new_experience\n适用条件：TEST only\n证据引用：task:'+task_id+'\nTEST actual pool conclusion','execution_evidence':{}}
        with patch.object(_FakePoolResource,'run_task',low):
            task=self.submit('method',host=self.host())
            self.assertEqual('awaiting_host',Orchestrator(self.config,self.store).run_once()['status'])
        result=complete_host(self.config,self.store,task,dict(self.receipt(task),score=8))
        self.assertEqual('pool_plus_host', result['value_assessment']['review_mode'])
        self.assertEqual('pending_delivery_review', result['candidate_status'])
        candidate = self.store.get_memory_candidate(task)
        self.assertEqual('pending_delivery_review', candidate['status'])
        self.assertEqual([], [item for item in self.store.value_review_queue() if item['task_id']==task])
        body=Path(candidate['staged_path']).read_text()
        self.assertIn('TEST actual pool conclusion',body)
        self.assertIn('TEST 宿主实际结论',body)
        with self.store.read_only() as db:
            assessment = json.loads(db.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (task,),
            ).fetchone()[0])
        self.assertEqual(3, assessment['version'])
        self.assertEqual('pool_plus_host', assessment['review_mode'])
