"""价值分不是可靠性分：门槛必须影响候选准入。"""
import json
import unittest
from unittest.mock import patch

import test_card51_scoring as scoring_fixture
from test_pool_dispatch import _patch_pool_cli
from xiaoba_agentos.orchestrator import Orchestrator


class Card51ValueTests(unittest.TestCase):
    setUp = scoring_fixture.Card51ScoringTests.setUp
    rows = scoring_fixture.Card51ScoringTests.rows
    def value_task(self, scores, duplicate=False):
        task = self.store.submit('TEST new reusable experience', 2,
            snapshot={'needs_review': True, 'review_context': {'host': {'id': 'TEST-host'}}, 'experience_policy': {'version': 1, 'kind': 'method'}})
        for node, score in zip(('A', 'B'), scores):
            run = self.store.start_agent_run(task, node, {'purpose': 'review'})
            text = '审查结论：FAIL\n评分：'+str(score)+'\nTEST reviewed evidence'
            if duplicate and node == 'B': text += '\n重复经验：TEST-existing'
            self.store.finish_agent_run(run, 'completed', text, None, 1)
        # Exercise exactly the Worker staging/settlement path with genuine scores.
        from xiaoba_agentos.memory import MemoryBridge
        import hashlib
        path = MemoryBridge(self.config, self.store).stage_experience(task, 'TEST prompt', 'TEST full candidate body', self.store.task_runs(task))
        self.store.complete_with_memory_candidate(task, 'TEST full candidate body', str(path), hashlib.sha256(path.read_bytes()).hexdigest(), [], expected_attempt=0)
        return task, path

    def test_below_six_is_not_in_user_initial_review_and_only_summary_is_staged(self):
        task, path = self.value_task([5, 5])
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertNotIn('TEST full candidate body', path.read_text())
        self.assertIn('5', self.store.get_task(task)['result'])
        self.assertEqual({1}, {r['points'] for r in self.rows()})

    def test_average_six_inclusive_passes_even_with_fail_verdict(self):
        task, _ = self.value_task([6, 6])
        self.assertEqual('pending_delivery_review', self.store.get_memory_candidate(task)['status'])

    def test_spread_four_overrides_low_average_and_preserves_disagreement(self):
        task, _ = self.value_task([1, 5])
        self.assertEqual('pending_delivery_review', self.store.get_memory_candidate(task)['status'])
        with self.store.read_only() as db:
            event = json.loads(db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'", (task,)).fetchone()[0])
        self.assertEqual('disagreement', event['reason'])
        self.assertEqual(4, event['spread'])

    def test_duplicate_overrides_low_average(self):
        task, _ = self.value_task([1, 1], duplicate=True)
        self.assertEqual('pending_delivery_review', self.store.get_memory_candidate(task)['status'])

    def test_invalid_or_missing_score_never_silently_passes_new_pool_experience(self):
        for score in (-1, 11, float('nan'), 'invalid'):
            with self.subTest(score=score), self.assertRaises(ValueError): self.value_task([score, 6])


class Card56ReceiptFieldTests(unittest.TestCase):
    def test_empty_fields_do_not_consume_the_next_line(self):
        from xiaoba_agentos.value_scoring import from_body, decide
        value = from_body("TEST", "评分: 5\n重复经验: \n证据引用: TEST evidence\n---\nTEST body")
        self.assertIsNone(value["duplicate_of"])
        blank_score = from_body("TEST", "评分: \n证据引用: 8\n重复经验: ")
        self.assertIsNone(blank_score["score"])
        with self.assertRaisesRegex(ValueError, "0–10"):
            decide([blank_score])

    def test_matching_numeric_and_duplicate_aliases_are_one_value(self):
        from xiaoba_agentos.value_scoring import from_body
        for header, body, expected in (("", "无", None), ("none", "null", None),
                                       ("TEST-old", "TEST-old", "TEST-old")):
            with self.subTest(header=header, body=body):
                value = from_body("TEST", "评分: 8\n重复经验: " + header
                    + "\n证据引用: TEST evidence\n---\nscore: 8.0\nduplicate_of: " + body)
                self.assertEqual(8, value["score"])
                self.assertEqual(expected, value["duplicate_of"])

    def test_conflicting_fields_are_explicitly_rejected(self):
        from xiaoba_agentos.value_scoring import from_body
        for suffix, field in (("score: 7", "评分"), ("duplicate_of: TEST-other", "重复经验")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field + ".*自相矛盾"):
                from_body("TEST", "评分: 8\n重复经验: TEST-old\n---\n" + suffix)




class Card57ValueSourceTests(unittest.TestCase):
    setUp = scoring_fixture.Card51ScoringTests.setUp

    def task(self, host=False):
        snapshot = {'needs_review': True, 'experience_policy': {'version': 1, 'kind': 'method'}}
        if host:
            snapshot['review_context'] = {'host': {'id': 'TEST-host'}}
        task = self.store.submit('TEST ordinary multi task', 2, snapshot=snapshot)
        for node in ('A', 'B'):
            run = self.store.start_agent_run(task, node, {'purpose': 'review'})
            self.store.finish_agent_run(run, 'completed', 'TEST ordinary answer', None, 1)
        return task

    def test_plain_multi_completes_unscored_once(self):
        import hashlib
        from xiaoba_agentos.memory import MemoryBridge
        task = self.task()
        path = MemoryBridge(self.config, self.store).stage_experience(task, 'TEST prompt', 'TEST answer', self.store.task_runs(task))
        self.store.complete_with_memory_candidate(task, 'TEST answer', str(path), hashlib.sha256(path.read_bytes()).hexdigest(), [], expected_attempt=0)
        self.assertEqual('completed', self.store.get_task(task)['status'])
        self.assertEqual('pending_delivery_review', self.store.get_memory_candidate(task)['status'])
        with self.store.read_only() as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type='memory.value_unscored'", (task,)).fetchone()[0])

    def test_host_score_does_not_hide_missing_pool_score(self):
        from xiaoba_agentos.value_scoring import native_value
        task = self.task(host=True)
        with self.store.read_only() as db, self.assertRaisesRegex(ValueError, '评分字段'):
            native_value(db, task, {'reviewer_id': 'TEST-host', 'score': 8})


if __name__ == '__main__':
    unittest.main()
