"""端口监听探测三态的测试：三处共用 portprobe.listener_pids，检测不可用时不得当成"没有监听"。

运行方式与同目录其它测试一致（临时端口、临时目录，不接触真实数据）：
    /usr/bin/python3 -m unittest discover -s tests

"检测不可用"用两种方式模拟：lsof 找不到（find_lsof 返回 None）和 lsof 被拒（真的执行一个打印报错、
以 1 退出的假 lsof）。两种情况都要求：不接管、不结束任何进程、页面能看到警告。
"""
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_core import config as cfg, logs  # noqa: E402
from monitor_core.modules import portprobe  # noqa: E402
from monitor_core.modules import review as review_mod  # noqa: E402
from monitor_core.modules.keypool import KeyPoolModule  # noqa: E402
from monitor_core.modules.localtools import LocalToolsModule  # noqa: E402
from monitor_core.modules.review import ReviewModule  # noqa: E402
from monitor_core.web import Context  # noqa: E402

UNAVAILABLE_MATCH = "检测不可用"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def write_script(path, body):
    path.write_text("#!%s\n%s\n" % (sys.executable, body), encoding="utf-8")
    os.chmod(str(path), 0o755)
    return str(path)


def fake_lsof(tmp, body):
    """一个假的可执行文件，冒充 lsof 的失败行为。"""
    return write_script(Path(tmp) / ("fake-lsof-%d" % free_port()), body)


def unavailable_case(tmp, missing=True):
    """返回一个上下文管理器：把 find_lsof 换成"找不到"或"被拒"的假 lsof。"""
    if missing:
        return unittest.mock.patch.object(portprobe, "find_lsof", return_value=None)
    denied = fake_lsof(tmp, "import sys\nsys.stderr.write('lsof: Operation not permitted\\n')\nsys.exit(1)\n")
    return unittest.mock.patch.object(portprobe, "find_lsof", return_value=denied)


def log_lines(root, module_id):
    """读临时日志目录里属于某个模块的行。"""
    path = Path(root) / "logs" / "monitor.log"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("mod") == module_id:
            rows.append(entry)
    return rows


def warnings(root, module_id, needle):
    return [e for e in log_lines(root, module_id) if needle in e.get("msg", "")]


HOLDER = """
import socket, sys, time
sock = socket.socket()
sock.bind(("127.0.0.1", int(sys.argv[1])))
sock.listen(5)
time.sleep(120)
"""


class PortProbeTests(unittest.TestCase):
    """listener_pids 自己的三态：确认没有 / 确认存在 / 检测不可用。"""

    def test_real_listener_reports_listening_with_its_pid(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            state, pids, detail = portprobe.listener_pids(sock.getsockname()[1])
            self.assertEqual(state, portprobe.LISTENING)
            self.assertIn(os.getpid(), pids)
            self.assertEqual(detail, "")

    def test_unused_port_is_clear(self):
        self.assertEqual(portprobe.listener_pids(free_port()), (portprobe.CLEAR, [], ""))

    def test_bad_port_argument_is_unavailable(self):
        state, pids, detail = portprobe.listener_pids("not-a-port")
        self.assertEqual(state, portprobe.UNAVAILABLE)
        self.assertIn("端口号无效", detail)

    def test_missing_lsof_is_unavailable(self):
        with unittest.mock.patch.object(portprobe.shutil, "which", return_value=None):
            state, pids, detail = portprobe.listener_pids(free_port(), fallback=())
        self.assertEqual(state, portprobe.UNAVAILABLE)
        self.assertEqual(pids, [])
        self.assertIn("找不到 lsof", detail)
        self.assertIn(UNAVAILABLE_MATCH, detail)

    def test_fallback_path_is_used_when_which_finds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fake_lsof(tmp, "print(4242)\n")  # 假 lsof 报一个 pid，等价于真实输出
            with unittest.mock.patch.object(portprobe.shutil, "which", return_value=None):
                self.assertEqual(portprobe.find_lsof(fallback=(path,)), path)
                state, pids, detail = portprobe.listener_pids(free_port(), fallback=(path,))
            self.assertEqual((state, detail), (portprobe.LISTENING, ""))
            self.assertEqual(pids, [4242])

    def test_non_executable_fallback_is_skipped_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-executable"
            path.write_text("#!%s\n" % sys.executable, encoding="utf-8")   # 没有 +x
            with unittest.mock.patch.object(portprobe.shutil, "which", return_value=None):
                self.assertIsNone(portprobe.find_lsof(fallback=(str(path),)))
                state, _, detail = portprobe.listener_pids(free_port(), fallback=(str(path),))
            self.assertEqual(state, portprobe.UNAVAILABLE)
            self.assertIn(UNAVAILABLE_MATCH, detail)

    def test_denied_lsof_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fake_lsof(tmp, "import sys\nsys.stderr.write('lsof: Operation not permitted\\n')\nsys.exit(1)\n")
            state, pids, detail = portprobe.listener_pids(free_port(), lsof=path)
            self.assertEqual(state, portprobe.UNAVAILABLE)
            self.assertEqual(pids, [])
            self.assertIn("Operation not permitted", detail)
            self.assertIn(UNAVAILABLE_MATCH, detail)

    def test_other_failure_exit_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fake_lsof(tmp, "import sys\nsys.stderr.write('boom\\n')\nsys.exit(2)\n")
            state, _, detail = portprobe.listener_pids(free_port(), lsof=path)
            self.assertEqual(state, portprobe.UNAVAILABLE)
            self.assertIn("boom", detail)

    def test_exit_one_without_any_output_is_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fake_lsof(tmp, "import sys\nsys.exit(1)\n")
            self.assertEqual(portprobe.listener_pids(free_port(), lsof=path), (portprobe.CLEAR, [], ""))

    def test_timeout_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = fake_lsof(tmp, "import time\ntime.sleep(5)\n")
            state, pids, detail = portprobe.listener_pids(free_port(), timeout=0.3, lsof=path)
            self.assertEqual(state, portprobe.UNAVAILABLE)
            self.assertEqual(pids, [])
            self.assertIn("超时", detail)
            self.assertIn(UNAVAILABLE_MATCH, detail)


FAKE_CONTROLLER = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        raw = b'{"ok": true, "services": {}}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
HTTPServer(("127.0.0.1", PORT_PLACEHOLDER), H).serve_forever()
"""


class LocalToolsUnavailableTests(unittest.TestCase):
    """本地工具控制器：检测不可用时不接管旧控制器，警告页面能看到。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.port = free_port()
        self.script = root / "local_tools_control_server.py"
        self.script.write_text(FAKE_CONTROLLER.replace("PORT_PLACEHOLDER", str(self.port)), encoding="utf-8")
        config = cfg.load(root / "none.json", {
            "localtools": {"port": self.port, "script": str(self.script), "python": sys.executable},
            "log": {"dir": str(root / "logs")}, "watchdog": {"backoff_seconds": [0.1]}})
        self.root = root
        self.module = LocalToolsModule(Context(config, logs.setup(str(root / "logs")), free_port()))

    def tearDown(self):
        if self.module.process:
            self.module.process.stop()
        self.tmp.cleanup()

    def test_detection_unavailable_leaves_controller_alone(self):
        for missing in (True, False):
            with self.subTest(lsof="missing" if missing else "denied"):
                stray = subprocess.Popen([sys.executable, str(self.script)])
                try:
                    self.assertTrue(wait_for(self.module._port_busy))
                    self.module.probe_warning = ""
                    with unavailable_case(self.root, missing):
                        self.module._retire_previous()
                        self.module.start()
                    self.assertIsNone(self.module.process)                 # 不接管：没有拉起新控制器
                    self.assertIsNone(stray.poll())                        # 旧控制器还活着，没有被结束
                    self.assertIn("端口 %d 被其他程序占用" % self.port, self.module.blocked)
                    self.assertIn(UNAVAILABLE_MATCH, self.module.blocked)
                    health = self.module.health()
                    self.assertEqual(health["status"], "failed")
                    self.assertIn(UNAVAILABLE_MATCH, health["detail"])     # 页面能看到
                finally:
                    if stray.poll() is None:
                        stray.kill()
                        stray.wait(5)
                entries = warnings(self.root, "localtools", "detection unavailable")
                self.assertTrue(entries and entries[-1].get("level") == "warning")

    def test_detection_unavailable_with_free_port_still_starts_and_warns(self):
        with unavailable_case(self.root, True):
            self.module.start()
        self.assertTrue(wait_for(lambda: self.module.health()["status"] == "ok"))
        self.assertIn(UNAVAILABLE_MATCH, self.module.health()["detail"])
        self.module.stop()


class FakeBackend:
    """模仿裁决后端的接口与它自己的来源、csrf 校验（与 test_monitor.py 的同名夹具一致）。"""

    def __init__(self):
        self.port = free_port()
        self.csrf = "backend-secret"
        self.state = {"ok": True, "csrf": self.csrf, "revision": "r1", "total": 1, "holds": 0,
                      "reviews": [], "sync": {"ok": True, "state": "synced", "at": "now"}, "items": []}
        backend = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/state":
                    return self.reply(200, backend.state)
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                self.reply(404, {"error": "not found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class ReviewUnavailableTests(unittest.TestCase):
    """裁决模块：检测不可用时不认领上一次的后端、也不另起新后端，警告页面能看到。"""

    def _module(self, root, backend_port, manage=True):
        config = cfg.load(Path(root) / "none.json", {"agentos_project": str(root), "agentos_config": str(Path(root) / "a.json"),
                                                     "review": {"port": backend_port}, "log": {"dir": str(Path(root) / "logs")}})
        config["root"] = str(root)
        return ReviewModule(Context(config, logs.setup(str(Path(root) / "logs")), free_port()), manage=manage)

    def test_detection_unavailable_does_not_adopt_or_spawn(self):
        backend = FakeBackend()
        try:
            for missing in (True, False):
                with self.subTest(lsof="missing" if missing else "denied"):
                    with tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp)
                        (root / "run").mkdir()
                        (root / "integrations" / "review-center").mkdir(parents=True)
                        (root / "integrations" / "review-center" / "server.py").write_text("# fake\n", encoding="utf-8")
                        module = self._module(root, backend.port)
                        module.state_file.write_text(json.dumps({"pid": os.getpid(), "port": backend.port}),
                                                     encoding="utf-8")
                        with unavailable_case(root, missing):
                            self.assertIsNone(module._previous_backend())   # 检测不可用：不认领
                            self.assertIn(UNAVAILABLE_MATCH, module.probe_warning)
                            self.assertIn(UNAVAILABLE_MATCH, module.health()["detail"])
                            module.start()                                  # 后端可能还在：不另起一个
                            time.sleep(1.5)
                            self.assertIsNone(module.process)              # 没有另起新后端
                            self.assertNotEqual(module.mode, "managed")
                            code, payload = module.call("/api/state")      # 旧后端没被动、还在线
                            self.assertEqual((code, payload.get("csrf")), (200, backend.csrf))
                            module.stop()
                        entries = warnings(root, "review", "detection unavailable")
                        self.assertTrue(entries and entries[-1].get("level") == "warning")
        finally:
            backend.close()

    def test_detection_available_still_adopts_previous_backend(self):
        backend = FakeBackend()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "run").mkdir()
                module = self._module(root, backend.port, manage=False)
                listener = module._listener_pid(backend.port)               # 真 lsof：本进程就是监听者
                self.assertEqual(listener, os.getpid())
                self.assertEqual(module.probe_warning, "")
                module.state_file.write_text(json.dumps({"pid": listener, "port": backend.port}), encoding="utf-8")
                self.assertEqual(module._previous_backend(), (listener, backend.port))
                module.state_file.write_text(json.dumps({"pid": 1, "port": backend.port}), encoding="utf-8")
                self.assertIsNone(module._previous_backend())               # 进程号对不上：不认领
        finally:
            backend.close()


class KeyPoolUnavailableTests(unittest.TestCase):
    """Key 池：检测不可用时不结束旧代理、保留认领记录，警告页面能看到。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.port = free_port()
        config = cfg.load(root / "none.json", {
            "keypool": {"port": self.port, "file": str(root / "keypool.json"), "usage_dir": str(root / "usage")},
            "log": {"dir": str(root / "logs")}})
        self.root = root
        self.script = write_script(root / "holder.py", HOLDER)
        self.module = KeyPoolModule(Context(config, logs.setup(str(root / "logs")), free_port()))
        # 认领记录落到临时目录，避免往仓库副本的 run/（通常不存在且 gitignore）里写。
        self.module.state_file = root / "run" / "keypool.json"
        self.module.state_file.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.module.process:
            self.module.process.stop()
        self.tmp.cleanup()

    def holder(self):
        """一个独立进程冒充"上次留下的代理"占着端口。"""
        proc = subprocess.Popen([sys.executable, self.script, str(self.port)])
        self.assertTrue(wait_for(lambda: self.module._port_busy(), 10))
        return proc

    def test_detection_unavailable_leaves_proxy_and_record_alone(self):
        for missing in (True, False):
            with self.subTest(lsof="missing" if missing else "denied"):
                old = self.holder()
                try:
                    self.module.state_file.write_text(json.dumps({"pid": old.pid, "port": self.port}), encoding="utf-8")
                    self.module.probe_warning = ""
                    with unavailable_case(self.root, missing):
                        self.module.start()
                    self.assertIsNone(self.module.process)                  # 不接管：代理没有启动
                    self.assertIsNone(old.poll())                           # 旧代理还活着，没有被结束
                    self.assertIn("端口 %d 被其他程序占用" % self.port, self.module.blocked)
                    self.assertIn(UNAVAILABLE_MATCH, self.module.blocked)
                    health = self.module.process_health()
                    self.assertEqual(health["status"], "failed")
                    self.assertIn(UNAVAILABLE_MATCH, health["detail"])      # 页面能看到
                    self.assertTrue(self.module.state_file.exists())        # 认领记录保留，等检测恢复
                finally:
                    if old.poll() is None:
                        old.kill()
                        old.wait(5)
                entries = warnings(self.root, "keypool", "detection unavailable")
                self.assertTrue(entries and entries[-1].get("level") == "warning")

    def test_retire_keeps_record_until_detection_recovers(self):
        old = self.holder()
        try:
            self.module.state_file.write_text(json.dumps({"pid": old.pid, "port": self.port}), encoding="utf-8")
            with unavailable_case(self.root, True):
                self.module._retire_previous()
                self.assertTrue(self.module.state_file.exists())            # 不删记录，下次启动还能认出它
                self.assertIn(UNAVAILABLE_MATCH, self.module.probe_warning)
            self.assertIsNone(old.poll())                                   # 没有结束任何进程
        finally:
            if old.poll() is None:
                old.kill()
                old.wait(5)
        self.module._retire_previous()                                      # 检测恢复：记录过期，清掉，不结束进程
        self.assertEqual(self.module.probe_warning, "")
        self.assertFalse(self.module.state_file.exists())


class TimeoutUnavailableTests(unittest.TestCase):
    """lsof 卡住不返回：调用方看到的是"检测不可用"，不是"没有监听"。"""

    def test_slow_lsof_times_out_and_does_not_adopt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir()
            slow = fake_lsof(tmp, "import time\ntime.sleep(5)\n")     # 冒充被卡住的 lsof
            real = portprobe.listener_pids
            port = free_port()

            def slow_probe(target, **kw):                              # 只把超时缩短，逻辑仍走真代码
                kw["timeout"] = 0.3
                return real(target, **kw)

            config = cfg.load(root / "none.json", {"agentos_project": str(root), "agentos_config": str(root / "a.json"),
                                                   "review": {"port": port}, "log": {"dir": str(root / "logs")}})
            config["root"] = str(root)
            module = ReviewModule(Context(config, logs.setup(str(root / "logs")), free_port()), manage=False)
            module.state_file.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
            with unittest.mock.patch.object(portprobe, "find_lsof", return_value=slow), \
                    unittest.mock.patch.object(review_mod, "listener_pids", side_effect=slow_probe):
                self.assertIsNone(module._previous_backend())          # 超时：不认领
                self.assertIn("超时", module.probe_warning)
                self.assertIn(UNAVAILABLE_MATCH, module.health()["detail"])
            self.assertTrue(warnings(root, "review", "detection unavailable"))


if __name__ == "__main__":
    unittest.main()
