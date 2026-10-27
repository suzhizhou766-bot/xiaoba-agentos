"""架构第 5 步补的底线测试。

只看守一条现在没有测试的纸面底线：B24 的 MH-011（用户裁决界面只监听本机内部端口）。
所有测试都在临时端口上跑，不连线上库，不发模型请求。
"""

import ast
import http.client
import importlib.util
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ASSETS = Path(__file__).resolve().parents[1] / "src" / "xiaoba_agentos" / "review_ui_assets"
SERVER_SOURCE = ASSETS / "server.py"


def load_server_module():
    spec = importlib.util.spec_from_file_location("step5_review_center_server", SERVER_SOURCE)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def main_bind_host():
    """`main()` 传给 `Server(...)` 的绑定主机，用 AST 读，不做字符串匹配。"""
    tree = ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "main"):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == "Server"):
                continue
            address = call.args[0] if call.args else None
            if isinstance(address, ast.Tuple) and address.elts:
                return ast.literal_eval(address.elts[0])
    return None


class StubCenter:
    """只提供 Handler 真正会碰的那几个成员。"""

    def __init__(self, csrf):
        self.csrf = csrf
        self.sync_status = {"ok": True}
        self.decided = []

    def state(self):
        return {"ok": True, "queue": []}

    def detail(self, task_id):
        return {"ok": True, "task_id": task_id}

    def decide(self, data):
        self.decided.append(data)
        return {"ok": True, "decision": data.get("decision")}

    def sync(self, force=False):
        return self.sync_status


class ReviewCenterLoopbackTest(unittest.TestCase):
    """B24 / MH-011：裁决界面必须只在本机内部端口上服务。"""

    def setUp(self):
        self.server_module = load_server_module()
        quiet = patch.object(self.server_module.Handler, "log_message", lambda *args: None)
        self.addCleanup(quiet.stop)
        quiet.start()
        self.center = StubCenter("step5-csrf-token")
        self.server = self.server_module.Server(("127.0.0.1", 0), self.server_module.Handler)
        self.server.center = self.center
        self.addCleanup(self.server.server_close)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.worker.join, 5)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.loopback_host = "127.0.0.1:%d" % self.port
        self.loopback_origin = "http://127.0.0.1:%d" % self.port

    def send(self, method, path, headers=None, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, body=body, headers=dict(headers or {}))
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8")
        finally:
            connection.close()

    def test_the_listening_socket_and_the_entrypoint_both_stay_on_loopback(self):
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        self.assertEqual("127.0.0.1", main_bind_host())

    def test_a_request_naming_another_host_is_refused(self):
        status, body = self.send(
            "GET", "/api/state", {"Host": "review.attacker.example", "Sec-Fetch-Site": "same-origin"}
        )
        self.assertEqual(403, status)
        self.assertIn("仅允许本机同源访问", body)

    def test_a_cross_site_request_is_refused_even_with_the_loopback_host(self):
        status, body = self.send(
            "GET", "/api/state", {"Host": self.loopback_host, "Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(403, status)
        self.assertIn("仅允许本机同源访问", body)

    def test_a_same_origin_request_is_served(self):
        status, body = self.send(
            "GET", "/api/state", {"Host": self.loopback_host, "Sec-Fetch-Site": "same-origin"}
        )
        self.assertEqual(200, status, body)
        self.assertTrue(body.startswith("{"), body)

    def test_a_decision_post_needs_the_loopback_origin_and_the_csrf_token(self):
        payload = '{"task_id": "t-1", "stage": "delivery", "decision": "approve"}'
        json_headers = {"Content-Type": "application/json"}

        cases = {
            "no_origin": {"Host": self.loopback_host, "Sec-Fetch-Site": "same-origin"},
            "foreign_origin": {
                "Host": self.loopback_host,
                "Sec-Fetch-Site": "same-origin",
                "Origin": "http://review.attacker.example",
            },
            "wrong_csrf": {
                "Host": self.loopback_host,
                "Sec-Fetch-Site": "same-origin",
                "Origin": self.loopback_origin,
                "X-Review-CSRF": "not-the-token",
            },
        }
        for name, headers in sorted(cases.items()):
            with self.subTest(case=name):
                headers = dict(headers, **json_headers)
                status, body = self.send("POST", "/api/decisions", headers, payload)
                self.assertEqual(403, status, body)
                self.assertEqual([], self.center.decided)

        status, body = self.send(
            "POST",
            "/api/decisions",
            {
                "Host": self.loopback_host,
                "Sec-Fetch-Site": "same-origin",
                "Origin": self.loopback_origin,
                "X-Review-CSRF": self.center.csrf,
                "Content-Type": "application/json",
            },
            payload,
        )
        self.assertEqual(200, status, body)
        self.assertEqual(["approve"], [item["decision"] for item in self.center.decided])


if __name__ == "__main__":
    unittest.main()
