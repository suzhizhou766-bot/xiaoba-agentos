"""监管进程身份：活着的无关 PID 不得被当成看护，也不得被关闭脚本发信号。

本文件用当前解释器直接调用入口，不依赖本机固定的 macOS Python，也不占用 8799。
"""
import io
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import xiaoba_monitor  # noqa: E402

MONITOR = Path(__file__).resolve().parent.parent
PORT = 54321


def supervisor_argv(port, root=MONITOR):
    return [
        sys.executable, "-B", str(root / "xiaoba_monitor.py"),
        "--supervise", "--detach", "--port", str(port),
    ]


class ArgvClassificationTests(unittest.TestCase):
    def test_same_directory_and_port_match(self):
        kind, detail = xiaoba_monitor.classify_supervisor_argv(supervisor_argv(PORT), PORT)
        self.assertEqual((kind, detail), (xiaoba_monitor.SUPERVISOR_MATCH, ""))

    def test_same_directory_other_port_is_not_a_match(self):
        kind, _detail = xiaoba_monitor.classify_supervisor_argv(supervisor_argv(PORT), PORT + 1)
        self.assertEqual(kind, xiaoba_monitor.SUPERVISOR_OTHER)

    def test_monitor_without_supervise_is_unknown(self):
        argv = [sys.executable, str(MONITOR / "xiaoba_monitor.py"), "--resolve-port"]
        kind, detail = xiaoba_monitor.classify_supervisor_argv(argv, PORT)
        self.assertEqual(kind, xiaoba_monitor.SUPERVISOR_UNKNOWN)
        self.assertIn("--supervise", detail)

    def test_unrelated_command_is_stale(self):
        kind, _detail = xiaoba_monitor.classify_supervisor_argv([sys.executable, "-c", "pass"], PORT)
        self.assertEqual(kind, xiaoba_monitor.SUPERVISOR_STALE)

    def test_ps_line_with_split_script_path_is_unknown(self):
        spaced = str(MONITOR.parent / "路径 含空格" / "xiaoba_monitor.py")
        kind, detail = xiaoba_monitor.classify_supervisor_command(
            "/usr/bin/python3 -B %s --supervise --port %d" % (spaced, PORT),
            PORT,
            root=MONITOR.parent / "路径 含空格",
        )
        self.assertEqual(kind, xiaoba_monitor.SUPERVISOR_UNKNOWN)
        self.assertIn("无法安全拆开", detail)


class LiveProcessTests(unittest.TestCase):
    def test_current_process_is_not_the_supervisor(self):
        kind, _detail = xiaoba_monitor.classify_supervisor_process(os.getpid(), PORT)
        self.assertEqual(kind, xiaoba_monitor.SUPERVISOR_STALE)

    def test_dead_pid_is_not_a_running_supervisor(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.assertEqual(0, child.wait(timeout=10))
        kind, _detail = xiaoba_monitor.classify_supervisor_process(child.pid, PORT)
        # 退出后的 pid 可能被立刻复用；复用后的命令也不是本目录监管进程。
        self.assertIn(kind, (xiaoba_monitor.SUPERVISOR_STALE, xiaoba_monitor.SUPERVISOR_UNKNOWN))
        self.assertNotEqual(kind, xiaoba_monitor.SUPERVISOR_MATCH)

    def test_cli_prints_stale_for_an_unrelated_pid(self):
        done = subprocess.run(
            [sys.executable, "-B", str(MONITOR / "xiaoba_monitor.py"),
             "--classify-supervisor-pid", str(os.getpid()), "--port", str(PORT)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "stale")

    def test_supervise_does_not_treat_unrelated_live_pid_as_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with unittest.mock.patch.object(xiaoba_monitor, "RUN_DIR", Path(tmp)), \
                    unittest.mock.patch.object(xiaoba_monitor, "port_in_use", return_value=True) as occupied, \
                    unittest.mock.patch("sys.stderr", stderr):
                (Path(tmp) / "supervisor.pid").write_text(str(os.getpid()))
                code = xiaoba_monitor.supervise({"port": PORT})
            self.assertEqual(code, 2)
            self.assertNotIn("看护进程已在运行", stderr.getvalue())
            self.assertIn("已被占用", stderr.getvalue())
            occupied.assert_called_once_with(PORT)

    def test_supervise_keeps_a_matching_supervisor_and_does_not_check_the_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with unittest.mock.patch.object(xiaoba_monitor, "RUN_DIR", Path(tmp)), \
                    unittest.mock.patch.object(
                        xiaoba_monitor, "classify_supervisor_process",
                        return_value=(xiaoba_monitor.SUPERVISOR_MATCH, ""),
                    ), \
                    unittest.mock.patch.object(xiaoba_monitor, "port_in_use") as occupied, \
                    unittest.mock.patch("sys.stderr", stderr):
                (Path(tmp) / "supervisor.pid").write_text(str(os.getpid()))
                code = xiaoba_monitor.supervise({"port": PORT})
            self.assertEqual(code, 2)
            self.assertIn("看护进程已在运行", stderr.getvalue())
            occupied.assert_not_called()

    def test_supervise_does_not_start_when_identity_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with unittest.mock.patch.object(xiaoba_monitor, "RUN_DIR", Path(tmp)), \
                    unittest.mock.patch.object(
                        xiaoba_monitor, "classify_supervisor_process",
                        return_value=(xiaoba_monitor.SUPERVISOR_UNKNOWN, "读不到进程命令行"),
                    ), \
                    unittest.mock.patch.object(xiaoba_monitor, "port_in_use") as occupied, \
                    unittest.mock.patch("sys.stderr", stderr):
                (Path(tmp) / "supervisor.pid").write_text(str(os.getpid()))
                code = xiaoba_monitor.supervise({"port": PORT})
            self.assertEqual(code, 2)
            self.assertIn("无法确认", stderr.getvalue())
            self.assertIn("未再启动第二个", stderr.getvalue())
            occupied.assert_not_called()


class ScriptContractTests(unittest.TestCase):
    def test_start_and_close_share_the_python_classifier(self):
        start = (MONITOR / "启动监看中心.command").read_text(encoding="utf-8")
        close = (MONITOR / "关闭监看中心.command").read_text(encoding="utf-8")
        self.assertIn("--classify-supervisor-pid", start)
        self.assertIn('--port "$PORT"', start)
        self.assertNotIn("/bin/ps", start)
        self.assertIn("--classify-supervisor-pid", close)
        self.assertIn('kill "$SUPERVISOR_PID"', close)
        self.assertNotIn('kill "$(cat "$PID_FILE")"', close)
        self.assertIn("不向 pid", close)


if __name__ == "__main__":
    unittest.main()
