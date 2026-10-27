"""监看中心自动化测试（只用标准库）。运行：/usr/bin/python3 -m unittest discover -s tests -v

全部在临时端口和临时目录里进行，用假的裁决后端，不接触真实的 AgentOS 数据，也不会产生任何裁决。
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
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_core import config as cfg, logs  # noqa: E402
from monitor_core.modules import Module  # noqa: E402
from monitor_core.modules.director import DirectorModule  # noqa: E402
from monitor_core.modules.localtools import LocalToolsModule  # noqa: E402
from monitor_core.modules.review import ReviewModule  # noqa: E402
from monitor_core.modules.system import SystemModule  # noqa: E402
from monitor_core.supervise import Backoff, Breaker, ManagedProcess  # noqa: E402
from monitor_core.web import App, Context, Route  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(method, url, headers=None, body=None, timeout=5):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class EchoModule(Module):
    id, title = "echo", "测试"

    def routes(self):
        return [Route("GET", "/t/read", lambda req: (200, {"ok": True}), "read"),
                Route("POST", "/t/post", lambda req: (200, {"ok": True}), "post"),
                Route("POST", "/t/write", lambda req: (200, {"got": req.json()}), "write"),
                Route("GET", "/t/boom", self.boom, "read")]

    def boom(self, req):
        raise RuntimeError("故意出错")


class FakeBackend:
    """模仿裁决后端的接口与它自己的来源、csrf 校验。"""

    def __init__(self, decision_started=None, decision_release=None):
        self.port = free_port()
        self.csrf = "backend-secret"
        self.decisions = []
        self.decision_started = decision_started
        self.decision_release = decision_release
        self.decision_response = None
        self.state = {"ok": True, "csrf": self.csrf, "revision": "r1", "total": 1, "holds": 0,
                      "reviews": [{"task_id": "t" * 8, "stage": "delivery", "decision": "approve", "created_at": 5}],
                      "sync": {"ok": True, "state": "synced", "at": "now"},
                      "items": [{"task_id": "11111111-2222-3333-4444-555555555555", "title": "标题",
                                 "stage": "delivery", "status": "pending_delivery_review",
                                 "status_label": "待初审", "created_at": 1, "updated_at": 2}]}
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
                if self.path.startswith("/api/candidate"):
                    return self.reply(200, {"ok": True, "body": "正文"})
                self.reply(404, {"error": "not found"})

            def do_POST(self):
                expected_origin = "http://127.0.0.1:%d" % backend.port
                if self.headers.get("Origin") != expected_origin or self.headers.get("X-Review-CSRF") != backend.csrf:
                    return self.reply(403, {"ok": False, "error": "forbidden"})
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/decisions":
                    backend.decisions.append(payload)
                    if backend.decision_started is not None:
                        backend.decision_started.set()
                    if backend.decision_release is not None:
                        backend.decision_release.wait(10)
                    response = backend.decision_response or {"ok": True, "items": [], "sync": {"ok": True}}
                    return self.reply(200, {**response, "id": payload.get("id")})
                if self.path == "/api/sync":
                    return self.reply(200, {"ok": True, "state": "synced"})
                self.reply(404, {"error": "not found"})

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class AppCase(unittest.TestCase):
    """起一个真实的监看内核，接上假的裁决后端。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "page.html").write_text("<p>页面</p>", encoding="utf-8")
        (root / "director.html").write_text("<p>导演台</p>", encoding="utf-8")
        (root / "token").write_text("", encoding="utf-8")
        cls.backend = FakeBackend()
        cls.port = free_port()
        config = cfg.load(root / "none.json", {
            "port": cls.port, "page_file": str(root / "page.html"), "agentos_project": str(root),
            "agentos_config": str(root / "agentos.json"),
            "director": {"file": str(root / "director.html"), "migration_token_file": str(root / "token")},
            "review": {"port": cls.backend.port}})
        cls.root = root
        cls.ctx = Context(config, logs.setup(None), cls.port)
        cls.app = App(cls.ctx)
        cls.review = ReviewModule(cls.ctx, manage=False)   # 不拉起后端，直接连到假的后端端口
        for module in (SystemModule(cls.ctx), cls.review, DirectorModule(cls.ctx), EchoModule(cls.ctx)):
            cls.app.register(module)
        cls.httpd = cls.app.server()
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.page_headers = {"Origin": cls.base, "X-Xiaoba-Token": cls.ctx.page_token, "Content-Type": "application/json"}

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.backend.close()
        cls.tmp.cleanup()


class GuardTests(AppCase):
    def test_read_allows_local_and_blocks_foreign_host_and_cross_site(self):
        self.assertEqual(fetch("GET", self.base + "/t/read")[0], 200)
        self.assertEqual(fetch("GET", self.base + "/t/read", {"Host": "evil.example"})[0], 403)
        self.assertEqual(fetch("GET", self.base + "/t/read", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(fetch("GET", self.base + "/t/read", {"Sec-Fetch-Site": "same-site"})[0], 403)

    def test_post_requires_own_origin(self):
        self.assertEqual(fetch("POST", self.base + "/t/post", {}, {})[0], 403)
        self.assertEqual(fetch("POST", self.base + "/t/post", {"Origin": "http://evil.example"}, {})[0], 403)
        self.assertEqual(fetch("POST", self.base + "/t/post", {"Origin": self.base}, {})[0], 200)

    def test_write_requires_origin_token_and_json(self):
        good = dict(self.page_headers)
        self.assertEqual(fetch("POST", self.base + "/t/write", good, {"a": 1})[0], 200)
        self.assertEqual(fetch("POST", self.base + "/t/write", {**good, "X-Xiaoba-Token": "wrong"}, {"a": 1})[0], 403)
        self.assertEqual(fetch("POST", self.base + "/t/write", {k: v for k, v in good.items() if k != "X-Xiaoba-Token"}, {"a": 1})[0], 403)
        self.assertEqual(fetch("POST", self.base + "/t/write", {**good, "Origin": "http://evil.example"}, {"a": 1})[0], 403)
        self.assertEqual(fetch("POST", self.base + "/t/write", {**good, "Content-Type": "text/plain"}, {"a": 1})[0], 403)
        self.assertEqual(fetch("POST", self.base + "/t/write", {**good, "Sec-Fetch-Site": "cross-site"}, {"a": 1})[0], 403)

    def test_bad_body_is_400_unknown_is_404_and_handler_error_is_contained(self):
        self.assertEqual(fetch("POST", self.base + "/t/write", dict(self.page_headers), b"not json")[0], 400)
        self.assertEqual(fetch("GET", self.base + "/nope")[0], 404)
        self.assertEqual(fetch("GET", self.base + "/t/boom")[0], 500)
        self.assertEqual(fetch("GET", self.base + "/t/read")[0], 200)  # 出错后服务仍然可用

    def test_method_must_match_route(self):
        self.assertEqual(fetch("GET", self.base + "/t/write")[0], 404)

    def test_security_headers_on_every_response(self):
        _, headers, _ = fetch("GET", self.base + "/t/read")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_duplicate_routes_and_modules_are_rejected(self):
        with self.assertRaises(ValueError):
            self.app.register(EchoModule(self.ctx))
        with self.assertRaises(ValueError):
            Route("GET", "/x", lambda req: None, "anything-goes")


class SystemTests(AppCase):
    def test_page_carries_token_and_localhost_redirects(self):
        code, _, body = fetch("GET", self.base + "/")
        self.assertEqual(code, 200)
        self.assertIn(self.ctx.page_token.encode(), body)
        code, headers, _ = fetch("GET", self.base + "/", {"Host": "localhost:%d" % self.port})
        self.assertEqual((code, headers.get("Location")), (302, self.base + "/"))

    def test_health_lists_modules(self):
        code, _, body = fetch("GET", self.base + "/api/health")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual({m["id"] for m in data["modules"]}, {"system", "review", "director", "echo"})
        self.assertEqual(fetch("GET", self.base + "/healthz")[0], 200)


class ReviewTests(AppCase):
    def test_state_is_proxied_without_backend_csrf(self):
        code, _, body = fetch("GET", self.base + "/api/review/state")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertNotIn("csrf", data)
        self.assertNotIn(self.backend.csrf.encode(), body)
        self.assertEqual(len(data["items"]), 1)

    def test_candidate_id_must_be_uuid(self):
        self.assertEqual(fetch("GET", self.base + "/api/review/candidate?id=../../etc")[0], 400)
        self.assertEqual(fetch("GET", self.base + "/api/review/candidate?id=11111111-2222-3333-4444-555555555555")[0], 200)

    def test_decision_needs_page_token_and_reaches_backend_with_its_csrf(self):
        before = len(self.backend.decisions)
        self.assertEqual(fetch("POST", self.base + "/api/review/decisions", {"Origin": self.base, "Content-Type": "application/json"}, {"id": "x", "items": []})[0], 403)
        self.assertEqual(len(self.backend.decisions), before)  # 被门禁拦下的请求不会到达后端
        code, _, _ = fetch("POST", self.base + "/api/review/decisions", dict(self.page_headers), {"id": "x", "items": []})
        self.assertEqual(code, 200)
        self.assertEqual(self.backend.decisions[-1], {"id": "x", "items": []})

    def test_state_reports_new_review_while_decision_waits_for_receipt(self):
        batch_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        task_id = "11111111-2222-3333-4444-555555555555"
        review_id = "22222222-3333-4444-5555-666666666666"
        payload = {"id": batch_id, "items": [{"task_id": task_id, "stage": "delivery", "decision": "approve"}]}
        started, release = threading.Event(), threading.Event()
        responses, errors = [], []
        original = (self.backend.state, self.backend.decision_started,
                    self.backend.decision_release, self.backend.decision_response)
        self.backend.decision_started, self.backend.decision_release = started, release
        self.backend.decision_response = {"ok": True, "items": [{"task_id": task_id, "ok": True,
                                          "recorded": True, "review_id": review_id,
                                          "status": "pending_final_review"}], "sync": {"ok": True}}

        def submit():
            try:
                responses.append(fetch("POST", self.base + "/api/review/decisions",
                                       dict(self.page_headers), payload, timeout=8))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=submit, daemon=True)
        try:
            thread.start()
            self.assertTrue(started.wait(3), "提交没有到达假后端")
            new_review = {"id": review_id, "task_id": task_id, "stage": "delivery",
                          "decision": "approve", "created_at": time.time()}
            self.backend.state = {**original[0], "revision": "r2",
                                  "reviews": original[0]["reviews"] + [new_review]}
            before = time.monotonic()
            code, _, body = fetch("GET", self.base + "/api/review/state", timeout=1)
            elapsed = time.monotonic() - before
            self.assertEqual(code, 200)
            self.assertLess(elapsed, 1, "状态读取被尚未结束的提交阻塞")
            state = json.loads(body)
            self.assertEqual(state["revision"], "r2")
            self.assertEqual(state["reviews"][-1], new_review)
            self.assertNotIn("csrf", state)
            self.assertTrue(thread.is_alive(), "正式回执应仍在等待后端完成")
            self.assertEqual(self.backend.decisions[-1], payload)
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive(), "释放后没有收到正式回执")
            self.assertEqual(errors, [])
            self.assertEqual(len(responses), 1)
            code, _, body = responses[0]
            receipt = json.loads(body)
            self.assertEqual(code, 200)
            self.assertEqual(receipt["id"], batch_id)
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["items"][0]["review_id"], review_id)
            self.assertTrue(receipt["items"][0]["recorded"])
        finally:
            release.set()
            thread.join(10)
            (self.backend.state, self.backend.decision_started,
             self.backend.decision_release, self.backend.decision_response) = original

    # ---- 待核对的保留文件：用一个假的命令行入口，不接触真实的小八 ----
    def _fake_cli(self, objects, resolve_ok=True, extra=None):
        root = Path(self.tmp.name)
        calls = root / "cli-calls.jsonl"
        calls.write_text("", encoding="utf-8")
        listing = {"objects": objects, "enumeration_ok": True, "anomalies": [], "problems": [], "checked_at": "now"}
        listing.update(extra or {})
        outcome = {"ok": resolve_ok, "status": "resolved" if resolve_ok else "refused", "trash_path": "/trash/x",
                   "issues": [] if resolve_ok else ["the object at the path is not the recorded one"]}
        script = root / "fake-xiaoba"
        script.write_text(
            "#!/usr/bin/python3\nimport json, sys\n"
            "open(%r, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1] == 'memory-retained-list':\n    print(json.dumps(%r)); sys.exit(0)\n"
            "print(json.dumps(%r)); sys.exit(0 if %r else 1)\n" % (str(calls), listing, outcome, resolve_ok),
            encoding="utf-8")
        script.chmod(0o700)
        self.review.cli = script
        return lambda: [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]

    KEPT = {"retained_path": "/vault/.note.md.11.rollback", "original_path": "/vault/note.md", "state": "retained_object",
            "manual_verification": "pending", "task_id": "abcdef0123456789", "operation": "memory.note",
            "reason": "the previous bytes were put back, so the object this call replaced is retained",
            "recorded_at": "2026-10-12T20:00:00+0800", "size_bytes": 2048, "dev": 16777230, "ino": 4242, "readable": True}

    def test_retained_list_is_trimmed_and_translated(self):
        calls = self._fake_cli([self.KEPT, dict(self.KEPT, retained_path="/vault/.done.md.11.rollback", manual_verification="resolved")])
        code, _, body = fetch("GET", self.base + "/api/review/retained")
        data = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(data["pending"], 1)              # 已处理的不再列出
        item = data["items"][0]
        self.assertEqual(item["path"], "/vault/.note.md.11.rollback")
        self.assertEqual(item["operation"], "经验笔记")
        self.assertIn("旧内容已放回", item["reason"])
        self.assertEqual(item["task"], "abcdef01")
        self.assertNotIn(b"4242", body)                    # 文件身份只在服务端用，不下发给页面
        self.assertEqual(calls(), [["memory-retained-list"]])

    def test_retained_resolve_needs_page_token_and_only_takes_listed_objects(self):
        calls = self._fake_cli([self.KEPT])
        url, plain = self.base + "/api/review/retained/resolve", {"Origin": self.base, "Content-Type": "application/json"}
        ask = {"path": self.KEPT["retained_path"], "action": "trash"}
        self.assertEqual(fetch("POST", url, plain, ask)[0], 403)
        self.assertEqual(calls(), [])                      # 被门禁拦下的请求不会碰命令行
        self.assertEqual(fetch("POST", url, dict(self.page_headers), {"path": "/etc/passwd", "action": "trash"})[0], 409)
        self.assertEqual(fetch("POST", url, dict(self.page_headers), dict(ask, action="delete"))[0], 400)
        self.assertFalse([c for c in calls() if c[0] == "memory-retained-resolve"])   # 不在清单里的、处理方式不认识的，都没有执行
        code, _, body = fetch("POST", url, dict(self.page_headers), ask)
        self.assertEqual((code, json.loads(body)["ok"]), (200, True))
        self.assertEqual(calls()[-1], ["memory-retained-resolve", "--trash", "--expect-dev", "16777230", "--expect-ino", "4242",
                                       self.KEPT["retained_path"]])

    def test_retained_resolve_reports_refusal_and_handles_deleted_objects(self):
        gone = dict(self.KEPT, state="record_without_object", dev=None, ino=None)
        calls = self._fake_cli([gone], resolve_ok=False)
        code, _, body = fetch("GET", self.base + "/api/review/retained")
        self.assertFalse(json.loads(body)["items"][0]["exists"])
        code, _, body = fetch("POST", self.base + "/api/review/retained/resolve", dict(self.page_headers),
                              {"path": gone["retained_path"], "action": "confirm_deleted"})
        self.assertEqual(code, 409)
        self.assertIn("not the recorded one", json.loads(body)["error"])   # 命令行拒绝时，原因原样带给用户，不报成功
        self.assertEqual(calls()[-1], ["memory-retained-resolve", "--confirm-deleted", gone["retained_path"]])

    def test_retained_list_failure_is_reported_not_shown_as_empty(self):
        self.review.cli = Path(self.tmp.name) / "no-such-entry"
        code, _, body = fetch("GET", self.base + "/api/review/retained")
        self.assertEqual(code, 502)
        self.assertFalse(json.loads(body)["ok"])

    def test_queue_summary_has_no_body_or_hash(self):
        summary = self.review.queue_summary()
        self.assertTrue(summary["online"])
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["byStage"], {"delivery": 1})
        self.assertNotIn("prompt", summary["items"][0])
        self.assertNotIn("evidence_sha256", summary["items"][0])

    def test_backend_down_gives_clear_error_not_crash(self):
        down = ReviewModule(Context(cfg.load(self.root / "none.json", {"review": {"port": free_port()}, "agentos_project": str(self.root)}),
                                    logs.setup(None), free_port()), manage=False)
        code, data = down.state()
        self.assertEqual(code, 502)
        self.assertIn("裁决后端", data["error"])
        self.assertFalse(down.queue_summary()["online"])


class DirectorTests(AppCase):
    def test_page_is_restricted_by_csp(self):
        code, headers, body = fetch("GET", self.base + "/director/")
        csp = headers.get("Content-Security-Policy", "")
        self.assertEqual(code, 200)
        self.assertIn("导演台".encode(), body)
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertIn("frame-src 'none'", csp)
        self.assertIn("connect-src http://127.0.0.1:%d/director/" % self.port, csp)
        self.assertNotIn("/api/review", csp)

    def test_migration_closed_without_token_then_open_receive_ack(self):
        url = self.base + "/director/migration"
        payload = {"token": "t0ken", "schemes": json.dumps([{"name": "a", "snapshot": {}}]), "autosave": None}
        self.assertEqual(fetch("POST", url, {"Origin": "null", "Content-Type": "text/plain"}, payload)[0], 403)  # 令牌文件为空 = 入口关闭
        (self.root / "token").write_text("t0ken\n", encoding="utf-8")
        try:
            self.assertEqual(fetch("POST", url, {"Origin": "null", "Content-Type": "text/plain"}, {**payload, "token": "bad"})[0], 403)
            code, headers, body = fetch("POST", url, {"Origin": "null", "Content-Type": "text/plain"}, payload)
            self.assertEqual((code, json.loads(body)["schemes"]), (200, 1))
            self.assertEqual(headers.get("Access-Control-Allow-Origin"), "*")
            pending = json.loads(fetch("GET", url)[2])["pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(fetch("POST", url + "/ack", {"Origin": "http://evil.example", "Content-Type": "application/json"}, {"ids": [pending[0]["id"]]})[0], 403)
            self.assertEqual(fetch("POST", url + "/ack", {"Origin": self.base, "Content-Type": "application/json"}, {"ids": [pending[0]["id"]]})[0], 200)
            self.assertEqual(json.loads(fetch("GET", url)[2])["pending"], [])
        finally:
            (self.root / "token").write_text("", encoding="utf-8")


class ReviewLifecycleTests(unittest.TestCase):
    def _module(self, root, backend_port):
        config = cfg.load(Path(root) / "none.json", {"agentos_project": str(root), "agentos_config": str(Path(root) / "a.json"),
                                                     "review": {"port": backend_port}, "log": {"dir": str(Path(root) / "logs")}})
        config["root"] = str(root)
        return ReviewModule(Context(config, logs.setup(None), free_port()), manage=False)

    def test_prefers_own_previous_backend_then_legacy_service(self):
        backend = FakeBackend()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                module = self._module(tmp, free_port())           # 旧端口上没有服务
                self.assertIsNone(module._previous_backend())
                (Path(tmp) / "run").mkdir()
                listener = module._listener_pid(backend.port)
                self.assertEqual(listener, os.getpid())
                module.state_file.write_text(json.dumps({"pid": listener, "port": backend.port}))
                self.assertEqual(module._previous_backend(), (listener, backend.port))
                module.state_file.write_text(json.dumps({"pid": 1, "port": backend.port}))   # 进程号对不上：不认领
                self.assertIsNone(module._previous_backend())
                legacy = self._module(tmp, backend.port)            # 旧端口上有服务在跑
                self.assertTrue(legacy._alive(legacy.legacy_port))
        finally:
            backend.close()

    def test_launcher_refuses_non_local_public_url(self):
        launcher = Path(__file__).resolve().parent.parent / "monitor_core" / "review_backend.py"
        import subprocess
        result = subprocess.run([sys.executable, str(launcher), "--server-script", "x", "--port", "1", "--config", "x",
                                 "--public-url", "http://example.com:80"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--public-url", result.stderr)

    def test_config_follows_worker_launch_agent(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "worker.plist"
            with open(plist, "wb") as handle:
                plistlib.dump({"EnvironmentVariables": {"XIAOBA_CONFIG": "/tmp/some-config.json"}}, handle)
            self.assertEqual(cfg.worker_config_path(plist), Path("/tmp/some-config.json"))
            self.assertIsNone(cfg.worker_config_path(Path(tmp) / "missing.plist"))


FAKE_CONTROLLER = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
STATE = {"demo": True}
class H(BaseHTTPRequestHandler):
    def _send(self, body):
        raw = json.dumps(body).encode(); self.send_response(200)
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        self._send({"ok": True, "services": {"demo": {"running": STATE["demo"]}}})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        STATE[body["service"]] = body["action"] == "start"
        open(sys.argv[2], "a").write(body["service"] + ":" + body["action"] + "\\n")
        self._send({"ok": True})
    def log_message(self, *args): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


class LocalToolsTests(unittest.TestCase):
    """控制器随监看中心启停：已有的控制器被接管，关闭时先关服务再关控制器。"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.port = free_port()
        self.calls = self.root / "calls.txt"
        self.script = self.root / "local_tools_control_server.py"
        self.script.write_text(FAKE_CONTROLLER.replace("sys.argv[1]", str(self.port)).replace("sys.argv[2]", repr(str(self.calls))),
                               encoding="utf-8")
        config = cfg.load(self.root / "none.json", {
            "localtools": {"port": self.port, "script": str(self.script), "python": sys.executable},
            "log": {"dir": str(self.root / "logs")}, "watchdog": {"backoff_seconds": [0.1]}})
        self.module = LocalToolsModule(Context(config, logs.setup(None), free_port()))

    def wait(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    def test_takes_over_existing_controller_and_stops_services_on_close(self):
        stray = subprocess.Popen([sys.executable, str(self.script)])
        try:
            self.assertTrue(self.wait(self.module._port_busy))
            self.module.start()
            self.assertTrue(self.wait(lambda: self.module.health()["status"] == "ok"))
            self.assertIsNotNone(stray.wait(5))                       # 旧的那个被结束
            self.assertNotEqual(self.module.process.status()["pid"], stray.pid)
            self.module.stop()
            self.assertEqual(self.calls.read_text(), "demo:stop\n")   # 关闭时先关掉开着的服务
            self.assertFalse(self.module._port_busy())
        finally:
            if stray.poll() is None:
                stray.kill()
            self.module.stop()

    def test_foreign_program_on_port_is_left_alone(self):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", self.port))
            sock.listen(64)
            self.module.start()
            self.assertEqual(self.module.health()["status"], "failed")
            self.assertIsNone(self.module.process)


class SuperviseTests(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        backoff = Backoff((1, 2, 5))
        self.assertEqual([backoff.next() for _ in range(5)], [1, 2, 5, 5, 5])
        backoff.reset()
        self.assertEqual(backoff.next(), 1)

    def test_breaker_opens_only_on_consecutive_fast_failures(self):
        breaker = Breaker(max_failures=3, fast_seconds=10)
        self.assertFalse(breaker.record_exit(1))
        self.assertFalse(breaker.record_exit(1))
        self.assertFalse(breaker.record_exit(60))   # 运行得够久，计数清零
        self.assertFalse(breaker.record_exit(1))
        self.assertFalse(breaker.record_exit(1))
        self.assertTrue(breaker.record_exit(1))
        breaker.reset()
        self.assertFalse(breaker.open)

    def test_crashing_child_is_restarted_then_circuit_opens(self):
        proc = ManagedProcess("crash", [sys.executable, "-c", "import sys; sys.exit(3)"], logs.setup(None),
                              backoff=Backoff((0.05,)), breaker=Breaker(max_failures=3, fast_seconds=60))
        proc.start()
        deadline = time.time() + 10
        while proc.state != "failed" and time.time() < deadline:
            time.sleep(0.05)
        status = proc.status()
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["restarts"], 2)
        self.assertEqual(status["last_exit"], 3)
        self.assertIn("已停止自动重启", status["error"])

    def test_healthy_child_runs_is_restarted_after_kill_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "ready"
            code = "import pathlib,time; pathlib.Path(%r).write_text('1'); time.sleep(60)" % str(flag)
            proc = ManagedProcess("steady", [sys.executable, "-c", code], logs.setup(None), ready=flag.exists,
                                  ready_timeout=10, backoff=Backoff((0.05,)), breaker=Breaker(5, 0.01))
            proc.start()
            deadline = time.time() + 10
            while proc.state != "running" and time.time() < deadline:
                time.sleep(0.05)
            first = proc.status()["pid"]
            self.assertEqual(proc.state, "running")
            self.assertIsNotNone(first)
            proc.kill_child()   # 模拟进程意外消失
            deadline = time.time() + 10
            while (proc.state != "running" or proc.status()["pid"] == first) and time.time() < deadline:
                time.sleep(0.05)
            second = proc.status()["pid"]
            self.assertEqual(proc.state, "running")
            self.assertNotEqual(first, second)
            proc.stop()
            self.assertEqual(proc.state, "stopped")
            with self.assertRaises(OSError):
                os.kill(second, 0)


class DshDesktopModelTests(unittest.TestCase):
    """运行总览里 DSH 审查的模型取桌面端现在选的，不是上次探针时记下的。"""

    def read(self, text):
        from monitor_core.modules.overview import dsh_desktop_model
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cordis.patch.yml"
            path.write_text(text, encoding="utf-8")
            return dsh_desktop_model(path)

    def test_reads_the_default_model_and_the_provider_display_name(self):
        text = ("- id: agent-default-model\n  config:\n    provider: xiaoba\n    model: DeepSeek-V4.1-Flash\n"
                "- id: llm-pi-ai\n  config:\n    providers:\n      other:\n        displayName: 别家\n"
                "      xiaoba:\n        displayName: 小八并发\n        models:\n          - id: m\n")
        self.assertEqual(self.read(text), "小八并发 · DeepSeek-V4.1-Flash")

    def test_provider_without_display_name_shows_its_id(self):
        text = "- id: agent-default-model\n  config:\n    provider: deepseek\n    model: \"deepseek-chat\"\n- id: ui\n"
        self.assertEqual(self.read(text), "deepseek · deepseek-chat")

    def test_missing_or_incomplete_selection_gives_none(self):
        from monitor_core.modules.overview import dsh_desktop_model
        self.assertIsNone(self.read("- id: agent-default-model\n  config:\n    provider: xiaoba\n"))
        self.assertIsNone(self.read("- id: ui-chat\n  config:\n    model: x\n    provider: y\n"))
        self.assertIsNone(dsh_desktop_model("/nonexistent/cordis.patch.yml"))


if __name__ == "__main__":
    unittest.main()
