"""切换流程的隔离文件/数据库回归；服务是显式测试替身，不是线上验收。"""
import copy
import importlib.util
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('switch_release', ROOT / 'scripts/switch_release.py')
switch = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = switch
_spec.loader.exec_module(switch)


def write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload if isinstance(payload, bytes) else payload.encode())


def seal(package):
    source = package / 'agentos'
    write(source / 'SOURCE_MANIFEST.json', switch.encoded({'algorithm': 'sha256', 'files': switch.source_files(source)}))
    release = json.loads((package / 'RELEASE.json').read_bytes())
    release['agentos_source_manifest_sha256'] = switch.state(source / 'SOURCE_MANIFEST.json')['sha256']
    write(package / 'RELEASE.json', switch.encoded(release))
    payload = ''.join(f'{sha}  ./{rel}\n' for rel, sha in switch.files(package).items() if rel != 'FILES.sha256')
    write(package / 'FILES.sha256', payload)


class Fixture:
    """Synthetic release metadata is exclusively a unit-test fixture."""
    def __init__(self, root):
        root = root.resolve()
        self.root = root
        runtime = root / 'runtime'
        config = runtime / '.runtime/active.json'
        previous = runtime / '.runtime/releases/v9.0.0'
        package = root / 'package'
        vault = root / 'assistant/第二大脑'
        graph = runtime / '.runtime/knowlp'
        entries = (root / 'entries/agents', root / 'entries/codex')
        plist = root / 'worker.plist'
        for target, version in [(previous, 'v9.0.0'), (package, 'v9.0.1')]:
            source = target / 'agentos'
            for name in switch.SOURCE_DIRS:
                (source / name).mkdir(parents=True, exist_ok=True)
            for name in switch.SOURCE_FILES:
                write(source / name, version + '\n')
            write(source / 'src/xiaoba_agentos/runtime.py', 'VALUE = ' + repr(version) + '\n')
            write(source / 'scripts/build_knowlp_index.py',
                  "def diagnose(vault): return {'ok': True}\n"
                  "def build(vault): return {'nodes.json': {'z_format': 1, 'notes': sorted(p.read_text() for p in vault.rglob('*.md'))}}\n")
            write(source / switch.SPEC, switch.encoded({'schema_version': 1, 'steps': []}))
            write(target / 'second-brain/root.md', version + '\n')
            write(target / 'interface/SKILL.md', version + '\n')
            for name in ['xiaoba', 'verify-release']:
                write(target / 'interface/scripts' / name, '#!/usr/bin/env python3\nfrom pathlib import Path\nDEFAULT_CONFIG = Path("/unbound")\n')
            write(target / 'RELEASE.json', switch.encoded({
                'version': version, 'status': 'verified_immutable_release', 'database_schema_version': 7,
                'final_publish_requires_human': True, 'tests': {'result': 'passed'},
                'acceptance': {'end_to_end_evidence': {'successful_agents': ['agy_review', 'dsh_review']}}}))
            seal(target)
        new_spec = {'schema_version': 1, 'steps': [],
                    'from_source_manifest_sha256': switch.state(previous / 'agentos/SOURCE_MANIFEST.json')['sha256']}
        write(package / 'agentos' / switch.SPEC, switch.encoded(new_spec))
        seal(package)
        for child in (previous / 'agentos').iterdir():
            if child.is_dir():
                shutil.copytree(child, runtime / child.name)
            else:
                shutil.copy2(child, runtime / child.name)
        write(config, switch.encoded({'project_dir': str(runtime), 'runtime_dir': str(runtime / '.runtime'),
              'release': {'expected_version': 'v9.0.0', 'index_path': str(runtime / '.runtime/releases/index.json')},
              'paths': {'obsidian_vault': str(vault), 'knowlp_graph_dir': str(graph)},
              'memory': {'obsidian_vault': str(vault)}, 'private_provider_marker': 'must-remain-unchanged'}))
        os.chmod(config, 0o600)
        index = {'current_release': 'v9.0.0', 'releases': [{
                 'version': 'v9.0.0', 'path': 'v9.0.0', 'status': 'verified_immutable',
                 'files_manifest_sha256': switch.state(previous / 'FILES.sha256')['sha256']}],
                 'unrelated_index_field': 'preserve'}
        write(runtime / '.runtime/releases/index.json', switch.encoded(index))
        write(vault / 'root.md', (previous / 'second-brain/root.md').read_bytes())
        write(vault / 'user-content.md', '用户手写内容，不属于生成树\n')
        write(graph / 'nodes.json', switch.graph_bytes({'z_format': 1, 'notes': sorted(p.read_text() for p in vault.rglob('*.md'))}))
        for entry in entries:
            for name in ['xiaoba', 'verify-release']:
                template = (previous / 'interface/scripts' / name).read_text()
                write(entry / 'scripts' / name, template.replace('/unbound', str(config)))
            write(entry / 'SKILL.md', (previous / 'interface/SKILL.md').read_bytes())
        write(plist, plistlib.dumps({'Label': switch.LABEL, 'WorkingDirectory': str(runtime),
                                    'EnvironmentVariables': {'XIAOBA_CONFIG': str(config)}}))
        db = sqlite3.connect(runtime / '.runtime/agentos.sqlite3')
        db.executescript('CREATE TABLE tasks (status TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0);'
                         'CREATE TABLE repair_jobs (status TEXT, lease_until REAL);')
        db.close()
        self.layout = switch.Layout(runtime, config, package, vault, graph, entries, plist)
        self.system = FakeSystem(self.layout)
        self.system.record_worker(100)

    def whole_tree(self):
        # Service owns worker-state; all remaining bytes/modes must be restored exactly.
        # Card 18 also retains a metadata-only recovery journal. Check its
        # terminal state separately instead of treating it as published content.
        journal = switch.recovery_path(self.layout)
        if journal.exists():
            assert switch.load(journal)['status'] in {'complete', 'rolled_back'}
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in self.root.rglob('*') if p.is_file() and p not in (self.layout.worker, journal)}

    def add_step(self):
        spec = switch.load(self.layout.package / 'agentos' / switch.SPEC)
        payload = b'reviewed shared asset\n'
        write(self.layout.package / 'agentos/docs/shared-asset.md', payload)
        step = {'id': '0001-shared-asset',
                'check': {'kind': 'absent_or_identical', 'sha256': switch.digest(payload)},
                'execute': {'kind': 'install_absent_file', 'root': 'assistant',
                            'path': 'skills/reviewed-asset/SKILL.md', 'source': 'docs/shared-asset.md'},
                'undo': {'kind': 'restore_in_memory'}}
        spec['steps'].append(step)
        write(self.layout.package / 'agentos' / switch.SPEC, switch.encoded(spec))
        seal(self.layout.package)
        return step


class FakeSystem(switch.System):
    def __init__(self, layout):
        self.layout = layout
        self.live = set()
        self.calls = []
        self.next_pid = 200
        self.fake_stop = False
        self.bad_bootstrap = False
        self.stale_start = False
        self.fail_doctor_once = False
        self.doctor_report = None   # (returncode, report) to return instead of a passing doctor
        self.doctor_by_version = {}  # current release -> (returncode, report), checked first

    def doctor(self, argv):
        self.calls.append(argv)
        if self.fail_doctor_once:
            self.fail_doctor_once = False
            raise switch.SwitchError('doctor failed')
        if self.doctor_by_version:
            current = switch.load(self.layout.index)['current_release']
            if current in self.doctor_by_version:
                return copy.deepcopy(self.doctor_by_version[current])
        return self.doctor_report or (0, {'ok': True})

    def record_worker(self, pid):
        self.live.add(pid)
        write(self.layout.worker, switch.encoded({'pid': pid, 'started_at': 1000.0,
              'code_fingerprint': switch.code_fingerprint(self.layout.runtime),
              'manifest_fingerprint': switch.state(self.layout.runtime / 'SOURCE_MANIFEST.json')['sha256']}))

    def alive(self, pid):
        return pid in self.live

    def process(self, pid):
        switch.require(self.alive(pid), 'PID does not exist')
        return f'python -m xiaoba_agentos.cli --config {self.layout.config} run'

    def registered(self, layout):
        return bool(self.live)

    def worker_pids(self, layout):
        return sorted(self.live)

    def wait(self, predicate, reason, timeout=45):
        switch.require(predicate(), reason)

    def command(self, argv):
        self.calls.append(argv)
        if argv[:2] == ['launchctl', 'bootout']:
            if not self.fake_stop:
                self.live.clear()
            return subprocess.CompletedProcess(argv, 0, '', '')
        if argv[:2] == ['launchctl', 'bootstrap']:
            if self.bad_bootstrap:
                self.bad_bootstrap = False
                return subprocess.CompletedProcess(argv, 5, '', '')
            self.next_pid += 1
            if self.stale_start:
                self.stale_start = False
                self.record_worker(100)
            else:
                self.record_worker(self.next_pid)
            return subprocess.CompletedProcess(argv, 0, '', '')
        raise AssertionError('unexpected command: ' + repr(argv))

    def json_command(self, argv):
        # Independent verification stub inspects real fixture trees/config/index.
        self.calls.append(argv)
        layout = self.layout
        if 'doctor' in argv:
            if self.fail_doctor_once:
                self.fail_doctor_once = False
                raise switch.SwitchError('doctor failed')
            return {'ok': True}
        config = switch.load(layout.config)
        index = switch.load(layout.index)
        source_hash = switch.state(layout.runtime / 'SOURCE_MANIFEST.json')['sha256']
        matches = [r for r in index['releases'] if r['version'] == index['current_release']]
        release = switch.verify_package(layout.index.parent / matches[0]['path'])
        switch.require(config['release']['expected_version'] == index['current_release'], 'binding mismatch')
        switch.require(release['agentos_source_manifest_sha256'] == source_hash, 'source mismatch')
        return {'ok': True, 'skipped': False, 'acceptance_mode': False,
                'config_path': str(layout.config), 'current_release': index['current_release'],
                'expected_version': config['release']['expected_version'],
                'release_source_manifest_sha256': source_hash, 'runtime_source_manifest_sha256': source_hash}


class SwitchReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fx = Fixture(Path(self.temp.name))

    def ticket(self):
        value = switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(value['ok'], value)
        return value

    def run_switch(self, **kwargs):
        return switch.apply(self.fx.layout, self.ticket(), self.fx.system, now=lambda: 1000, **kwargs)

    def test_success_requires_new_worker_and_preserves_user_content(self):
        original_config = switch.load(self.fx.layout.config)
        result = self.run_switch()
        self.assertTrue(result['activated'], result)
        self.assertTrue(result['read_back']['restart_verified'])
        self.assertNotEqual(100, result['new_pid'])
        self.assertFalse(self.fx.system.alive(100))
        self.assertEqual('用户手写内容，不属于生成树\n', (self.fx.layout.vault / 'user-content.md').read_text())
        changed = switch.load(self.fx.layout.config)
        changed['release']['expected_version'] = original_config['release']['expected_version']
        self.assertEqual(original_config, changed)
        self.assertEqual('preserve', switch.load(self.fx.layout.index)['unrelated_index_field'])

    def test_successful_switch_seals_the_activated_release_read_only(self):
        result = self.run_switch()
        self.assertTrue(result['activated'], result)
        sealed = result['release_sealed']
        self.assertTrue(sealed['ok'], sealed)
        package = self.fx.layout.index.parent / result['read_back']['version']
        entries = [package, *package.rglob('*')]
        self.assertEqual(len(entries), sealed['entries'])
        self.assertEqual([], [str(p) for p in entries if p.stat().st_mode & 0o222])
        # A process can no longer drop a file (e.g. a bytecode cache) into it.
        with self.assertRaises(PermissionError):
            (package / 'agentos' / 'stray.pyc').write_bytes(b'x')

    def test_a_failed_switch_leaves_nothing_sealed_and_reports_no_seal(self):
        def fault(name):
            if name == 'readback':
                raise switch.SwitchError('deliberate failure after readback')
        result = self.run_switch(fault=fault)
        self.assertFalse(result['activated'])
        self.assertNotIn('release_sealed', result)

    def test_a_seal_or_prune_that_raises_never_unwinds_a_finished_switch(self):
        # Only cleanup after successful activation catches all exceptions.
        # In particular, Ctrl-C must never unwind the already finished switch.
        for step in ('seal_release', 'prune_empty'):
            for error in (RecursionError, KeyboardInterrupt, SystemExit, GeneratorExit):
                with self.subTest(step=step, error=error.__name__), tempfile.TemporaryDirectory() as directory:
                    self.fx = Fixture(Path(directory))
                    with patch.object(switch, step, side_effect=error('deliberate')):
                        result = self.run_switch()
                    self.assertTrue(result['activated'], result)
                    self.assertTrue(result['ok'], result)
                    self.assertNotIn('error', result)
                    self.assertIsNone(result.get('rollback'))
                    key = 'release_sealed' if step == 'seal_release' else 'removed_empty_directories'
                    self.assertFalse(result[key]['ok'], result[key])
                    self.assertIn(error.__name__, str(result[key]))

    def test_interrupt_before_success_still_restores_the_previous_release(self):
        # The readback callback runs before the activation commit boundary.
        for error in (KeyboardInterrupt, SystemExit, GeneratorExit):
            with self.subTest(error=error.__name__), tempfile.TemporaryDirectory() as directory:
                fx = Fixture(Path(directory))
                before = fx.whole_tree()
                ticket = switch.preflight(fx.layout, fx.system, now=lambda: 1000)

                def fail(phase):
                    if phase == 'readback':
                        raise error('deliberate before activation')

                result = switch.apply(fx.layout, ticket, fx.system, now=lambda: 1000, fault=fail)
                self.assertFalse(result['activated'], result)
                self.assertFalse(result['ok'], result)
                self.assertIn(error.__name__, result['error'])
                self.assertEqual('已退回', result['rollback']['status'], result)
                self.assertEqual(before, fx.whole_tree())
                self.assertTrue(switch.worker(fx.layout, fx.system))
                self.assertNotIn('release_sealed', result)

    def test_every_phase_failure_restores_bytes_modes_and_previous_worker(self):
        phases = ['stage_package', 'stop_worker', 'replace_source', 'install_entries', 'bind_config',
                  'release_steps', 'vault_merge', 'rebuild_graph', 'publish_index', 'start_worker', 'readback']
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                fx = Fixture(Path(directory))
                before = fx.whole_tree()
                ticket = switch.preflight(fx.layout, fx.system, now=lambda: 1000)
                def fail(current):
                    if current == phase:
                        raise switch.SwitchError('injected failure at ' + phase)
                result = switch.apply(fx.layout, ticket, fx.system, now=lambda: 1000, fault=fail)
                self.assertFalse(result['activated'], result)
                self.assertIn('injected failure at ' + phase, result['error'])
                self.assertEqual('已退回', result['rollback']['status'], result)
                self.assertEqual(before, fx.whole_tree())
                self.assertTrue(switch.worker(fx.layout, fx.system))

    def test_bootout_uses_service_target_and_bootstrap_checks_returncode(self):
        result = self.run_switch()
        self.assertTrue(result['ok'], result)
        bootout = next(c for c in self.fx.system.calls if c[:2] == ['launchctl', 'bootout'])
        self.assertEqual(['launchctl', 'bootout', f'gui/{os.getuid()}/{switch.LABEL}'], bootout)
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(Path(directory))
            ticket = switch.preflight(fx.layout, fx.system, now=lambda: 1000)
            fx.system.bad_bootstrap = True
            result = switch.apply(fx.layout, ticket, fx.system, now=lambda: 1000)
            self.assertFalse(result['activated'])
            self.assertIn('returncode=5', result['error'])
            self.assertEqual('已退回', result['rollback']['status'], result)

    def test_old_worker_surviving_successful_service_command_is_failure(self):
        ticket = self.ticket()
        before = self.fx.whole_tree()
        self.fx.system.fake_stop = True
        result = switch.apply(self.fx.layout, ticket, self.fx.system, now=lambda: 1000)
        self.assertFalse(result['activated'])
        self.assertIn('old worker did not exit', result['error'])
        self.assertEqual('已退回', result['rollback']['status'], result)
        self.assertEqual(before, self.fx.whole_tree())

    def test_stale_pid_after_bootstrap_cannot_pass_readiness(self):
        ticket = self.ticket()
        self.fx.system.stale_start = True
        result = switch.apply(self.fx.layout, ticket, self.fx.system, now=lambda: 1000)
        self.assertFalse(result['activated'])
        self.assertIn('new worker did not become ready', result['error'])
        self.assertEqual('已退回', result['rollback']['status'], result)

    def test_plan_preflight_and_readback_write_nothing_and_never_stop_service(self):
        before = self.fx.whole_tree()
        with patch.object(Path, 'write_bytes', side_effect=AssertionError('write')), \
             patch.object(Path, 'write_text', side_effect=AssertionError('write')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('mkdir')), \
             patch.object(Path, 'unlink', side_effect=AssertionError('unlink')), \
             patch.object(os, 'chmod', side_effect=AssertionError('chmod')), \
             patch.object(os, 'replace', side_effect=AssertionError('replace')):
            self.assertTrue(switch.plan(self.fx.layout, self.fx.system)['ok'])
            self.assertTrue(switch.preflight(self.fx.layout, self.fx.system)['ok'])
            self.assertTrue(switch.readback(self.fx.layout, self.fx.system)['ok'])
        self.assertEqual(before, self.fx.whole_tree())
        self.assertFalse(any(c[0] == 'launchctl' for c in self.fx.system.calls))

    def test_ticket_expired_tampered_or_scene_changed_never_writes(self):
        for condition in ['expired', 'tampered', 'scene']:
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as directory:
                fx = Fixture(Path(directory))
                ticket = switch.preflight(fx.layout, fx.system, now=lambda: 1000)
                clock = 1000
                if condition == 'expired':
                    clock = 1000 + switch.TTL + 1
                elif condition == 'tampered':
                    ticket['issued_at'] = 1
                else:
                    write(fx.layout.vault / 'new-user-note.md', 'new user content')
                before = fx.whole_tree()
                result = switch.apply(fx.layout, ticket, fx.system, now=lambda: clock)
                self.assertFalse(result['activated'])
                self.assertEqual(0, result['writes_performed'])
                self.assertEqual(before, fx.whole_tree())

    def _only_task(self, status, lease, attempts):
        conn = sqlite3.connect(self.fx.layout.database)
        conn.execute('DELETE FROM tasks')
        conn.execute('INSERT INTO tasks (status, lease_until, attempts) VALUES (?,?,?)', (status, lease, attempts))
        conn.commit()
        conn.close()

    def test_unfinished_frozen_tasks_and_unreleased_expired_leases_block(self):
        for status, lease, attempts in [('pending', None, 1), ('pending', 5, 0), ('retry', None, 1), ('running', 5, 1),
                                        ('frozen_paused', None, 1), ('resume_ready', None, 1), ('completed', 1, 1)]:
            with self.subTest(status=status, lease=lease, attempts=attempts):
                self._only_task(status, lease, attempts)
                result = switch.preflight(self.fx.layout, self.fx.system)
                self.assertFalse(result['ok'], result)
                self.assertIn('queue is not idle', result['error'])

    def test_a_never_started_pending_task_waits_across_the_switch(self):
        # Round-45 rehearsal: the old release could neither run nor cancel its
        # queued tasks while its backend route was unreadable.
        self._only_task('pending', None, 0)
        result = switch.preflight(self.fx.layout, self.fx.system)
        self.assertTrue(result['ok'], result)
        self.assertEqual(1, result['queue']['pending_not_started'])
        self.assertEqual(0, result['queue']['unfinished'])

    # A real failing old report: only the backend route check decides `ok` here.
    # `retained_objects` is reported with ok False while retained objects await
    # manual review, but by the user's rule it never decides doctor's `ok`
    # (health.doctor), so it neither blocks nor counts as the route outage; the
    # ticket still lists it as evidence.
    OUTAGE = {'ok': False, 'paths': {'project_dir': True},
              **{name: {'ok': True} for name in ('release_binding', 'config_consistency', 'database_schema', 'event_chain',
                                          'memory_probe', 'memory_lifecycle', 'skill_registry', 'knowledge_catalog')},
              'local_cli_routes': {'ok': False, 'problems': ['agy_review: Antigravity 桌面模型读取失败']},
              'retained_objects': {'ok': False}}

    def outage(self, **extra):
        return (1, {**copy.deepcopy(self.OUTAGE), **copy.deepcopy(extra)})

    def test_a_passing_doctor_still_records_non_gating_failures(self):
        # Round-47 review: the evidence was dropped when doctor passed.
        self.fx.system.doctor_report = (0, {'ok': True, 'paths': {'project_dir': True}, 'retained_objects': {'ok': False}})
        result = switch.preflight(self.fx.layout, self.fx.system)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['checks']['doctor_ok'])
        self.assertEqual(['retained_objects'], result['checks']['old_non_gating_failures'])

    def test_a_route_outage_needs_a_complete_explicitly_failing_report(self):
        # Round-47 review: an absence of complaints is not evidence.
        incomplete = {k: v for k, v in copy.deepcopy(self.OUTAGE).items() if k != 'event_chain'}
        for report in ({**copy.deepcopy(self.OUTAGE), 'paths': {}},
                       {k: v for k, v in copy.deepcopy(self.OUTAGE).items() if k != 'ok'},
                       incomplete,
                       {'paths': {'project_dir': True}, 'local_cli_routes': {'ok': False, 'problems': ['down']}}):
            with self.subTest(keys=sorted(report)):
                self.fx.system.doctor_report = (1, report)
                result = switch.preflight(self.fx.layout, self.fx.system)
                self.assertFalse(result['ok'], result)
                self.assertIn('old release doctor failed beyond backend route readiness', result['error'])

    def test_old_doctor_failing_only_on_backend_routes_is_allowed_and_recorded(self):
        self.fx.system.doctor_report = self.outage()
        result = switch.preflight(self.fx.layout, self.fx.system)
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['checks']['doctor_ok'])
        self.assertEqual(['agy_review: Antigravity 桌面模型读取失败'], result['checks']['old_route_outage'])
        self.assertEqual(['retained_objects'], result['checks']['old_non_gating_failures'])

    def test_old_doctor_failing_on_anything_else_still_blocks(self):
        for extra in ({'skill_registry': {'ok': False}}, {'event_chain': {'ok': False}},
                      {'paths': {'project_dir': False}}, {'local_cli_routes': {'ok': True}},
                      # Round-46 review: a report whose only failure is a
                      # non-gating check is not a route outage either.
                      {'local_cli_routes': {'ok': True}, 'retained_objects': {'ok': False}}):
            with self.subTest(extra=extra):
                self.fx.system.doctor_report = self.outage(**extra)
                result = switch.preflight(self.fx.layout, self.fx.system)
                self.assertFalse(result['ok'], result)
                self.assertIn('old release doctor failed beyond backend route readiness', result['error'])

    def test_a_never_started_pending_task_is_still_queued_after_a_full_switch(self):
        # Round-46 review: preflight alone did not show the task survives.
        self._only_task('pending', None, 0)
        result = self.run_switch()
        self.assertTrue(result['activated'], result)
        conn = sqlite3.connect(self.fx.layout.database)
        try:
            self.assertEqual([('pending', None, 0)],
                             conn.execute('SELECT status, lease_until, attempts FROM tasks').fetchall())
        finally:
            conn.close()

    def test_old_route_outage_switches_and_undoes_cleanly(self):
        # Round-48 rehearsal: the undo read the previous release back with the
        # strict doctor rule, so a correct undo was reported as failed.
        old = switch.load(self.fx.layout.index)['current_release']
        self.fx.system.doctor_by_version = {old: self.outage()}
        failed = self.run_switch(fault=lambda name: (_ for _ in ()).throw(switch.SwitchError('x'))
                                 if name == 'stage_package' else None)
        self.assertFalse(failed['activated'])
        self.assertEqual('已退回', failed['rollback']['status'], failed)
        self.assertFalse(failed['rollback_read_back']['checks']['doctor_ok'])
        result = self.run_switch()
        self.assertTrue(result['activated'], result)
        self.assertTrue(result['read_back']['checks']['doctor_ok'])

    def test_undo_restores_files_the_switch_wrote_read_only(self):
        # Round-49 rehearsal: source copied from a read-only (sealed or frozen)
        # package keeps mode 0444, and undo could not write the old bytes back.
        target = self.fx.root / 'undo-target.txt'
        target.write_bytes(b'old bytes\n')
        target.chmod(0o640)
        writer = switch.Writer()
        writer.write(target, b'new bytes\n', switch.state(target), 0o444)
        self.assertEqual(0o444, target.stat().st_mode & 0o777)
        self.assertEqual([], writer.undo())
        self.assertEqual(b'old bytes\n', target.read_bytes())
        self.assertEqual(0o640, target.stat().st_mode & 0o777)

    def test_binding_accepts_dsh_plus_qoder_only_as_a_recorded_cover_for_agy(self):
        # User 2026-10-25: AGY account disabled; DSH plus the standby Qoder may bind
        # the release only when Qoder publicly covered AGY for an accepted reason.
        ok = switch.independent_acceptance
        self.assertTrue(ok({'successful_agents': ['agy_review', 'dsh_review']}))
        cover = {'successful_agents': ['dsh_review', 'qoder_review'], 'standby_substituted': True,
                 'replaced': 'agy_review', 'reason': 'health_probe_failed'}
        self.assertTrue(ok(cover))
        self.assertTrue(ok(dict(cover, reason='external_failure')))
        for bad in ({'successful_agents': ['dsh_review', 'qoder_review']},
                    dict(cover, standby_substituted=False),
                    dict(cover, replaced='dsh_review'),
                    dict(cover, reason='internal_unknown_candidate'),
                    dict(cover, successful_agents=['agy_review', 'qoder_review'], replaced='dsh_review'),
                    dict(cover, successful_agents=['qoder_review']),
                    dict(cover, successful_agents=['dsh_review']),
                    {'successful_agents': ['agy_review', 'dsh_review', 'qoder_review']}, {}):
            with self.subTest(evidence=bad):
                self.assertFalse(ok(bad))

    def test_a_route_outage_needs_every_other_gate_to_pass_explicitly(self):
        # Round-63 review: a gate whose ok is None, 0 or "false" is not a pass.
        for value in (None, 0, 'false'):
            with self.subTest(ok=value):
                self.fx.system.doctor_report = self.outage(event_chain={'ok': value})
                result = switch.preflight(self.fx.layout, self.fx.system)
                self.assertFalse(result['ok'], result)
                self.assertIn('old release doctor failed beyond backend route readiness', result['error'])
        # A report-only read-only check never turns a route outage into a refusal.
        self.fx.system.doctor_report = self.outage(release_read_only={'ok': False, 'checked': True, 'sealed': False})
        self.assertTrue(switch.preflight(self.fx.layout, self.fx.system)['ok'])

    def test_seal_resets_special_permission_bits(self):
        # Round-63 review: 04555 / 02555 / 04444 matched the low nine bits and were
        # reported sealed without being changed.
        package = self.fx.root / 'seal-special'
        package.mkdir()
        (package / 'tool').write_text('#!/bin/sh\n')
        (package / 'note').write_text('x\n')
        (package / 'dir').mkdir()
        os.chmod(package / 'tool', 0o4555)
        os.chmod(package / 'note', 0o4444)
        os.chmod(package / 'dir', 0o2555)
        result = switch.seal_release(package)
        modes = {name: (package / name).stat().st_mode & 0o7777 for name in ('tool', 'note', 'dir')}
        for path in [package / 'dir', package]:
            os.chmod(path, 0o755)
        self.assertTrue(result['ok'], result)
        self.assertEqual({'tool': 0o555, 'note': 0o444, 'dir': 0o555}, modes)

    def test_readback_never_accepts_a_failing_new_doctor(self):
        self.fx.system.doctor_report = self.outage()
        result = switch.readback(self.fx.layout, self.fx.system)
        self.assertFalse(result['ok'], result)
        self.assertIn('external verification failed', result['error'])

    def test_user_edited_generated_note_is_never_overwritten(self):
        write(self.fx.layout.vault / 'root.md', 'user changed generated text')
        for rel, value in switch.build_graph(self.fx.layout).items():
            write(self.fx.layout.graph / rel, switch.graph_bytes(value))
        result = switch.preflight(self.fx.layout, self.fx.system)
        self.assertFalse(result['ok'])
        self.assertIn('refusing overwrite', result['error'])

    def test_rollback_refuses_concurrent_content_and_reports_failure(self):
        def change_then_fail(phase):
            if phase == 'bind_config':
                write(self.fx.layout.config, b'concurrent user content')
                raise switch.SwitchError('fault after concurrent change')
        result = self.run_switch(fault=change_then_fail)
        self.assertFalse(result['activated'])
        self.assertEqual('退回失败，需要人工处理', result['rollback']['status'])
        self.assertEqual(b'concurrent user content', self.fx.layout.config.read_bytes())
        self.assertFalse(self.fx.system.live)

    def test_symlink_and_changed_package_are_rejected(self):
        p = self.fx.layout.package / 'agentos/src/xiaoba_agentos/runtime.py'
        p.write_text('tampered\n')
        self.assertFalse(switch.preflight(self.fx.layout, self.fx.system)['ok'])
        p.unlink()
        p.symlink_to(self.fx.layout.vault / 'user-content.md')
        self.assertFalse(switch.preflight(self.fx.layout, self.fx.system)['ok'])

    def test_readback_without_previous_pid_does_not_claim_restart(self):
        result = switch.readback(self.fx.layout, self.fx.system)
        self.assertTrue(result['ok'])
        self.assertFalse(result['restart_verified'])

    def test_numbered_step_executes_records_hash_and_undo_restores_absence(self):
        step = self.fx.add_step()
        before = self.fx.whole_tree()
        def fail(phase):
            if phase == 'step:' + step['id']:
                raise switch.SwitchError('failure after numbered step')
        result = self.run_switch(fault=fail)
        self.assertEqual('已退回', result['rollback']['status'], result)
        self.assertIn('failure after numbered step', result['error'])
        self.assertEqual(before, self.fx.whole_tree())
        result = self.run_switch()
        self.assertTrue(result['activated'], result)
        history = switch.load(self.fx.layout.history)
        self.assertEqual(switch.digest(switch.encoded(step)), history['steps'][step['id']]['sha256'])
        self.assertEqual(b'reviewed shared asset\n', (self.fx.layout.assistant / 'skills/reviewed-asset/SKILL.md').read_bytes())

    def test_executed_step_changed_in_later_package_is_rejected(self):
        step = self.fx.add_step()
        self.assertTrue(self.run_switch()['activated'])
        current = self.fx.layout.index.parent / 'v9.0.1'
        next_package = self.fx.root / 'next-package'
        shutil.copytree(current, next_package)
        # The activated release is sealed read-only (v1.6.5); a package built
        # from a copy of it starts writable again, like a fresh build would.
        for path in [next_package, *next_package.rglob('*')]:
            path.chmod(path.stat().st_mode | 0o200)
        release = switch.load(next_package / 'RELEASE.json')
        release['version'] = 'v9.0.2'
        write(next_package / 'RELEASE.json', switch.encoded(release))
        declaration = switch.load(next_package / 'agentos' / switch.SPEC)
        declaration['from_source_manifest_sha256'] = switch.state(current / 'agentos/SOURCE_MANIFEST.json')['sha256']
        declaration['steps'][0]['execute']['path'] = 'skills/changed-asset/SKILL.md'
        write(next_package / 'agentos' / switch.SPEC, switch.encoded(declaration))
        seal(next_package)
        old = self.fx.layout
        next_layout = switch.Layout(old.runtime, old.config, next_package, old.vault, old.graph, old.entries, old.plist)
        result = switch.preflight(next_layout, self.fx.system)
        self.assertFalse(result['ok'])
        self.assertIn('executed step has been modified', result['error'])

    def test_code_and_manifest_worker_fingerprints_are_both_required(self):
        original = switch.load(self.fx.layout.worker)
        for field in ['code_fingerprint', 'manifest_fingerprint']:
            with self.subTest(field=field):
                changed = dict(original)
                changed[field] = '0' * 64
                write(self.fx.layout.worker, switch.encoded(changed))
                result = switch.readback(self.fx.layout, self.fx.system)
                self.assertFalse(result['ok'])
        write(self.fx.layout.worker, switch.encoded(original))

    def test_late_entrypoint_change_is_not_overwritten(self):
        target = self.fx.layout.entries[0] / 'SKILL.md'
        def drift(phase):
            if phase == 'replace_source':
                write(target, b'concurrent card edit\n')
        result = self.run_switch(fault=drift)
        self.assertFalse(result['activated'])
        self.assertIn('scene changed after checkpoint before install_entries', result['error'])
        self.assertEqual(b'concurrent card edit\n', target.read_bytes())
        self.assertEqual('退回失败，需要人工处理', result['rollback']['status'])

    def test_fixed_command_contains_no_pinned_software_version(self):
        text = (ROOT / 'scripts/switch_release.py').read_text()
        self.assertEqual([], re.findall(r'v\d+\.\d+\.\d+', text))

    def test_doctor_failure_after_new_worker_ready_still_rolls_back(self):
        def fail_doctor(phase):
            if phase == 'start_worker':
                self.fx.system.fail_doctor_once = True
        result = self.run_switch(fault=fail_doctor)
        self.assertFalse(result['activated'])
        self.assertIn('doctor failed', result['error'])
        self.assertEqual('已退回', result['rollback']['status'], result)

    def test_missing_new_state_file_does_not_leave_new_service_running_during_undo(self):
        def lose_state(phase):
            if phase == 'start_worker':
                self.fx.layout.worker.unlink()
                raise switch.SwitchError('new state disappeared')
        result = self.run_switch(fault=lose_state)
        self.assertFalse(result['activated'])
        self.assertEqual('已退回', result['rollback']['status'], result)
        self.assertEqual('v9.0.0', switch.load(self.fx.layout.index)['current_release'])
        self.assertTrue(switch.worker(self.fx.layout, self.fx.system))

    def test_healthy_readback_of_wrong_release_is_not_switch_success(self):
        original = switch.readback
        def wrong_version(layout, system=None, old_pid=None, allow_route_outage=False):
            result = original(layout, system, old_pid, allow_route_outage)
            if old_pid is not None:
                result['version'] = 'v9.0.0'
            return result
        with patch.object(switch, 'readback', side_effect=wrong_version):
            result = self.run_switch()
        self.assertFalse(result['activated'])
        self.assertIn('not the requested target', result['error'])
        self.assertEqual('已退回', result['rollback']['status'], result)

    def test_declared_config_step_preserves_other_fields_and_undoes(self):
        declaration = switch.load(self.fx.layout.package / 'agentos' / switch.SPEC)
        declaration['steps'] = [{'id': '0001-disable-automatic-actions',
            'check': {'kind': 'config_value_equals', 'path': 'recovery.automatic_actions', 'value': None},
            'execute': {'kind': 'config_set', 'path': 'recovery.automatic_actions', 'value': []},
            'undo': {'kind': 'restore_in_memory'}}]
        write(self.fx.layout.package / 'agentos' / switch.SPEC, switch.encoded(declaration))
        seal(self.fx.layout.package)
        before = self.fx.whole_tree()
        def fail(phase):
            if phase.startswith('step:'):
                raise switch.SwitchError('config step fault')
        result = self.run_switch(fault=fail)
        self.assertEqual('已退回', result['rollback']['status'], result)
        self.assertEqual(before, self.fx.whole_tree())
        result = self.run_switch()
        self.assertTrue(result['activated'], result)
        config = switch.load(self.fx.layout.config)
        self.assertEqual([], config['recovery']['automatic_actions'])
        self.assertEqual('must-remain-unchanged', config['private_provider_marker'])

    def test_unreviewed_configuration_step_cannot_modify_routing(self):
        declaration = switch.load(self.fx.layout.package / 'agentos' / switch.SPEC)
        declaration['steps'] = [{'id': '0001-change-model',
            'check': {'kind': 'config_value_equals', 'path': 'agents.model', 'value': None},
            'execute': {'kind': 'config_set', 'path': 'agents.model', 'value': 'forbidden'},
            'undo': {'kind': 'restore_in_memory'}}]
        write(self.fx.layout.package / 'agentos' / switch.SPEC, switch.encoded(declaration))
        seal(self.fx.layout.package)
        result = switch.preflight(self.fx.layout, self.fx.system)
        self.assertFalse(result['ok'])
        self.assertIn('cannot change authentication/models/routes', result['error'])

    def test_index_readback_uses_official_rendering_and_detects_drift(self):
        self.assertTrue(self.run_switch()['activated'])
        self.assertTrue(switch.readback(self.fx.layout, self.fx.system)['knowlp_verified'])
        target = self.fx.layout.graph / 'nodes.json'
        value = json.loads(target.read_bytes())
        write(target, switch.encoded(value))  # Same JSON object, wrong canonical rendering.
        result = switch.readback(self.fx.layout, self.fx.system)
        self.assertFalse(result['ok'])
        self.assertIn('KnowLP index differs', result['error'])

    def test_appended_user_content_survives_generated_update_and_rollback(self):
        target = self.fx.layout.vault / 'root.md'
        suffix = b'\n## User-confirmed appendix\nExact personal content.\n'
        write(target, target.read_bytes() + suffix)
        for rel, value in switch.build_graph(self.fx.layout).items():
            write(self.fx.layout.graph / rel, switch.graph_bytes(value))
        before = self.fx.whole_tree()
        def fail(phase):
            if phase == 'vault_merge':
                raise switch.SwitchError('appendix rollback fault')
        result = self.run_switch(fault=fail)
        self.assertEqual('已退回', result['rollback']['status'], result)
        self.assertEqual(before, self.fx.whole_tree())
        result = self.run_switch()
        self.assertTrue(result['activated'], result)
        self.assertEqual(b'v9.0.1\n' + suffix, target.read_bytes())


if __name__ == '__main__':
    unittest.main()
