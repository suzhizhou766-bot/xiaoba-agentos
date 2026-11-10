"""卡 69：台账模块导入失败不能拦住接入口启动（卡 66 第 9 项的真反例）。

修复前 run.py 的 `configure` ImportError 没有兜底，进程带着 traceback 退出；
修复后只告警，接入口照常监听。
"""
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

CORE = Path('/Users/suzhizhou/Documents/小八AgentOS/xiaoba-agentos/development/integrated-candidate')
ENTRY = Path(__file__).resolve().parents[1] / 'run.py'


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class IngressLedgerImportGuardTests(unittest.TestCase):
    def test_entry_still_starts_when_ledger_module_breaks(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
            root = Path(temp).resolve()
            core = root / 'core-src'
            shutil.copytree(CORE / 'src', core, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (core / 'xiaoba_agentos/workday_ledger.py').write_text(
                "raise ImportError('TEST workday ledger')\n", encoding='utf-8')
            port = free_port()
            runtime = root / 'runtime'
            proc = subprocess.Popen(
                [sys.executable, '-B', str(ENTRY), '--core-source', str(core),
                 '--runtime', str(runtime), '--port', str(port)],
                cwd=str(ENTRY.parent), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                base = 'http://127.0.0.1:%d' % port
                deadline = time.monotonic() + 90
                started = False
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        self.fail('接入口带着台账 ImportError 退出了：\n' + proc.communicate()[0])
                    try:
                        with urllib.request.urlopen(base + '/healthz', timeout=.5) as response:
                            started = json.load(response)['ok']
                        break
                    except OSError:
                        time.sleep(.2)
                self.assertTrue(started, '接入口没有在临时端口 %d 起来' % port)
                self.assertIsNone(proc.poll(), '接入口启动后立刻退出了')
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.communicate(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate()


if __name__ == '__main__':
    unittest.main()
