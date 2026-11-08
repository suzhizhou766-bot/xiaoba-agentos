"""卡51反例：仅用临时库和明确 TEST 节点，不调用模型。"""
from datetime import datetime, timedelta
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from xiaoba_agentos.store import EventStore
from xiaoba_agentos.pool_dispatch import load_nodes, select_reviewers
from test_pool_dispatch import _source, _seed, _config, _patch_pool_cli
from xiaoba_agentos.orchestrator import Orchestrator


class Card51ScoringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root, [])
        self.store = EventStore(self.config.database_path); self.store.initialize()
        _seed(self.store, [_source('A', 'cli:codex', 'TEST a', 'both'),
                           _source('B', 'cli:claude', 'TEST b'),
                           _source('C', 'cli:qoder', 'TEST c')])

    def run_node(self, task, node='A', role='review', status='completed'):
        run = self.store.start_agent_run(task, node, {'purpose': role})
        self.store.finish_agent_run(run, status, '审查结论：PASS\nTEST accepted receipt' if status == 'completed' else None,
                                    None, 1)
        return run

    def rows(self):
        with self.store.read_only() as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE name='ri_task_assessments'").fetchone()
            return [json.loads(r[0]) for r in db.execute('SELECT data FROM ri_task_assessments')] if table else []

    def settle(self, node='A', role='review', status='completed'):
        task = self.store.submit('TEST real pool task', 2)
        self.run_node(task, node, role, status)
        self.store.complete_manually(task, 'TEST settled task')
        return task

    def test_settlement_automatically_scores_once_without_user_delivery(self):
        task = self.settle()
        self.assertEqual([(task, 'A', 'review', 1)], [(r['task_id'], r['node_id'], r['role'], r['points']) for r in self.rows()])
        self.store.append_event(task, 'task.completed', {'TEST': 'replay'})
        self.assertEqual(1, len(self.rows()))
        self.assertTrue(self.store.verify_event_chain()['ok'])

    def test_roles_and_configured_model_frozen_at_dispatch_not_current_pool(self):
        task = self.store.submit('TEST model changes', 2)
        self.run_node(task, role='collaboration')
        self.run_node(task)
        with self.store.connect() as db:
            src = json.loads(db.execute("SELECT data FROM ri_sources WHERE id='A'").fetchone()[0])
            src['node']['model'] = 'TEST changed after work'
            db.execute("UPDATE ri_sources SET data=? WHERE id='A'", (json.dumps(src),))
        self.store.complete_manually(task, 'TEST settled')
        self.assertEqual({'review', 'collaborate'}, {r['role'] for r in self.rows()})
        self.assertEqual({'TEST a'}, {r['model'] for r in self.rows()})

    def test_failure_rejected_receipt_abandonment_zero_and_probe_excluded(self):
        self.settle(status='permanent_error')
        task = self.store.submit('TEST rejected body', 2)
        run = self.store.start_agent_run(task, 'B', {'purpose': 'review'})
        self.store.finish_agent_run(run, 'completed', 'TEST missing review verdict', None, 1)
        self.store.complete_manually(task, 'TEST settled with another reviewer')
        task = self.store.submit('TEST abandoned work', 2)
        self.run_node(task, role='collaboration'); self.store.cancel_task(task)
        before = len(self.rows())
        task = self.store.submit('TEST health', 2)
        self.run_node(task, role='health_check'); self.store.complete_manually(task, 'TEST probe')
        self.store.complete_manually(self.store.submit('TEST host alone', 2), 'TEST host result')
        self.assertEqual(before, len(self.rows()))
        self.assertEqual([0, 0, 0], [r['points'] for r in self.rows()])
        # An explicit qualification task is excluded even with review purpose.
        task=self.store.submit('TEST node qualification',2,snapshot={'test_run':True})
        self.run_node(task);self.store.complete_manually(task,'TEST qualification only')
        self.assertEqual(before,len(self.rows()))

    def test_chain_invalid_precondition_does_not_block_terminal_and_records_skipped(self):
        task = self.store.submit('TEST chain invalid', 2); self.run_node(task)
        with self.store.connect() as db:
            db.execute("UPDATE events SET prev_hash='0'*64 WHERE sequence=1")
        self.assertFalse(self.store.verify_event_chain()['ok'])
        # Complete should succeed, not raise or rollback
        self.store.complete_manually(task, 'TEST result with invalid chain')
        self.assertEqual('completed', self.store.get_task(task)['status'])
        self.assertEqual([], self.rows())
        with self.store.read_only() as db:
            skipped = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM events WHERE event_type='task_scoring.skipped' ORDER BY sequence")]
        self.assertEqual(1, len(skipped))
        self.assertEqual({'task_id': task, 'node_id': 'A', 'role': 'review', 'reason': 'chain_invalid'}, skipped[0])

        # Cancel should also succeed with invalid chain
        task2 = self.store.submit('TEST cancel with invalid chain', 2); self.run_node(task2)
        self.store.cancel_task(task2)
        self.assertEqual('failed', self.store.get_task(task2)['status'])
        with self.store.read_only() as db:
            skipped2 = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM events WHERE event_type='task_scoring.skipped' ORDER BY sequence")]
        self.assertEqual(2, len(skipped2))
        self.assertEqual({'task_id': task2, 'node_id': 'A', 'role': 'review', 'reason': 'chain_invalid'}, skipped2[1])

    def test_shanghai_natural_week_uses_settlement_time(self):
        stamp = datetime(2026, 11, 9, 0, 1, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        with patch('time.time', return_value=stamp): self.settle()
        self.assertEqual('2026-11-09', self.rows()[0]['week_start'])

    def test_worker_path_also_automatically_scores_both_pool_nodes(self):
        task = self.store.submit('TEST review task', 2, snapshot={'needs_review': True})
        with patch('xiaoba_agentos.orchestrator.release_binding_status', return_value={'ok': True}), _patch_pool_cli():
            result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual('completed', result['status'], result)
        self.assertEqual(2, len(self.rows()))
        self.assertEqual({1}, {r['points'] for r in self.rows()})

    def test_worker_scores_model_from_dispatched_roster_not_concurrent_pool_edit(self):
        task=self.store.submit('TEST frozen dispatch model',2,snapshot={'needs_review':True})
        start=self.store.start_agent_run
        def edit_then_start(task_id,node,metadata=None):
            if node=='A':
                with self.store.connect() as db:
                    source=json.loads(db.execute("SELECT data FROM ri_sources WHERE id='A'").fetchone()[0])
                    source['node']['model']='TEST edited after roster was frozen'
                    db.execute("UPDATE ri_sources SET data=? WHERE id='A'",(json.dumps(source),))
            return start(task_id,node,metadata)
        with patch.object(self.store,'start_agent_run',side_effect=edit_then_start), \
             patch('xiaoba_agentos.orchestrator.release_binding_status',return_value={'ok':True}),_patch_pool_cli():
            self.assertEqual('completed',Orchestrator(self.config,self.store).run_once()['status'])
        self.assertEqual({'TEST a'},{v['model'] for v in self.rows() if v['node_id']=='A'})

    def test_event_without_actual_terminal_settlement_does_not_score(self):
        task=self.store.submit('TEST pending task',2);self.run_node(task)
        self.store.append_event(task,'task.completed',{'TEST':'not a settlement'})
        self.assertEqual([],self.rows())

    def test_legacy_manual_physical_key_does_not_create_second_task_sample(self):
        from xiaoba_agentos.task_scores import ensure_table,canonical,week_of
        task=self.store.submit('TEST already manually assessed',2);self.run_node(task)
        day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        value={'task_id':task,'node_id':'A','role':'review','model':'TEST a','source_ref':'agent-entry:A',
            'week_start':week_of(day)[0],'date':day,'task_type':'review','points':1,'outcome':'good','receipt_digests':[]}
        with self.store.connect() as db:
            ensure_table(db)
            db.execute('INSERT INTO ri_task_assessments VALUES (?,?,?,?,?)',('TEST-old-key',canonical(value),1,'TEST-old-assessment',0))
            self.store._append_event_tx(db,task,'task_scoring.assessed',{'key':'TEST-old-key','revision':1,
                'assessment_id':'TEST-old-assessment','current':value,'previous':None,'delta':1})
        self.store.complete_manually(task,'TEST settled after legacy manual assessment')
        self.assertEqual(1,len(self.rows()))

    def test_changed_receipt_cannot_earn_points_before_settlement(self):
        task=self.store.submit('TEST receipt changed',2);run=self.run_node(task)
        with self.store.connect() as db:
            db.execute('UPDATE agent_runs SET output=? WHERE id=?',('审查结论：PASS\nTEST forged result',run))
        self.store.complete_manually(task,'TEST result')
        self.assertEqual('completed',self.store.get_task(task)['status'])
        self.assertEqual([],self.rows())
        with self.store.read_only() as db:
            skipped = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM events WHERE event_type='task_scoring.skipped' ORDER BY sequence")]
        self.assertEqual(1, len(skipped))
        self.assertEqual({'task_id': task, 'node_id': 'A', 'role': 'review', 'reason': 'receipt_mismatch'}, skipped[0])

    def test_single_dirty_historical_row_does_not_block_settlement_or_selection(self):
        task_a = self.settle('A')
        self.assertEqual(1, len(self.rows()))
        # Tamper with Task A's run evidence in historical record
        with self.store.connect() as db:
            run_id = db.execute("SELECT id FROM agent_runs WHERE task_id=?", (task_a,)).fetchone()[0]
            db.execute("UPDATE agent_runs SET output='TEST forged after settlement' WHERE id=?", (run_id,))
        # Settlement of another task (Task B) must not fail or raise
        task_b = self.settle('B')
        self.assertEqual('completed', self.store.get_task(task_b)['status'])
        # Selection works: load_nodes does not raise, and Node B has score
        nodes = load_nodes(self.store)
        chosen = select_reviewers(nodes)['reviewers']
        self.assertEqual(['B', 'C'], [n['node_id'] for n in chosen])

    def test_ledger_inconsistent_fallback_and_doctor_warning(self):
        from xiaoba_agentos.task_scores import ensure_table, canonical
        with self.store.connect() as db:
            ensure_table(db)
            db.execute("INSERT INTO ri_task_assessments VALUES ('unaudited-key', ?, 1, 'assessment:unaudited', 0)",
                       (canonical({'task_id': 'fake', 'node_id': 'A', 'role': 'review', 'points': 1}),))
        task = self.store.submit('TEST ledger inconsistent task', 2); self.run_node(task)
        self.store.complete_manually(task, 'TEST complete with inconsistent ledger')
        self.assertEqual('completed', self.store.get_task(task)['status'])
        with self.store.read_only() as db:
            skipped = [json.loads(r[0]) for r in db.execute("SELECT payload_json FROM events WHERE event_type='task_scoring.skipped' ORDER BY sequence")]
        self.assertTrue(any(s.get('reason') == 'ledger_inconsistent' for s in skipped))
        # Selection does not fail, falls back to sample insufficient rules
        nodes = load_nodes(self.store)
        decision = select_reviewers(nodes)
        self.assertEqual('run', decision['route'])
        # Doctor shows warning
        from xiaoba_agentos.health import doctor
        doc = doctor(self.config, self.store)
        self.assertTrue(any('干活分' in w or '账本' in w for w in doc.get('warnings', [])))

    def test_first_seat_winner_second_seat_under_three_rotates_and_independence_wins(self):
        for node in ('A', 'A', 'A', 'A', 'B', 'B', 'B'): self.settle(node)
        nodes = load_nodes(self.store)
        chosen = select_reviewers(nodes)['reviewers']
        self.assertEqual(['A', 'C'], [n['node_id'] for n in chosen])
        self.assertEqual(4, chosen[0]['weekly_score']['tasks'])
        # C now has the same identity as A: reserve must never count it twice.
        for n in nodes:
            if n['node_id'] == 'C': n['independence'] = nodes[0]['independence']
        self.assertEqual(['A', 'B'], [n['node_id'] for n in select_reviewers(nodes)['reviewers']])

    def test_last_four_natural_weeks_not_lifetime_and_other_role_cannot_supply_samples(self):
        old = datetime.now(ZoneInfo('Asia/Shanghai')) - timedelta(weeks=4)
        with patch('time.time', return_value=old.timestamp()):
            for _ in range(3): self.settle('C')
        for _ in range(3): self.settle('A', 'collaboration')
        nodes = load_nodes(self.store)
        by_id = {n['node_id']: n for n in nodes}
        self.assertEqual(0, by_id['C']['weekly_score']['tasks'])
        self.assertEqual(0, by_id['C']['recent_role_samples'])
        self.assertEqual(0, by_id['A']['recent_role_samples'])


if __name__ == '__main__': unittest.main()
