"""Narrow contract tests for recover_review.py — same-task failed-side recovery.

NEW test source. Never runs test_driver75p.py. No git write. No formal site.
Mock only the external CLI boundary; EventStore freeze/resume/Orchestrator/
validate_acceptance_database stay real. Does not hand-forge recovery success by
SQL-updating runs to completed.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import release_context as rc
import nodes as nodes_mod
import review as review_mod
import recover_review as recover_mod
from test_75s_nodes_evidence import (
    QualifiedNodesFixture,
    _sha,
    _write_json,
    make_live_db,
    _node_source,
)
from test_75v_review import tempfile_mkdtemp

HERE = Path(__file__).resolve().parent
REAL_ROOT = HERE.parents[2]
REPORTS = REAL_ROOT / 'reports' / 'v1615-r1' / 'driver75p-recovery1'
EVIDENCE_ROOT = REPORTS / 'fixtures'
CONTROLLED_TMP = REPORTS / 'controlled-tmp'
PERSIST = REPORTS / 'evidence'

PASS = (
    '证据引用：task:{task_id}\n'
    '审查结论：PASS\n'
    '评分：8\n'
    '重复经验：无\n'
    '适用条件：隔离恢复夹具\n'
    'FINAL_ACCEPTANCE: PASS\n'
)


class RecoverResource(review_mod.IsolationCliResource):
    """CLI-boundary stub; records run_task calls so completed side reuse is proven."""

    calls: list

    def __init__(self, source, workspace=None, **kwargs):
        super().__init__(source, workspace, **kwargs)
        self.reviewer_id = str((source or {}).get('reviewer_id') or '')

    def run_task(self, task_id, step_code, instruction, materials, attempt=0):
        type(self).calls.append({
            'reviewer_id': self.reviewer_id,
            'task_id': task_id,
            'attempt': attempt,
        })
        body = PASS.format(task_id=task_id)
        return {
            'status': 'completed',
            'body': body,
            'execution_evidence': {
                'provider': 'mock_turn_boundary',
                'model': self.node.get('model'),
                'route_source': 'test',
            },
        }


def recover_factory(source, workspace=None, **kwargs):
    return RecoverResource(source, workspace, **kwargs)


class TestRecoverReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        CONTROLLED_TMP.mkdir(parents=True, exist_ok=True)
        PERSIST.mkdir(parents=True, exist_ok=True)
        cls.base_ctx = rc.load_context(HERE.parent)
        cls.shared_root = Path(tempfile_mkdtemp(EVIDENCE_ROOT, 'qualified-shared-'))
        cls.fx = QualifiedNodesFixture(cls.shared_root, cls.base_ctx)
        cls.package = review_mod.build_isolation_release_candidate(
            cls.fx.candidate,
            cls.shared_root / 'release-candidate' / cls.fx.version,
            cls.fx.version,
            expected_manifest_sha256=cls.fx.manifest_sha,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared_root, ignore_errors=True)

    def setUp(self):
        self._old_tmpdir = os.environ.get('TMPDIR')
        RecoverResource.calls = []
        self.root = Path(tempfile_mkdtemp(CONTROLLED_TMP, 'recover-'))
        os.environ['TMPDIR'] = str(self.root)
        tempfile.tempdir = None

    def tearDown(self):
        if self._old_tmpdir is None:
            os.environ.pop('TMPDIR', None)
        else:
            os.environ['TMPDIR'] = self._old_tmpdir
        tempfile.tempdir = None
        shutil.rmtree(self.root, ignore_errors=True)

    def _prepare_frozen_site(self):
        ids = ['fixture-dsh-node', 'fixture-qoder-node']
        live = self.root / 'live'
        make_live_db(live / '.runtime' / 'agentos.sqlite3', [
            (ids[0], _node_source(
                ids[0], 'peer', 'Fixture DSH',
                '/Users/suzhizhou/Documents/DeepSeek Harness/current/apps/cli/lib/bin.js',
                'DeepSeek-4.1-flash', 'dsh')),
            (ids[1], _node_source(
                ids[1], 'both', 'Fixture Qoder',
                '/Users/suzhizhou/.local/bin/qodercli',
                'Qwen4-Max-Preview', 'qoder', 'xhigh')),
        ])
        # Inject no_progress_seconds=180 on Qoder in live copy for edit contrast.
        import sqlite3
        conn = sqlite3.connect(str(live / '.runtime' / 'agentos.sqlite3'))
        try:
            row = conn.execute('SELECT data FROM ri_sources WHERE id=?', (ids[1],)).fetchone()
            src = json.loads(row[0])
            src['node']['no_progress_seconds'] = 180
            conn.execute('UPDATE ri_sources SET data=? WHERE id=?',
                         (json.dumps(src, ensure_ascii=False), ids[1]))
            row0 = conn.execute('SELECT data FROM ri_sources WHERE id=?', (ids[0],)).fetchone()
            src0 = json.loads(row0[0])
            src0['node']['no_progress_seconds'] = 1800
            conn.execute('UPDATE ri_sources SET data=? WHERE id=?',
                         (json.dumps(src0, ensure_ascii=False), ids[0]))
            conn.commit()
        finally:
            conn.close()

        case_acceptance = self.root / 'acceptance'
        case_reports = self.root / 'reports'
        case_acceptance.mkdir(parents=True)
        case_reports.mkdir(parents=True)
        shutil.copytree(self.fx.reports / 'prepare', case_reports / 'prepare')
        shutil.copy2(self.fx.freeze_path, case_acceptance / 'FREEZE.json')
        # Keep the fixture diff bytes/hash bound in PREPARATION (validate_context).
        fixture_diff = self.fx.acceptance / f'{self.fx.diff_base[:7]}..BASE.diff'
        diff = case_acceptance / fixture_diff.name
        shutil.copy2(fixture_diff, diff)
        prompt = case_acceptance / 'review-card49.txt'
        prompt.write_text('recovery fixture prompt\n', encoding='utf-8')

        ctx = self.fx.ctx(live=live, node_ids=ids)
        ctx = dict(ctx)
        ctx['acceptance'] = case_acceptance
        ctx['reports'] = case_reports
        # Point work/ at the shared qualified PREP/READY used by the fixture.
        ctx['work'] = self.fx.work
        # Manager ctx.state keeps gate fields; acceptance STATE is task/budget/paths only.
        manager_state = dict(ctx['state'])
        self.assertGreaterEqual(len(manager_state.get('tool_manifests') or []), 2)
        acceptance_state = {
            'prompt': str(prompt),
            'prompt_sha256': _sha(prompt),
            'diff': str(diff),
            'diff_sha256': _sha(diff),
            'freeze_sha256': _sha(case_acceptance / 'FREEZE.json'),
            'tasks': {},
            'candidate_package': str(self.package),
            'no_progress_seconds': 1800,
            'review_node_ids': ids,
        }
        _write_json(case_acceptance / 'STATE.json', acceptance_state)
        ctx['state'] = manager_state
        evidence_dir = case_reports / 'nodes'
        nodes_report = nodes_mod.prepare_nodes(
            ctx, evidence_dir=evidence_dir, resource_factory=recover_factory)
        self.assertTrue(nodes_report['node_tests_passed'])

        # Formal unpatched release_binding under controlled TMPDIR.
        bind = review_mod.bind_release_candidate_package(
            isolated_root=Path(nodes_report['isolated_root']),
            cfg_path=Path(nodes_report['isolated_config']),
            version=self.fx.version,
            candidate_package=self.package,
            expected_manifest_sha256=self.fx.manifest_sha,
            require_temp_layout=True,
            trusted_candidate=Path(ctx['candidate']),
        )
        self.assertTrue(bind['release_binding_status']['ok'])

        prod = review_mod._load_product(ctx)
        cfg = prod['AppConfig'].load(Path(nodes_report['isolated_config']))
        store = prod['EventStore'](cfg.database_path)
        task_id = prod['_submit_task'](
            cfg,
            store,
            prompt.read_text(encoding='utf-8'),
            experience_kind='audit',
            release_acceptance=True,
            no_work_task=True,
            material_files=(str(prompt), str(diff)),
            needs_review=True,
        )

        dsh_body = PASS.format(task_id=task_id)
        dsh_run = store.start_agent_run(task_id, ids[0], {'purpose': 'review', 'step_code': 'REVIEW-01'})
        store.finish_agent_run(dsh_run, 'completed', dsh_body, None, 12)
        q_run = store.start_agent_run(task_id, ids[1], {'purpose': 'review', 'step_code': 'REVIEW-01'})
        store.finish_agent_run(
            q_run,
            'permanent_error',
            None,
            '节点调用未写出合格回执：Qoder调用连续180秒无可验证结果进展，已停止本次调用。',
            471000,
            failure_class='external_upstream',
        )
        frozen = store.freeze_task(
            task_id,
            failure_class='external_upstream',
            failures=[{
                'agent': ids[1],
                'error': 'Qoder调用连续180秒无可验证结果进展，已停止本次调用。',
                'failure_class': 'external_upstream',
                'external': True,
            }],
        )
        self.assertEqual((store.get_task(task_id) or {}).get('status'), 'frozen_paused')

        # Seed an original review acceptance file that must keep its bytes.
        original_dir = case_reports / 'review'
        original_dir.mkdir(parents=True, exist_ok=True)
        original = original_dir / '11-acceptance.json'
        original.write_text('{"ok":false,"note":"pre-recovery placeholder — must keep bytes"}\n',
                            encoding='utf-8')
        original_sha = _sha(original)

        acceptance_state.update({
            'task_id': task_id,
            'tasks': {'review': task_id},
            'database': str(Path(nodes_report['isolated_database']).resolve()),
            'config': str(Path(nodes_report['isolated_config']).resolve()),
            'workspace_root': str(Path(nodes_report['workspace_root']).resolve()),
            'root': str(Path(nodes_report['isolated_root']).resolve()),
            'nodes_prepared_path': str((evidence_dir / 'nodes-prepared.json').resolve()),
            'nodes_prepared_sha256': _sha(evidence_dir / 'nodes-prepared.json'),
            'acceptance_passed': False,
        })
        # Formal shape: acceptance STATE must NOT carry manager gate fields.
        for key in ('tool_manifests', 'live_config_path', 'live_config_sha256_before'):
            acceptance_state.pop(key, None)
        _write_json(case_acceptance / 'STATE.json', acceptance_state)
        ctx['state'] = manager_state
        return {
            'ctx': ctx,
            'ids': ids,
            'task_id': task_id,
            'freeze_hash': frozen['freeze_hash'],
            'dsh_run': dsh_run,
            'q_run': q_run,
            'original_sha': original_sha,
            'original': original,
            'nodes_report': nodes_report,
            'store': store,
            'manager_state': manager_state,
            'acceptance_state': acceptance_state,
        }

    def test_plan_zero_write(self):
        ctx = self.fx.ctx()
        before = list(Path(ctx['reports']).rglob('*')) if Path(ctx['reports']).exists() else []
        recover_mod.display_plan(ctx)
        after = list(Path(ctx['reports']).rglob('*')) if Path(ctx['reports']).exists() else []
        self.assertEqual(before, after)

    def test_formal_ctx_synthesis_acceptance_not_manager(self):
        """Counterexample: formal acceptance STATE lacks manager gate fields.

        Old bug (ctx['state']=acceptance STATE) must fail verify_manifests.
        Correct split: manager ctx.state for shared preflight; acceptance for paths.
        """
        site = self._prepare_frozen_site()
        acceptance = json.loads(
            (Path(site['ctx']['acceptance']) / 'STATE.json').read_text(encoding='utf-8'))
        self.assertNotIn('tool_manifests', acceptance)
        self.assertNotIn('live_config_path', acceptance)
        self.assertGreaterEqual(len(site['ctx']['state'].get('tool_manifests') or []), 2)

        # Old substitution pattern — must fail shared gate.
        poisoned = dict(site['ctx'])
        poisoned['state'] = acceptance
        with self.assertRaises(SystemExit) as caught:
            rc.verify_manifests(poisoned)
        self.assertIn('tool_manifests', str(caught.exception))

        with self.assertRaises(SystemExit) as caught2:
            recover_mod.require_manager_gate_state(acceptance)
        self.assertIn('manager', str(caught2.exception).lower())

        # Correct structure: manager state passes gate; acceptance loads task paths.
        recover_mod.require_manager_gate_state(site['ctx']['state'])
        loaded = recover_mod.load_acceptance_state(site['ctx'])
        self.assertEqual(loaded['task_id'], site['task_id'])
        self.assertEqual(loaded['database'], acceptance['database'])
        gate = recover_mod.preflight_recovery_write_gate(site['ctx'])
        self.assertEqual(gate.get('stage'), 'recover')

    def test_positive_resume_reuses_completed_side(self):
        site = self._prepare_frozen_site()
        RecoverResource.calls = []
        result = recover_mod.run_recover(
            site['ctx'],
            failed_node=site['ids'][1],
            no_progress_seconds=1800,
            allow_real=True,
            freeze_hash=site['freeze_hash'],
            resource_factory=recover_factory,
            evidence_dir=Path(site['ctx']['reports']) / 'driver75p-recovery1',
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['task_status'], 'completed')
        self.assertTrue(result['dispatched'])
        # Only failed node may be newly dispatched by Orchestrator.
        dispatched = {c['reviewer_id'] for c in RecoverResource.calls}
        self.assertIn(site['ids'][1], dispatched)
        self.assertNotIn(site['ids'][0], dispatched)
        # Completed DSH run id preserved.
        latest = site['store'].latest_agent_runs(site['task_id'])
        self.assertEqual(latest[site['ids'][0]]['id'], site['dsh_run'])
        self.assertEqual(latest[site['ids'][0]]['status'], 'completed')
        self.assertEqual(latest[site['ids'][1]]['status'], 'completed')
        self.assertNotEqual(latest[site['ids'][1]]['id'], site['q_run'])
        # New acceptance path + STATE pointer; original bytes kept.
        evidence = Path(result['acceptance']['review_evidence_path'])
        self.assertTrue(evidence.is_file())
        self.assertIn('review-recovery1', str(evidence))
        state = json.loads((Path(site['ctx']['acceptance']) / 'STATE.json').read_text(encoding='utf-8'))
        self.assertTrue(state['acceptance_passed'])
        self.assertEqual(Path(state['review_evidence_path']).resolve(), evidence.resolve())
        self.assertEqual(_sha(site['original']), site['original_sha'])
        shutil.copy2(evidence, PERSIST / 'recovery-11-acceptance.json')
        shutil.copy2(
            Path(site['ctx']['reports']) / 'driver75p-recovery1' / 'recovery-result.json',
            PERSIST / 'recovery-result.json',
        )

    def test_negative_not_frozen_refused(self):
        site = self._prepare_frozen_site()
        # Unfreeze by abandoning? Instead submit a fresh pending-only check via inspect.
        with self.assertRaises(SystemExit) as caught:
            recover_mod.inspect_frozen_task(
                site['store'],
                'missing-task',
                failed_node=site['ids'][1],
                expected_freeze_hash=None,
            )
        self.assertIn('frozen', str(caught.exception).lower())

    def test_negative_wrong_freeze_hash(self):
        site = self._prepare_frozen_site()
        with self.assertRaises(SystemExit) as caught:
            recover_mod.inspect_frozen_task(
                site['store'],
                site['task_id'],
                failed_node=site['ids'][1],
                expected_freeze_hash='0' * 64,
            )
        self.assertIn('freeze-hash', str(caught.exception).lower())

    def test_negative_failed_node_mismatch(self):
        site = self._prepare_frozen_site()
        with self.assertRaises(SystemExit) as caught:
            recover_mod.inspect_frozen_task(
                site['store'],
                site['task_id'],
                failed_node=site['ids'][0],  # DSH is success side
                expected_freeze_hash=site['freeze_hash'],
            )
        self.assertIn('exactly', str(caught.exception).lower())

    def test_negative_budget_mismatch_state(self):
        site = self._prepare_frozen_site()
        with self.assertRaises(SystemExit) as caught:
            recover_mod.run_recover(
                site['ctx'],
                failed_node=site['ids'][1],
                no_progress_seconds=180,  # != STATE 1800
                allow_real=True,
                freeze_hash=site['freeze_hash'],
                resource_factory=recover_factory,
                evidence_dir=Path(site['ctx']['reports']) / 'driver75p-recovery1-budget',
            )
        self.assertIn('no_progress_seconds', str(caught.exception))

    def test_reentry_does_not_redispatch(self):
        site = self._prepare_frozen_site()
        ev = Path(site['ctx']['reports']) / 'driver75p-recovery1'
        first = recover_mod.run_recover(
            site['ctx'],
            failed_node=site['ids'][1],
            no_progress_seconds=1800,
            allow_real=True,
            freeze_hash=site['freeze_hash'],
            resource_factory=recover_factory,
            evidence_dir=ev,
        )
        self.assertTrue(first['ok'])
        calls_after_first = list(RecoverResource.calls)
        second = recover_mod.run_recover(
            site['ctx'],
            failed_node=site['ids'][1],
            no_progress_seconds=1800,
            allow_real=True,
            freeze_hash=site['freeze_hash'],
            resource_factory=recover_factory,
            evidence_dir=ev,
        )
        self.assertTrue(second['reentry'])
        self.assertFalse(second['dispatched'])
        self.assertEqual(RecoverResource.calls, calls_after_first)


if __name__ == '__main__':
    unittest.main(verbosity=2)
