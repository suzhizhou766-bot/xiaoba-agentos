import plistlib
import unittest
from pathlib import Path
import os
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]


class RunnerBundleTest(unittest.TestCase):
    def test_runner_bundle_identity_and_launchagent_entrypoint(self):
        with (ROOT / "macos" / "runner" / "Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        self.assertEqual("com.xiaoba.agentos.runner", info["CFBundleIdentifier"])
        self.assertEqual("XiaobaAgentOSRunner", info["CFBundleExecutable"])
        template = (ROOT / "launchd" / "com.xiaoba.agentos.plist.template").read_text(encoding="utf-8")
        self.assertIn("@@RUNNER_EXECUTABLE@@", template)
        self.assertIn("XIAOBA_PROJECT_DIR", template)
        self.assertNotIn("<string>/bin/zsh</string>", template)


class WorkerLauncherTest(unittest.TestCase):
    LAUNCHER = ROOT / "launchd" / "worker" / "bin" / "agentos"

    def _run(self, home: Path, *args: str):
        return subprocess.run(["/bin/zsh", str(self.LAUNCHER), *args], capture_output=True, text=True,
                              env={"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": "/workspace/src"})

    def test_service_template_targets_the_launcher_and_carries_no_workspace_pythonpath(self):
        template = (ROOT / "launchd" / "com.xiaoba.agentos.plist.template").read_text(encoding="utf-8")
        self.assertIn("<string>@@PROJECT_DIR@@/launchd/worker</string>", template)
        self.assertNotIn("PYTHONPATH", template)
        self.assertTrue(os.access(self.LAUNCHER, os.X_OK))

    def test_launcher_hands_over_to_the_stable_entrypoint_without_workspace_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            scripts = home / ".agents" / "skills" / "xiaoba-agentos" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "xiaoba").write_text(
                '#!/bin/zsh\nprint -r -- "args=$*"\nprint -r -- "pythonpath=${PYTHONPATH-unset}"\n', encoding="utf-8")
            (scripts / "xiaoba").chmod(0o755)
            result = self._run(home, "--config", "/tmp/some config.json", "run")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("args=--config /tmp/some config.json run", result.stdout)
            self.assertIn("pythonpath=unset", result.stdout)

    def test_launcher_refuses_anything_else(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertEqual(78, self._run(home, "--config", "/tmp/c.json", "run").returncode)
            for args in (("run",), ("--config", "/tmp/c.json", "doctor"), ("--config", "/tmp/c.json", "run", "--once")):
                self.assertEqual(64, self._run(home, *args).returncode)


if __name__ == "__main__":
    unittest.main()
