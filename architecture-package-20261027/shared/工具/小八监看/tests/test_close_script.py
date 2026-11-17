"""关闭监看中心.command：按配置解析端口；lsof 不可用时如实说“查不了”。

测试把候选监看目录复制到临时目录再跑（需 xiaoba_monitor.py 做 --resolve-port），
假 lsof 杀不到任何真实进程，不碰正在跑的监看中心。
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

MONITOR = Path(__file__).resolve().parent.parent
PY = "/Users/suzhizhou/Documents/私人助理/配置/python/cpython-3.14.7-macos-aarch64-none/bin/python3.14"

FAKE_LSOF = """#!/bin/bash
# 假的 lsof：FAKE_MODE 决定它怎么回答（none=没监听，broken=跑不起来，busy=有监听）
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
        # 缺省配置 → resolve-port=8799；假 lsof 接管，不会查真 8799。
        self.script = self.dir / "关闭监看中心.command"
        self.lsof = Path(self.tmp.name) / "fake-lsof.sh"
        self.lsof.write_text(FAKE_LSOF, encoding="utf-8")
        self.lsof.chmod(self.lsof.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, mode, env_extra=None):
        env = dict(os.environ, XIAOBA_LSOF=str(self.lsof), FAKE_MODE=mode)
        env.pop("XIAOBA_MONITOR_PORT", None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(["/bin/bash", str(self.script)], capture_output=True, text=True, timeout=120, env=env)

    def test_reports_closed_when_lsof_says_nothing_is_listening(self):
        done = self.run_script("none")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("小八监看中心已关闭。", done.stdout)

    def test_says_it_cannot_tell_when_lsof_is_missing(self):
        done = self.run_script("broken")
        self.assertEqual(done.returncode, 0)
        self.assertIn("查不了", done.stderr)
        self.assertIn("无法确认", done.stderr)
        self.assertNotIn("小八监看中心已关闭", done.stdout)

    def test_still_reports_still_running_when_port_is_listening(self):
        done = self.run_script("busy")
        self.assertEqual(done.returncode, 1)
        self.assertIn("监看中心仍在运行", done.stderr)

    def test_invalid_config_fails_before_lsof(self):
        (self.dir / "monitor.json").write_text(json.dumps({"port": 0}))
        done = self.run_script("busy")
        self.assertNotEqual(0, done.returncode)
        self.assertIn("关闭失败", done.stderr)

    def test_close_logic_uses_resolved_port_not_hardcoded_8799(self):
        text = (MONITOR / "关闭监看中心.command").read_text(encoding="utf-8")
        self.assertIn('LSOF="${XIAOBA_LSOF:-/usr/sbin/lsof}"', text)
        self.assertIn("--resolve-port", text)
        self.assertIn('kill "$(cat "$PID_FILE")"', text)
        self.assertIn('tiTCP:${PORT}', text)
        self.assertIn('iTCP:${PORT}', text)
        self.assertNotIn("tiTCP:8799", text)
        self.assertIn(PY, text)


if __name__ == "__main__":
    unittest.main()
