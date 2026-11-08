"""卡 6 尾巴 · 关闭监看中心.command：lsof 不可用时要如实说“查不了，无法确认是否已关闭”。

测试把脚本复制到临时目录再跑，脚本里的 DIR 就是临时目录：
没有 supervisor.pid 可停，假 lsof 也杀不到任何真实进程，不碰正在跑的监看中心。
运行：/usr/bin/python3 -m unittest discover -s tests
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "关闭监看中心.command"

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
        self.dir = Path(self.tmp.name)
        self.script = self.dir / "关闭监看中心.command"
        shutil.copyfile(SCRIPT, self.script)
        self.lsof = self.dir / "fake-lsof.sh"
        self.lsof.write_text(FAKE_LSOF, encoding="utf-8")
        self.lsof.chmod(self.lsof.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, mode):
        env = dict(os.environ, XIAOBA_LSOF=str(self.lsof), FAKE_MODE=mode)
        return subprocess.run(["/bin/bash", str(self.script)], capture_output=True, text=True, timeout=120, env=env)

    def test_reports_closed_when_lsof_says_nothing_is_listening(self):
        done = self.run_script("none")
        self.assertEqual(done.returncode, 0)
        self.assertIn("小八监看中心已关闭。", done.stdout)

    def test_says_it_cannot_tell_when_lsof_is_missing(self):
        done = self.run_script("broken")
        self.assertEqual(done.returncode, 0)          # 和以前一样不报错退出，只是不再谎称已关闭
        self.assertIn("查不了", done.stderr)
        self.assertIn("无法确认", done.stderr)
        self.assertNotIn("小八监看中心已关闭", done.stdout)

    def test_still_reports_still_running_when_port_is_listening(self):
        done = self.run_script("busy")
        self.assertEqual(done.returncode, 1)
        self.assertIn("监看中心仍在运行", done.stderr)

    def test_stop_logic_is_unchanged(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('LSOF="${XIAOBA_LSOF:-/usr/sbin/lsof}"', text)      # 默认还是系统那个 lsof
        self.assertIn('kill "$(cat "$PID_FILE")"', text)                  # 停看护进程
        self.assertIn('PIDS="$("$LSOF" -nP -tiTCP:8799 -sTCP:LISTEN 2>/dev/null)"', text)
        self.assertIn('[ -n "$PIDS" ] && kill $PIDS 2>/dev/null', text)    # 清残留
        self.assertIn('"$LSOF" -nP -iTCP:8799 -sTCP:LISTEN >/dev/null 2>&1', text)


if __name__ == "__main__":
    unittest.main()
