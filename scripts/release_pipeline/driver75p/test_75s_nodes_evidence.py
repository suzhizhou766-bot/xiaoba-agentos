"""75S isolated verification — baseline raw-log gate + real nodes preparation.

NEW test source. The old test_driver75p.py stays read-only and is NEVER executed.
No git init/config/add/commit/push anywhere (direct or indirect); only read-only
git rev-parse / cat-file / ls-tree / archive are used. All fixtures and temporary
directories live under this card's evidence directory, never system tmp.

The nodes tests construct QUALIFIED in-card inputs (a read-only git-archive export
of the assigned BASE, two small valid tool manifests within the release root,
PREP/READY/FREEZE/STATE bound to the real candidate manifest, a qualified active
config, and synthetic baseline logs shaped like real unittest output). These
synthetic logs prove the gate logic; they are NOT a real baseline run. The
strengthened write-before gate is exercised for real: missing/drifted inputs must
reject with zero marker, zero isolated pool initialization and zero CLI call.

Run with the pinned interpreter: python3.14 -B -m unittest test_75s_nodes_evidence
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_ROOT = HERE.parents[2]
REPORTS = REAL_ROOT / 'reports' / 'v1615-r1' / 'driver75p-r2'
EVIDENCE_ROOT = REPORTS / 'fixtures'
PERSIST_EVIDENCE = REPORTS / 'evidence'
# Exclusive new evidence for default-factory接续 (do not overwrite r2 logs).
FACTORY_REPORTS = REAL_ROOT / 'reports' / 'v1615-r1' / 'driver75p-nodes-factory'
FACTORY_EVIDENCE = FACTORY_REPORTS / 'fixtures'
FACTORY_PERSIST = FACTORY_REPORTS / 'evidence'

import release_context as rc
import nodes as nodes_mod
from isolated_site import build_isolated_config, open_isolated_stores


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _suite_log(suite: str, tests: int, skipped: int, *, ok: bool = True,
               ran: bool = True, failed: bool = False) -> str:
    """A realistic raw unittest log: stderr completion lines + stdout JSON line."""
    lines = []
    if ran:
        lines.append(f'Ran {tests} tests in 1.234s')
        lines.append('')
        if failed:
            lines.append('FAILED (failures=1)')
        elif ok:
            lines.append(f'OK (skipped={skipped})' if skipped else 'OK')
    lines.append(json.dumps({'suite': suite, 'tests': tests, 'skipped': skipped,
                             'errors': 0, 'failures': 1 if failed else 0, 'ok': ok and not failed},
                            ensure_ascii=False))
    return '\n'.join(lines) + '\n'


def tempfile_mkdtemp(parent: Path, prefix: str) -> Path:
    """mkdtemp rooted in the card evidence dir (never system tmp)."""
    import tempfile
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))


class BaselineEvidenceFixture:
    """Build a self-consistent fake evidence tree for the read-only gate."""

    def __init__(self, root: Path):
        self.root = root
        self.work = root / 'work'
        self.acceptance = root / 'acceptance'
        self.reports = root / 'reports'
        self.candidate = root / 'candidate'
        self.candidate.mkdir(parents=True, exist_ok=True)
        self.prep_reports = self.reports / 'prepare'
        self.prep_reports.mkdir(parents=True, exist_ok=True)
        self.expected = {
            'core': {'tests': 1666, 'skipped': 0},
            'ingress': {'tests': 406, 'skipped': 0},
            'architecture': {'tests': 41, 'skipped': 1},
            'monitor': {'tests': 311, 'skipped': 0},
        }
        self.base = 'a' * 40
        self.runner_sha = _sha(HERE / 'baseline_runner.py')
        self.manifest_file = self.candidate / 'SOURCE_MANIFEST.json'
        _write_json(self.manifest_file, {'files': {'x.py': 'c' * 64}})
        self.manifest_sha = _sha(self.manifest_file)
        self._build()

    def _build(self):
        baseline_logs = {}
        tests = {}
        files = {'core': '01-core.log', 'ingress': '02-ingress.log',
                 'architecture': '03-architecture.log', 'monitor': '04-monitor.log'}
        for suite, fname in files.items():
            t, s = self.expected[suite]['tests'], self.expected[suite]['skipped']
            log = self.prep_reports / fname
            log.write_text(_suite_log(suite, t, s), encoding='utf-8')
            baseline_logs[fname] = {
                'suite': suite, 'log_file': fname, 'tests': t, 'skipped': s, 'ok': True,
                'exit_code': 0, 'interpreter': str(rc.PIN_PATH), 'runner_sha256': self.runner_sha,
                'sha256': _sha(log),
            }
            tests[suite] = t
            tests[f'{suite}_skipped'] = s
        self.prep = {
            'round': 'v1615-r1', 'base': self.base, 'version': 'v1.6.15',
            'source_manifest_sha256': self.manifest_sha, 'baseline_logs': baseline_logs,
            'tests': tests, 'diff_sha256': 'd' * 64,
        }
        self.prep_path = self.work / 'PREPARATION.json'
        _write_json(self.prep_path, self.prep)
        self.base_ready_path = self.work / 'BASE_READY.json'
        _write_json(self.base_ready_path, {'base': self.base, 'round': 'v1615-r1',
                                           'preparation_sha256': _sha(self.prep_path)})
        self.freeze = {
            'base': self.base, 'source_manifest_sha256': self.manifest_sha, 'tests': tests,
            'test_logs': {n: r['sha256'] for n, r in baseline_logs.items()},
        }
        self.freeze_path = self.acceptance / 'FREEZE.json'
        _write_json(self.freeze_path, self.freeze)
        self.state_path = self.acceptance / 'STATE.json'
        _write_json(self.state_path, {'freeze_sha256': _sha(self.freeze_path), 'tasks': {}})

    def ctx(self):
        return {
            'work': self.work, 'acceptance': self.acceptance, 'reports': self.reports,
            'candidate': self.candidate, 'round': 'v1615-r1', 'version': 'v1.6.15',
            'assigned': {'base': self.base, 'source_manifest_sha256': self.manifest_sha},
            'state': {'expected_suites': self.expected},
        }

    def rewrite_prep(self):
        _write_json(self.prep_path, self.prep)
        _write_json(self.base_ready_path, {'base': self.base, 'round': 'v1615-r1',
                                           'preparation_sha256': _sha(self.prep_path)})
        self.freeze['test_logs'] = {n: r['sha256'] for n, r in self.prep['baseline_logs'].items()}
        self.freeze['tests'] = self.prep['tests']
        _write_json(self.freeze_path, self.freeze)
        _write_json(self.state_path, {'freeze_sha256': _sha(self.freeze_path), 'tasks': {}})


class TestBaselineEvidenceGate(unittest.TestCase):
    def setUp(self):
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile_mkdtemp(EVIDENCE_ROOT, 'evidence-'))
        self.fx = BaselineEvidenceFixture(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_positive_consistent_evidence(self):
        result = rc.validate_baseline_evidence(self.fx.ctx())
        self.assertTrue(result['ok'])
        self.assertEqual(result['total_tests'], 1666 + 406 + 41 + 311)
        self.assertEqual(result['total_skipped'], 1)
        self.assertEqual(set(result['suites']), {'core', 'ingress', 'architecture', 'monitor'})

    def test_reject_suite_count_drift(self):
        self.fx.prep['baseline_logs']['01-core.log']['tests'] = 1665
        self.fx.prep['tests']['core'] = 1665
        self.fx.rewrite_prep()
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_log_hash_drift(self):
        log = self.fx.prep_reports / '01-core.log'
        log.write_text(_suite_log('core', 1666, 0) + 'tampered\n', encoding='utf-8')
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_missing_genuine_ran_line(self):
        log = self.fx.prep_reports / '02-ingress.log'
        log.write_text(json.dumps({'suite': 'ingress', 'tests': 406, 'skipped': 0,
                                   'errors': 0, 'failures': 0, 'ok': True}) + '\n', encoding='utf-8')
        self.fx.prep['baseline_logs']['02-ingress.log']['sha256'] = _sha(log)
        self.fx.rewrite_prep()
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_failed_log(self):
        log = self.fx.prep_reports / '04-monitor.log'
        log.write_text(_suite_log('monitor', 311, 0, ok=False, failed=True), encoding='utf-8')
        self.fx.prep['baseline_logs']['04-monitor.log']['sha256'] = _sha(log)
        self.fx.rewrite_prep()
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_skipped_mismatch(self):
        log = self.fx.prep_reports / '03-architecture.log'
        log.write_text(_suite_log('architecture', 41, 0), encoding='utf-8')
        self.fx.prep['baseline_logs']['03-architecture.log']['sha256'] = _sha(log)
        self.fx.rewrite_prep()
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_missing_suite(self):
        del self.fx.prep['baseline_logs']['04-monitor.log']
        self.fx.rewrite_prep()
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_manifest_mismatch(self):
        _write_json(self.fx.manifest_file, {'files': {'y.py': 'e' * 64}})
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_freeze_tests_mismatch(self):
        self.fx.freeze['tests'] = dict(self.fx.prep['tests'], core=1)
        _write_json(self.fx.freeze_path, self.fx.freeze)
        _write_json(self.fx.state_path, {'freeze_sha256': _sha(self.fx.freeze_path), 'tasks': {}})
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_missing_exit_code(self):
        del self.fx.prep['baseline_logs']['01-core.log']['exit_code']
        self.fx.rewrite_prep()
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_runner_hash_drift(self):
        self.fx.prep['baseline_logs']['01-core.log']['runner_sha256'] = 'f' * 64
        self.fx.rewrite_prep()
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())

    def test_reject_base_ready_binding_drift(self):
        _write_json(self.fx.base_ready_path, {'base': self.fx.base, 'round': 'v1615-r1',
                                              'preparation_sha256': '0' * 64})
        with self.assertRaises(SystemExit):
            rc.validate_baseline_evidence(self.fx.ctx())


class StubResource:
    """Fake heavy CLI for isolated test_run; real NodeRuntime machinery stays."""
    supports_resource_plan = True
    persistent_resource = False
    test_budget_seconds = 60
    pending_action = None

    def __init__(self, source, workspace_root=None, *, fail=False):
        self.kind = source['kind']
        self.source_ref = source['source_ref']
        self.node = source['node']
        self.workspace = None
        self._fail = fail

    def resource_identity(self):
        return {'protocol': 'cli-worklog-v1',
                'agent_type': self.node.get('agent_type') or 'other',
                'implementation_sha256': '0' * 64, 'workspace': 'isolated-stub'}

    def test_resource(self, plan, challenge):
        if self._fail:
            raise ValueError('stub refusal: no challenge answer')
        return {'provider': 'stub_provider', 'route_source': 'native_cli_per_step',
                'model': plan.get('model'), 'output': challenge,
                'execution_mode': 'native_cli_per_step'}

    def retire(self):
        pass

    def close(self):
        pass


def _node_source(nid, kind, name, entry, model, agent_type=None, effort=None):
    """A live ri_sources record shaped like production (old test deliberately kept)."""
    node = {'version': 1, 'revision': 1, 'enabled': True, 'removed': False,
            'name': name, 'agent': name, 'interface': 'cli', 'entry': entry, 'model': model,
            'test': {'status': 'passed', 'test_id': 'OLD-TEST-MUST-NOT-BE-COPIED'}}
    if agent_type is not None:
        node['agent_type'] = agent_type
    if effort is not None:
        node['reasoning_effort'] = effort
    return {'reviewer_id': nid, 'kind': kind, 'source_ref': 'agent-entry:fixture', 'node': node}


def make_live_db(db_path: Path, rows) -> None:
    """Build an in-card live ri_sources database (id, data) for controlled tests."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute('CREATE TABLE ri_sources (id TEXT PRIMARY KEY, data TEXT)')
        for sql_id, source in rows:
            conn.execute('INSERT INTO ri_sources VALUES (?,?)',
                         (sql_id, json.dumps(source, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


class QualifiedNodesFixture:
    """Qualified in-card inputs so the strengthened write-before gate passes.

    Read-only git-archive export of the assigned BASE (per-blob verified), two
    small valid tool manifests within the release root, PREP/READY/FREEZE/STATE
    bound to the real candidate manifest, a qualified active-config fixture, and
    synthetic baseline logs shaped like real unittest output (NOT a real run).
    """

    def __init__(self, root: Path, base_ctx: dict):
        self.root = root
        self.repo = Path(base_ctx['repo'])
        self.real_root = Path(base_ctx['root'])
        self.real_live = Path(base_ctx['live'])
        self.real_record = base_ctx['record']
        self.real_parameters = base_ctx['parameters']
        self.real_previous_package = base_ctx['previous_package']
        self.real_node_ids = list(base_ctx['state']['review_node_ids'])
        self.base = base_ctx['assigned']['base']
        self.diff_base = base_ctx['assigned']['diff_base']
        self.version = base_ctx['version']
        self.round = base_ctx['round']
        self.expected = base_ctx['state']['expected_suites']
        self.source = root / 'source'
        self.candidate = self.source / 'development' / 'integrated-candidate'
        self.work = root / 'work'
        self.acceptance = root / 'acceptance'
        self.reports = root / 'reports'
        self.runner_sha = _sha(HERE / 'baseline_runner.py')
        self._export_source()
        self.manifest_sha = _sha(self.candidate / 'SOURCE_MANIFEST.json')
        self._build_baseline_evidence()
        self._build_active_config()
        self._build_manifests()

    def _export_source(self):
        self.source.mkdir(parents=True, exist_ok=True)
        archive = subprocess.check_output(
            ['git', '-C', str(self.repo), 'archive', self.base, '--',
             'development', 'monitor'])
        with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as tf:
            tf.extractall(self.source, filter='data')

    def _build_baseline_evidence(self):
        prep_reports = self.reports / 'prepare'
        prep_reports.mkdir(parents=True, exist_ok=True)
        files = {'core': '01-core.log', 'ingress': '02-ingress.log',
                 'architecture': '03-architecture.log', 'monitor': '04-monitor.log'}
        baseline_logs = {}
        tests = {}
        for suite, fname in files.items():
            t, s = self.expected[suite]['tests'], self.expected[suite]['skipped']
            log = prep_reports / fname
            log.write_text(_suite_log(suite, t, s), encoding='utf-8')
            baseline_logs[fname] = {
                'suite': suite, 'log_file': fname, 'tests': t, 'skipped': s, 'ok': True,
                'exit_code': 0, 'interpreter': str(rc.PIN_PATH),
                'runner_sha256': self.runner_sha, 'sha256': _sha(log),
            }
            tests[suite] = t
            tests[f'{suite}_skipped'] = s
        diff_name = f'{self.diff_base[:7]}..BASE.diff'
        diff_file = self.acceptance / diff_name
        diff_file.parent.mkdir(parents=True, exist_ok=True)
        diff_file.write_text('fixture diff for isolated gate verification\n', encoding='utf-8')
        self.prep = {
            'round': self.round, 'base': self.base, 'version': self.version,
            'source_manifest_sha256': self.manifest_sha, 'baseline_logs': baseline_logs,
            'tests': tests, 'diff_sha256': _sha(diff_file),
        }
        self.prep_path = self.work / 'PREPARATION.json'
        _write_json(self.prep_path, self.prep)
        self.base_ready_path = self.work / 'BASE_READY.json'
        _write_json(self.base_ready_path, {'base': self.base, 'round': self.round,
                                           'preparation_sha256': _sha(self.prep_path)})
        self.freeze = {
            'base': self.base, 'source_manifest_sha256': self.manifest_sha, 'tests': tests,
            'test_logs': {n: r['sha256'] for n, r in baseline_logs.items()},
        }
        self.freeze_path = self.acceptance / 'FREEZE.json'
        _write_json(self.freeze_path, self.freeze)
        self.state_path = self.acceptance / 'STATE.json'
        _write_json(self.state_path, {'freeze_sha256': _sha(self.freeze_path), 'tasks': {}})

    def _build_active_config(self):
        self.active_config = self.root / 'active-config.json'
        _write_json(self.active_config, {
            'schema_version': 2,
            'note': 'isolated fixture active config for B09 binding; not the real live config',
            'release': {'expected_version': self.version},
        })
        self.active_config_sha = _sha(self.active_config)

    def _build_manifests(self):
        mdir = EVIDENCE_ROOT / 'manifests'
        mdir.mkdir(parents=True, exist_ok=True)
        rel = lambda name: f'work/v1615-r1/driver75p/{name}'
        group_a = ['nodes.py', 'release_context.py', 'verify_export.py']
        group_b = ['isolated_site.py', 'baseline_runner.py', 'cli.py']
        self.manifest_a = mdir / 'fixture-tool-hashes-A.json'
        self.manifest_b = mdir / 'fixture-tool-hashes-B.json'
        _write_json(self.manifest_a, {'tools': {rel(n): _sha(HERE / n) for n in group_a}})
        _write_json(self.manifest_b, {'tools': {rel(n): _sha(HERE / n) for n in group_b}})

    def ctx(self, *, live=None, node_ids=None, state_overrides=None):
        state = {
            'review_node_ids': list(node_ids) if node_ids is not None else list(self.real_node_ids),
            'expected_suites': self.expected,
            'tool_manifests': [str(self.manifest_a), str(self.manifest_b)],
            'live_config_path': str(self.active_config),
            'live_config_sha256_before': self.active_config_sha,
        }
        if state_overrides:
            state.update(state_overrides)
        return {
            'root': self.real_root, 'work': self.work,
            'live': Path(live) if live is not None else self.real_live,
            'repo': self.repo, 'source': self.source, 'candidate': self.candidate,
            'acceptance': self.acceptance, 'reports': self.reports,
            'previous_package': self.real_previous_package,
            'round': self.round, 'version': self.version,
            'record': self.real_record,
            'assigned': {'base': self.base, 'diff_base': self.diff_base,
                         'source_manifest_sha256': self.manifest_sha},
            'parameters': self.real_parameters, 'state': state,
        }


class TestNodesPreparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
        cls.base_ctx = rc.load_context(HERE.parent)
        cls.shared_root = Path(tempfile_mkdtemp(EVIDENCE_ROOT, 'qualified-shared-'))
        cls.fx = QualifiedNodesFixture(cls.shared_root, cls.base_ctx)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.shared_root, ignore_errors=True)

    def setUp(self):
        self.root = Path(tempfile_mkdtemp(EVIDENCE_ROOT, 'nodes-'))
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def factory(self, source):
        self.calls.append(source.get('reviewer_id'))
        return StubResource(source)

    def assert_zero_side_effects(self, evidence_dir):
        self.assertFalse(evidence_dir.exists(),
                         f'evidence dir created despite rejection: {evidence_dir}')
        self.assertEqual(self.calls, [],
                         'resource_factory invoked despite rejection (a real CLI would have run)')

    def _persist(self, name, src):
        PERSIST_EVIDENCE.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, PERSIST_EVIDENCE / name)

    # --- positives: two genuine passed tests this round -----------------------
    def test_positive_real_production_nodes_two_passed(self):
        ctx = self.fx.ctx()  # real live ri_sources, manager-selected node IDs
        ev = self.root / 'pos-real'
        report = nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assertTrue(report['node_tests_passed'])
        self.assertEqual(len(report['nodes']), 2)
        self.assertEqual(report['review_node_ids'], ctx['state']['review_node_ids'])
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertEqual(set(self.calls), set(report['review_node_ids']))
        for node in report['nodes']:
            self.assertFalse(node['old_test_copied'])
            self.assertEqual(node['test_status'], 'passed')
            self.assertTrue(node['model'])
            self.assertTrue(node['spec_digest'])
            self.assertTrue(node['runner_digest'])
        for nid, ready in report['readiness'].items():
            self.assertTrue(ready['ready'], f'{nid} not ready: {ready}')
        self.assertTrue(report['preflight']['baseline_evidence']['ok'])
        self.assertEqual(report['live_config_sha256_before'], report['live_config_sha256_actual'])
        self.assertFalse(report['old_test_copied_as_new'])
        self.assertTrue(report['no_production_mutation'])
        self.assertTrue((ev / 'nodes-prepared.json').is_file())
        self.assertTrue((ev / 'NODE_TESTS.started').is_dir())
        self._persist('nodes-positive-real.json', ev / 'nodes-prepared.json')

    def test_positive_fixture_live_db_two_passed(self):
        live = self.root / 'fixture-live'
        ids = ['fixture-agy-node', 'fixture-qoder-node']
        make_live_db(live / '.runtime' / 'agentos.sqlite3', [
            (ids[0], _node_source(ids[0], 'peer', 'Fixture AGY',
                                  '/Users/suzhizhou/.local/bin/agy', 'Fixture Model A')),
            (ids[1], _node_source(ids[1], 'both', 'Fixture Qoder',
                                  '/Users/suzhizhou/.local/bin/qodercli', 'Fixture Model B',
                                  'qoder', 'xhigh')),
        ])
        ctx = self.fx.ctx(live=live, node_ids=ids)
        ev = self.root / 'pos-fixture'
        report = nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assertTrue(report['node_tests_passed'])
        self.assertEqual(report['review_node_ids'], ids)
        self.assertGreaterEqual(len(self.calls), 2)
        self.assertEqual(set(self.calls), set(report['review_node_ids']))
        for node in report['nodes']:
            self.assertFalse(node['old_test_copied'])
            self.assertEqual(node['test_status'], 'passed')
        self.assertTrue(report['heavy_cli_injected'])
        self._persist('nodes-positive-fixture.json', ev / 'nodes-prepared.json')

    # --- gate-rejection negatives: zero marker / zero pool / zero CLI ---------
    def test_negative_missing_manifest_zero_side_effects(self):
        ctx = self.fx.ctx(state_overrides={'tool_manifests': [str(self.fx.manifest_a)]})
        ev = self.root / 'neg-manifest'
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assert_zero_side_effects(ev)

    def test_negative_bad_source_blob_zero_side_effects(self):
        ctx = self.fx.ctx()
        target = self.fx.candidate / 'pyproject.toml'
        original = target.read_bytes()
        ev = self.root / 'neg-blob'
        try:
            target.write_bytes(original + b'\n# fixture-blob-drift\n')
            with self.assertRaises(SystemExit):
                nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
            self.assert_zero_side_effects(ev)
        finally:
            target.write_bytes(original)

    def test_negative_bad_active_config_hash_zero_side_effects(self):
        ctx = self.fx.ctx(state_overrides={'live_config_sha256_before': '0' * 64})
        ev = self.root / 'neg-config'
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assert_zero_side_effects(ev)

    def test_negative_missing_freeze_zero_side_effects(self):
        acc = self.root / 'acc-no-freeze'
        shutil.copytree(self.fx.acceptance, acc)
        (acc / 'FREEZE.json').unlink()
        ctx = self.fx.ctx()
        ctx['acceptance'] = acc
        ev = self.root / 'neg-freeze'
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assert_zero_side_effects(ev)

    def test_negative_candidate_manifest_drift_zero_side_effects(self):
        ctx = self.fx.ctx()
        ctx['assigned'] = dict(ctx['assigned'], source_manifest_sha256='0' * 64)
        ev = self.root / 'neg-manifest-drift'
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assert_zero_side_effects(ev)

    def test_negative_sql_id_mismatch_zero_side_effects(self):
        live = self.root / 'live-mismatch'
        ids = ['nid-mismatch', 'nid-ok']
        make_live_db(live / '.runtime' / 'agentos.sqlite3', [
            (ids[0], _node_source('DIFFERENT-ID', 'peer', 'X',
                                  '/Users/suzhizhou/.local/bin/agy', 'Model X')),
            (ids[1], _node_source(ids[1], 'peer', 'Y',
                                  '/Users/suzhizhou/.local/bin/agy', 'Model Y')),
        ])
        ctx = self.fx.ctx(live=live, node_ids=ids)
        ev = self.root / 'neg-id'
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assert_zero_side_effects(ev)

    def test_negative_missing_model_zero_side_effects(self):
        live = self.root / 'live-nomodel'
        ids = ['nid-nomodel', 'nid-ok2']
        make_live_db(live / '.runtime' / 'agentos.sqlite3', [
            (ids[0], _node_source(ids[0], 'peer', 'X',
                                  '/Users/suzhizhou/.local/bin/agy', '')),
            (ids[1], _node_source(ids[1], 'peer', 'Y',
                                  '/Users/suzhizhou/.local/bin/agy', 'Model Y')),
        ])
        ctx = self.fx.ctx(live=live, node_ids=ids)
        ev = self.root / 'neg-model'
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assert_zero_side_effects(ev)

    def test_negative_invalid_manager_ids_zero_side_effects(self):
        ctx = self.fx.ctx(node_ids=['only-one'])
        ev = self.root / 'bad-ids'
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assert_zero_side_effects(ev)

    # --- real test failure preserves original text; single claim --------------
    def test_failure_preserves_original_no_retry(self):
        ctx = self.fx.ctx()
        ev = self.root / 'neg-fail'

        def fail_factory(source):
            self.calls.append(source.get('reviewer_id'))
            return StubResource(source, fail=True)

        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=fail_factory)
        report = json.loads((ev / 'nodes-prepared.json').read_text(encoding='utf-8'))
        self.assertFalse(report['node_tests_passed'])
        for node in report['nodes']:
            self.assertEqual(node['test_status'], 'failed')
        for nid, test in report['tests'].items():
            self.assertEqual(test['status'], 'failed')
            self.assertIsNone(test.get('execution_evidence'))
        self._persist('nodes-failure-preserves.json', ev / 'nodes-prepared.json')

    def test_single_claim_marker_blocks_rerun(self):
        ctx = self.fx.ctx()
        ev = self.root / 'claim'
        nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        with self.assertRaises(SystemExit):
            nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)

    def test_default_none_trusted_factory_mock_turn_boundary(self):
        """Default resource_factory=None wires run.py-shaped trusted factory.

        External I/O is mocked only at CliWorklogResource._turn. BackendRegistry /
        NodeRuntime / json_stdio_factory / resource_identity stay real. Fake turn
        output is labeled mock_turn_boundary and is not a real node CLI result.
        """
        from unittest.mock import patch

        FACTORY_EVIDENCE.mkdir(parents=True, exist_ok=True)
        FACTORY_PERSIST.mkdir(parents=True, exist_ok=True)
        case_root = Path(tempfile_mkdtemp(FACTORY_EVIDENCE, 'default-factory-'))
        try:
            live = case_root / 'live'
            ids = ['fixture-agy-node', 'fixture-qoder-node']
            entry_a = '/Users/suzhizhou/.local/bin/agy'
            entry_b = '/Users/suzhizhou/.local/bin/qodercli'
            model_a = 'Fixture Model A'
            model_b = 'Fixture Model B'
            make_live_db(live / '.runtime' / 'agentos.sqlite3', [
                (ids[0], _node_source(ids[0], 'peer', 'Fixture AGY', entry_a, model_a)),
                (ids[1], _node_source(ids[1], 'both', 'Fixture Qoder', entry_b, model_b,
                                      'qoder', 'xhigh')),
            ])
            ctx = self.fx.ctx(live=live, node_ids=ids)
            ev = case_root / 'evidence'
            turn_calls: list[dict] = []

            def mock_turn(self, instruction, context, timeout=None, kind='review',
                          record_id=None, step_code=None, task_id=None, health_only=False):
                materials = json.loads(context)
                challenge = materials.get('challenge')
                identity = self.resource_identity()
                turn_calls.append({
                    'model': self.model,
                    'entry': self.entry,
                    'agent_type': self.agent_type,
                    'identity': identity,
                    'external_io': 'mock_turn_boundary',
                })
                value = {
                    'status': 'completed',
                    'body': challenge,
                    'worklog': 'mock_turn_boundary_not_real_cli',
                }
                evidence = {
                    'provider': 'mock_turn_boundary',
                    'route_source': 'native_cli_per_step',
                    'model': self.model,
                    'execution_mode': 'native_cli_per_step',
                }
                return value, evidence

            # Import after product path is loadable via fixture candidate/ingress.
            nodes_mod.load_product(ctx)
            from xiaoba_review.resource_cli import CliWorklogResource

            with patch.object(CliWorklogResource, '_turn', mock_turn):
                report = nodes_mod.prepare_nodes(ctx, evidence_dir=ev)

            self.assertTrue(report['node_tests_passed'])
            self.assertFalse(report['heavy_cli_injected'])
            self.assertTrue(report['default_trusted_factory'])
            self.assertEqual(report['bound_workspace_root'], report['workspace_root'])
            self.assertEqual(len(turn_calls), 2)

            by_entry = {c['entry']: c for c in turn_calls}
            self.assertEqual(by_entry[entry_a]['model'], model_a)
            self.assertEqual(by_entry[entry_b]['model'], model_b)
            self.assertEqual(by_entry[entry_b]['agent_type'], 'qoder')

            nodes_by_id = {n['node_id']: n for n in report['nodes']}
            self.assertEqual(nodes_by_id[ids[0]]['model'], model_a)
            self.assertEqual(nodes_by_id[ids[0]]['entry'], entry_a)
            self.assertIsNone(nodes_by_id[ids[0]].get('reasoning_effort'))
            self.assertEqual(nodes_by_id[ids[1]]['model'], model_b)
            self.assertEqual(nodes_by_id[ids[1]]['entry'], entry_b)
            self.assertEqual(nodes_by_id[ids[1]]['reasoning_effort'], 'xhigh')

            bound = report['workspace_root']
            for call in turn_calls:
                identity = call['identity']
                self.assertEqual(identity.get('protocol'), 'cli-worklog-v1')
                self.assertNotEqual(identity.get('implementation_sha256'), '0' * 64)
                self.assertNotEqual(identity.get('workspace'), 'isolated-stub')
                self.assertTrue(str(identity.get('workspace') or '').startswith(bound))

            # Same DB bound event review Orchestrator / pool_dispatch would read.
            conn = sqlite3.connect(
                f"file:{report['isolated_database']}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT payload_json FROM events "
                    "WHERE event_type='agent_pool.workspace_bound' "
                    "ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(json.loads(row[0])['root'], bound)

            # Absolute-entry readiness via product pool_dispatch (not stub factory).
            prod = nodes_mod.load_product(ctx)
            cfg, native, review = open_isolated_stores(
                Path(report['isolated_config']),
                workspace_root=Path(report['workspace_root']),
                outbox_root=Path(report['isolated_root']) / 'outbox',
            )
            self.assertEqual(str(review.workspace_root), bound)
            for nid in ids:
                with native.read_only() as db:
                    source = review.source(db, nid)
                qual = prod['pool_dispatch'].node_readiness(source, native)
                self.assertTrue(qual.get('ready'), f'{nid} pool_dispatch readiness={qual}')
                self.assertEqual(qual.get('reason'), 'tested')

            for nid, test in report['tests'].items():
                self.assertEqual(test.get('status'), 'passed')
                evidence = test.get('execution_evidence') or {}
                self.assertEqual(
                    evidence.get('provider'), 'mock_turn_boundary',
                    'external I/O must stay labeled mock; not a real CLI result',
                )

            shutil.copyfile(ev / 'nodes-prepared.json',
                            FACTORY_PERSIST / 'nodes-default-factory-mock-turn.json')
        finally:
            shutil.rmtree(case_root, ignore_errors=True)

    def test_explicit_stub_injection_contract_unchanged(self):
        """Explicit resource_factory still overrides the trusted default."""
        live = self.root / 'stub-live'
        ids = ['fixture-agy-node', 'fixture-qoder-node']
        make_live_db(live / '.runtime' / 'agentos.sqlite3', [
            (ids[0], _node_source(ids[0], 'peer', 'Fixture AGY',
                                  '/Users/suzhizhou/.local/bin/agy', 'Fixture Model A')),
            (ids[1], _node_source(ids[1], 'both', 'Fixture Qoder',
                                  '/Users/suzhizhou/.local/bin/qodercli', 'Fixture Model B',
                                  'qoder', 'xhigh')),
        ])
        ctx = self.fx.ctx(live=live, node_ids=ids)
        ev = self.root / 'stub-explicit'
        report = nodes_mod.prepare_nodes(ctx, evidence_dir=ev, resource_factory=self.factory)
        self.assertTrue(report['node_tests_passed'])
        self.assertTrue(report['heavy_cli_injected'])
        self.assertFalse(report.get('default_trusted_factory'))
        self.assertGreaterEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)