import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch

from fixtures_review import _config
from xiaoba_agentos import cli
from xiaoba_agentos.store import EventStore


PREFLIGHT_READY = {
    "primary_agents": ["a", "b"],
    "minimum_successful_agents": 2,
    "paths": {"fixture": True},
    "event_chain": {"ok": True},
    "local_cli_routes": {"ok": True},
    "memory_probe": {"ok": True},
    "ready_for_external_tasks": True,
}
REPLACED_RUNNER_NOTICE = "未能移入废纸篓，留在 /tmp/replaced-Runner.app-9，请用户处理"
FAILED_COPY = "/runtime/backups/launchd/failed-install-1.plist"
_RESTART_UNSET = object()


class InstallServiceNeverBacksUpUnaskedTest(unittest.TestCase):
    """User rule (B03): no backup file, directory or snapshot without an explicit request."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = _config(self.root)
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.backups = self.config.runtime_dir / "backups"

    def _backup_files(self):
        return sorted(p for p in self.backups.rglob("*") if p.is_file()) if self.backups.exists() else []

    def _make_legacy(self):
        with closing(sqlite3.connect(str(self.store.path))) as conn, conn:
            conn.execute("PRAGMA user_version=6")

    def test_a_current_database_is_left_alone_and_nothing_is_copied(self):
        self.assertIsNone(cli._prepare_database_for_install(self.config, self.store, False))
        self.assertIsNone(cli._prepare_database_for_install(self.config, self.store, True))
        self.assertEqual([], self._backup_files())

    def test_a_database_needing_migration_is_refused_without_explicit_authorization(self):
        self._make_legacy()
        with self.assertRaisesRegex(RuntimeError, "--backup-database"):
            cli._prepare_database_for_install(self.config, self.store, False)
        self.assertEqual([], self._backup_files())
        with closing(sqlite3.connect(str(self.store.path))) as conn:
            self.assertEqual(6, conn.execute("PRAGMA user_version").fetchone()[0])

    def test_an_authorized_backup_is_made_once_and_is_a_complete_database(self):
        self._make_legacy()
        target = cli._prepare_database_for_install(self.config, self.store, True)
        self.assertEqual([target], self._backup_files())
        with closing(sqlite3.connect(str(target))) as conn:
            self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(0o600, target.stat().st_mode & 0o777)

    def test_a_backup_that_fails_part_way_leaves_no_half_copy_behind(self):
        # Round 30: the copy was written before it was checked, and a failure
        # left a broken file in backups/database that looked like a restore point.
        self._make_legacy()
        real_connect = sqlite3.connect

        class FailingSource:
            def __init__(self, conn):
                object.__setattr__(self, "_conn", conn)

            def __setattr__(self, name, value):
                setattr(self._conn, name, value)

            def backup(self, destination):
                self._conn.backup(destination)
                raise sqlite3.OperationalError("disk I/O error")

            def __getattr__(self, name):
                return getattr(self._conn, name)

        def connect(target, *args, **kwargs):
            conn = real_connect(target, *args, **kwargs)
            # Read-only connections only; the store's own reads never call backup().
            return FailingSource(conn) if kwargs.get("uri") else conn

        with patch.object(cli.sqlite3, "connect", side_effect=connect):
            with self.assertRaises(sqlite3.OperationalError):
                cli._prepare_database_for_install(self.config, self.store, True)
        self.assertEqual([], self._backup_files())
        with closing(sqlite3.connect(str(self.store.path))) as conn:
            self.assertEqual(6, conn.execute("PRAGMA user_version").fetchone()[0])

    def test_restoring_the_database_drops_the_failed_migrations_wal(self):
        # Round 36: the live database's -wal/-shm from the failed migration were
        # left beside the restored file, to be replayed onto it on next open.
        self._make_legacy()
        backup = self.root / "authorized-backup.sqlite3"
        with closing(sqlite3.connect(str(self.store.path))) as source, \
                closing(sqlite3.connect(str(backup))) as copy:
            source.backup(copy)
        for suffix in ("-wal", "-shm"):
            Path(str(self.store.path) + suffix).write_bytes(b"pages of a failed migration")
        cli._restore_database(self.store, backup)
        for suffix in ("-wal", "-shm"):
            self.assertFalse(Path(str(self.store.path) + suffix).exists(), suffix)
        self.assertFalse(self.store.path.with_suffix(".sqlite3.restore").exists())
        with closing(sqlite3.connect(str(self.store.path))) as conn:
            self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual(6, conn.execute("PRAGMA user_version").fetchone()[0])

    def test_rolling_back_the_service_restores_the_old_plist_from_memory_only(self):
        destination = self.root / "LaunchAgents" / "com.xiaoba.agentos.plist"
        destination.parent.mkdir()
        destination.write_bytes(b"new, failing plist")
        absent = type("R", (), {"returncode": 113, "stdout": "", "stderr": ""})()
        with patch.object(cli, "_launchctl_print", return_value=absent), \
                patch.object(cli.subprocess, "run") as run:
            cli._rollback_service(self.config, destination, b"previous plist", "gui/0", "com.xiaoba.agentos", False)
        self.assertEqual(b"previous plist", destination.read_bytes())
        self.assertFalse(destination.with_suffix(".plist.agentos-restore").exists())
        run.assert_not_called()
        # Exactly one file anywhere under backups: the failed new plist, moved aside
        # for diagnosis with its own content. The old plist is never copied.
        moved = self._backup_files()
        self.assertEqual(1, len(moved), moved)
        self.assertEqual("launchd", moved[0].parent.name)
        self.assertTrue(moved[0].name.startswith("failed-install-"), moved)
        self.assertEqual(b"new, failing plist", moved[0].read_bytes())

    def test_a_rollback_with_nothing_to_move_reports_no_copy_and_creates_no_directory(self):
        # Round 25: the path of a copy that was never made used to be reported,
        # and backups/launchd was created even with nothing to put in it.
        destination = self.root / "LaunchAgents" / "com.xiaoba.agentos.plist"
        destination.parent.mkdir()
        absent = type("R", (), {"returncode": 113, "stdout": "", "stderr": ""})()
        with patch.object(cli, "_launchctl_print", return_value=absent), \
                patch.object(cli.subprocess, "run"):
            kept = cli._rollback_service(self.config, destination, None, "gui/0", "com.xiaoba.agentos", False)
        self.assertIsNone(kept)
        self.assertFalse((self.config.runtime_dir / "backups" / "launchd").exists())
        self.assertFalse(destination.exists())

    def test_a_rollback_that_cannot_stage_the_original_leaves_the_plist_in_place(self):
        # The original is staged before the failed plist is moved, so a write
        # failure never leaves the LaunchAgents path empty.
        destination = self.root / "LaunchAgents" / "com.xiaoba.agentos.plist"
        destination.parent.mkdir()
        destination.write_bytes(b"new, failing plist")
        absent = type("R", (), {"returncode": 113, "stdout": "", "stderr": ""})()
        with patch.object(cli, "_launchctl_print", return_value=absent), \
                patch.object(cli.subprocess, "run"), \
                patch.object(Path, "write_bytes", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                cli._rollback_service(self.config, destination, b"previous plist", "gui/0", "com.xiaoba.agentos", False)
        self.assertEqual(b"new, failing plist", destination.read_bytes())
        self.assertEqual([], self._backup_files())

    def test_a_rollback_that_cannot_put_the_original_back_says_where_it_is(self):
        # Round 31: after the failed plist was moved aside, a failing os.replace
        # left the LaunchAgents path empty with no word about the original.
        destination = self.root / "LaunchAgents" / "com.xiaoba.agentos.plist"
        destination.parent.mkdir()
        destination.write_bytes(b"new, failing plist")
        absent = type("R", (), {"returncode": 113, "stdout": "", "stderr": ""})()
        with patch.object(cli, "_launchctl_print", return_value=absent), \
                patch.object(cli.subprocess, "run"), \
                patch.object(cli.os, "replace", side_effect=OSError(30, "Read-only file system")):
            with self.assertRaises(RuntimeError) as caught:
                cli._rollback_service(self.config, destination, b"previous plist", "gui/0", "com.xiaoba.agentos", False)
        staged = destination.with_suffix(".plist.agentos-restore")
        self.assertEqual(b"previous plist", staged.read_bytes())
        self.assertIn(str(staged), str(caught.exception))

    def test_toggling_agents_rewrites_the_config_without_copying_it(self):
        name = self.config.raw["agents"][0]["name"]
        result = cli._set_agents(self.config, [name], False)
        self.assertNotIn("backup", result)
        self.assertEqual([], self._backup_files())
        raw = __import__("json").loads(self.config.path.read_text(encoding="utf-8"))
        self.assertFalse(next(a for a in raw["agents"] if a["name"] == name)["enabled"])
        self.assertFalse(self.config.path.with_suffix(".json.tmp").exists())

    def test_rebuilding_the_runner_sends_the_old_one_to_the_trash_not_to_backups(self):
        project = self.root / "project"
        (project / "macos/runner").mkdir(parents=True)
        (project / "macos/runner/main.swift").write_text("// stub\n", encoding="utf-8")
        (project / "macos/runner/Info.plist").write_text("<plist/>\n", encoding="utf-8")
        apps = self.root / "Applications"
        target = apps / cli.RUNNER_APP_NAME
        (target / "Contents").mkdir(parents=True)
        (target / "Contents/old-marker").write_text("old runner", encoding="utf-8")
        trash = self.root / "Trash"
        trash.mkdir()

        def fake_run(argv, **kwargs):
            if argv[0] == "/usr/bin/swiftc":
                Path(argv[argv.index("-o") + 1]).write_text("binary", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        moved = []
        def fake_trash(path):
            destination = trash / path.name
            __import__("shutil").move(str(path), str(destination))
            moved.append(destination)
            return str(destination)

        config = type("C", (), {"project_dir": project, "runtime_dir": self.config.runtime_dir,
                                "path": self.config.path})()
        with patch.object(cli.subprocess, "run", side_effect=fake_run), \
                patch.object(cli, "_verify_runner_app", return_value={"ok": True, "executable": "/usr/bin/true"}), \
                patch.object(cli, "_move_to_trash", side_effect=fake_trash):
            result = cli._build_runner_app(config, target)
        self.assertEqual([], self._backup_files())
        self.assertFalse((self.config.runtime_dir / "backups" / "runner").exists())
        self.assertEqual(1, len(moved))
        self.assertEqual("old runner", (moved[0] / "Contents/old-marker").read_text(encoding="utf-8"))
        self.assertEqual(str(moved[0]), result["previous_runner"])
        self.assertTrue((target / "Contents/MacOS").is_dir())

    def test_a_runner_that_cannot_reach_the_trash_is_kept_in_the_runtime_directory(self):
        project = self.root / "project"
        (project / "macos/runner").mkdir(parents=True)
        (project / "macos/runner/main.swift").write_text("// stub\n", encoding="utf-8")
        (project / "macos/runner/Info.plist").write_text("<plist/>\n", encoding="utf-8")
        target = self.root / "Applications" / cli.RUNNER_APP_NAME
        (target / "Contents").mkdir(parents=True)
        (target / "Contents/old-marker").write_text("old runner", encoding="utf-8")

        def fake_run(argv, **kwargs):
            if argv[0] == "/usr/bin/swiftc":
                Path(argv[argv.index("-o") + 1]).write_text("binary", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        config = type("C", (), {"project_dir": project, "runtime_dir": self.config.runtime_dir,
                                "path": self.config.path})()
        with patch.object(cli.subprocess, "run", side_effect=fake_run), \
                patch.object(cli, "_verify_runner_app", return_value={"ok": True, "executable": "/usr/bin/true"}), \
                patch.object(cli, "_move_to_trash", return_value=None):
            result = cli._build_runner_app(config, target)
        kept = sorted(self.config.runtime_dir.glob("replaced-" + cli.RUNNER_APP_NAME + "-*"))
        self.assertEqual(1, len(kept))
        self.assertEqual("old runner", (kept[0] / "Contents/old-marker").read_text(encoding="utf-8"))
        self.assertIn(str(kept[0]), result["previous_runner"])
        self.assertIn("未能移入废纸篓", result["previous_runner"])
        self.assertEqual([], self._backup_files())

    def test_a_runner_that_cannot_be_put_back_is_kept_and_reported(self):
        # Round 27: replacing fails and moving the old Runner back fails too; it
        # used to stay in the temporary directory and be deleted with it.
        import shutil as real_shutil
        project = self.root / "project"
        (project / "macos/runner").mkdir(parents=True)
        (project / "macos/runner/main.swift").write_text("// stub\n", encoding="utf-8")
        (project / "macos/runner/Info.plist").write_text("<plist/>\n", encoding="utf-8")
        target = self.root / "Applications" / cli.RUNNER_APP_NAME
        (target / "Contents").mkdir(parents=True)
        (target / "Contents/old-marker").write_text("old runner", encoding="utf-8")

        def fake_run(argv, **kwargs):
            if argv[0] == "/usr/bin/swiftc":
                Path(argv[argv.index("-o") + 1]).write_text("binary", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        real_replace, real_move = cli.os.replace, real_shutil.move

        def failing_replace(source, destination):
            if str(destination) == str(target):
                raise OSError(28, "No space left on device")
            return real_replace(source, destination)

        def failing_move(source, destination):
            if str(destination) == str(target):
                raise OSError(13, "Permission denied")
            return real_move(source, destination)

        config = type("C", (), {"project_dir": project, "runtime_dir": self.config.runtime_dir,
                                "path": self.config.path})()
        with patch.object(cli.subprocess, "run", side_effect=fake_run), \
                patch.object(cli, "_verify_runner_app", return_value={"ok": True, "executable": "/usr/bin/true"}), \
                patch.object(cli.os, "replace", side_effect=failing_replace), \
                patch.object(cli.shutil, "move", side_effect=failing_move), \
                patch.object(cli, "_move_to_trash", return_value=None):
            with self.assertRaises(RuntimeError) as caught:
                cli._build_runner_app(config, target)
        # Round 28: the old Runner waits outside the build's temporary
        # directory, so it is still there after that directory is cleaned up,
        # at exactly the path the error names.
        kept = sorted(target.parent.glob("." + cli.RUNNER_APP_NAME + ".previous-*"))
        self.assertEqual(1, len(kept))
        self.assertEqual("old runner", (kept[0] / "Contents/old-marker").read_text(encoding="utf-8"))
        self.assertIn(str(kept[0]), str(caught.exception))
        self.assertEqual([], sorted(target.parent.glob("xiaoba-runner-*")))
        self.assertEqual([], self._backup_files())

    def test_a_failed_swap_that_puts_the_old_runner_back_reports_the_original_error(self):
        # Round 32: the put-back-succeeded path re-raises the replacement error
        # itself (it never claims the Runner was left aside), the old Runner is
        # in place again, and no .previous-… is left behind.
        project = self.root / "project"
        (project / "macos/runner").mkdir(parents=True)
        (project / "macos/runner/main.swift").write_text("// stub\n", encoding="utf-8")
        (project / "macos/runner/Info.plist").write_text("<plist/>\n", encoding="utf-8")
        target = self.root / "Applications" / cli.RUNNER_APP_NAME
        (target / "Contents").mkdir(parents=True)
        (target / "Contents/old-marker").write_text("old runner", encoding="utf-8")

        def fake_run(argv, **kwargs):
            if argv[0] == "/usr/bin/swiftc":
                Path(argv[argv.index("-o") + 1]).write_text("binary", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        real_replace = cli.os.replace
        original = OSError(28, "No space left on device")

        def failing_replace(source, destination):
            if str(destination) == str(target):
                raise original
            return real_replace(source, destination)

        config = type("C", (), {"project_dir": project, "runtime_dir": self.config.runtime_dir,
                                "path": self.config.path})()
        with patch.object(cli.subprocess, "run", side_effect=fake_run), \
                patch.object(cli, "_verify_runner_app", return_value={"ok": True, "executable": "/usr/bin/true"}), \
                patch.object(cli.os, "replace", side_effect=failing_replace):
            with self.assertRaises(OSError) as caught:
                cli._build_runner_app(config, target)
        self.assertIs(original, caught.exception)
        self.assertEqual("old runner", (target / "Contents/old-marker").read_text(encoding="utf-8"))
        self.assertEqual([], sorted(target.parent.glob("." + cli.RUNNER_APP_NAME + ".previous-*")))

    def test_a_target_that_reappears_never_costs_the_old_runner(self):
        # Round 28: replacing fails while something else has put a bundle at the
        # target again; the old Runner used to be deleted with no word about it.
        import shutil as real_shutil
        project = self.root / "project"
        (project / "macos/runner").mkdir(parents=True)
        (project / "macos/runner/main.swift").write_text("// stub\n", encoding="utf-8")
        (project / "macos/runner/Info.plist").write_text("<plist/>\n", encoding="utf-8")
        target = self.root / "Applications" / cli.RUNNER_APP_NAME
        (target / "Contents").mkdir(parents=True)
        (target / "Contents/old-marker").write_text("old runner", encoding="utf-8")

        def fake_run(argv, **kwargs):
            if argv[0] == "/usr/bin/swiftc":
                Path(argv[argv.index("-o") + 1]).write_text("binary", encoding="utf-8")
            return type("R", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

        real_replace = cli.os.replace

        def reappearing_replace(source, destination):
            if str(destination) == str(target):
                (target / "Contents").mkdir(parents=True)
                raise OSError(17, "File exists")
            return real_replace(source, destination)

        config = type("C", (), {"project_dir": project, "runtime_dir": self.config.runtime_dir,
                                "path": self.config.path})()
        with patch.object(cli.subprocess, "run", side_effect=fake_run), \
                patch.object(cli, "_verify_runner_app", return_value={"ok": True, "executable": "/usr/bin/true"}), \
                patch.object(cli.os, "replace", side_effect=reappearing_replace):
            with self.assertRaises(RuntimeError) as caught:
                cli._build_runner_app(config, target)
        kept = sorted(target.parent.glob("." + cli.RUNNER_APP_NAME + ".previous-*"))
        self.assertEqual(1, len(kept))
        self.assertEqual("old runner", (kept[0] / "Contents/old-marker").read_text(encoding="utf-8"))
        self.assertIn(str(kept[0]), str(caught.exception))
        self.assertEqual([], self._backup_files())

    def test_install_service_reports_a_replaced_runner_it_had_to_keep(self):
        rendered = self.root / "rendered/com.xiaoba.agentos.plist"
        rendered.parent.mkdir()
        rendered.write_text("<plist/>\n")
        preflight = {"primary_agents": ["a", "b"], "minimum_successful_agents": 2,
                     "paths": {"fixture": True}, "event_chain": {"ok": True},
                     "local_cli_routes": {"ok": True}, "memory_probe": {"ok": True},
                     "ready_for_external_tasks": True}
        loaded = subprocess.CompletedProcess([], 0, "state = running\npid = 4242", "")
        notice = "未能移入废纸篓，留在 /tmp/replaced-Runner.app-1，请用户处理"
        with patch.object(Path, "home", return_value=self.root), \
                patch.object(cli, "doctor", return_value=preflight), \
                patch.object(cli, "_verify_runner_app", return_value={"ok": False}), \
                patch.object(cli, "_build_runner_app",
                             return_value={"ok": True, "previous_runner": notice}), \
                patch.object(cli, "_render_launchagent", return_value=rendered), \
                patch.object(cli.subprocess, "run", return_value=loaded), \
                patch.object(cli, "_launchctl_print", return_value=loaded), \
                patch.object(cli, "_wait_service_unloaded", return_value=True), \
                patch.object(cli, "_prepare_database_for_install", return_value=None), \
                patch.object(cli, "_rotate_worker_logs", return_value=[]), \
                patch.object(cli, "_worker_runtime_status", return_value={"ok": True}):
            result = cli._install_service(self.config, self.store)
        self.assertEqual("installed_and_running", result["status"])
        self.assertEqual(notice, result["previous_runner"])
        self.assertEqual([], self._backup_files())

    def test_an_early_install_failure_still_carries_the_runner_notice(self):
        # Round 33: rendering the plist (or reading the old one) failed before
        # any handler that appends the notice; a rerun never reports it again.
        preflight = {"primary_agents": ["a", "b"], "minimum_successful_agents": 2,
                     "paths": {"fixture": True}, "event_chain": {"ok": True},
                     "local_cli_routes": {"ok": True}, "memory_probe": {"ok": True},
                     "ready_for_external_tasks": True}
        notice = "未能移入废纸篓，留在 /tmp/replaced-Runner.app-1，请用户处理"
        with patch.object(Path, "home", return_value=self.root), \
                patch.object(cli, "doctor", return_value=preflight), \
                patch.object(cli, "_verify_runner_app", return_value={"ok": False}), \
                patch.object(cli, "_build_runner_app",
                             return_value={"ok": True, "previous_runner": notice}), \
                patch.object(cli, "_render_launchagent", side_effect=OSError(2, "template missing")):
            with self.assertRaises(RuntimeError) as caught:
                cli._install_service(self.config, self.store)
        self.assertIn("template missing", str(caught.exception))
        self.assertIn(notice, str(caught.exception))
        self.assertEqual([], self._backup_files())

    def test_the_install_service_command_exposes_the_explicit_flag(self):
        args = cli.build_parser().parse_args(["install-service", "--backup-database"])
        self.assertTrue(args.backup_database)
        self.assertFalse(cli.build_parser().parse_args(["install-service"]).backup_database)

    def test_install_failure_without_backup_preserves_service_recovery_and_reports_it_honestly(self):
        rendered = self.root / 'rendered/com.xiaoba.agentos.plist'
        rendered.parent.mkdir()
        rendered.write_text('<plist/>\n')
        preflight = {'primary_agents': ['a', 'b'], 'minimum_successful_agents': 2,
                     'paths': {'fixture': True}, 'event_chain': {'ok': True},
                     'local_cli_routes': {'ok': True}, 'memory_probe': {'ok': True}}
        loaded = subprocess.CompletedProcess([], 0, 'state = running', '')
        for initialize_error in (True, False):
            with self.subTest(initialize_error=initialize_error):
                store = Mock()
                if initialize_error:
                    store.initialize.side_effect = RuntimeError('synthetic initialize failure')
                with patch.object(Path, 'home', return_value=self.root), \
                        patch.object(cli, 'doctor', side_effect=[preflight, {'ready_for_external_tasks': False}]), \
                        patch.object(cli, '_build_runner_app', return_value={'ok': True}), \
                        patch.object(cli, '_render_launchagent', return_value=rendered), \
                        patch.object(cli.subprocess, 'run', return_value=loaded), \
                        patch.object(cli, '_launchctl_print', return_value=loaded), \
                        patch.object(cli, '_wait_service_unloaded', return_value=True), \
                        patch.object(cli, '_prepare_database_for_install', return_value=None), \
                        patch.object(cli, '_restore_database') as restore, \
                        patch.object(cli, '_restart_existing_service') as restart:
                    with self.assertRaises(RuntimeError) as caught:
                        cli._install_service(self.config, store)
                    restore.assert_not_called()
                    restart.assert_called_once()
                    self.assertIn('未创建数据库备份', str(caught.exception))
                    self.assertNotIn('已恢复备份', str(caught.exception))
                self.assertEqual([], self._backup_files())


    def _install_failure(self, where, failed_copy=FAILED_COPY, doctor_after=None, restarted=_RESTART_UNSET,
                         database_backup=None):
        """Run install-service with exactly one failure injected at ``where``.

        The Runner is always rebuilt (``_verify_runner_app`` says no) and the
        rebuild always reports a replaced bundle it could not trash, so every
        error raised after the build has to carry that notice.
        """
        self.restarts = Mock() if restarted is _RESTART_UNSET else Mock(return_value=restarted)
        rendered = self.root / "rendered/com.xiaoba.agentos.plist"
        rendered.parent.mkdir(parents=True, exist_ok=True)
        rendered.write_text("<plist/>\n", encoding="utf-8")
        idle = subprocess.CompletedProcess([], 0, "ok", "")
        running = subprocess.CompletedProcess([], 0, "state = running\npid = 4242", "")
        not_running = subprocess.CompletedProcess(
            [], 0, "state = spawn scheduled\nlast exit code = 78", ""
        )

        def fake_run(argv, **kwargs):
            if argv[0] == "/usr/bin/plutil":
                return subprocess.CompletedProcess(argv, 1 if where == "lint" else 0, "", "plist lint broke")
            if argv[:2] == ["/bin/launchctl", "bootstrap"]:
                return subprocess.CompletedProcess(
                    argv, 1 if where == "bootstrap" else 0, "", "bootstrap broke"
                )
            return idle

        store = Mock()
        if where == "initialize":
            store.initialize.side_effect = RuntimeError("synthetic initialize failure")

        after = doctor_after or {"ready_for_external_tasks": True}
        patches = [
            patch.object(Path, "home", return_value=self.root),
            patch.object(cli, "doctor", side_effect=[PREFLIGHT_READY, after]),
            patch.object(cli, "_verify_runner_app", return_value={"ok": False}),
            patch.object(cli, "_build_runner_app",
                         return_value={"ok": True, "previous_runner": REPLACED_RUNNER_NOTICE}),
            patch.object(cli, "_render_launchagent", return_value=rendered),
            patch.object(cli.subprocess, "run", side_effect=fake_run),
            patch.object(cli, "_launchctl_print", return_value=not_running if where == "timeout" else running),
            patch.object(cli, "_wait_service_unloaded", return_value=True),
            patch.object(cli, "_rotate_worker_logs",
                         side_effect=OSError(28, "No space left on device") if where == "rotate" else None,
                         return_value=[]),
            patch.object(cli, "_restart_existing_service", self.restarts),
            patch.object(cli, "_worker_runtime_status", return_value={"ok": True}),
            patch.object(cli, "_rollback_service", return_value=failed_copy),
        ]
        if where == "database":
            patches.append(
                patch.object(cli, "_prepare_database_for_install",
                             side_effect=RuntimeError("数据库需要迁移，迁移前要先备份"))
            )
        elif where == "database_oserror":
            patches.append(
                patch.object(cli, "_prepare_database_for_install",
                             side_effect=OSError(2, "No such file or directory"))
            )
        else:
            patches.append(patch.object(cli, "_prepare_database_for_install", return_value=database_backup))
            self.restores = Mock()
            patches.append(patch.object(cli, "_restore_database", self.restores))
        if where == "timeout":
            # The 8-second wait is real time; jump past the deadline instead.
            patches.append(patch.object(cli.time, "monotonic", side_effect=[0.0, 100.0]))
        with ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with self.assertRaises(RuntimeError) as caught:
                cli._install_service(self.config, store)
        return str(caught.exception)

    def test_bootstrap_failure_reports_where_the_failed_copy_was_put(self):
        message = self._install_failure("bootstrap", failed_copy=FAILED_COPY)
        self.assertIn("LaunchAgent 启动失败，已回滚", message)
        self.assertIn("失败副本：" + FAILED_COPY, message)

    def test_bootstrap_failure_says_so_when_the_rollback_left_no_failed_copy(self):
        message = self._install_failure("bootstrap", failed_copy=None)
        self.assertIn("没有留下失败副本", message)
        self.assertNotIn("失败副本：None", message)

    def test_every_failure_after_a_rebuilt_runner_still_reports_the_replaced_runner(self):
        for where in ("lint", "database", "database_oserror", "initialize", "doctor", "rotate", "bootstrap", "timeout"):
            with self.subTest(where=where):
                after = {"ready_for_external_tasks": False} if where == "doctor" else None
                message = self._install_failure(where, doctor_after=after)
                self.assertIn(REPLACED_RUNNER_NOTICE, message)

    def test_a_failure_after_the_old_service_stopped_puts_it_back(self):
        # Rotating logs or writing the new plist fails after the old service was
        # booted out: the old plist comes back from memory and the old service is
        # started again (round-23 review).
        destination = self.root / "Library" / "LaunchAgents" / "com.xiaoba.agentos.plist"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"previous plist")
        message = self._install_failure("rotate")
        self.assertIn("已写回原服务配置并重启原服务", message)
        self.assertIn(REPLACED_RUNNER_NOTICE, message)
        self.assertEqual(b"previous plist", destination.read_bytes())
        self.restarts.assert_called_once()
        self.assertFalse(destination.with_suffix(".plist.agentos-new").exists())

    def test_a_restart_launchctl_refused_is_reported_not_claimed(self):
        # Round-37 review: the old-service restart used to drop launchctl's
        # result, so the message said "重启原服务" even when bootstrap failed.
        destination = self.root / "Library" / "LaunchAgents" / "com.xiaoba.agentos.plist"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"previous plist")
        for where in ("rotate", "initialize", "database"):
            with self.subTest(where=where):
                message = self._install_failure(where, restarted=False)
                self.assertIn("原服务重启失败", message)
                self.assertNotIn("并重启原服务", message)
                self.assertIn(REPLACED_RUNNER_NOTICE, message)

    def test_a_service_that_was_not_loaded_is_not_said_to_be_restarted(self):
        destination = self.root / "Library" / "LaunchAgents" / "com.xiaoba.agentos.plist"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"previous plist")
        message = self._install_failure("rotate", restarted=None)
        self.assertIn("原服务此前未加载，未重启", message)
        self.assertNotIn("并重启原服务", message)

    def test_restart_existing_service_returns_what_launchctl_did(self):
        destination = self.root / "service.plist"
        destination.write_bytes(b"<plist/>")
        refused = subprocess.CompletedProcess([], 5, "", "Bootstrap failed: 5")
        accepted = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(cli.subprocess, "run", return_value=refused) as run:
            self.assertIs(False, cli._restart_existing_service("gui/501", destination, True))
            run.assert_called_once()
        with patch.object(cli.subprocess, "run", return_value=accepted):
            self.assertIs(True, cli._restart_existing_service("gui/501", destination, True))
        with patch.object(cli.subprocess, "run") as run:
            self.assertIsNone(cli._restart_existing_service("gui/501", destination, False))
            self.assertIsNone(cli._restart_existing_service("gui/501", self.root / "missing.plist", True))
            run.assert_not_called()

    def test_an_empty_paths_report_does_not_pass_the_install_gate(self):
        for paths in ({}, None):
            with self.subTest(paths=paths):
                preflight = dict(PREFLIGHT_READY)
                if paths is None:
                    preflight.pop("paths")
                else:
                    preflight["paths"] = paths
                with patch.object(Path, "home", return_value=self.root), \
                        patch.object(cli, "doctor", return_value=preflight):
                    with self.assertRaises(RuntimeError) as caught:
                        cli._install_service(self.config, Mock())
                self.assertIn("安装前检查未通过", str(caught.exception))

    def test_a_doctor_failure_never_prints_a_bare_none(self):
        message = self._install_failure(
            "doctor", doctor_after={"ready_for_external_tasks": False, "database_schema": None}
        )
        self.assertNotIn("None", message)
        self.assertIn("doctor 未通过：原因未知", message)
        self.assertIn(REPLACED_RUNNER_NOTICE, message)

    def test_a_doctor_failure_says_migration_only_when_one_happened(self):
        # Round-42 DSH review: with a current database nothing is migrated and
        # nothing restored, yet the message said "迁移后". An authorized
        # migration (it always has a backup) is still called one and restored.
        failing = {"ready_for_external_tasks": False, "problems": ["synthetic doctor refusal"]}
        message = self._install_failure("doctor", doctor_after=failing)
        self.assertNotIn("迁移后", message)
        self.assertIn("初始化后（数据库无需迁移） doctor 未通过：synthetic doctor refusal", message)
        self.assertIn("本次未创建数据库备份", message)
        self.restores.assert_not_called()
        backup = self.root / "agentos.sqlite3.backup"
        message = self._install_failure("doctor", doctor_after=failing, database_backup=backup)
        self.assertIn("迁移后 doctor 未通过：synthetic doctor refusal", message)
        self.assertIn("已恢复数据库备份", message)
        self.restores.assert_called_once()
        self.assertEqual(backup, self.restores.call_args.args[1])

    def test_a_doctor_failure_reports_the_failing_check_instead_of_the_schema_object(self):
        message = self._install_failure(
            "doctor",
            doctor_after={
                "ready_for_external_tasks": False,
                "database_schema": {"ok": False, "problems": ["schema version 6 < expected 7"]},
            },
        )
        # The reason is named by the check it came from; the raw check dictionary
        # is not the message.
        self.assertIn("database_schema: schema version 6 < expected 7", message)
        self.assertNotIn("{'ok'", message)
        self.assertIn(REPLACED_RUNNER_NOTICE, message)


if __name__ == "__main__":
    unittest.main()
