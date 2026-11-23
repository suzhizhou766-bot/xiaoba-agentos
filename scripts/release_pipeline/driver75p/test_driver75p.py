"""Isolation test suite for 75P driver (release_context, prepare, freeze, review, cli).

Validates all rejection gates, reproduces & verifies fixes for the 2 manager probes,
checks dynamic baseline parsing, negative assertions, and clean CLI behavior.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Add driver75p to sys.path
DRIVER_DIR = Path(__file__).resolve().parent
if str(DRIVER_DIR) not in sys.path:
    sys.path.insert(0, str(DRIVER_DIR))

import release_context as rc
from cli import parse_args, display_plan
from freeze import run_freeze
from prepare import run_prepare
from review import run_review


class TestDriver75PIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix='test-75p-isolation-', dir='/private/tmp')).resolve()
        self.root = self.tmp_dir / 'root'
        self.work = self.root / 'work/v1615-r1'
        self.source = self.root / 'source-v1615-r1'
        self.candidate = self.source / 'development/integrated-candidate'
        self.acceptance = self.root / 'acceptance-v1615-r1'
        self.reports = self.root / 'reports/v1615-r1'

        for p in (self.root, self.work, self.acceptance, self.reports):
            p.mkdir(parents=True, exist_ok=True)

        # Create mock tools
        self.manifest_tool = self.root / 'reports/tool-manifest-1.json'
        self.manifest_tool2 = self.root / 'reports/tool-manifest-2.json'
        self.manifest_tool.parent.mkdir(parents=True, exist_ok=True)

        tool_a = self.work / 'tool_a.py'
        tool_a.write_text('# tool a\n', encoding='utf-8')
        tool_b = self.work / 'tool_b.py'
        tool_b.write_text('# tool b\n', encoding='utf-8')

        m1_data = {str(tool_a.relative_to(self.root)): rc._sha256_file(tool_a)}
        m2_data = {str(tool_b.relative_to(self.root)): rc._sha256_file(tool_b)}
        self.manifest_tool.write_text(json.dumps(m1_data), encoding='utf-8')
        self.manifest_tool2.write_text(json.dumps(m2_data), encoding='utf-8')

        # Fake repo with valid commit structure
        self.repo = self.tmp_dir / 'repo'
        self.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '-b', 'main'], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=self.repo, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.local'], cwd=self.repo, check=True)

        cand_in_repo = self.repo / 'development/integrated-candidate'
        (cand_in_repo / 'docs').mkdir(parents=True, exist_ok=True)
        (cand_in_repo / 'config').mkdir(parents=True, exist_ok=True)
        (cand_in_repo / 'pyproject.toml').write_text('[project]\nversion = "1.6.15"\n', encoding='utf-8')
        (cand_in_repo / 'config/agentos.json').write_text(
            json.dumps({'release': {'expected_version': 'v1.6.15'}}), encoding='utf-8'
        )
        (cand_in_repo / 'docs/RELEASE_V1.6.15.md').write_text('# Release\n', encoding='utf-8')
        (cand_in_repo / 'SOURCE_MANIFEST.json').write_text('{"files": 310}\n', encoding='utf-8')
        self.source_manifest_sha = rc._sha256_file(cand_in_repo / 'SOURCE_MANIFEST.json')

        subprocess.run(['git', 'add', '.'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-m', 'Initial base'], cwd=self.repo, check=True, capture_output=True)
        self.base_commit = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True
        ).strip()

        # Dummy diff base commit
        (self.repo / 'dummy.txt').write_text('before\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-m', 'diff base'], cwd=self.repo, check=True, capture_output=True)
        self.diff_base = subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True
        ).strip()

        # Synthetic Context
        self.ctx = {
            'root': self.root,
            'work': self.work,
            'live': self.root / 'live',
            'repo': self.repo,
            'source': self.source,
            'candidate': self.candidate,
            'acceptance': self.acceptance,
            'reports': self.reports,
            'previous_package': self.root / 'prev_pkg',
            'round': 'v1615-r1',
            'version': 'v1.6.15',
            'assigned': {
                'round': 'v1615-r1',
                'version': 'v1.6.15',
                'base': self.base_commit,
                'diff_base': self.diff_base,
                'source_manifest_sha256': self.source_manifest_sha,
            },
            'parameters': {
                'round': 'v1615-r1',
                'version': 'v1.6.15',
                'release_root': str(self.root),
                'tools_dir': str(self.work),
                'diff': {
                    'repo': str(self.repo),
                    'base': self.base_commit,
                    'diff_base': self.diff_base,
                    'paths': ['development/integrated-candidate'],
                    'exclude': [],
                },
            },
            'record': {
                'round': 'v1615-r1',
                'version': 'v1.6.15',
                'attempt': 2,
                'acceptance_step4_dir': str(self.acceptance / 'step4-attempt2'),
                'reports_step4_dir': str(self.reports / 'round1b-attempt2'),
                'releases_package_dir': str(self.root / 'releases/v1615-r1-attempt2/v1.6.15'),
                'step4_helpers_dir': str(self.work / 'round1b'),
            },
            'state': {
                'round': 'v1615-r1',
                'version': 'v1.6.15',
                'current_attempt': 2,
                'previous_package': str(self.root / 'prev_pkg'),
                'tool_manifests': [str(self.manifest_tool), str(self.manifest_tool2)],
                'review_node_ids': [
                    '3e3664c2-3997-4461-bd41-7f790e490767',
                    '4f683199-bb6e-4d3a-814b-7f8954079731',
                ],
            },
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_probe1_reproduced_and_rejected_outside_absolute_file(self) -> None:
        """Probe 1: verify_manifests must explicitly reject files outside release_root."""
        outside_file = self.tmp_dir / 'outside.py'
        outside_file.write_text('# outside\n', encoding='utf-8')
        bad_manifest = self.root / 'reports/bad-manifest.json'
        bad_manifest.write_text(
            json.dumps({str(outside_file): rc._sha256_file(outside_file)}), encoding='utf-8'
        )
        ctx = copy.deepcopy(self.ctx)
        ctx['state']['tool_manifests'] = [str(bad_manifest), str(self.manifest_tool2)]

        with self.assertRaises(SystemExit) as cm:
            rc.verify_manifests(ctx)
        self.assertIn('escapes release_root', str(cm.exception))

    def test_probe2_reproduced_and_rejected_wrong_git_source_version(self) -> None:
        """Probe 2: export preflight must reject Git candidate version 14 when manager expects 15."""
        cand_in_repo = self.repo / 'development/integrated-candidate'
        (cand_in_repo / 'pyproject.toml').write_text('[project]\nversion = "1.6.14"\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=self.repo, check=True)
        subprocess.run(['git', 'commit', '-m', 'version 14 commit'], cwd=self.repo, check=True, capture_output=True)
        bad_base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repo, text=True).strip()

        ctx = copy.deepcopy(self.ctx)
        ctx['assigned']['base'] = bad_base
        ctx['parameters']['diff']['base'] = bad_base

        with self.assertRaises(SystemExit) as cm:
            rc.validate_context(ctx, stage='export')
        self.assertIn('git candidate version', str(cm.exception))

    def test_identical_manifest_paths_rejected(self) -> None:
        """Manifest paths must be distinct."""
        ctx = copy.deepcopy(self.ctx)
        ctx['state']['tool_manifests'] = [str(self.manifest_tool), str(self.manifest_tool)]
        with self.assertRaises(SystemExit) as cm:
            rc.verify_manifests(ctx)
        self.assertIn('must be distinct files', str(cm.exception))

    def test_non_positive_attempt_rejected(self) -> None:
        """Attempt in record must be a positive integer."""
        ctx = copy.deepcopy(self.ctx)
        # Write real files for load_context
        ctx_work = self.work
        (ctx_work / 'MANAGEMENT-STATE.json').write_text(json.dumps(ctx['state']), encoding='utf-8')
        (ctx_work / 'PREPARE-PARAMETERS.json').write_text(json.dumps(ctx['parameters']), encoding='utf-8')
        (ctx_work / 'BASE_ASSIGNED.json').write_text(json.dumps(ctx['assigned']), encoding='utf-8')

        bad_rec = copy.deepcopy(ctx['record'])
        bad_rec['attempt'] = 0
        rec_path = self.root / 'attempt-record.json'
        rec_path.write_text(json.dumps(bad_rec), encoding='utf-8')

        (ctx_work / 'ATTEMPT-POINTER.json').write_text(
            json.dumps({'record_path': str(rec_path)}), encoding='utf-8'
        )

        with self.assertRaises(SystemExit) as cm:
            rc.load_context(ctx_work)
        self.assertIn('attempt must be a positive integer', str(cm.exception))

    def test_prepare_single_claim_and_failed_subprocess_logging(self) -> None:
        """Prepare claims single attempt and guarantees failed subprocess logs are written."""
        # Inject fake runner that fails on 02-ingress
        def fake_runner(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
            if 'export_archive' in cmd:
                (self.source / 'development/integrated-candidate/docs').mkdir(parents=True, exist_ok=True)
                (self.source / 'development/integrated-candidate/config').mkdir(parents=True, exist_ok=True)
                (self.source / 'development/integrated-candidate/pyproject.toml').write_text(
                    '[project]\nversion = "1.6.15"\n', encoding='utf-8'
                )
                (self.source / 'development/integrated-candidate/config/agentos.json').write_text(
                    json.dumps({'release': {'expected_version': 'v1.6.15'}}), encoding='utf-8'
                )
                (self.source / 'development/integrated-candidate/SOURCE_MANIFEST.json').write_text(
                    '{"files": 310}\n', encoding='utf-8'
                )
                return 0, 'archive exported'
            if 'verify_export_blobs' in cmd:
                return 0, '{"ok": true, "files": 10, "changed": [], "extra": [], "missing": []}'
            if 'baseline_runner.py' in ' '.join(cmd) and 'core' in cmd:
                return 0, 'Ran 10 tests in 0.05s\n\nOK\n{"suite":"core","tests":10,"skipped":0,"ok":true}'
            if 'baseline_runner.py' in ' '.join(cmd) and 'ingress' in cmd:
                return 1, 'INGRESS TEST FAILED: assertion error in test_ingress_01'
            return 0, 'ok'

        with self.assertRaises(SystemExit) as cm:
            run_prepare(self.ctx, subproc_runner=fake_runner)

        # Log must have been saved before raising
        ingress_log = self.reports / 'prepare/02-ingress.log'
        self.assertTrue(ingress_log.is_file(), 'Failed command log must be preserved')
        self.assertIn('INGRESS TEST FAILED', ingress_log.read_text(encoding='utf-8'))

        # Single claim marker was created
        self.assertTrue((self.work / 'PREPARATION.started').exists())

        # Second attempt must be rejected by claim marker
        with self.assertRaises(SystemExit) as cm2:
            run_prepare(self.ctx, subproc_runner=fake_runner)
        self.assertIn('preparation attempt already claimed', str(cm2.exception))

    def test_prepare_rejects_zero_tests_dummy_baseline(self) -> None:
        """Baseline reporting 0 tests must be rejected."""
        if (self.work / 'PREPARATION.started').exists():
            (self.work / 'PREPARATION.started').rmdir()

        def fake_runner_zero(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
            if 'export_archive' in cmd:
                (self.source / 'development/integrated-candidate/docs').mkdir(parents=True, exist_ok=True)
                (self.source / 'development/integrated-candidate/config').mkdir(parents=True, exist_ok=True)
                (self.source / 'development/integrated-candidate/pyproject.toml').write_text(
                    '[project]\nversion = "1.6.15"\n', encoding='utf-8'
                )
                (self.source / 'development/integrated-candidate/config/agentos.json').write_text(
                    json.dumps({'release': {'expected_version': 'v1.6.15'}}), encoding='utf-8'
                )
                (self.source / 'development/integrated-candidate/SOURCE_MANIFEST.json').write_text(
                    '{"files": 310}\n', encoding='utf-8'
                )
                return 0, 'archive exported'
            if 'verify_export_blobs' in cmd:
                return 0, '{"ok": true, "files": 10, "changed": [], "extra": [], "missing": []}'
            if 'baseline_runner.py' in ' '.join(cmd):
                return 0, 'Ran 0 tests in 0.0s\n\nOK\n{"suite":"core","tests":0,"skipped":0,"ok":true}'
            return 0, 'ok'

        with self.assertRaises(SystemExit) as cm:
            run_prepare(self.ctx, subproc_runner=fake_runner_zero)
        self.assertIn('reported 0 tests', str(cm.exception))

    def test_optimization_flag_rejected(self) -> None:
        """Interpreter running with optimization (-O) must be rejected."""
        cmd = [
            str(rc.PIN_PATH),
            '-B',
            '-O',
            '-c',
            'import sys; sys.path.insert(0, "' + str(DRIVER_DIR) + '"); import release_context; release_context.check_interpreter()',
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn('python optimization', res.stderr + res.stdout)

    def test_review_two_vote_gate_rejected_when_only_one_passes(self) -> None:
        """Review must reject when fewer than 2 independent nodes pass."""
        cand = self.candidate
        (cand / 'docs').mkdir(parents=True, exist_ok=True)
        (cand / 'docs/RELEASE_V1.6.15.md').write_text('doc', encoding='utf-8')
        (cand / 'pyproject.toml').write_text('[project]\nversion = "1.6.15"\n', encoding='utf-8')
        (cand / 'SOURCE_MANIFEST.json').write_text('{"files": 310}\n', encoding='utf-8')
        (cand / 'scripts').mkdir(parents=True, exist_ok=True)
        (cand / 'scripts/create_manifest.py').write_text('# manifest\n', encoding='utf-8')
        (cand / 'scripts/build_release.py').write_text(
            'def _passed_verdict(text): return "FINAL_ACCEPTANCE: PASS" in text\n', encoding='utf-8'
        )

        diff_name = f'{self.diff_base[:7]}..BASE.diff'
        diff_file = self.acceptance / diff_name
        diff_file.write_text('diff text\n', encoding='utf-8')

        prep_data = {
            'base': self.base_commit,
            'diff_sha256': rc._sha256_file(diff_file),
            'source_manifest_sha256': self.source_manifest_sha,
        }
        (self.work / 'PREPARATION.json').write_text(json.dumps(prep_data), encoding='utf-8')
        base_ready_data = {
            'base': self.base_commit,
            'round': 'v1615-r1',
            'preparation_sha256': rc._sha256_file(self.work / 'PREPARATION.json'),
        }
        (self.work / 'BASE_READY.json').write_text(json.dumps(base_ready_data), encoding='utf-8')

        prompt_file = self.acceptance / 'prompt.txt'
        prompt_file.write_text('prompt text', encoding='utf-8')

        freeze_file = self.acceptance / 'FREEZE.json'
        freeze_data = {'base': self.base_commit}
        freeze_file.write_text(json.dumps(freeze_data), encoding='utf-8')

        state_file = self.acceptance / 'STATE.json'
        state_data = {
            'base': self.base_commit,
            'freeze_sha256': rc._sha256_file(freeze_file),
            'prompt': str(prompt_file),
            'prompt_sha256': rc._sha256_file(prompt_file),
            'diff': str(diff_file),
            'diff_sha256': rc._sha256_file(diff_file),
            'candidate_package': str(self.root / 'pkg'),
            'config': str(self.tmp_dir / 'cfg.json'),
            'tasks': {},
        }
        state_file.write_text(json.dumps(state_data), encoding='utf-8')

        def fake_driver(st: dict, tid: str) -> dict:
            # 1 pass, 1 fail
            return {
                'task': {'status': 'completed', 'id': tid},
                'runs': [
                    {'agent_name': 'node-agy', 'status': 'completed', 'output': 'FINAL_ACCEPTANCE: PASS'},
                    {'agent_name': 'node-qoder', 'status': 'failed', 'output': 'FINAL_ACCEPTANCE: FAIL'},
                ],
            }

        def fake_subproc(cmd: list[str], cwd: Path, env: dict) -> tuple[int, str]:
            if 'create_manifest.py' in ' '.join(cmd):
                return 0, '{"files": 310}'
            return 0, '{"task_id": "test-task-123"}'

        with self.assertRaises(SystemExit) as cm:
            run_review(self.ctx, subproc_runner=fake_subproc, orchestrator_driver=fake_driver)
        self.assertIn('review acceptance gate failed', str(cm.exception))

    def test_cli_plan_output(self) -> None:
        """CLI --plan must output structured plan without throwing."""
        display_plan(self.ctx, stage=None)
        display_plan(self.ctx, stage='prepare')
        display_plan(self.ctx, stage='freeze')
        display_plan(self.ctx, stage='review')


if __name__ == '__main__':
    unittest.main()
