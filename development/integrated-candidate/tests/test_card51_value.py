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
            snapshot={'needs_review': True, 'experience_policy': {'version': 1, 'kind': 'method'}})
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

    def test_below_six_keeps_task_answer_and_removes_candidate_body(self):
        task, path = self.value_task([5, 5])
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertFalse(path.exists())
        self.assertEqual('TEST full candidate body', self.store.get_task(task)['result'])
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
            with self.subTest(score=score):
                task, _ = self.value_task([score, 6])
                self.assertEqual('completed', self.store.get_task(task)['status'])
                self.assertIsNone(self.store.get_memory_candidate(task))
                queued = [item for item in self.store.value_review_queue() if item['task_id'] == task]
                self.assertEqual(1, len(queued))
                self.assertEqual('invalid_score_receipt', queued[0]['reason'])
                with self.store.read_only() as db:
                    self.assertEqual(1, db.execute(
                        "SELECT count(*) FROM events WHERE task_id=? AND event_type='memory.value_unscored'",
                        (task,),
                    ).fetchone()[0])
                    self.assertEqual(0, db.execute(
                        "SELECT count(*) FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                        (task,),
                    ).fetchone()[0])


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
        self.assertIsNone(self.store.get_memory_candidate(task))
        queued = [item for item in self.store.value_review_queue() if item['task_id'] == task]
        self.assertEqual(1, len(queued))
        self.assertEqual('invalid_score_receipt', queued[0]['reason'])
        self.assertEqual('awaiting_value_review', self.store.value_candidate_status(
            task, hashlib.sha256(path.read_bytes()).hexdigest()
        ))
        with self.store.read_only() as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type='memory.value_unscored'", (task,)).fetchone()[0])

    def test_host_score_does_not_hide_missing_pool_score(self):
        from xiaoba_agentos.value_scoring import native_value
        task = self.task(host=True)
        with self.store.read_only() as db, self.assertRaisesRegex(ValueError, '评分字段'):
            native_value(db, task, {'reviewer_id': 'TEST-host', 'score': 8})


class Card57ScopeBoundaryTests(unittest.TestCase):
    """保留P1评分边界反例，并按J11c待评分队列验收两类缺分任务。

    普通任务没有评分来源，审查任务缺必需评分，两者均完成用户任务并保留
    各自的待评分原因。未评分材料不能进入可批准的候选表。
    """
    setUp = scoring_fixture.Card51ScoringTests.setUp

    def _task_with_unscored_receipts(self, *, host):
        snapshot = {'needs_review': True, 'experience_policy': {'version': 1, 'kind': 'method'}}
        if host:
            snapshot['review_context'] = {'host': {'id': 'TEST-host'}}
        task = self.store.submit('TEST scope boundary task', 2, snapshot=snapshot)
        for node in ('A', 'B'):
            run = self.store.start_agent_run(task, node, {'purpose': 'review'})
            # 回执正文完全不含评分字段，也没有可被误读成评分的数字行。
            self.store.finish_agent_run(run, 'completed',
                                        '审查结论：PASS\n证据引用：task:' + task + '\nTEST body without score',
                                        None, 1)
        return task

    def test_ordinary_task_without_score_ticket_still_completes_unscored(self):
        import hashlib
        from xiaoba_agentos.memory import MemoryBridge
        task = self._task_with_unscored_receipts(host=False)
        path = MemoryBridge(self.config, self.store).stage_experience(
            task, 'TEST prompt', 'TEST answer', self.store.task_runs(task))
        self.store.complete_with_memory_candidate(
            task, 'TEST answer', str(path), hashlib.sha256(path.read_bytes()).hexdigest(),
            [], expected_attempt=0)
        self.assertEqual('completed', self.store.get_task(task)['status'])
        self.assertIsNone(self.store.get_memory_candidate(task))
        queued = [item for item in self.store.value_review_queue() if item['task_id'] == task]
        self.assertEqual(1, len(queued))
        self.assertEqual('invalid_score_receipt', queued[0]['reason'])
        with self.store.read_only() as db:
            row = db.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_unscored' "
                "ORDER BY sequence DESC LIMIT 1", (task,)).fetchone()
            self.assertEqual('invalid_score_receipt', json.loads(row[0])['reason'])
            self.assertFalse(db.execute(
                "SELECT 1 FROM events WHERE task_id=? AND event_type='memory.value_assessed'", (task,)).fetchone())

    def test_review_task_missing_score_completes_but_cannot_enter_initial_review(self):
        import hashlib
        from xiaoba_agentos.memory import MemoryBridge
        task = self._task_with_unscored_receipts(host=True)
        bridge = MemoryBridge(self.config, self.store)
        path = bridge.stage_experience(task, 'TEST prompt', 'TEST answer', self.store.task_runs(task))
        self.store.complete_with_memory_candidate(
            task, 'TEST answer', str(path), hashlib.sha256(path.read_bytes()).hexdigest(),
            [], expected_attempt=0)
        self.assertEqual('completed', self.store.get_task(task)['status'])
        self.assertIsNone(self.store.get_memory_candidate(task))
        queued = [item for item in self.store.value_review_queue() if item['task_id'] == task]
        self.assertEqual(1, len(queued))
        self.assertEqual('invalid_score_receipt', queued[0]['reason'])
        with self.store.read_only() as db:
            self.assertFalse(db.execute(
                "SELECT 1 FROM events WHERE task_id=? AND event_type='memory.value_assessed'", (task,)).fetchone())


if __name__ == '__main__':
    unittest.main()
