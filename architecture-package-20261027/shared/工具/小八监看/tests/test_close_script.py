"""关闭监看中心.command：--close-report；停止与端口探测分离。

测试把候选监看目录复制到临时目录再跑；假 lsof 不碰真 8799。
"""
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

MONITOR = Path(__file__).resolve().parent.parent
PY = "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"


def free_port() -> int:
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    if port == 8799:
        return free_port()
    return port

FAKE_LSOF = """#!/bin/bash
mode="${FAKE_MODE:-none}"
if [ "$mode" = "broken" ]; then
  echo "lsof: command not found" >&2
  exit 127
fi
if [ "$mode" = "none" ]; then
  exit 1
fi
for a in "$@"; do
  case "$a" in
    -t*) echo 4194303; exit 0 ;;
  esac
done
echo "COMMAND   PID   USER   FD   TYPE  DEVICE SIZE/OFF NODE NAME"
exit 0
"""


class CloseScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "monitor"
        shutil.copytree(
            MONITOR, self.dir,
            ignore=shutil.ignore_patterns(
                "__pycache__", "*.pyc", "run", "logs", "node_modules", "tests", "小八3D导演台",
            ),
        )
        self.script = self.dir / "关闭监看中心.command"
        self.lsof = Path(self.tmp.name) / "fake-lsof.sh"
        self.lsof.write_text(FAKE_LSOF, encoding="utf-8")
        self.lsof.chmod(self.lsof.stat().st_mode | stat.S_IXUSR)
        # 避开默认/生产 8799：probe_port_status 拒绝把它当成功依据
        self.port = free_port()
        (self.dir / "monitor.json").write_text(json.dumps({"port": self.port}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, mode, env_extra=None):
        env = dict(os.environ, XIAOBA_LSOF=str(self.lsof), FAKE_MODE=mode)
        env["XIAOBA_MONITOR_PORT"] = str(self.port)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["/bin/bash", str(self.script)], capture_output=True, text=True, timeout=120, env=env,
        )

    def _json(self, done):
        for line in (done.stdout or "").splitlines():
            if line.startswith("{"):
                return json.loads(line)
        return None

    def test_reports_closed_when_lsof_says_nothing_is_listening(self):
        done = self.run_script("none")
        report = self._json(done)
        self.assertIsNotNone(report, done.stdout)
        self.assertTrue(report["closed_ok"], report)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("小八监看中心已关闭。", done.stdout)
        self.assertEqual(report["port_probe"]["status"], "free")

    def test_says_it_cannot_tell_when_lsof_is_missing(self):
        done = self.run_script("broken")
        report = self._json(done)
        self.assertIsNotNone(report)
        self.assertFalse(report["closed_ok"])
        self.assertEqual(report["port_probe"]["status"], "unobservable")
        self.assertEqual(done.returncode, 4)
        self.assertIn("查不了", done.stderr)
        self.assertNotIn("小八监看中心已关闭", done.stdout)

    def test_still_reports_not_closed_when_port_is_listening(self):
        done = self.run_script("busy")
        report = self._json(done)
        self.assertIsNotNone(report)
        self.assertFalse(report["closed_ok"])
        self.assertNotEqual(0, done.returncode)
        self.assertNotIn("小八监看中心已关闭", done.stdout)
        # 无 claim：stop 为 already_stopped；端口 busy → 不得 closed_ok
        self.assertEqual(report["port_probe"]["status"], "busy")
        self.assertTrue(
            ("仍有监听" in done.stderr) or ("关闭未成功" in done.stderr) or ("仍有监听" in (report.get("user_message") or "")),
            (done.stderr, report),
        )

    def test_invalid_config_fails_before_lsof(self):
        (self.dir / "monitor.json").write_text(json.dumps({"port": 0}))
        done = self.run_script("busy", env_extra={"XIAOBA_MONITOR_PORT": "0"})
        self.assertNotEqual(0, done.returncode)
        self.assertIn("关闭失败", done.stderr)

    def test_close_logic_uses_close_report_not_lsof_kill(self):
        text = (MONITOR / "关闭监看中心.command").read_text(encoding="utf-8")
        self.assertIn("--resolve-port", text)
        self.assertIn("--close-report", text)
        self.assertIn("monitor_core/lifecycle.py", text)
        self.assertIn("XIAOBA_LSOF", text)
        self.assertNotIn('kill "$(cat "$PID_FILE")"', text)
        self.assertNotIn("tiTCP:${PORT}", text)
        self.assertNotIn("tiTCP:8799", text)
        self.assertIn(PY, text)

    def test_foreign_listener_is_not_signaled_by_close_script(self):
        done = self.run_script("busy")
        report = self._json(done)
        self.assertIsNotNone(report)
        self.assertEqual(report["stop"].get("signals", 0), 0)
        self.assertFalse(report["closed_ok"])


if __name__ == "__main__":
    unittest.main()
