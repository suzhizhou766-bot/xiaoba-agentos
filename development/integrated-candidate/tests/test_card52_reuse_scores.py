"""Card52: isolated SQLite events, formal use, delivery and relevance-first ranking."""
import concurrent.futures
import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_experience_loop as fixtures
from xiaoba_agentos.experience_loop import record_application, record_delivery
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.review_flow import reconcile_failed_reuses, reconcile_validated_reuses


class Card52ReuseTests(unittest.TestCase):
    setUp = fixtures.ExperienceLoopTests.setUp
    file = fixtures.ExperienceLoopTests.file
    candidate = fixtures.ExperienceLoopTests.candidate
    application = fixtures.ExperienceLoopTests.application
    delivery = fixtures.ExperienceLoopTests.delivery
    event_count = fixtures.ExperienceLoopTests.event_count

    def formal(self, status='published'):
        c = self.candidate()
        vault = self.root / 'vault'
        vault.mkdir(exist_ok=True)
        path = vault / ('formal-' + c['task_id'] + '.md')
        path.write_text('---\nstatus: active\nsource_task_id: ' + c['task_id'] + '\n---\nTEST formal method')
        with self.store.connect() as db:
            db.execute('UPDATE memory_candidates SET status=?,published_path=?,published_at=1 WHERE task_id=?',
                       (status, str(path), c['task_id']))
        c = self.store.get_memory_candidate(c['task_id'])
        c['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        return c

    def app(self, c, mode='unchanged', cited=True, task_id=None):
        value = self.application(c)
        task_id = task_id or self.store.submit('TEST actual formal use', 1)
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', (('TEST cites ' + c['task_id']) if cited else 'TEST no citation', task_id))
        value['real_task']['id'] = task_id
        value['candidate_sha256'] = c['source_sha256']
        value['reuse_assessment'] = {'outcome': mode, 'changed': 'TEST adjusted step' if mode != 'unchanged' else '',
                                    'added': 'TEST added boundary' if mode != 'unchanged' else '', 'discarded': ''}
        return value

    def deliver(self, app, **changes):
        return record_delivery(self.store, self.delivery(real_task_id=app['real_task']['id'], **changes))

    def events(self, kind):
        with self.store.read_only() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload_json FROM events WHERE event_type=? ORDER BY sequence', (kind,))]

    def projection(self, c):
        from xiaoba_agentos.reuse_scores import projection_tx
        with self.store.read_only() as db:
            return projection_tx(db).get(c['task_id'], {'score': 0, 'last_used_at': None})

    def test_delivery_scores_once_even_for_new_message_artifact_and_concurrency(self):
        c = self.formal(); app = self.app(c)
        record_application(self.store, app)
        self.assertEqual(0, self.event_count('memory.reuse_scored'))
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: self.deliver(app), range(3)))
        self.deliver(app, message_id='TEST-second-delivery')
        later = copy.deepcopy(app)
        later['artifact'] = dict(self.file('v2.txt', 'TEST product v2'), version='v2')
        record_application(self.store, later)
        record_delivery(self.store, self.delivery(real_task_id=app['real_task']['id'], artifact=later['artifact'], message_id='TEST-v2'))
        self.assertEqual(1, self.event_count('memory.reuse_scored'))
        self.assertEqual(1, self.projection(c)['score'])
        self.assertIsNotNone(self.projection(c)['last_used_at'])
        self.assertTrue(self.store.verify_event_chain()['ok'])

    def test_modified_and_inapplicable_zero_and_revision_event_only(self):
        for outcome in ('modified', 'inapplicable'):
            c = self.formal(); app = self.app(c, outcome)
            reviews = self.event_count('memory.reviewed')
            record_application(self.store, app); self.deliver(app, message_id='TEST-'+outcome)
            event = self.events('memory.reuse_revision_requested')[-1]
            self.assertEqual(c['source_sha256'], event['source_sha256'])
            self.assertEqual(app['reuse_assessment'], event['reuse_assessment'])
            self.assertEqual(0, self.projection(c)['score'])
            self.assertIsNotNone(self.projection(c)['last_used_at'])
            self.assertEqual(reviews, self.event_count('memory.reviewed'))
            self.assertEqual('published', self.store.get_memory_candidate(c['task_id'])['status'])
            self.assertNotIn(c['task_id'], [r['task_id'] for r in self.store.review_queue('final')])

    def test_uncited_or_missing_assessment_skips_without_blocking_delivery(self):
        for reason in ('uncited', 'missing_assessment'):
            c = self.formal(); app = self.app(c, cited=reason != 'uncited')
            if reason == 'missing_assessment': app.pop('reuse_assessment')
            record_application(self.store, app)
            delivery = self.deliver(app, message_id='TEST-'+reason)
            self.assertIn('event_id', delivery)
            self.assertEqual(0, self.projection(c)['score'])
            self.assertIsNone(self.projection(c)['last_used_at'])
        self.assertEqual(2, self.event_count('memory.reuse_scoring_skipped'))

    def test_source_drift_skips_scoring_but_delivery_evidence_drift_blocks_acceptance(self):
        for which in ('source', 'evidence', 'artifact'):
            with self.subTest(which=which):
                c = self.formal(); app = self.app(c); record_application(self.store, app)
                path = Path(c['published_path']) if which == 'source' else Path(
                    self.evidence['path'] if which == 'evidence' else self.artifact['path'])
                old = path.read_bytes(); path.write_bytes(old + b' TEST drift')
                before = self.event_count('task.delivery_recorded')
                try:
                    if which == 'source':
                        self.deliver(app, message_id='TEST-drift-'+which)
                        self.assertEqual(before + 1, self.event_count('task.delivery_recorded'))
                    else:
                        with self.assertRaisesRegex(ValueError, 'changed'):
                            self.deliver(app, message_id='TEST-drift-'+which)
                        self.assertEqual(before, self.event_count('task.delivery_recorded'))
                    self.assertEqual(0, self.projection(c)['score'])
                finally:
                    path.write_bytes(old)
        self.assertEqual(1, self.event_count('memory.reuse_scoring_skipped'))

    def test_withdrawal_and_reacceptance_never_subtract_or_double_score(self):
        c = self.formal(); app = self.app(c); record_application(self.store, app); self.deliver(app)
        self.deliver(app, message_id='TEST-withdraw', user_message='撤回验收', decision='withdraw')
        self.deliver(app, message_id='TEST-reaccept')
        self.assertEqual(1, self.projection(c)['score'])
        self.assertEqual(1, self.event_count('memory.reuse_scored'))

    def test_different_tasks_accumulate_permanently_and_last_use_advances(self):
        c = self.formal()
        for number in range(2):
            app = self.app(c); record_application(self.store, app)
            self.deliver(app, message_id='TEST-task-'+str(number))
        events = self.events('memory.reuse_scored')
        self.assertEqual(2, self.projection(c)['score'])
        self.assertEqual(events[-1]['used_at'], self.projection(c)['last_used_at'])

    def test_score_write_failure_rolls_back_partial_score_and_revision_but_not_delivery(self):
        c = self.formal(); app = self.app(c, 'modified'); record_application(self.store, app)
        original = self.store._append_event_tx
        def append(db, task_id, kind, value):
            if kind == 'memory.reuse_revision_requested': raise sqlite3.OperationalError('TEST fault')
            return original(db, task_id, kind, value)
        with patch.object(self.store, '_append_event_tx', side_effect=append):
            self.deliver(app)
        self.assertEqual(1, self.event_count('task.delivery_recorded'))
        self.assertEqual(0, self.event_count('memory.reuse_scored'))
        self.assertEqual(0, self.event_count('memory.reuse_revision_requested'))
        self.assertEqual(1, self.event_count('memory.reuse_scoring_skipped'))
        self.assertTrue(self.store.verify_event_chain()['ok'])

    def test_bad_chain_blocks_formal_delivery_before_any_write(self):
        c = self.formal(); app = self.app(c); record_application(self.store, app)
        with self.store.connect() as db: db.execute("UPDATE events SET event_hash='TEST broken' WHERE sequence=1")
        with self.assertRaisesRegex(ValueError, 'event chain'):
            self.deliver(app)
        self.assertEqual(0, self.event_count('task.delivery_recorded'))
        self.assertEqual(0, self.event_count('memory.reuse_scored'))
        self.assertEqual(0, self.event_count('memory.reuse_scoring_skipped'))

    def test_old_failure_success_and_direct_store_paths_do_not_mutate_formal(self):
        for status in ('published', 'revalidation_required', 'retired'):
            c = self.formal(status); task = self.store.submit('TEST source use', 1)
            with self.store.connect() as db: db.execute('UPDATE tasks SET result=? WHERE id=?', (c['task_id'], task))
            self.store.record_memory_reuse(task, c['task_id'], c['published_path'])
            before = self.store.get_memory_candidate(c['task_id'])
            with patch('xiaoba_agentos.review_flow.MemoryBridge') as bridge:
                self.assertEqual(([], [], []), reconcile_failed_reuses(self.cfg, self.store, task))
                self.assertEqual(([], [], []), reconcile_validated_reuses(self.cfg, self.store, task))
                bridge.return_value.retire_candidate.assert_not_called()
                bridge.return_value.set_validation_status.assert_not_called()
            self.store.decide_reuse_outcomes(task, False)
            self.store.decide_reuse_outcomes(task, True)
            self.assertEqual(before, self.store.get_memory_candidate(c['task_id']))
            self.assertEqual(0, before['reuse_count'])

    def test_no_database_structure_changes(self):
        c = self.formal()
        with self.store.read_only() as db: before = list(db.execute('SELECT sql FROM sqlite_master ORDER BY name'))
        app = self.app(c); record_application(self.store, app); self.deliver(app)
        with self.store.read_only() as db: after = list(db.execute('SELECT sql FROM sqlite_master ORDER BY name'))
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])

    def test_retrieval_ranks_relevant_pool_six_plus_one_without_reading_unmatched(self):
        candidates = [self.formal() for _ in range(9)]
        for i, c in enumerate(candidates[:7]):
            for j in range(i + 1):
                self.store.append_event(c['task_id'], 'memory.reuse_scored', {
                    'source_task_id': c['task_id'], 'real_task_id': f'TEST-ranking-{i}-{j}',
                    'score': 1, 'used_at': i + j + 1})
        self.cfg.raw['memory'].update(knowlp_enabled=True, knowlp_python='/TEST/python', knowlp_module='/TEST/module.py')
        bridge = MemoryBridge(self.cfg, self.store)
        relevant = candidates[:8]  # ninth never matched; it must never be read or included
        completed = subprocess.CompletedProcess([], 0, json.dumps({'hits': [
            {'title': c['task_id'], 'path': Path(c['published_path']).name} for c in relevant]}), '')
        with patch('xiaoba_agentos.memory.subprocess.run', return_value=completed) as run:
            result = bridge.retrieve_with_receipt('TEST relevant')
        hits = result['hits']
        self.assertEqual([c['task_id'] for c in reversed(candidates[1:7])], [h['source_task_id'] for h in hits[:6]])
        self.assertEqual(candidates[7]['task_id'], hits[6]['source_task_id'])
        self.assertEqual(7, len(hits))
        self.assertGreater(int(run.call_args.args[0][-1]), 6)
        self.assertEqual(0, self.store.get_memory_candidate(candidates[7]['task_id'])['reuse_count'])

    def test_one_bad_source_does_not_prevent_another_source_scoring(self):
        bad, good = self.formal(), self.formal()
        bad_app = self.app(bad); good_app = self.app(good, task_id=bad_app['real_task']['id'])
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', (bad['task_id']+' '+good['task_id'], bad_app['real_task']['id']))
        bad_app.pop('reuse_assessment')
        for app in (bad_app, good_app): record_application(self.store, app)
        self.deliver(good_app)
        self.assertEqual(0, self.projection(bad)['score'])
        self.assertEqual(1, self.projection(good)['score'])
        self.assertEqual(1, self.event_count('memory.reuse_scoring_skipped'))

    def test_missing_source_after_application_does_not_undo_delivery(self):
        c = self.formal(); app = self.app(c); record_application(self.store, app)
        with self.store.connect() as db:
            db.execute('DELETE FROM memory_candidates WHERE task_id=?', (c['task_id'],))
        self.deliver(app)
        self.assertEqual(1, self.event_count('task.delivery_recorded'))
        self.assertEqual(1, self.event_count('memory.reuse_scoring_skipped'))

    def test_inconsistent_assessment_skips_rather_than_inventing_a_score(self):
        c = self.formal(); app = self.app(c)
        app['reuse_assessment']['changed'] = 'TEST actually modified'
        record_application(self.store, app); self.deliver(app)
        self.assertEqual(0, self.event_count('memory.reuse_scored'))
        self.assertEqual(1, self.event_count('memory.reuse_scoring_skipped'))

    def test_stale_historical_source_cannot_register_new_application(self):
        for status in ('revalidation_required', 'retired'):
            c = self.formal(status)
            with self.assertRaises(ValueError): record_application(self.store, self.app(c))

    def test_no_delivery_does_not_score_even_after_old_success_settlement(self):
        c = self.formal(); app = self.app(c); record_application(self.store, app)
        self.store.record_memory_reuse(app['real_task']['id'], c['task_id'], c['published_path'])
        reconcile_validated_reuses(self.cfg, self.store, app['real_task']['id'])
        self.assertEqual(0, self.event_count('memory.reuse_scored'))
        self.assertIsNone(self.projection(c)['last_used_at'])

    def test_duplicate_locator_hits_cannot_take_multiple_primary_places(self):
        c = self.formal()
        self.cfg.raw['memory'].update(knowlp_enabled=True, knowlp_python='/TEST/python', knowlp_module='/TEST/module.py')
        completed = subprocess.CompletedProcess([], 0, json.dumps({'hits': [
            {'path': Path(c['published_path']).name} for _ in range(8)]}), '')
        with patch('xiaoba_agentos.memory.subprocess.run', return_value=completed):
            hits = MemoryBridge(self.cfg, self.store).retrieve_hits('TEST relevant')
        self.assertEqual(1, len(hits))


    def test_pending_registration_stays_pending_when_source_is_published_before_delivery(self):
        c = self.candidate(); app = self.application(c)
        record_application(self.store, app)
        # Isolated historical-state fixture: publication between application
        # registration and a later delivery must not reclassify the application.
        with self.store.connect() as db:
            db.execute("UPDATE memory_candidates SET status='published',published_path=staged_path,published_at=1 WHERE task_id=?", (c['task_id'],))
        self.deliver(app)
        self.assertEqual(1, self.event_count('memory.delivery_attached'))
        self.assertEqual(0, self.event_count('memory.reuse_scoring_skipped'))
        self.assertEqual(0, self.event_count('memory.reuse_scored'))

    def test_formal_only_application_has_no_new_candidate_final_binding(self):
        from xiaoba_agentos.experience_loop import final_binding, validate_trial_application
        c = self.formal(); app = self.app(c); app.pop('delivery_review_id')
        registered = record_application(self.store, app)
        with self.store.read_only() as db:
            self.assertIsNone(final_binding(db, c))
            with self.assertRaisesRegex(ValueError, 'formal reuse'):
                validate_trial_application(db, c, {'experience_application': {
                    'application_id': registered['application_id'], 'real_task_id': app['real_task']['id'],
                    'artifact_sha256': app['artifact']['sha256']}})

    def test_direct_new_candidate_attachment_rejects_formal_app_explicitly(self):
        from xiaoba_agentos.experience_loop import attach_delivery_tx
        c = self.formal(); app = self.app(c); app.pop('delivery_review_id')
        registered = record_application(self.store, app)
        with self.store.connect() as db:
            with self.assertRaisesRegex(ValueError, 'formal reuse'):
                attach_delivery_tx(self.store, db, c['task_id'], 'TEST-invalid-binding', [registered])
        self.assertEqual(0, self.event_count('memory.delivery_attached'))

    def test_mixed_pending_and_formal_history_keeps_pending_final_binding(self):
        from xiaoba_agentos.experience_loop import final_binding, attach_delivery_tx
        c = self.candidate(); pending = record_application(self.store, self.application(c))
        path = self.root / 'vault' / 'TEST-mixed.md'; path.parent.mkdir(exist_ok=True)
        path.write_text('---\nstatus: active\n---\nTEST published method')
        with self.store.connect() as db:
            db.execute("UPDATE memory_candidates SET status='published',published_path=?,published_at=1 WHERE task_id=?", (str(path), c['task_id']))
        formal = dict(c, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        app = self.app(formal); app.pop('delivery_review_id')
        registered = record_application(self.store, app)
        with self.store.connect() as db:
            self.assertIsNotNone(final_binding(db, c))
            attach_delivery_tx(self.store, db, c['task_id'], 'TEST-mixed-binding', [pending, registered])
        self.assertEqual(1, self.event_count('memory.delivery_attached'))

    def test_malformed_score_history_stops_all_sources_and_is_visible_in_doctor(self):
        from xiaoba_agentos.health import doctor
        bad, good = self.formal(), self.formal()
        a = self.app(bad); b = self.app(good, task_id=a['real_task']['id'])
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', (bad['task_id']+' '+good['task_id'], a['real_task']['id']))
        for app in (a, b): record_application(self.store, app)
        self.store.append_event(bad['task_id'], 'memory.reuse_scored', {'TEST_malformed': True})
        self.deliver(a)
        self.assertEqual(1, self.event_count('task.delivery_recorded'))
        self.assertEqual(1, self.event_count('memory.reuse_scored'))  # only the injected malformed event
        self.assertEqual({bad['task_id'], good['task_id']}, {
            e['source_task_id'] for e in self.events('memory.reuse_scoring_skipped')})
        self.cfg.raw['memory'].update(knowlp_python=sys.executable, knowlp_module=__file__)
        self.cfg.raw['integrations']['node_command'] = sys.executable
        with patch('xiaoba_agentos.health.SkillRegistry') as skills, patch('xiaoba_agentos.health.KnowledgeCatalog') as knowledge:
            skills.return_value.verify.return_value = {'ok': True}
            knowledge.return_value.verify.return_value = {'ok': True}
            result = doctor(self.cfg, self.store)
        self.assertTrue(result['event_chain']['ok'])
        self.assertFalse(result['reuse_scores']['ok'])
        self.assertTrue(any('复用分' in w and '全局' in w for w in result['warnings']))

    def test_formal_source_up_to_retrieval_limit_can_be_registered(self):
        c = self.formal(); path = Path(c['published_path'])
        data = path.read_bytes(); path.write_bytes(data + b' ' * (1_000_000 - len(data)))
        c['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIsNotNone(MemoryBridge(self.cfg, self.store)._verified_source({'path': path.name}))
        app = self.app(c); record_application(self.store, app); self.deliver(app)
        self.assertEqual(1, self.projection(c)['score'])

    def test_oversized_formal_source_reports_explicit_matching_limit(self):
        c = self.formal(); path = Path(c['published_path']); path.write_bytes(b'TEST ' + b' ' * 1_000_000)
        c['source_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIsNone(MemoryBridge(self.cfg, self.store)._verified_source({'path': path.name}))
        with self.assertRaisesRegex(ValueError, '1000000-byte limit'):
            record_application(self.store, self.app(c))

    def test_same_task_two_formal_hits_only_cited_source_scores(self):
        cited, uncited = self.formal(), self.formal()
        a = self.app(cited); b = self.app(uncited, task_id=a['real_task']['id'])
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET result=? WHERE id=?', ('TEST cites '+cited['task_id'], a['real_task']['id']))
        for source, app in ((cited, a), (uncited, b)):
            self.store.record_memory_reuse(a['real_task']['id'], source['task_id'], source['published_path'])
            record_application(self.store, app)
        self.deliver(a)
        self.assertEqual([cited['task_id']], [e['source_task_id'] for e in self.events('memory.reuse_scored')])
        self.assertEqual(1, self.projection(cited)['score'])
        self.assertIsNotNone(self.projection(cited)['last_used_at'])
        self.assertEqual(0, self.projection(uncited)['score'])
        self.assertIsNone(self.projection(uncited)['last_used_at'])
        self.assertEqual([uncited['task_id']], [e['source_task_id'] for e in self.events('memory.reuse_scoring_skipped')])
        reconcile_failed_reuses(self.cfg, self.store, a['real_task']['id'])
        reconcile_validated_reuses(self.cfg, self.store, a['real_task']['id'])
        self.assertEqual(0, self.event_count('memory.reuse_uncited'))
