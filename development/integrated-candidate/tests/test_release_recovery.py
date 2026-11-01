"""Card 18: real process death, isolated Vault/SQLite/releases only."""
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch

import test_memory_publication_regressions as memory_fixture
import test_switch_release as release_fixture
from xiaoba_agentos.memory import MemoryBridge

switch = release_fixture.switch


def approve_fixture(store, candidate):
    """Synthetic user decisions on a temporary DB, never production approval."""
    for stage in ('delivery', 'final'):
        store.review_memory(candidate['task_id'], stage, 'approve', 'isolated fixture', candidate['evidence_sha256'])


def killed_child(action):
    pid = os.fork()
    if pid == 0:
        try: action()
        except BaseException: os._exit(91)
        os._exit(92)
    _, status = os.waitpid(pid, 0)
    if not os.WIFSIGNALED(status) or os.WTERMSIG(status) != signal.SIGKILL:
        raise AssertionError(f'child was not SIGKILLed: {status}')


class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config, self.store, self.bridge, self.catalog = memory_fixture.PublicationRegressionsTest().fixture(Path(self.temp.name))
        self.candidate = self.store.create_authored_candidate('中断发布', '隔离发布恢复正文。', self.bridge.outbox, author='fixture')

    def test_bottom_level_refuses_pending_before_vault_write(self):
        before = self.catalog.read_bytes()
        for call in (self.bridge.publish_candidate, self.bridge._publish_candidate_unlocked):
            with self.assertRaisesRegex(ValueError, 'requires final approval'):
                call(self.candidate['task_id'])
        self.assertEqual(before, self.catalog.read_bytes())
        self.assertEqual([], list(self.catalog.parent.glob('经验/*.md')))

    def test_bottom_level_checks_actual_bytes_even_without_expected_hash(self):
        candidate = self.candidate
        registered = self.bridge.outbox / 'registered.md'
        registered.write_bytes(Path(candidate['staged_path']).read_bytes())
        self.store.stage_memory_candidate(candidate['task_id'], str(registered), candidate['evidence_sha256'])
        approve_fixture(self.store, candidate)
        Path(candidate['staged_path']).write_text('unreviewed replacement')
        before = self.catalog.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'changed after final review'):
            self.bridge.publish_candidate(candidate['task_id'])
        self.assertEqual(before, self.catalog.read_bytes())

    def test_sigkill_after_first_note_replace_retries_once_across_date_change(self):
        approve_fixture(self.store, self.candidate)
        real_replace = os.replace
        def crash():
            def replace(source, target):
                real_replace(source, target)
                if Path(target).parent.name == '经验' and str(target).endswith('.md'):
                    os.kill(os.getpid(), signal.SIGKILL)
            with patch('xiaoba_agentos.memory.os.replace', side_effect=replace):
                self.bridge.publish_candidate(self.candidate['task_id'])
        killed_child(crash)
        journal = self.bridge.outbox / (self.candidate['task_id'] + '.publication.json')
        if journal.exists(): self.assertEqual('prepared', json.loads(journal.read_text())['phase'])
        with patch('xiaoba_agentos.memory.time.strftime', return_value='2099-01-01'):
            result = MemoryBridge(self.config).publish_candidate(self.candidate['task_id'])
        again = self.bridge.publish_candidate(self.candidate['task_id'])
        self.assertEqual(result['path'], again['path'])
        self.assertEqual(1, len(list(self.catalog.parent.glob('经验/*.md'))))
        self.assertEqual(1, self.catalog.read_text().count(Path(result['path']).stem))
        self.assertEqual('complete', json.loads(journal.read_text())['phase'])

    def test_foreign_note_is_not_adopted_from_recovery_intent(self):
        approve_fixture(self.store, self.candidate)
        replace = os.replace
        def crash():
            def stop(source, target):
                replace(source, target)
                if Path(target).parent.name == '经验': os.kill(os.getpid(), signal.SIGKILL)
            with patch('xiaoba_agentos.memory.os.replace', side_effect=stop): self.bridge.publish_candidate(self.candidate['task_id'])
        killed_child(crash)
        note = next(self.catalog.parent.glob('经验/*.md')); note.write_text('foreign edit')
        with self.assertRaisesRegex(RuntimeError, 'conflicts'):
            self.bridge.publish_candidate(self.candidate['task_id'])
        self.assertEqual('foreign edit', note.read_text())


class AtomicWriteModeTests(unittest.TestCase):
    def test_atomic_write_keeps_the_existing_mode_and_defaults_to_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            note = Path(directory) / 'note.md'
            note.write_text('old'); note.chmod(0o640)
            MemoryBridge._atomic_write_text(note, 'new')
            self.assertEqual(0o640, note.stat().st_mode & 0o777)
            fresh = Path(directory) / 'fresh.md'
            MemoryBridge._atomic_write_text(fresh, 'new')
            self.assertEqual(0o644, fresh.stat().st_mode & 0o777)


class SwitchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.fx = release_fixture.Fixture(Path(self.temp.name))

    def crash(self, phase):
        ticket = switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(ticket['ok'], ticket)
        def action():
            def fault(name):
                if name == phase: os.kill(os.getpid(), signal.SIGKILL)
            switch.apply(self.fx.layout, ticket, self.fx.system, now=lambda: 1000, fault=fault)
        killed_child(action)

    def test_sigkill_after_bind_config_is_queryable_and_resumable(self):
        self.crash('bind_config')
        status = switch.recovery_status(self.fx.layout)
        self.assertTrue(status['pending']); self.assertEqual('bind_config', status['last_completed_phase'])
        self.assertEqual('v9.0.1', switch.load(self.fx.layout.config)['release']['expected_version'])
        self.assertEqual('v9.0.0', switch.load(self.fx.layout.index)['current_release'])
        self.fx.system.live.clear()  # Fixture service state after the child's bootout.
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(result['ok'], result)
        self.assertEqual('v9.0.1', switch.load(self.fx.layout.index)['current_release'])
        self.assertTrue(switch.readback(self.fx.layout, self.fx.system)['ok'])
        self.assertFalse(switch.recovery_status(self.fx.layout)['pending'])
        self.assertNotIn('must-remain-unchanged', switch.recovery_path(self.fx.layout).read_text())

    def test_resume_refuses_checkpoint_drift_and_keeps_journal(self):
        self.crash('bind_config')
        before = switch.recovery_path(self.fx.layout).read_bytes()
        self.fx.layout.config.write_bytes(b'foreign edit')
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertFalse(result['ok']); self.assertIn('checkpoint', result['error'])
        self.assertEqual(b'foreign edit', self.fx.layout.config.read_bytes())
        self.assertEqual(before, switch.recovery_path(self.fx.layout).read_bytes())

    def test_resume_refuses_unexpected_live_worker_before_writing(self):
        self.crash('bind_config')
        before = self.fx.layout.config.read_bytes()
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertFalse(result['ok']); self.assertIn('worker restarted', result['error'])
        self.assertEqual(0, result['writes_performed'])
        self.assertEqual(before, self.fx.layout.config.read_bytes())

    def test_every_completed_phase_can_resume_after_sigkill(self):
        phases = ['stage_package', 'stop_worker', 'replace_source', 'install_entries',
                  'release_steps', 'vault_merge', 'rebuild_graph', 'publish_index', 'start_worker', 'readback']
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.fx = release_fixture.Fixture(Path(directory))
                self.crash(phase)
                if phase != 'stage_package': self.fx.system.live.clear()
                if phase in ('start_worker', 'readback'):
                    self.fx.system.live.add(switch.load(self.fx.layout.worker)['pid'])
                result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
                self.assertTrue(result['ok'], result)

    def test_sigkill_during_staging_can_resume_while_the_old_worker_keeps_running(self):
        ticket = switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)
        real = switch.Writer.write
        def action():
            count = [0]
            def write(writer, path, *args, **kwargs):
                count[0] += 1
                if count[0] == 3:
                    os.kill(os.getpid(), signal.SIGKILL)
                return real(writer, path, *args, **kwargs)
            with patch.object(switch.Writer, 'write', write):
                switch.apply(self.fx.layout, ticket, self.fx.system, now=lambda: 1000)
        killed_child(action)
        self.assertEqual('stage_package', switch.recovery_status(self.fx.layout)['active_phase'])
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(result['ok'], result)
        self.assertEqual('v9.0.1', switch.load(self.fx.layout.index)['current_release'])

    def test_sigkill_after_bootout_before_checkpoint_does_not_bootout_again(self):
        ticket = switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)
        def action():
            def stop(layout, pid):
                os.kill(os.getpid(), signal.SIGKILL)
            with patch.object(self.fx.system, 'stop', stop):
                switch.apply(self.fx.layout, ticket, self.fx.system, now=lambda: 1000)
        killed_child(action)
        self.assertEqual('stop_worker', switch.recovery_status(self.fx.layout)['active_phase'])
        self.fx.system.live.clear()  # launchctl bootout already took effect.
        with patch.object(self.fx.system, 'stop', side_effect=AssertionError('second bootout')):
            result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(result['ok'], result)

    def crash_inside(self, phase, temporary=False):
        if phase == 'install_entries':
            for name in ('xiaoba', 'verify-release'):
                path = self.fx.layout.package / 'interface/scripts' / name
                path.write_text(path.read_text() + '# new entry bytes\n')
            release_fixture.seal(self.fx.layout.package)
        ticket = switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(ticket['ok'], ticket)
        real = os.replace
        def action():
            def replace(source, target):
                journal = switch.load(switch.recovery_path(self.fx.layout)) if switch.recovery_path(self.fx.layout).exists() else {}
                hit = '.switch-' in Path(source).name and journal.get('active_phase') == phase
                if hit and temporary:
                    os.kill(os.getpid(), signal.SIGKILL)
                real(source, target)
                if hit:
                    os.kill(os.getpid(), signal.SIGKILL)
            with patch.object(switch.os, 'replace', replace):
                switch.apply(self.fx.layout, ticket, self.fx.system, now=lambda: 1000)
        killed_child(action)
        self.assertEqual(phase, switch.recovery_status(self.fx.layout)['active_phase'])
        if phase != 'stage_package': self.fx.system.live.clear()

    def test_sigkill_inside_source_and_entries_resumes(self):
        for phase in ('replace_source', 'install_entries', 'bind_config', 'vault_merge', 'rebuild_graph', 'publish_index'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.fx = release_fixture.Fixture(Path(directory))
                self.crash_inside(phase)
                result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
                self.assertTrue(result['ok'], result)
                self.assertEqual('v9.0.1', switch.load(self.fx.layout.config)['release']['expected_version'])
                self.assertEqual('v9.0.1', switch.load(self.fx.layout.index)['current_release'])
                self.assertTrue(switch.readback(self.fx.layout, self.fx.system)['ok'])

    def test_sigkill_then_third_content_refuses_without_any_write(self):
        self.crash_inside('replace_source')
        target = self.fx.layout.runtime / 'README.md'
        target.write_bytes(b'third content')
        before = switch.files(self.fx.root)
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertFalse(result['ok'], result)
        self.assertIn(str(target), result['error'])
        self.assertEqual(0, result['writes_performed'])
        self.assertEqual(before, switch.files(self.fx.root))

    def test_sigkill_copy_temporary_is_preserved_in_leftovers(self):
        for phase in ('stage_package', 'replace_source', 'install_entries'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                self.fx = release_fixture.Fixture(Path(directory))
                self.crash_inside(phase, temporary=True)
                temps = list(self.fx.root.rglob('.*.switch-*'))
                self.assertEqual(1, len(temps))
                payload = temps[0].read_bytes()
                result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
                self.assertTrue(result['ok'], result)
                self.assertFalse(temps[0].exists())
                leftovers = self.fx.layout.runtime / '.runtime/switch-leftovers'
                kept = [p for p in leftovers.rglob('*') if p.is_file()]
                self.assertEqual([payload], [p.read_bytes() for p in kept])

    def test_sigkill_abandon_only_changes_record_and_unblocks_apply(self):
        self.crash_inside('replace_source')
        blocked = switch.apply(self.fx.layout, {}, self.fx.system)
        self.assertIn('interrupted switch pending', blocked['error'])
        before = switch.files(self.fx.root)
        result = switch.abandon(self.fx.layout, self.fx.system)
        self.assertTrue(result['ok'], result)
        self.assertEqual('replace_source', result['active_phase'])
        self.assertFalse(result['worker_running'])
        self.assertTrue(result['new_content_paths'])
        after = switch.files(self.fx.root)
        journal = switch.recovery_path(self.fx.layout).relative_to(self.fx.root).as_posix()
        self.assertEqual({journal}, {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)})
        self.assertEqual('abandoned', switch.recovery_status(self.fx.layout)['status'])
        unblocked = switch.apply(self.fx.layout, {}, self.fx.system)
        self.assertIn('successful preflight required', unblocked['error'])

    def test_sigkill_active_phase_does_not_relax_other_phases(self):
        self.crash_inside('install_entries', temporary=True)
        target = self.fx.layout.runtime / 'README.md'
        target.write_bytes(b'foreign previous-phase change')
        before = switch.files(self.fx.root)
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertFalse(result['ok'], result)
        self.assertIn('checkpoint', result['error'])
        self.assertEqual(before, switch.files(self.fx.root))

    def test_sigkill_entry_third_mode_refuses_without_moving_temporary(self):
        self.crash_inside('install_entries', temporary=True)
        target = self.fx.layout.entries[-1] / 'SKILL.md'
        target.chmod(0o700)
        before = switch.files(self.fx.root)
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertFalse(result['ok'], result)
        self.assertIn(str(target), result['error'])
        self.assertEqual(before, switch.files(self.fx.root))

    def test_sigkill_release_steps_and_appended_note_resume(self):
        declaration = switch.load(self.fx.layout.package / 'agentos' / switch.SPEC)
        declaration['steps'] = [{'id': '0001-disable-automatic-actions',
            'check': {'kind': 'config_value_equals', 'path': 'recovery.automatic_actions', 'value': None},
            'execute': {'kind': 'config_set', 'path': 'recovery.automatic_actions', 'value': []},
            'undo': {'kind': 'restore_in_memory'}}]
        release_fixture.write(self.fx.layout.package / 'agentos' / switch.SPEC, switch.encoded(declaration))
        release_fixture.seal(self.fx.layout.package)
        self.crash_inside('release_steps')
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(result['ok'], result)
        self.assertEqual([], switch.load(self.fx.layout.config)['recovery']['automatic_actions'])
        with tempfile.TemporaryDirectory() as directory:
            self.fx = release_fixture.Fixture(Path(directory))
            note = self.fx.layout.vault / 'root.md'
            note.write_bytes(note.read_bytes() + b'user appendix\n')
            for rel, value in switch.build_graph(self.fx.layout).items():
                (self.fx.layout.graph / rel).write_bytes(switch.graph_bytes(value))
            self.crash_inside('vault_merge')
            result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
            self.assertTrue(result['ok'], result)
            self.assertEqual(b'v9.0.1\nuser appendix\n', note.read_bytes())

    def test_sigkill_abandon_says_what_blocks_the_next_preflight(self):
        # 只传空预检单测不到：真实预检会被上次留下的目标目录挡住。
        self.crash_inside('stage_package', temporary=True)
        result = switch.abandon(self.fx.layout, self.fx.system)
        target = Path(switch.load(switch.recovery_path(self.fx.layout))['target'])
        self.assertTrue(any(str(target) in step for step in result['next_steps']), result['next_steps'])
        blocked = switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertFalse(blocked['ok'])
        self.assertIn(str(target), blocked['error'])
        os.rename(target, target.with_name('.abandoned-' + target.name))
        self.assertTrue(switch.preflight(self.fx.layout, self.fx.system, now=lambda: 1000)['ok'])

    def test_sigkill_abandon_after_replacing_source_reports_stopped_worker_and_new_files(self):
        self.crash_inside('replace_source')
        steps = switch.abandon(self.fx.layout, self.fx.system)['next_steps']
        self.assertTrue(any('Worker 已停' in step for step in steps), steps)
        self.assertTrue(any('已是新内容' in step for step in steps), steps)

    def test_sigkill_abandon_reports_live_worker_and_shares_switch_lock(self):
        import fcntl
        self.crash_inside('stage_package', temporary=True)
        before = switch.files(self.fx.root)
        fd = os.open(self.fx.layout.runtime / '.runtime', os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = switch.abandon(self.fx.layout, self.fx.system)
            self.assertFalse(result['ok'], result)
            self.assertEqual(before, switch.files(self.fx.root))
        finally:
            os.close(fd)
        result = switch.abandon(self.fx.layout, self.fx.system)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['worker_running'])
        self.assertTrue(list(self.fx.root.rglob('.*.switch-*')))

    def test_sigkill_during_resume_preserves_original_phase_plan(self):
        self.crash_inside('replace_source')
        initial = switch.load(switch.recovery_path(self.fx.layout))['phase_files']
        real = switch.Writer.write
        def action():
            def write(writer, path, *args, **kwargs):
                real(writer, path, *args, **kwargs)
                os.kill(os.getpid(), signal.SIGKILL)
            with patch.object(switch.Writer, 'write', write):
                switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        killed_child(action)
        self.assertEqual(initial, switch.load(switch.recovery_path(self.fx.layout))['phase_files'])
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(result['ok'], result)

    def test_sigkill_partial_temporary_bytes_are_preserved(self):
        self.crash_inside('replace_source', temporary=True)
        temp = next(self.fx.root.rglob('.*.switch-*'))
        temp.write_bytes(b'partial new bytes')
        result = switch.resume(self.fx.layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(result['ok'], result)
        self.assertEqual([b'partial new bytes'], [Path(p).read_bytes() for p in result['preserved_leftovers']])

    def test_undo_short_write_leaves_installed_file_complete_manually(self):
        path = self.fx.root / 'undo.txt'; path.write_bytes(b'ORIGINAL')
        writer = switch.Writer(); writer.write(path, b'COMPLETE NEW FILE', switch.state(path))
        class ShortFile:
            def __init__(self, output): self.output = output
            def __enter__(self): return self
            def __exit__(self, *args): self.output.close()
            def write(self, value): self.output.write(value[:2]); return 2
        real_fdopen = os.fdopen
        real_write = Path.write_bytes
        def short_direct(target, value):
            return real_write(target, value[:2] if target == path else value)
        with patch.object(switch.os, 'fdopen', side_effect=lambda *a, **k: ShortFile(real_fdopen(*a, **k))), \
                patch.object(Path, 'write_bytes', short_direct):
            errors = writer.undo()
        self.assertTrue(errors); self.assertEqual(b'COMPLETE NEW FILE', path.read_bytes())
        self.assertEqual([], writer.undo()); self.assertEqual(b'ORIGINAL', path.read_bytes())

    def test_builder_replaced_after_verification_is_never_executed(self):
        layout = self.fx.layout; builder = layout.runtime / 'scripts/build_knowlp_index.py'
        marker = self.fx.root / 'executed'
        ticket = switch.preflight(layout, self.fx.system, now=lambda: 1000)
        self.assertTrue(ticket['ok'], ticket)
        def replace_after_verification(phase):
            if phase == 'vault_merge':
                original = builder.read_text()
                builder.write_text(f'from pathlib import Path\nPath({str(marker)!r}).write_text("bad")\n' + original)
        result = switch.apply(layout, ticket, self.fx.system, now=lambda: 1000, fault=replace_after_verification)
        self.assertFalse(result['ok'], result)
        self.assertFalse(marker.exists())


if __name__ == '__main__': unittest.main()
