"""Production-path counterexamples, isolated stores and fake adapters only."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_pool_dispatch import _config, _seed, _page_source, _patch_pool_cli
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.cli import _submit_task, main


class Card37Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root, [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.enterContext(patch('xiaoba_agentos.orchestrator.release_binding_status', return_value={'ok': True}))
        self.enterContext(_patch_pool_cli())

    def seed(self, count=1):
        _seed(self.store, [_page_source(str(i), 'cli:fake'+str(i), str(i), agent_type='other') for i in range(count)])

    def submit(self, **kw):
        return _submit_task(self.config, self.store, 'TEST 审查', experience_kind='audit', **kw)

    def host(self):
        return {'id': 'host', 'entry': 'cli:host', 'model': 'test'}

    def receipt(self, task_id):
        task = self.store.get_task(task_id)
        return {'receipt_id': 'host-'+task_id, 'task_id': task_id,
                'snapshot_sha256': task['execution_snapshot_hash'], 'reviewer_id': 'host',
                'verdict': 'PASS', 'findings': 'TEST 宿主实际结论',
                'evidence_refs': ['task:'+task_id], 'review_type': 'new_experience',
                'score': 8, 'applicability': 'TEST fixtures only', 'duplicate_of': None,
                'started_at': time.time()-1, 'finished_at': time.time()}

    def test_host_second_vote_requires_validated_receipt_and_cli_settles(self):
        self.seed()
        task = self.submit(host=self.host())
        result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual('awaiting_host', result['status'])
        self.assertNotEqual('completed', self.store.get_task(task)['status'])
        from xiaoba_agentos.host_completion import complete_host
        bad = self.receipt(task); bad['evidence_refs'] = ['unrelated']
        with self.assertRaises(ValueError): complete_host(self.config, self.store, task, bad)
        path = self.root/'receipt.json'; path.write_text(json.dumps(self.receipt(task)))
        self.assertEqual(0, main(['--config', str(self.config.path), 'task-host-complete', task, '--receipt', str(path)]))
        settled = self.store.get_task(task)
        self.assertEqual('completed', settled['status'])
        self.assertIn('宿主实际结论', settled['result'])
        self.assertNotIn('无独立审查', settled['result'])
        self.assertEqual(1, len(self.store.latest_agent_runs(task)))
        complete_host(self.config, self.store, task, json.loads(path.read_text()))
        with self.store.read_only() as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM events WHERE task_id=? AND event_type='task.completed'", (task,)).fetchone()[0])

    def test_real_submission_uses_work_runs_and_author_records_not_idle_claims(self):
        self.seed(3)
        work = self.submit(needs_review=False)
        self.store.record_task_participant(work, 'host', 'author')
        run = self.store.start_agent_run(work, '0')
        self.store.finish_agent_run(run, 'completed', 'TEST contribution', None, 1)
        review = self.submit(work_task_id=work, host=self.host())
        from xiaoba_agentos.pool_dispatch import worker_roster
        roster = worker_roster(self.config, self.store, self.store.get_task(review))
        self.assertEqual({'1', '2'}, {a.name for a in roster['agents']})
        self.assertFalse(roster.get('host'))
        # Remove another reviewer: an author-host cannot become the second vote.
        _seed(self.store, [_page_source('2', 'cli:fake2', '2', tested=False)])
        self.assertNotEqual('pool', worker_roster(self.config, self.store, self.store.get_task(review))['mode'])

    def test_no_review_task_completes_from_cli_with_host_content(self):
        task = self.submit(needs_review=False, host=self.host())
        body = self.root/'body.json'
        body.write_text(json.dumps({'task_id': task, 'snapshot_sha256': self.store.get_task(task)['execution_snapshot_hash'],
                                    'reviewer_id': 'host', 'result': 'TEST host work result'}))
        self.assertEqual(0, main(['--config', str(self.config.path), 'task-host-complete', task, '--receipt', str(body)]))
        self.assertEqual('TEST host work result', self.store.get_task(task)['result'])

    def test_waiting_head_does_not_block_runnable_tail_or_consume_attempts(self):
        self.seed(2)
        head = self.store.submit('TEST host only', 2, priority=9, snapshot={'needs_review': False})
        blocked = self.store.submit('TEST unavailable', 2, priority=8,
                                    snapshot={'executor_ids': ['0', '1']})
        tail = self.store.submit('TEST review', 2)
        for _ in range(3):
            Orchestrator(self.config, self.store).run_once()
        self.assertEqual('completed', self.store.get_task(tail)['status'])
        for task in (head, blocked):
            value = self.store.get_task(task)
            self.assertEqual(0, value['attempts'])
            self.assertTrue(value['last_error'])

    def test_host_worker_refused_by_cli_and_fallback_is_explicit(self):
        from xiaoba_agentos.host_completion import complete_host
        work = self.submit(needs_review=False, host=self.host())
        review = self.submit(work_task_id=work, host=self.host())
        with self.assertRaisesRegex(ValueError, '干过活'):
            complete_host(self.config, self.store, review, self.receipt(review), user_chose_host=True)
        clean = self.submit(host=self.host())
        with self.assertRaises(ValueError):
            complete_host(self.config, self.store, clean, self.receipt(clean))
        result = complete_host(self.config, self.store, clean, self.receipt(clean), user_chose_host=True)
        self.assertEqual('无独立审查', result['label'])
        self.assertIn('宿主实际结论', self.store.get_task(clean)['result'])

    def test_waiting_second_vote_polls_leave_attempts_and_runs_unchanged(self):
        self.seed()
        task = self.submit(host=self.host())
        Orchestrator(self.config, self.store).run_once()
        for _ in range(4):
            self.assertEqual('awaiting_host', Orchestrator(self.config, self.store).run_once()['status'])
        self.assertEqual(1, self.store.get_task(task)['attempts'])
        self.assertEqual(1, self.store.agent_invocation_count(task))

    def test_actual_submit_cli_records_identity_and_work_link(self):
        work = self.submit(needs_review=False)
        self.store.record_task_participant(work, '0', 'executor')
        self.seed(3)
        self.assertEqual(0, main(['--config', str(self.config.path), 'submit', 'TEST native CLI review',
                                 '--memory-kind', 'audit', '--work-task', work,
                                 '--host-id', 'host', '--host-entry', 'cli:host', '--host-model', 'test']))
        task = next(v for v in self.store.list_tasks() if v['prompt']=='TEST native CLI review')
        from xiaoba_agentos.pool_dispatch import worker_roster
        self.assertEqual({'1','2'}, {a.name for a in worker_roster(self.config, self.store, task)['agents']})

    def test_legacy_waiting_task_can_bind_host_then_complete_from_cli(self):
        task = self.store.submit('TEST existing host task', 2, snapshot={'needs_review': False})
        self.assertEqual(0, main(['--config', str(self.config.path), 'task-host-bind', task,
                                 '--host-id', 'host', '--host-entry', 'cli:host', '--host-model', 'test']))
        from xiaoba_agentos.host_completion import complete_host
        receipt = {'task_id': task, 'snapshot_sha256': self.store.get_task(task)['execution_snapshot_hash'],
                   'reviewer_id': 'host', 'result': 'TEST existing task actual result'}
        self.assertEqual('completed', complete_host(self.config, self.store, task, receipt)['status'])

    def test_release_evidence_contains_only_gate_fields(self):
        import importlib.util
        import sys
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('build_release', root/'scripts/build_release.py')
        module = importlib.util.module_from_spec(spec);sys.modules[spec.name] = module;spec.loader.exec_module(module)
        (self.root/'config').mkdir()
        (self.root/'config/agentos.json').write_text(json.dumps({'worker': {'minimum_successful_agents': 2}, 'agents': []}))
        (self.root/'SOURCE_MANIFEST.json').write_text('{}')
        manifest = __import__('hashlib').sha256(b'{}').hexdigest()
        payload = {
            'task': {'id': 'test', 'status': 'completed', 'execution_mode': 'production',
                     'result': '本回答：小八 AgentOS 双 Agent 裁判\n\nTEST result',
                     'provenance_json': json.dumps({'kind': 'agentos_multi_adjudicated', 'minimum_successful_agents': 2}),
                     'execution_snapshot_json': json.dumps({'source_manifest_sha256': manifest})},
            'runs': [{'agent_name': n, 'status': 'completed', 'output': 'FINAL_ACCEPTANCE: PASS\nTEST'} for n in ('a', 'b')],
            'memory_candidate': {'status': 'pending_delivery_review'},
            'pool_nodes': [{'node_id': n, 'enabled': True, 'test_passed': True, 'independence': [n, 'm'],
                            '_source': {'raw': 'never ship'}, 'unexpected': 'also private'} for n in ('a', 'b')]}
        result = module._validate_acceptance_payload(payload, self.root)
        self.assertNotIn('_source', json.dumps(result))
        self.assertNotIn('unexpected', json.dumps(result))
