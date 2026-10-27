import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos import interpreter

ROOT = Path(__file__).resolve().parents[1]
PINNED = str(interpreter.PINNED_PYTHON)
ENTRY_SCRIPTS = (
    ROOT / ".agents/skills/xiaoba-agentos/scripts/xiaoba",
    ROOT / ".agents/skills/xiaoba-agentos/scripts/verify-release",
)


class InterpreterPinTest(unittest.TestCase):
    def test_pinned_interpreter_is_314_by_absolute_path(self):
        self.assertTrue(interpreter.PINNED_PYTHON.is_absolute())
        self.assertEqual((3, 14), interpreter.PINNED_VERSION)
        self.assertTrue(PINNED.endswith("/bin/python3.14"))
        self.assertIn("/配置/python/", PINNED)
        self.assertNotIn("/.runtime/", PINNED)

    def test_launcher_execs_the_pinned_interpreter_only(self):
        text = (ROOT / "bin/agentos").read_text(encoding="utf-8")
        self.assertEqual(1, text.count(PINNED))
        self.assertNotIn("/usr/bin/python3", text)
        self.assertIn('exec "${PYTHON}" -m xiaoba_agentos.cli "$@"', text)

    def test_entry_scripts_start_on_the_pinned_interpreter(self):
        for script in ENTRY_SCRIPTS:
            with self.subTest(script=script.name):
                first = script.read_text(encoding="utf-8").splitlines()[0]
                self.assertEqual("#!" + PINNED, first)

    def test_no_core_code_hardcodes_the_system_python(self):
        offenders = []
        for base in (ROOT / "src", ROOT / "scripts", ROOT / "bin", ROOT / "launchd"):
            for path in base.rglob("*"):
                if path.is_file() and path.suffix not in {".pyc"}:
                    try:
                        text = path.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue
                    if "/usr/bin/python3" in text:
                        offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual([], offenders)

    def test_launcher_fails_closed_when_the_interpreter_is_missing(self):
        text = (ROOT / "bin/agentos").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "no-python" / "python3.14"
            launcher = Path(directory) / "bin" / "agentos"
            launcher.parent.mkdir()
            launcher.write_text(text.replace(PINNED, str(missing)), encoding="utf-8")
            launcher.chmod(0o755)
            done = subprocess.run([str(launcher), "doctor"], capture_output=True, text=True,
                                  timeout=30, check=False)
        self.assertEqual(78, done.returncode)
        self.assertIn("AgentOS core interpreter unavailable", done.stderr)
        self.assertEqual("", done.stdout)

    def test_pinned_interpreter_exists_on_this_machine_and_is_314(self):
        if not interpreter.PINNED_PYTHON.is_file():
            self.skipTest("pinned core interpreter not installed on this machine")
        done = subprocess.run([PINNED, "-I", "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                              capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual("3.14", done.stdout.strip())
        report = interpreter.describe()
        self.assertTrue(report["pinned_available"])
        if os.path.realpath(sys.executable) == os.path.realpath(PINNED):
            self.assertTrue(report["running_is_pinned"])
            self.assertEqual(interpreter.PINNED_VERSION, sys.version_info[:2])
        else:
            self.assertFalse(report["running_is_pinned"])

    def test_build_refuses_any_interpreter_but_the_pinned_one(self):
        import importlib.util
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            spec = importlib.util.spec_from_file_location("build_release_pin_check", ROOT / "scripts/build_release.py")
            build = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(build)
        finally:
            sys.path.remove(str(ROOT / "scripts"))
        self.assertEqual(interpreter.PINNED_PYTHON, build.pinned_core_python(ROOT))
        with self.assertRaisesRegex(RuntimeError, "pinned core interpreter"):
            build.require_core_python(ROOT, running="/usr/bin/python3")
        if interpreter.PINNED_PYTHON.is_file():
            build.require_core_python(ROOT, running=PINNED)
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory)
            (fake / "src/xiaoba_agentos").mkdir(parents=True)
            (fake / "src/xiaoba_agentos/interpreter.py").write_text("x = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not declare"):
                build.require_core_python(fake, running=PINNED)

    def test_describe_reports_running_and_pinned(self):
        report = interpreter.describe()
        self.assertEqual(PINNED, report["pinned"])
        self.assertEqual("3.14", report["pinned_version"])
        self.assertRegex(report["running_version"], r"^3\.\d+\.\d+$")
        self.assertIsInstance(report["running_is_pinned"], bool)
        self.assertIsInstance(report["pinned_available"], bool)


if __name__ == "__main__":
    unittest.main()
