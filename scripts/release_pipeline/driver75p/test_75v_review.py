"""75V isolated verification — real Orchestrator + official validate_acceptance_database.

NEW test source. Never executes old test_driver75p.py. No git write.
Evidence under reports/v1615-r1/driver75p-r3 (old logs kept; new runs append/sibling).

Formal positive does NOT patch release_binding_status: binds a product-built
release_candidate under a controlled TMPDIR. Narrow patched case remains separate.

Run: python3.14 -B -m unittest test_75v_review
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import release_context as rc
import nodes as nodes_mod
import review as review_mod
from test_75s_nodes_evidence import (
    QualifiedNodesFixture,
    _sha,
    _write_json,
    make_live_db,
    _node_source,
)

HERE = Path(__file__).resolve().parent
REAL_ROOT = HERE.parents[2]
REPORTS = REAL_ROOT / 'reports' / 'v1615-r1' / 'driver75p-r3'
EVIDENCE_ROOT = REPORTS / 'fixtures'
PERSIST = REPORTS / 'evidence'
CONTROLLED_TMP = REPORTS / 'controlled-tmp'


def tempfile_mkdtemp(parent: Path, prefix: str) -> Path:
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))


def _enrich_state_for_review(fx: QualifiedNodesFixture, *, candidate_package: Path) -> None:
    prompt = fx.acceptance / 'review-card49.txt'
    prompt.write_text(
        '你是小八 AgentOS 的独立审查者之一。只读审查本轮发布材料，给出 FINAL_ACCEPTANCE。\n',
        encoding='utf-8',
    )
    diff_name = f'{fx.diff_base[:7]}..BASE.diff'
    diff_file = fx.acceptance / diff_name
    if not diff_file.is_file():
        diff_file.write_text('fixture diff for 75V review isolation\n', encoding='utf-8')
    state = json.loads(fx.state_path.read_text(encoding='utf-8'))
    state.update({
        'prompt': str(prompt),
        'prompt_sha256': _sha(prompt),
        'diff': str(diff_file),
        'diff_sha256': _sha(diff_file),
        'tasks': {},
        'freeze_sha256': _sha(fx.freeze_path),
        'candidate_package': str(candidate_package),
    })
    _write_json(fx.state_path, state)
    fx.prep['diff_sha256'] = _sha(diff_file)
    _write_json(fx.prep_path, fx.prep)
    _write_json(fx.base_ready_path, {
        'base': fx.base, 'round': fx.round,
        'preparation_sha256': _sha(fx.prep_path),
    })


class Test75VReview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        PERSIST.mkdir(parents=True, exist_ok=True)
        CONTROLLED_TMP.mkdir(parents=True, exist_ok=True)
        cls.base_ctx = rc.load_context(HERE.parent)
        cls.shared_root = Path(tempfile_mkdtemp(EVIDENCE_ROOT, 'qualified-shared-'))
        cls.fx = QualifiedNodesFixture(cls.shared_root, cls.base_ctx)
        # One product-helper release_candidate shared by all cases (not hand-forged RELEASE).
        cls.package = review_mod.build_isolation_release_candidate(
            cls.fx.candidate,
            cls.shared_root / 'release-candidate' / cls.fx.version,
            cls.fx.version,
            expected_manifest_sha256=cls.fx.manifest_sha,
        )
        _enrich_state_for_review(cls.fx, candidate_package=cls.package)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared_root, ignore_errors=True)

    def setUp(self):
        self._old_tmpdir = os.environ.get('TMPDIR')
        self.factory = review_mod.default_pass_resource_factory()

    def tearDown(self):
        if self._old_tmpdir is None:
            os.environ.pop('TMPDIR', None)
        else:
            os.environ['TMPDIR'] = self._old_tmpdir
        if hasattr(self, 'root'):
            shutil.rmtree(self.root, ignore_errors=True)

    def _enter_controlled_tmpdir(self):
        self.root = Path(tempfile_mkdtemp(CONTROLLED_TMP, 'review-'))
        os.environ['TMPDIR'] = str(self.root)
        # Force tempfile module to re-evaluate (CPython caches gettempdir).
        tempfile.tempdir = None
        self.assertEqual(Path(tempfile.gettempdir()).resolve(), self.root.resolve())

    def _fixture_live(self, ids, *, same_model=False):
        live = self.root / 'fixture-live'
        model_a = 'Fixture Model Shared' if same_model else 'Fixture Model A'
        model_b = 'Fixture Model Shared' if same_model else 'Fixture Model B'
        entry_a = '/Users/suzhizhou/.local/bin/agy'
        entry_b = entry_a if same_model else '/Users/suzhizhou/.local/bin/qodercli'
        if same_model:
            make_live_db(live / '.runtime' / 'agentos.sqlite3', [
                (ids[0], _node_source(ids[0], 'peer', 'Fixture A', entry_a, model_a, 'agy')),
                (ids[1], _node_source(ids[1], 'both', 'Fixture B', entry_b, model_b, 'agy')),
            ])
        else:
            make_live_db(live / '.runtime' / 'agentos.sqlite3', [
                (ids[0], _node_source(ids[0], 'peer', 'Fixture AGY', entry_a, model_a)),
                (ids[1], _node_source(ids[1], 'both', 'Fixture Qoder', entry_b, model_b,
                                      'qoder', 'xhigh')),
            ])
        return live

    def _prepare(self, *, same_model=False, node_ids=None, under_tmpdir=True):
        if under_tmpdir or not hasattr(self, 'root'):
            self._enter_controlled_tmpdir()
        ids = node_ids or ['fixture-agy-node', 'fixture-qoder-node']
        live = self._fixture_live(ids, same_model=same_model)
        ctx = self.fx.ctx(live=live, node_ids=ids)
        case_acceptance = self.root / 'acceptance'
        case_reports = self.root / 'reports'
        case_acceptance.mkdir(parents=True)
        case_reports.mkdir(parents=True)
        shutil.copytree(self.fx.reports / 'prepare', case_reports / 'prepare')
        shutil.copy2(self.fx.freeze_path, case_acceptance / 'FREEZE.json')
        state = json.loads(self.fx.state_path.read_text(encoding='utf-8'))
        prompt_src = Path(state['prompt'])
        diff_src = Path(state['diff'])
        prompt = case_acceptance / prompt_src.name
        diff = case_acceptance / diff_src.name
        shutil.copy2(prompt_src, prompt)
        shutil.copy2(diff_src, diff)
        state['prompt'] = str(prompt)
        state['prompt_sha256'] = _sha(prompt)
        state['diff'] = str(diff)
        state['diff_sha256'] = _sha(diff)
        state['freeze_sha256'] = _sha(case_acceptance / 'FREEZE.json')
        state['tasks'] = {}
        state['candidate_package'] = str(self.package)
        _write_json(case_acceptance / 'STATE.json', state)
        ctx = dict(ctx)
        ctx['acceptance'] = case_acceptance
        ctx['reports'] = case_reports
        evidence_dir = case_reports / 'nodes'
        nodes_report = nodes_mod.prepare_nodes(
            ctx, evidence_dir=evidence_dir, resource_factory=self.factory)
        self.assertTrue(nodes_report['node_tests_passed'])
        # Nodes site must sit under controlled TMPDIR for formal unpatched binding.
        self.assertTrue(
            Path(nodes_report['isolated_root']).resolve().is_relative_to(self.root.resolve())
        )
        return ctx, nodes_report, evidence_dir

    def _persist(self, name: str, path: Path) -> None:
        shutil.copy2(path, PERSIST / name)

    def test_positive_formal_release_binding_unpatched(self):
        """Formal entry: real release_binding_status (no patch) + Orchestrator + validator."""
        ctx, nodes_report, evidence_dir = self._prepare()
        result = review_mod.run_review(
            ctx,
            nodes_evidence_dir=evidence_dir,
            resource_factory=self.factory,
            patch_release_binding=False,
        )
        self.assertTrue(result['ok'])
        self.assertFalse(result['isolation']['release_binding_patched'])
        self.assertTrue(result['isolation']['package_bind']['release_binding_status']['ok'])
        self.assertGreaterEqual(result['passed_votes'], 2)
        self.assertEqual(result['failed_votes'], 0)
        self.assertGreaterEqual(len(set(result['distinct_agents'])), 2)
        self.assertEqual(result['official']['candidate_status'], 'audit_only')
        state = json.loads((Path(ctx['acceptance']) / 'STATE.json').read_text(encoding='utf-8'))
        self.assertTrue(state['acceptance_passed'])
        self.assertEqual(state['task_id'], result['task_id'])
        self.assertEqual(state['tasks']['review'], result['task_id'])
        self.assertEqual(Path(state['database']).resolve(),
                         Path(nodes_report['isolated_database']).resolve())
        self.assertEqual(Path(state['config']).resolve(),
                         Path(nodes_report['isolated_config']).resolve())
        review_dir = Path(ctx['reports']) / 'review'
        binding = json.loads((review_dir / '11-release-binding.json').read_text(encoding='utf-8'))
        self.assertTrue(binding['release_binding_status']['ok'])
        incoming_files = Path(state['candidate_package']) / 'FILES.sha256'
        bound_files = Path(binding['bound_package']) / 'FILES.sha256'
        self.assertEqual(incoming_files.read_bytes(), bound_files.read_bytes())
        self.assertEqual(binding['files_manifest_sha256'], binding['incoming_files_manifest_sha256'])
        self.assertTrue(binding['files_bytes_preserved'])
        self.assertTrue((review_dir / '11-package-verify.json').is_file())
        self._persist('review-formal-acceptance.json', review_dir / '11-acceptance.json')
        self._persist('review-formal-orchestrator.json', review_dir / '11-orchestrator.json')
        self._persist('review-formal-release-binding.json', review_dir / '11-release-binding.json')
        self._persist('review-formal-package-verify.json', review_dir / '11-package-verify.json')

    def test_positive_patched_release_binding_narrow_contract(self):
        """Narrow contract only — must not be claimed as formal entry closed."""
        ctx, nodes_report, evidence_dir = self._prepare()
        result = review_mod.run_review(
            ctx,
            nodes_evidence_dir=evidence_dir,
            resource_factory=self.factory,
            patch_release_binding=True,
        )
        self.assertTrue(result['ok'])
        self.assertTrue(result['isolation']['release_binding_patched'])
        self.assertGreaterEqual(result['passed_votes'], 2)
        self.assertEqual(set(result['official']['successful_agents']),
                         set(nodes_report['review_node_ids']))
        self._persist('review-patched-narrow-acceptance.json',
                      Path(ctx['reports']) / 'review' / '11-acceptance.json')

    def test_resume_does_not_resubmit(self):
        ctx, _nodes_report, evidence_dir = self._prepare()
        first = review_mod.run_review(
            ctx, nodes_evidence_dir=evidence_dir, resource_factory=self.factory,
            patch_release_binding=False)
        self.assertTrue(first['ok'])
        second = review_mod.run_review(
            ctx, nodes_evidence_dir=evidence_dir, resource_factory=self.factory,
            patch_release_binding=False)
        self.assertTrue(second['ok'])
        self.assertEqual(second['task_id'], first['task_id'])
        self.assertTrue(second['submit'].get('resumed'))
        self.assertGreaterEqual(second['passed_votes'], 2)

    def test_negative_missing_nodes_prepared(self):
        ctx, _nodes_report, _evidence_dir = self._prepare()
        missing_dir = self.root / 'no-nodes-evidence'
        missing_dir.mkdir()
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx, nodes_evidence_dir=missing_dir, resource_factory=self.factory,
                patch_release_binding=False)
        self.assertIn('nodes-prepared.json missing', str(caught.exception).lower())

    def test_negative_prompt_hash_drift(self):
        ctx, _nodes_report, evidence_dir = self._prepare()
        state_path = Path(ctx['acceptance']) / 'STATE.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state['prompt_sha256'] = '0' * 64
        _write_json(state_path, state)
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx, nodes_evidence_dir=evidence_dir, resource_factory=self.factory,
                patch_release_binding=False)
        self.assertIn('prompt', str(caught.exception).lower())

    def test_negative_missing_candidate_package(self):
        ctx, _nodes_report, evidence_dir = self._prepare()
        state_path = Path(ctx['acceptance']) / 'STATE.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state.pop('candidate_package', None)
        _write_json(state_path, state)
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx, nodes_evidence_dir=evidence_dir, resource_factory=self.factory,
                patch_release_binding=False)
        self.assertIn('candidate_package', str(caught.exception))

    def test_negative_fail_vote_stops(self):
        ctx, nodes_report, evidence_dir = self._prepare()
        fail_id = nodes_report['review_node_ids'][0]
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx,
                nodes_evidence_dir=evidence_dir,
                resource_factory=review_mod.default_pass_resource_factory(
                    fail_body_by_node={
                        fail_id: (
                            '证据引用：task:x\n审查结论：FAIL\n'
                            'FINAL_ACCEPTANCE: FAIL\n发现阻断缺陷\n'
                        ),
                    },
                ),
                patch_release_binding=False,
            )
        msg = str(caught.exception)
        self.assertTrue(
            'validate_acceptance_database' in msg
            or 'incomplete' in msg
            or 'rejected' in msg
            or 'FAIL' in msg
            or '不足' in msg
            or 'PASS' in msg,
            msg,
        )
        self.assertTrue((Path(ctx['reports']) / 'review' / '11-acceptance.json').is_file())

    def test_negative_same_entry_model_not_independent(self):
        ctx, _nodes_report, evidence_dir = self._prepare(same_model=True)
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx,
                nodes_evidence_dir=evidence_dir,
                resource_factory=self.factory,
                patch_release_binding=False,
            )
        msg = str(caught.exception)
        self.assertTrue(
            '独立' in msg
            or 'incomplete' in msg
            or 'validate_acceptance_database' in msg
            or '不派' in msg
            or '入口' in msg,
            msg,
        )

    def test_negative_duplicate_claim_marker(self):
        ctx, _nodes_report, evidence_dir = self._prepare()
        marker = Path(ctx['acceptance']) / 'SUBMIT.started'
        marker.write_text('claimed\n', encoding='utf-8')
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx, nodes_evidence_dir=evidence_dir, resource_factory=self.factory,
                patch_release_binding=False)
        self.assertIn('SUBMIT.started', str(caught.exception))

    def test_negative_missing_material(self):
        ctx, _nodes_report, evidence_dir = self._prepare()
        state_path = Path(ctx['acceptance']) / 'STATE.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        missing = Path(ctx['acceptance']) / 'missing-prompt.txt'
        state['prompt'] = str(missing)
        state['prompt_sha256'] = 'a' * 64
        _write_json(state_path, state)
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx, nodes_evidence_dir=evidence_dir, resource_factory=self.factory,
                patch_release_binding=False)
        self.assertIn('missing', str(caught.exception).lower())

    def test_negative_empty_index_reproduces_manager_gap_without_package(self):
        """Manager reproduction: empty index + no package bind → release_binding false."""
        self._enter_controlled_tmpdir()
        # Use nodes site then strip package bind: call product release_binding on empty index.
        ctx, nodes_report, _evidence_dir = self._prepare(under_tmpdir=False)
        prod = nodes_mod.load_product(ctx)
        from xiaoba_agentos.config import AppConfig
        from xiaoba_agentos.release import release_binding_status
        cfg = AppConfig.load(Path(nodes_report['isolated_config']))
        status = release_binding_status(cfg)
        self.assertFalse(status.get('ok'))
        problems = ' '.join(status.get('problems') or [])
        self.assertIn('exactly one', problems)

    def test_negative_tampered_package_script_rejected_before_submit(self):
        """r3 counterexample: build_release bytes change, FILES kept → refuse before SUBMIT/load."""
        ctx, nodes_report, evidence_dir = self._prepare()
        bad_root = self.root / 'bad-incoming' / self.fx.version
        shutil.copytree(self.package, bad_root)
        files_path = bad_root / 'FILES.sha256'
        files_before = files_path.read_bytes()
        files_sha_before = _sha(files_path)
        br = bad_root / 'agentos' / 'scripts' / 'build_release.py'
        br.write_text(
            br.read_text(encoding='utf-8')
            + '\nfrom pathlib import Path as _ProbePath\n'
            + "_ProbePath(__file__).with_name('UNVERIFIED_EXECUTED')"
            + ".write_text('card-local harmless marker')\n",
            encoding='utf-8',
        )
        self.assertEqual(files_path.read_bytes(), files_before)
        state_path = Path(ctx['acceptance']) / 'STATE.json'
        state = json.loads(state_path.read_text(encoding='utf-8'))
        state['candidate_package'] = str(bad_root)
        _write_json(state_path, state)
        with self.assertRaises(SystemExit) as caught:
            review_mod.run_review(
                ctx,
                nodes_evidence_dir=evidence_dir,
                resource_factory=self.factory,
                patch_release_binding=False,
            )
        msg = str(caught.exception)
        self.assertIn('verify_package', msg)
        self.assertIn('before SUBMIT', msg)
        self.assertFalse((Path(ctx['acceptance']) / 'SUBMIT.started').exists())
        self.assertFalse((bad_root / 'agentos' / 'scripts' / 'UNVERIFIED_EXECUTED').exists())
        dest_marker = (
            Path(nodes_report['isolated_root']) / 'releases' / self.fx.version
            / 'agentos' / 'scripts' / 'UNVERIFIED_EXECUTED'
        )
        self.assertFalse(dest_marker.exists())
        self.assertFalse(
            (Path(nodes_report['isolated_root']) / 'releases' / self.fx.version).exists()
        )
        self.assertEqual(files_path.read_bytes(), files_before)
        self.assertEqual(_sha(files_path), files_sha_before)
        PERSIST.mkdir(parents=True, exist_ok=True)
        (PERSIST / 'review-r3-tampered-reject-msg.txt').write_text(msg + '\n', encoding='utf-8')

    def test_negative_dest_drift_kept_in_place(self):
        """Existing dest content drift: keep bytes in place and refuse overwrite."""
        ctx, nodes_report, _evidence_dir = self._prepare()
        bind_kwargs = dict(
            isolated_root=Path(nodes_report['isolated_root']),
            cfg_path=Path(nodes_report['isolated_config']),
            version=self.fx.version,
            candidate_package=self.package,
            expected_manifest_sha256=self.fx.manifest_sha,
            require_temp_layout=False,
            trusted_candidate=Path(ctx['candidate']),
        )
        first = review_mod.bind_release_candidate_package(**bind_kwargs)
        dest = Path(first['bound_package'])
        incoming_files = (self.package / 'FILES.sha256').read_bytes()
        self.assertEqual((dest / 'FILES.sha256').read_bytes(), incoming_files)
        probe = dest / 'agentos' / 'README.md'
        original = probe.read_bytes()
        probe.write_bytes(original + b'\n# dest-drift-r3\n')
        drifted = probe.read_bytes()
        with self.assertRaises(SystemExit) as caught:
            review_mod.bind_release_candidate_package(**bind_kwargs)
        msg = str(caught.exception).lower()
        self.assertTrue('kept' in msg or 'drift' in msg or 'verify_package' in msg, msg)
        self.assertEqual(probe.read_bytes(), drifted)
        self.assertEqual((dest / 'FILES.sha256').read_bytes(), incoming_files)
        self.assertNotEqual(drifted, original)

    def test_positive_same_hash_dest_reused_readonly(self):
        """Complete same-hash dest is reused without rewrite."""
        ctx, nodes_report, _evidence_dir = self._prepare()
        bind_kwargs = dict(
            isolated_root=Path(nodes_report['isolated_root']),
            cfg_path=Path(nodes_report['isolated_config']),
            version=self.fx.version,
            candidate_package=self.package,
            expected_manifest_sha256=self.fx.manifest_sha,
            require_temp_layout=False,
            trusted_candidate=Path(ctx['candidate']),
        )
        first = review_mod.bind_release_candidate_package(**bind_kwargs)
        files_bytes = (Path(first['bound_package']) / 'FILES.sha256').read_bytes()
        second = review_mod.bind_release_candidate_package(**bind_kwargs)
        self.assertTrue(second['dest_reused_readonly'])
        self.assertEqual(
            (Path(second['bound_package']) / 'FILES.sha256').read_bytes(),
            files_bytes,
        )
        self.assertEqual(
            second['files_manifest_sha256'],
            second['incoming_files_manifest_sha256'],
        )


if __name__ == '__main__':
    unittest.main()
