"""卡 69：启动脚本必须让监看中心脱离调用方的会话和进程组。

只跑临时副本、临时端口，不碰线上的 8799，也不启动线上监看中心。
"""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request

MONITOR = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def healthy(port, timeout=.5):
    try:
        with urllib.request.urlopen('http://127.0.0.1:%d/healthz' % port, timeout=timeout) as response:
            return response.status == 200
    except Exception:
        return False


class DetachedLaunchTests(unittest.TestCase):
    def test_supervisor_leaves_callers_session_and_survives_group_kill(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp).resolve()
            copy = root / 'monitor'
            # 整份复制到临时目录：脚本的 logs/、run/、monitor.json 都只落在临时副本里。
            shutil.copytree(MONITOR, copy, ignore=shutil.ignore_patterns(
                '__pycache__', '*.pyc', 'run', 'logs', 'node_modules', 'tests', '小八3D导演台'))
            port = free_port()
            (root / 'agentos.json').write_text('{}')
            (copy / 'monitor.json').write_text(json.dumps({
                'port': port,
                'agentos_project': str(root / 'agentos-project'),
                'agentos_config': str(root / 'agentos.json'),
                'review': {'port': free_port(), 'start_timeout_seconds': 5},
                'keypool': {'port': free_port(), 'file': str(root / 'keypool.json'),
                            'usage_dir': str(root / 'keypool-usage')},
                'localtools': {'port': free_port(), 'script': str(root / 'missing-local-tools.py')},
                'stage': {'dir': str(root / 'dist'), 'library_dir': str(root / 'library')},
                'taskcards': {'dirs': [], 'ledger': str(root / 'taskcards.jsonl'), 'receipt_root': str(root)},
                'log': {'dir': str(root / 'logs')},
            }))
            env = dict(os.environ, XIAOBA_MONITOR_PORT=str(port), XIAOBA_MONITOR_NO_BROWSER='1')
            # 调用方自己就是会话和进程组的组长，等同“终端里的一条命令”。
            caller = subprocess.Popen(['/bin/bash', str(copy / '启动监看中心.command')],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
            supervisor = None
            try:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline and not healthy(port):
                    time.sleep(.1)
                self.assertTrue(healthy(port), '监看中心没有在临时端口 %d 起来' % port)
                pid_file = copy / 'run' / 'supervisor.pid'
                self.assertTrue(pid_file.is_file(), '启动脚本没有留下 run/supervisor.pid')
                supervisor = int(pid_file.read_text().strip())
                self.assertNotEqual(caller.pid, os.getsid(supervisor), 'supervisor 仍在调用方的会话里')
                self.assertNotEqual(caller.pid, os.getpgid(supervisor), 'supervisor 仍在调用方的进程组里')
                # 模拟调用方进程组被整组终止（关终端、宿主回收后台命令都会这样）。
                try:
                    os.killpg(caller.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    caller.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not healthy(port):
                    time.sleep(.2)
                self.assertTrue(healthy(port), '调用方进程组被终止后，监看中心也不响应了')
                os.kill(supervisor, 0)
            finally:
                if caller.poll() is None:
                    try:
                        os.killpg(caller.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if supervisor:
                    try:
                        os.kill(supervisor, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and healthy(port):
                        time.sleep(.2)
                # 兜底：临时端口上还有监听就结束它，测试不许留下进程。
                strays = subprocess.run(['/usr/sbin/lsof', '-nP', '-tiTCP:%d' % port, '-sTCP:LISTEN'],
                                        capture_output=True, text=True).stdout.split()
                for pid in strays:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except (ProcessLookupError, ValueError):
                        pass


if __name__ == '__main__':
    unittest.main()
