"""切换流程的隔离文件/数据库回归；服务是显式测试替身，不是线上验收。"""
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
        db.executescript('CREATE TABLE tasks (status TEXT, lease_until REAL);'
                         'CREATE TABLE repair_jobs (status TEXT, lease_until REAL);')
        db.close()
        self.layout = switch.Layout(runtime, config, package, vault, graph, entries, plist)
        self.system = FakeSystem(self.layout)
        self.system.record_worker(100)

    def whole_tree(self):
        # Service owns worker-state; all remaining bytes/modes must be restored exactly.
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode & 0o777)
                for p in self.root.rglob('*') if p.is_file() and p != self.layout.worker}

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

    def test_unfinished_frozen_tasks_and_unreleased_expired_leases_block(self):
        for status, lease in [('pending', None), ('frozen_paused', None), ('resume_ready', None), ('completed', 1)]:
            with self.subTest(status=status, lease=lease):
                conn = sqlite3.connect(self.fx.layout.database)
                conn.execute('DELETE FROM tasks')
                conn.execute('INSERT INTO tasks VALUES (?,?)', (status, lease))
                conn.commit()
                conn.close()
                result = switch.preflight(self.fx.layout, self.fx.system)
                self.assertFalse(result['ok'], result)
                self.assertIn('queue is not idle', result['error'])

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
        self.assertIn('entrypoint changed after preflight', result['error'])
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
        def wrong_version(layout, system=None, old_pid=None):
            result = original(layout, system, old_pid)
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
