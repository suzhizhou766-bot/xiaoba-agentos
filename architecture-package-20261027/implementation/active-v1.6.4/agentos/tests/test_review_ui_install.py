"""The portal update refuses drift and can undo in memory without a backup."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos import review_ui_install as ui


class ReviewUIInstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.project, self.assistant = self.root / "project", self.root / "assistant"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.config = SimpleNamespace(project_dir=self.project, runtime_dir=self.runtime,
                                      memory={"obsidian_vault": str(self.assistant / "vault")})
        self.before, entries = {}, []
        for name in ("workflow_query.py", "review_action.py", "xiaoba-monitor.html", "server.py"):
            base, relative = ui.TARGETS[name]
            target = (self.project if base == "project" else self.assistant) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            old = None if name == "workflow_query.py" else ("before " + name).encode()
            new = ("after " + name).encode()
            if old is not None:
                target.write_bytes(old)
            self.before[target] = old
            (self.assets / name).write_bytes(new)
            entries.append({"asset": name, "base": base, "relative": relative,
                            "before_sha256": ui.sha(old) if old else None, "after_sha256": ui.sha(new)})
        (self.assets / "manifest.json").write_text(json.dumps({"version": 1, "files": entries}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_is_read_only(self):
        report = ui.run(self.config, assets=self.assets)
        self.assertFalse(report["up_to_date"])
        for path, old in self.before.items():
            self.assertEqual(ui.current(path), old)
        self.assertFalse((self.runtime / "review-ui-install.lock").exists())

    def test_apply_readback_and_repeat_without_backup(self):
        report = ui.run(self.config, apply=True, assets=self.assets)
        self.assertTrue(report["up_to_date"])
        self.assertTrue(ui.run(self.config, assets=self.assets)["up_to_date"])
        self.assertFalse(ui.run(self.config, apply=True, assets=self.assets)["restart_monitor_required"])
        self.assertEqual([p.name for p in self.runtime.iterdir()], ["review-ui-install.lock"])

    def test_drift_is_refused_before_any_write(self):
        changed = self.assistant / ui.TARGETS["xiaoba-monitor.html"][1]
        changed.write_text("another editor's work")
        with self.assertRaisesRegex(ValueError, "rebase required"):
            ui.run(self.config, apply=True, assets=self.assets)
        self.assertEqual(changed.read_text(), "another editor's work")
        self.assertFalse((self.project / ui.TARGETS["workflow_query.py"][1]).exists())

    def test_post_replace_failure_rolls_back_original_bytes(self):
        real = ui.atomic
        calls = []
        def failing(path, data, mode):
            real(path, data, mode)
            calls.append(path)
            if len(calls) == 2:
                raise OSError("failure after replacement")
        with patch.object(ui, "atomic", failing):
            with self.assertRaises(OSError):
                ui.run(self.config, apply=True, assets=self.assets)
        for path, old in self.before.items():
            self.assertEqual(ui.current(path), old)

    def test_modified_asset_is_refused(self):
        (self.assets / "server.py").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "asset hash mismatch"):
            ui.run(self.config, apply=True, assets=self.assets)

    def test_symlink_target_is_refused(self):
        target = self.project / ui.TARGETS["workflow_query.py"][1]
        target.symlink_to(self.root / "somewhere")
        with self.assertRaisesRegex(ValueError, "symlink"):
            ui.run(self.config, apply=True, assets=self.assets)
