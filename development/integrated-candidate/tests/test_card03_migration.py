"""C03-04 extension lock, rollback and legacy-selection regressions."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from fixtures_review import _admit_authored_candidate
from xiaoba_agentos.experience_workflow import migrate_legacy_validation_records
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.validation_migration import apply_validation_schema_migration
from test_schema_fingerprint import compute_fingerprint, read_schema_objects


class Card03MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = EventStore(self.root / 'fixture.sqlite')
        self.store.initialize()

    def candidate(self, *, kind='method', decision='approve'):
        if kind == 'legacy':
            task = self.store.submit('TEST legacy migration fixture', 2)
            self.store.complete(task, 'TEST completed legacy task', expected_attempt=0)
            path = self.root / (task + '.md')
            path.write_text('TEST legacy candidate')
            import hashlib
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.store.stage_memory_candidate(task, str(path), sha, [])
            candidate = self.store.get_memory_candidate(task)
        else:
            candidate = self.store.create_authored_candidate('TEST migration ' + kind,
                'TEST migration selection', self.root / 'memory-outbox', author='TEST', experience_kind=kind)
            candidate = _admit_authored_candidate(self.store, candidate)
        if decision is not None:
            self.store.review_memory(candidate['task_id'], 'delivery', decision,
                'TEST user decision', candidate['evidence_sha256'])
        return self.store.get_memory_candidate(candidate['task_id'])

    def test_concurrent_process_installs_share_sqlite_lock_and_one_install(self):
        import os
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        env['PYTHONPATH'] = os.pathsep.join([
            str(Path(__file__).resolve().parents[1] / 'src'),
            str(Path(__file__).resolve().parent),
        ])
        source = ('import json,sys; from xiaoba_agentos.validation_migration import apply_validation_schema_migration; '
                  'print(json.dumps(apply_validation_schema_migration(sys.argv[1])))')
        processes = [subprocess.Popen([sys.executable, '-B', '-c', source, str(self.store.path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(4)]
        results = []
        try:
            for process in processes:
                output, errors = process.communicate(timeout=10)
                self.assertEqual(0, process.returncode, errors)
                results.append(json.loads(output))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
        self.assertEqual(1, sum(not result['already_installed'] for result in results))
        self.assertTrue(self.store.is_validation_extension_installed())

    def test_authoritative_install_checks_run_inside_immediate_transaction(self):
        statements = []
        db = sqlite3.connect(self.store.path, isolation_level=None)
        self.addCleanup(db.close)
        db.set_trace_callback(statements.append)
        apply_validation_schema_migration(db)
        self.assertEqual('BEGIN IMMEDIATE', statements[0])
        self.assertEqual('COMMIT', statements[-1])
        statements.clear()
        apply_validation_schema_migration(db)
        self.assertEqual('BEGIN IMMEDIATE', statements[0])
        self.assertEqual('COMMIT', statements[-1])

    def test_extension_changes_full_fingerprint_but_not_core_schema_version(self):
        before_objects, before_version = read_schema_objects(self.store.path)
        before_sha, before_count, _ = compute_fingerprint(self.store.path)
        self.store.apply_validation_extension()
        after_objects, after_version = read_schema_objects(self.store.path)
        after_sha, after_count, _ = compute_fingerprint(self.store.path)
        self.assertEqual(before_version, after_version)
        self.assertNotEqual(before_sha, after_sha)
        self.assertEqual(8, after_count - before_count)
        self.assertEqual([], list(set(before_objects) - set(after_objects)))
        self.store.rollback_validation_extension()
        self.assertEqual((before_sha, before_count, before_version), compute_fingerprint(self.store.path))

    def test_rollback_preserves_business_events_even_when_tables_are_empty(self):
        self.store.apply_validation_extension()
        self.store.append_event(None, 'memory.validation_reserved', {'TEST': 'fixture audit event'})
        with self.assertRaisesRegex(ValueError, 'audit events exist'):
            self.store.rollback_validation_extension()
        self.assertTrue(self.store.is_validation_extension_installed())
        self.assertTrue(self.store.verify_event_chain()['ok'])

    def test_legacy_migration_skips_rule_legacy_rejected_and_revision_candidates(self):
        cases = {
            'method': self.candidate(),
            'rule': self.candidate(kind='rule'),
            'legacy': self.candidate(kind='legacy'),
            'rejected': self.candidate(decision='reject'),
            'revision': self.candidate(decision='revise'),
            'unreviewed': self.candidate(decision=None),
        }
        before = {name: dict(row) for name, row in cases.items()}
        self.store.apply_validation_extension()
        with self.store.connect() as db:
            result = migrate_legacy_validation_records(db)
        self.assertEqual(1, result['migrated'])
        self.assertEqual(1, result['preserved_terminal'])
        self.assertEqual(4, result['skipped_ineligible'])
        self.assertEqual('pending_round_1', self.store.get_validation_lineage(cases['method']['task_id'])['lineage_status'])
        for name, candidate in cases.items():
            self.assertEqual(before[name], self.store.get_memory_candidate(candidate['task_id']))
            if name != 'method':
                self.assertIsNone(self.store.get_validation_lineage(candidate['task_id']))
        with self.assertRaisesRegex(ValueError, 'business rows'):
            self.store.rollback_validation_extension()
        self.assertTrue(self.store.verify_event_chain()['ok'])


if __name__ == '__main__':
    unittest.main()
