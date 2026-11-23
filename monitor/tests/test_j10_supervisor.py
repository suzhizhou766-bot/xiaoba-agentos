"""J10：回环健康探测避开系统代理，启动成功同时要求有效看护。

仅使用临时副本和临时端口。复用75A夹具的准备/清理方法，不继承其测试类。
"""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock
import urllib.request

try:
    from . import test_card69_detach as detach_tests, test_card75a_port as port_tests
except ImportError:
    import test_card69_detach as detach_tests
    import test_card75a_port as port_tests
import xiaoba_monitor


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"ok": true}'
        self.send_response(200 if self.path == "/healthz" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def healthy_listener(port=0):
    """退出时先关闭监听，避免夹具把测试进程自身当作残留。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), _HealthHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def local_health_status(port):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


class LoopbackHealthTests(unittest.TestCase):
    def test_healthy_avoids_macos_proxy_discovery_without_proxy_environment(self):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.lower().endswith("_proxy")
        }
        # patch对象在非macOS上也可创建；本机macOS旧实现会调用它并令healthy失败。
        with healthy_listener() as port, mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(urllib.request, "_opener", None), \
                mock.patch.object(urllib.request, "_get_proxies", create=True,
                                  side_effect=AssertionError("不得读取系统代理")) as proxies, \
                mock.patch.object(urllib.request, "_get_proxy_settings", create=True,
                                  side_effect=AssertionError("不得读取系统代理绕过设置")) as settings:
            self.assertEqual(200, local_health_status(port))
            self.assertTrue(xiaoba_monitor.healthy(port), "本机健康探测不应依赖系统代理发现")
            proxies.assert_not_called()
            settings.assert_not_called()


class SupervisedStartTests(unittest.TestCase):
    def setUp(self):
        # 显式使用夹具方法；模块导入避免unittest在这里重复收集75A测试类。
        self.fixture = port_tests.PortLaunchTests(
            methodName="test_duplicate_start_while_listening_keeps_single_instance")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def _launch_environment(self):
        environment = dict(os.environ, XIAOBA_MONITOR_NO_BROWSER="1")
        environment.pop("XIAOBA_MONITOR_PORT", None)
        return environment

    def _run_start(self):
        return subprocess.run(
            ["/bin/bash", str(self.fixture.copy / "启动监看中心.command")],
            env=self._launch_environment(), capture_output=True, text=True, timeout=30,
        )

    def _reject_unwatched_listener(self, supervisor_pid=None):
        port = self.fixture.alloc()
        self.fixture.write_monitor_json(port)
        pid_file = self.fixture.copy / "run" / "supervisor.pid"
        if supervisor_pid is not None:
            pid_file.parent.mkdir(exist_ok=True)
            pid_file.write_text(str(supervisor_pid))
        with healthy_listener(port):
            self.assertEqual(200, local_health_status(port))
            done = self._run_start()
            self.assertNotEqual(0, done.returncode, done.stderr + done.stdout)
            self.assertNotIn("小八监看中心已开启", done.stdout + done.stderr)
            self.assertEqual(200, local_health_status(port), "启动检查不得结束已有健康服务")
        # 错误PID仅写进测试文件，不赋给fixture.supervisor；清理不得向无关进程发信号。
        self.assertIsNone(self.fixture.supervisor)

    def test_healthy_listener_without_supervisor_pid_is_not_reported_opened(self):
        self._reject_unwatched_listener()

    def test_healthy_listener_with_dead_supervisor_pid_is_not_reported_opened(self):
        with subprocess.Popen([sys.executable, "-B", "-c", "pass"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as child:
            self.assertEqual(0, child.wait(timeout=10))
            dead_pid = child.pid
        with self.assertRaises(ProcessLookupError):
            os.kill(dead_pid, 0)
        self._reject_unwatched_listener(dead_pid)

    def test_healthy_listener_with_unrelated_live_pid_is_not_reported_opened(self):
        self._reject_unwatched_listener(os.getpid())
        os.kill(os.getpid(), 0)

    def test_supervisor_for_previous_port_does_not_validate_new_port(self):
        old_port = self.fixture.alloc()
        self.fixture.write_monitor_json(old_port)
        first = self.fixture.start(self._launch_environment())
        self.assertTrue(self.fixture.wait_healthy(old_port))
        supervisor = self.fixture.read_supervisor()
        out, error = first.communicate(timeout=15)
        self.assertEqual(0, first.returncode, error + out)
        new_port = self.fixture.alloc()
        self.fixture.write_monitor_json(new_port)
        with healthy_listener(new_port):
            done = self._run_start()
            self.assertNotEqual(0, done.returncode, done.stderr + done.stdout)
            self.assertNotIn("小八监看中心已开启", done.stdout + done.stderr)
            os.kill(supervisor, 0)
            self.assertEqual(200, local_health_status(old_port))
            self.assertEqual(200, local_health_status(new_port))
            self.assertEqual(supervisor, self.fixture.read_supervisor())

    def _wait_monitor_ready(self, supervisor):
        path = self.fixture.root / "logs" / "supervisor.log"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            os.kill(supervisor, 0)
            if path.is_file():
                for line in path.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("mod") == "monitor" and event.get("msg") == "ready":
                        return True
            time.sleep(0.05)
        return False

    def test_duplicate_start_keeps_existing_valid_supervisor_and_single_listener(self):
        port = self.fixture.alloc()
        self.fixture.write_monitor_json(port)
        first = self.fixture.start(self._launch_environment())
        self.assertTrue(self.fixture.wait_healthy(port))
        supervisor = self.fixture.read_supervisor()
        self.assertTrue(self._wait_monitor_ready(supervisor), "看护未完成自身就绪探测")
        first_out, first_error = first.communicate(timeout=15)
        self.assertEqual(0, first.returncode, first_error + first_out)
        done = self._run_start()
        self.assertEqual(0, done.returncode, done.stderr + done.stdout)
        self.assertIn("小八监看中心已开启", done.stdout)
        self.assertIn("http://127.0.0.1:%d/" % port, done.stdout)
        self.assertEqual(supervisor, self.fixture.read_supervisor())
        os.kill(supervisor, 0)
        self.assertEqual(1, len(port_tests.listening_pids(port)))


class NoProxyDetachedLaunchTests(unittest.TestCase):
    """清除代理变量是固定回归组；不依赖执行宿主本来是否带代理。"""

    def _run_without_proxy(self, case_class, method_name):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.lower().endswith("_proxy")
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            case = case_class(methodName=method_name)
            try:
                case.setUp()
                try:
                    getattr(case, method_name)()
                finally:
                    case.tearDown()
            finally:
                case.doCleanups()

    def test_card69_detached_launch_without_proxy_environment(self):
        self._run_without_proxy(
            detach_tests.DetachedLaunchTests,
            "test_supervisor_leaves_callers_session_and_survives_group_kill")

    def test_card75a_detached_launch_without_proxy_environment(self):
        self._run_without_proxy(
            port_tests.PortLaunchTests,
            "test_detach_survives_caller_group_kill")

    def test_card75a_configured_port_launch_without_proxy_environment(self):
        self._run_without_proxy(
            port_tests.PortLaunchTests,
            "test_custom_port_without_env_starts_and_opens_same_url")


if __name__ == "__main__":
    unittest.main()
