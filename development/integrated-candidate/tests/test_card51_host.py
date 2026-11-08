"""Both host receipt settlement and Worker value admission use the same rules."""
import hashlib
import json
import unittest
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
            return {'status':'completed','body':'审查结论：FAIL\n评分：4\nTEST low value evidence','execution_evidence':{}}
        with patch.object(_FakePoolResource,'run_task',low):
            task=self.submit('method',host=self.host())
            # With two reviewers this is the ordinary Worker path.
            result=Orchestrator(self.config,self.store).run_once()
        self.assertEqual('completed',result['status'],result)
        self.assertEqual('discarded',result['delivery_review']['status'])
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertNotIn('TEST low value evidence',result['result'])
        self.assertEqual(2,len(self.scores()))
        self.assertTrue(all(r['points']==1 for r in self.scores()))

    def test_host_low_value_fallback_and_score_failure_rollback(self):
        self.seed()
        def low(resource,task_id,step_code,instruction,materials):
            return {'status':'completed','body':'审查结论：PASS\n评分：4\nTEST accepted low value','execution_evidence':{}}
        with patch.object(_FakePoolResource,'run_task',low):
            task=self.submit('method',host=self.host())
            self.assertEqual('awaiting_host',Orchestrator(self.config,self.store).run_once()['status'])
        receipt=dict(self.receipt(task),score=4)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_score BEFORE INSERT ON events WHEN NEW.event_type='task_scoring.assessed' BEGIN SELECT RAISE(ABORT,'TEST rollback'); END")
        with self.assertRaises(Exception):complete_host(self.config,self.store,task,receipt)
        self.assertEqual('pending',self.store.get_task(task)['status'])
        with self.store.connect() as db:db.execute('DROP TRIGGER fail_score')
        result=complete_host(self.config,self.store,task,receipt)
        self.assertEqual('discard',result['value_assessment']['decision'])
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertEqual(1,self.scores()[0]['points'])

    def test_host_combined_score_preserves_full_candidate_inside_same_transaction(self):
        self.seed()
        def low(resource,task_id,step_code,instruction,materials):
            return {'status':'completed','body':'审查结论：PASS\n评分：4\nTEST actual pool conclusion','execution_evidence':{}}
        with patch.object(_FakePoolResource,'run_task',low):
            task=self.submit('method',host=self.host())
            self.assertEqual('awaiting_host',Orchestrator(self.config,self.store).run_once()['status'])
        result=complete_host(self.config,self.store,task,dict(self.receipt(task),score=8))
        self.assertEqual('initial_review',result['value_assessment']['decision'])
        candidate=self.store.get_memory_candidate(task)
        from pathlib import Path
        body=Path(candidate['staged_path']).read_text()
        self.assertIn('TEST actual pool conclusion',body)
        self.assertIn('TEST 宿主实际结论',body)
        self.assertNotIn('丢弃候选',body)
