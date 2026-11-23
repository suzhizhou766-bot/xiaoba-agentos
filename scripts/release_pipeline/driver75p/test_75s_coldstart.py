"""75S segment-2 cold-start isolated tests (prepare/freeze leaf contracts).

Scope: prepare.py / freeze.py / cold_start_contract.py only. review.py is owned by
75V and is neither imported nor modified here.

Evidence policy mirrors segment 1: Git refs / version / manifest / per-blob checks
are REAL and read-only against the assigned BASE (4c6faf4) and diff_base (314b9a0);
the heavy baseline / create_manifest / build_second_brain / build_release runners
are injected as isolated fakes. All write targets (source / acceptance / reports /
work / freeze site) live inside this card's reports directory, never system tmp.
The old test_driver75p.py is never imported or executed. PIN 3.14.7 -B, no git write,
no real release / model / production mutation.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import release_context as rc
import cold_start_contract as csc
from prepare import run_prepare
from freeze import run_freeze

REAL_ROOT = HERE.parents[2]
R3 = REAL_ROOT / 'reports' / 'v1615-r1' / 'driver75p-r3'
EVIDENCE = R3 / 'coldstart-evidence'
PERSIST = R3 / 'persist'

EXPECTED_ROOTS = [
    'monitor',
    'development',
    'docs/architecture-topology',
]


def _persist(name: str, files: list[Path]) -> None:
    dest = PERSIST / name
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        if f.is_file():
            shutil.copy2(f, dest / f.name)


class TestDeriveExportRoots(unittest.TestCase):
    def test_six_scopes_collapse_to_three_non_overlapping_roots(self) -> None:
        scopes = rc.load_context()['parameters']['diff']['paths']
        self.assertEqual(len(scopes), 6)
        self.assertEqual(csc.derive_export_roots(scopes), EXPECTED_ROOTS)

    def test_development_whole_layout_preserved(self) -> None:
        scopes = ['development/integrated-candidate', 'development/review-ingress',
                  'development/interface/SKILL.md', 'development/architecture-center/web']
        self.assertEqual(csc.derive_export_roots(scopes), ['development'])

    def test_descendant_roots_dropped(self) -> None:
        self.assertEqual(csc.derive_export_roots(['docs', 'docs/architecture-topology']), ['docs'])

    def test_empty_scopes_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            csc.derive_export_roots([])


class TestColdStartContractRealInputs(unittest.TestCase):
    """Read-only contract checks against the real formal attempt-3 context."""

    def setUp(self) -> None:
        self.ctx = rc.load_context()

    def test_previous_source_matches_manager(self) -> None:
        res = csc.verify_previous_source(self.ctx)
        self.assertEqual(res['actual'], self.ctx['state']['previous_source_sha256'])
        self.assertEqual(res['actual'], res['expected'])

    def test_previous_source_drift_rejected(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        ctx['state']['previous_source_sha256'] = '0' * 64
        with self.assertRaises(SystemExit) as cm:
            csc.verify_previous_source(ctx)
        self.assertIn('previous_source_sha256', str(cm.exception))

    def test_monitor_authorization_three_way_and_same_source(self) -> None:
        res = csc.verify_monitor_authorization(self.ctx)
        self.assertTrue(res['same_source']['ok'])
        self.assertEqual(len(res['authorized_files']), 3)
        for f in res['authorized_files']:
            self.assertEqual(f['working_tree_sha256'], f['sha256'])
            self.assertEqual(f['git_blob_sha256'], f['sha256'])

    def test_monitor_syntax_hash_drift_rejected(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        ctx['record']['syntax'][0]['sha256'] = '0' * 64
        with self.assertRaises(SystemExit) as cm:
            csc.verify_monitor_authorization(ctx)
        self.assertIn('record.syntax', str(cm.exception))

    def test_monitor_post_files_set_mismatch_rejected(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        ctx['parameters']['post_files'] = ctx['parameters']['post_files'][:2]
        with self.assertRaises(SystemExit) as cm:
            csc.verify_monitor_authorization(ctx)
        self.assertIn('文件集合不一致', str(cm.exception))

    def test_record_diff_authority_matches_params_scope_git_diff(self) -> None:
        res = csc.verify_record_diff(self.ctx)
        self.assertEqual(res['diff_sha256'], self.ctx['record']['diff']['diff_sha256'])
        self.assertEqual(res['git_diff_sha256'], res['diff_sha256'])
        self.assertEqual(res['diff_path'], self.ctx['record']['diff']['diff_path'])

    def test_record_diff_hash_drift_rejected(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        ctx['record']['diff']['diff_sha256'] = '0' * 64
        with self.assertRaises(SystemExit) as cm:
            csc.verify_record_diff(ctx)
        self.assertIn('diff_sha256', str(cm.exception))

    def test_record_diff_scope_drift_rejected(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        # Drop a declared scope: the actual git diff no longer matches the authority.
        ctx['parameters']['diff']['paths'] = ctx['parameters']['diff']['paths'][:-1]
        with self.assertRaises(SystemExit) as cm:
            csc.verify_record_diff(ctx)
        self.assertIn('scope 漂移', str(cm.exception))


class TestPrepareFreezeIsolated(unittest.TestCase):
    """Normal prepare/freeze entries with real git export + fake heavy runners."""

    def setUp(self) -> None:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.base_ctx = rc.load_context()
        self.tmp = Path(tempfile.mkdtemp(prefix='coldstart-', dir=EVIDENCE)).resolve()
        self.root = self.tmp / 'root'
        self.work = self.root / 'work' / 'v1615-r1'
        self.source = self.root / 'source-v1615-r1'
        self.candidate = self.source / 'development' / 'integrated-candidate'
        self.acceptance = self.root / 'acceptance-v1615-r1'
        self.reports = self.root / 'reports' / 'v1615-r1'
        for p in (self.work, self.acceptance, self.reports):
            p.mkdir(parents=True, exist_ok=True)

        tool_a = self.work / 'tool_a.py'
        tool_a.write_text('# tool a\n', encoding='utf-8')
        tool_b = self.work / 'tool_b.py'
        tool_b.write_text('# tool b\n', encoding='utf-8')
        m1 = self.reports / 'tool-manifest-1.json'
        m2 = self.reports / 'tool-manifest-2.json'
        m1.write_text(json.dumps({str(tool_a.relative_to(self.root)): rc._sha256_file(tool_a)}), encoding='utf-8')
        m2.write_text(json.dumps({str(tool_b.relative_to(self.root)): rc._sha256_file(tool_b)}), encoding='utf-8')

        ctx = copy.deepcopy(self.base_ctx)
        ctx['root'] = self.root
        ctx['work'] = self.work
        ctx['source'] = self.source
        ctx['candidate'] = self.candidate
        ctx['acceptance'] = self.acceptance
        ctx['reports'] = self.reports
        ctx['state'] = copy.deepcopy(self.base_ctx['state'])
        ctx['state']['tool_manifests'] = [str(m1), str(m2)]
        ctx['parameters'] = copy.deepcopy(self.base_ctx['parameters'])
        ctx['parameters']['release_root'] = str(self.root)
        self.ctx = ctx
        self.suites = ctx['state']['expected_suites']

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _prepare_runner(self):
        suites = self.suites

        def runner(cmd, cwd, env):
            s = ' '.join(cmd)
            if 'baseline_runner.py' in s:
                suite = cmd[-1]
                exp = suites[suite]
                line = json.dumps({'suite': suite, 'tests': exp['tests'], 'skipped': exp['skipped'], 'ok': True})
                return 0, f'Ran {exp["tests"]} tests in 1.0s\n\nOK (skipped={exp["skipped"]})\n{line}\n'
            if 'create_manifest.py' in s:
                return 0, '{"ok": true, "files": 999}\n'
            if 'build_second_brain.py' in s:
                return 0, 'brain generated (fake)\n'
            if 'verify_fake_brain.py' in s:
                return 0, 'brain verified (fake)\n'
            return 0, 'ok\n'
        return runner

    def _freeze_runner(self):
        def runner(cmd, cwd, env):
            s = ' '.join(cmd)
            if 'build_release.py' in s:
                target = Path(cmd[cmd.index('--target') + 1])
                target.mkdir(parents=True, exist_ok=True)
                (target / 'RELEASE.json').write_text(
                    json.dumps({'release_tier': {'tier': 'minor', 'reason': 'isolated-fake'}}), encoding='utf-8')
                (target / 'FILES.sha256').write_text('isolated fake files manifest\n', encoding='utf-8')
                return 0, 'candidate built (fake)\n'
            if 'verify_brain' in s:
                return 0, 'brain verified (fake)\n'
            return 0, 'ok\n'
        return runner

    def _tamper_baseline_log(self, filename: str, new_content: str) -> None:
        """Rewrite one raw baseline log and refresh the PREPARATION/BASE_READY hashes
        so the sha256 bindings stay consistent. This isolates the genuine Ran/OK/FAILED
        parse as the only remaining ground for the strict freeze gate to reject on."""
        log_path = self.reports / 'prepare' / filename
        log_path.write_text(new_content, encoding='utf-8')
        prep_path = self.work / 'PREPARATION.json'
        prep = json.loads(prep_path.read_text(encoding='utf-8'))
        prep['baseline_logs'][filename]['sha256'] = rc._sha256_file(log_path)
        prep_path.write_text(json.dumps(prep, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        ready_path = self.work / 'BASE_READY.json'
        ready = json.loads(ready_path.read_text(encoding='utf-8'))
        ready['preparation_sha256'] = rc._sha256_file(prep_path)
        ready_path.write_text(json.dumps(ready, indent=2) + '\n', encoding='utf-8')

    def _tracking_freeze_runner(self, built: list):
        inner = self._freeze_runner()

        def runner(cmd, cwd, env):
            if 'build_release.py' in ' '.join(cmd):
                built.append(True)
            return inner(cmd, cwd, env)
        return runner

    def test_prepare_normal_entry_binds_authority_and_full_export(self) -> None:
        res = run_prepare(self.ctx, subproc_runner=self._prepare_runner())
        self.assertTrue(res['ok'])
        prep = json.loads((self.work / 'PREPARATION.json').read_text(encoding='utf-8'))
        auth = self.ctx['record']['diff']['diff_path']
        self.assertEqual(prep['diff_file'], auth)
        self.assertEqual(prep['diff_sha256'], self.ctx['record']['diff']['diff_sha256'])
        copy_path = Path(prep['round_diff_file'])
        self.assertEqual(copy_path.read_bytes(), Path(auth).read_bytes())
        self.assertEqual(rc._sha256_file(copy_path), prep['diff_sha256'])
        self.assertEqual(prep['export_roots'], EXPECTED_ROOTS)
        self.assertTrue((self.source / 'docs' / 'architecture-topology').is_dir())
        self.assertTrue((self.source / 'development' / 'review-ingress').is_dir())
        ev = json.loads((self.reports / 'prepare' / '00-export-verified.json').read_text(encoding='utf-8'))
        self.assertTrue(ev['ok'])
        self.assertGreater(ev['files'], 514)  # six-scope export exceeds the old two-root count
        self.assertEqual(prep['tests']['core'], self.suites['core']['tests'])
        self.assertEqual(prep['tests']['architecture_skipped'], self.suites['architecture']['skipped'])
        ready = json.loads((self.work / 'BASE_READY.json').read_text(encoding='utf-8'))
        self.assertEqual(ready['preparation_sha256'], rc._sha256_file(self.work / 'PREPARATION.json'))
        self.assertTrue(prep['cold_start_contract']['monitor_authorization']['same_source']['ok'])
        _persist('prepare-positive', [
            self.work / 'PREPARATION.json', self.work / 'BASE_READY.json',
            self.reports / 'prepare' / '00-export-verified.json',
            self.reports / 'prepare' / '08-git-diff.log',
        ])

    def test_freeze_normal_entry_parameterized_root15_site(self) -> None:
        run_prepare(self.ctx, subproc_runner=self._prepare_runner())
        site = self.root / 'freeze-site-test'
        res = run_freeze(self.ctx, subproc_runner=self._freeze_runner(), site_root=site)
        self.assertTrue(res['ok'])
        state = json.loads((self.acceptance / 'STATE.json').read_text(encoding='utf-8'))
        freeze = json.loads((self.acceptance / 'FREEZE.json').read_text(encoding='utf-8'))
        self.assertEqual(state['root'], str(site.resolve()))
        self.assertTrue(Path(state['root']).is_relative_to(self.root))
        self.assertNotIn('/private/tmp', state['root'])
        self.assertEqual(state['diff'], self.ctx['record']['diff']['diff_path'])
        self.assertEqual(state['diff_sha256'], self.ctx['record']['diff']['diff_sha256'])
        self.assertEqual(freeze['diff_sha256'], self.ctx['record']['diff']['diff_sha256'])
        prompt = Path(state['prompt']).read_text(encoding='utf-8')
        for scope in self.ctx['parameters']['diff']['paths']:
            self.assertIn(scope, prompt)
        self.assertNotIn('excluded web', prompt)
        self.assertNotIn('全部 6 个范围（包括', prompt)
        self.assertEqual(state['tasks'], {})
        self.assertIn('handoff', state)
        self.assertTrue(Path(state['config']).is_relative_to(site))
        self.assertEqual(state['prompt_sha256'], rc._sha256_file(Path(state['prompt'])))
        _persist('freeze-positive', [
            self.acceptance / 'STATE.json', self.acceptance / 'FREEZE.json',
            Path(state['prompt']), self.reports / 'freeze' / '00-freeze-export-verified.json',
        ])

    def test_freeze_rejects_site_outside_root15(self) -> None:
        run_prepare(self.ctx, subproc_runner=self._prepare_runner())
        outside = self.tmp / 'outside-site'  # sibling of root, escapes ROOT15
        with self.assertRaises(SystemExit) as cm:
            run_freeze(self.ctx, subproc_runner=self._freeze_runner(), site_root=outside)
        self.assertIn('ROOT15', str(cm.exception))
        self.assertFalse((self.acceptance / 'STATE.json').exists())

    def test_freeze_rejects_json_only_baseline_before_claim_and_build(self) -> None:
        """A log carrying only a JSON pass claim (no genuine Ran/OK) must be rejected
        by the pre-write strict gate even after its recorded hash is refreshed. The old
        weak loop accepted this via the '"tests": N' / '"ok": true' JSON fallbacks."""
        run_prepare(self.ctx, subproc_runner=self._prepare_runner())
        exp = self.suites['core']
        json_only = json.dumps(
            {'suite': 'core', 'tests': exp['tests'], 'skipped': exp['skipped'], 'ok': True}
        ) + '\n'
        self._tamper_baseline_log('01-core.log', json_only)
        built: list = []
        site = self.root / 'freeze-site-jsononly'
        with self.assertRaises(SystemExit) as cm:
            run_freeze(self.ctx, subproc_runner=self._tracking_freeze_runner(built), site_root=site)
        self.assertIn('genuinely report Ran', str(cm.exception))
        self.assertFalse((self.acceptance / 'FREEZE.started').exists())
        self.assertFalse((self.acceptance / 'STATE.json').exists())
        self.assertFalse(site.exists())
        self.assertEqual(built, [])

    def test_freeze_rejects_failed_baseline_before_claim_and_build(self) -> None:
        """A log containing FAILED (with a lying JSON ok=true) must be rejected before
        FREEZE.started and build_release. The old weak loop accepted it via '"ok": true'."""
        run_prepare(self.ctx, subproc_runner=self._prepare_runner())
        exp = self.suites['core']
        failed_log = (
            f'Ran {exp["tests"]} tests in 1.0s\n\n'
            'FAILED (failures=1)\n'
            + json.dumps({'suite': 'core', 'tests': exp['tests'], 'skipped': exp['skipped'], 'ok': True})
            + '\n'
        )
        self._tamper_baseline_log('01-core.log', failed_log)
        built: list = []
        site = self.root / 'freeze-site-failed'
        with self.assertRaises(SystemExit) as cm:
            run_freeze(self.ctx, subproc_runner=self._tracking_freeze_runner(built), site_root=site)
        self.assertIn('FAILED', str(cm.exception))
        self.assertFalse((self.acceptance / 'FREEZE.started').exists())
        self.assertFalse((self.acceptance / 'STATE.json').exists())
        self.assertFalse(site.exists())
        self.assertEqual(built, [])

    def test_prepare_rejects_previous_source_drift_zero_side_effects(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        ctx['state']['previous_source_sha256'] = '0' * 64
        with self.assertRaises(SystemExit) as cm:
            run_prepare(ctx, subproc_runner=self._prepare_runner())
        self.assertIn('previous_source_sha256', str(cm.exception))
        self.assertFalse((self.work / 'PREPARATION.started').exists())
        self.assertFalse(self.source.exists())

    def test_prepare_rejects_monitor_drift_zero_side_effects(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        ctx['record']['syntax'][0]['sha256'] = '0' * 64
        with self.assertRaises(SystemExit) as cm:
            run_prepare(ctx, subproc_runner=self._prepare_runner())
        self.assertIn('record.syntax', str(cm.exception))
        self.assertFalse((self.work / 'PREPARATION.started').exists())
        self.assertFalse(self.source.exists())

    def test_prepare_rejects_record_diff_drift_zero_side_effects(self) -> None:
        ctx = copy.deepcopy(self.ctx)
        ctx['record']['diff']['diff_sha256'] = '0' * 64
        with self.assertRaises(SystemExit) as cm:
            run_prepare(ctx, subproc_runner=self._prepare_runner())
        self.assertIn('diff_sha256', str(cm.exception))
        self.assertFalse((self.work / 'PREPARATION.started').exists())
        self.assertFalse(self.source.exists())

    def test_prepare_single_claim(self) -> None:
        run_prepare(self.ctx, subproc_runner=self._prepare_runner())
        self.assertTrue((self.work / 'PREPARATION.started').exists())
        with self.assertRaises(SystemExit) as cm:
            run_prepare(self.ctx, subproc_runner=self._prepare_runner())
        self.assertIn('already', str(cm.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)