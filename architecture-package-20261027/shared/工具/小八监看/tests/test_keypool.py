"""Key 池测试（只用标准库）：假的上游、临时端口、临时目录，不接触任何真实的中转站与 Key。

运行：/usr/bin/python3 -m unittest discover -s tests -v
"""
import http.client
import http.server
import contextlib
import io
import json
import os
import socket
import stat
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
from monitor_core import keypool_proxy as kp  # noqa: E402
from monitor_core.modules.keypool import KeyPoolModule  # noqa: E402
from monitor_core.web import App, Context  # noqa: E402

MASTER = "sk-xb-test-master-key-000000"
ADMIN = "admin-token-for-tests-0000000000"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeUpstream:
    """mode: ok / 429 / 401 / stream / slow"""

    def __init__(self, mode="ok", delay=0.0):
        self.mode, self.delay, self.hits, self.auth = mode, delay, 0, []
        self.bodies, self.only = [], None           # only：这个假中转站支持的模型；None 表示都支持
        self.reject_status, self.reject_text = 404, "The model `%s` does not exist"
        self.deny = {}                              # Key → (状态码, 回复)：这个 Key 被拒（比如额度用完）
        self.agents, self.block_agent = [], None    # block_agent：客户端标识以它开头就像站点防护那样回 403
        self.events = None                          # stream 模式下改发这些 SSE 事件（bytes 列表）
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                upstream.hits += 1
                upstream.auth.append(self.headers.get("Authorization") or self.headers.get("x-api-key"))
                upstream.last_path = self.path
                upstream.agents.append(self.headers.get("User-Agent"))
                if upstream.block_agent and (upstream.agents[-1] or "").startswith(upstream.block_agent):
                    data = b"error code: 1010"
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if upstream.delay:
                    time.sleep(upstream.delay)
                upstream.bodies.append(body)
                wanted = None
                try:
                    wanted = json.loads(body.decode("utf-8")).get("model") if body else None
                except ValueError:
                    pass
                used = (upstream.auth[-1] or "").replace("Bearer ", "")
                if used in upstream.deny:
                    code, text = upstream.deny[used]
                    data = json.dumps({"error": {"message": text}}, ensure_ascii=False).encode("utf-8")
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if upstream.only is not None and wanted is not None and wanted not in upstream.only:
                    data = json.dumps({"error": {"message": upstream.reject_text % wanted}}).encode("utf-8")
                    self.send_response(upstream.reject_status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if upstream.mode == "301":
                    data = b"<html>301 Moved Permanently</html>"
                    self.send_response(301)
                    self.send_header("Location", "https://moved.example/v1" + self.path)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if upstream.mode in ("429", "401"):
                    data = b'{"error":"nope"}'
                    self.send_response(int(upstream.mode))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if upstream.mode == "stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    events = upstream.events or [b"data: {\"i\": %d}\n\n" % i for i in range(3)] + [b"data: [DONE]\n\n"]
                    for event in events:
                        if isinstance(event, (int, float)):   # 数字：停这么多秒再发下一段（模拟上游不出字）
                            time.sleep(event)
                            continue
                        try:
                            self.wfile.write(event)
                            self.wfile.flush()
                        except OSError:                       # Key 池已经放弃这个节点、断开了
                            break
                        time.sleep(0.05)
                    self.close_connection = True
                    return
                data = json.dumps({"port": upstream.port, "path": self.path, "echo": body.decode("utf-8", "replace"),
                                   "data": [{"id": "m1"}, {"id": "m2"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _any

        # 默认的等待队列只有 5，测试里并发连接多于它时系统会直接重置连接
        server_class = type("Server", (http.server.ThreadingHTTPServer,), {"request_queue_size": 64})
        self.server = server_class(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def write_config(path, nodes, **extra):
    data = {"masterKey": MASTER, "nodes": nodes, "settings": {"queueTimeoutSeconds": 2, "timeoutSeconds": 10}}
    data.update(extra)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    os.replace(tmp, str(path))


def call(port, path="/v1/chat/completions", body=None, key=MASTER, header="Authorization", method="POST", timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers[header] = ("Bearer " + key) if header == "Authorization" else key
    payload = json.dumps(body if body is not None else {"model": "m1", "messages": []}).encode("utf-8")
    conn.request(method, path, body=payload if method != "GET" else None, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


class ProxyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "keypool.json"
        self.ups = []
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        for up in self.ups:
            up.close()
        self.tmp.cleanup()

    def upstream(self, mode="ok", delay=0.0):
        up = FakeUpstream(mode, delay)
        self.ups.append(up)
        return up

    def start(self, nodes, **settings):
        write_config(self.config, nodes)
        if settings:
            data = json.loads(self.config.read_text(encoding="utf-8"))
            data["settings"].update(settings)
            self.config.write_text(json.dumps(data), encoding="utf-8")
        self.port = free_port()
        self.server = kp.make_server(str(self.config), self.port, ADMIN)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def state(self):
        request = urllib.request.Request("http://127.0.0.1:%d/_admin/state" % self.port,
                                         headers={"X-KeyPool-Admin": ADMIN})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


class ProxyTests(ProxyCase):
    def test_forwarding_never_falls_back_to_python_urllib_agent(self):
        up = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": up.url, "key": "ka"}])
        self.assertEqual(call(self.port)[0], 200)               # 客户端没带标识：用 Key 池自己的
        self.assertEqual(up.agents[-1], kp.CLIENT_UA)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m1","messages":[]}',
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + MASTER,
                              "User-Agent": "some-client/1.0"})
        self.assertEqual(conn.getresponse().status, 200)        # 客户端带了标识：原样转发
        conn.close()
        self.assertEqual(up.agents[-1], "some-client/1.0")

    def test_wrong_key_is_rejected_and_upstream_not_called(self):
        up = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": up.url, "key": "ka"}])
        self.assertEqual(call(self.port, key="bad")[0], 401)
        self.assertEqual(call(self.port, key=None)[0], 401)
        self.assertEqual(up.hits, 0)

    def test_requests_are_spread_and_upstream_gets_its_own_key(self):
        a, b = self.upstream(delay=0.2), self.upstream(delay=0.2)
        self.start([{"id": "a", "name": "A", "baseUrl": a.url + "/v1", "key": "ka"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"}])
        results = []
        threads = [threading.Thread(target=lambda: results.append(call(self.port))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([r[0] for r in results], [200] * 8)
        self.assertGreaterEqual(a.hits, 2)
        self.assertGreaterEqual(b.hits, 2)
        self.assertEqual(set(a.auth), {"Bearer ka"})
        self.assertEqual(set(b.auth), {"Bearer kb"})
        self.assertEqual(a.last_path, "/v1/chat/completions")  # baseUrl 带 /v1 也不会拼出 /v1/v1

    def test_x_api_key_clients_get_x_api_key_upstream(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"}])
        self.assertEqual(call(self.port, "/v1/messages", header="x-api-key")[0], 200)
        self.assertEqual(a.auth, ["ka"])

    def test_429_and_unreachable_fail_over_then_cool_down(self):
        limited, good = self.upstream("429"), self.upstream()
        dead_port = free_port()
        self.start([{"id": "lim", "name": "L", "baseUrl": limited.url, "key": "k1"},
                    {"id": "dead", "name": "D", "baseUrl": "http://127.0.0.1:%d" % dead_port, "key": "k2"},
                    {"id": "good", "name": "G", "baseUrl": good.url, "key": "k3"}], breakerFailures=1)   # 失败一次就停
        for _ in range(4):
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(good.hits, 4)
        nodes = {n["id"]: n for n in self.state()["nodes"]}
        self.assertEqual(nodes["lim"]["status"], "cooling")
        self.assertEqual(nodes["dead"]["status"], "down")
        self.assertLessEqual(limited.hits, 1)  # 冷却期内不再被选中

    def test_breaker_needs_consecutive_failures_then_probes_before_recovering(self):
        flaky, good = self.upstream("429"), self.upstream()
        self.start([{"id": "f", "name": "F", "baseUrl": flaky.url, "key": "k1"},
                    {"id": "g", "name": "G", "baseUrl": good.url, "key": "k2"}])       # 默认：连续 3 次才停
        node = lambda: {n["id"]: n for n in self.state()["nodes"]}["f"]
        pool = self.server.pool
        pool.nodes["g"].last_used = 9e12                                             # 两个都没用过时不靠随机
        for expected in (1, 2):
            pool.nodes["f"].last_used = 0                                            # 让它先被选中
            self.assertEqual(call(self.port)[0], 200)                                # 失败后换到 G，客户端无感
            self.assertEqual((node()["failStreak"], node()["breaker"], node()["cooldownLeft"]), (expected, "closed", 0))
        flaky.mode = "ok"
        pool.nodes["f"].last_used = 0
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(node()["failStreak"], 0)                                    # 成功一次：连续失败清零
        flaky.mode = "429"
        for _ in range(3):
            pool.nodes["f"].last_used = 0
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((node()["breaker"], node()["status"]), ("open", "cooling"))
        self.assertGreater(node()["cooldownLeft"], 0)
        hits = flaky.hits
        for _ in range(3):
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(flaky.hits, hits)                                           # 停用期间不再分给它
        # 冷却结束：进入试探。试探失败立刻再停，不用再攒三次
        pool.nodes["f"].cooldown_until, pool.nodes["f"].last_used = 0, 0
        self.assertEqual(node()["breaker"], "half")
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((flaky.hits, node()["breaker"]), (hits + 1, "open"))
        # 再次冷却结束，这回试探成功：连续成功 2 次才算恢复
        flaky.mode = "ok"
        pool.nodes["f"].cooldown_until, pool.nodes["f"].last_used = 0, 0
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(node()["breaker"], "half")
        pool.nodes["f"].last_used = 0
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((node()["breaker"], node()["failStreak"]), ("closed", 0))

    def test_node_can_send_its_own_client_identity(self):
        up = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": up.url, "key": "ka", "userAgent": "station-only/3.1"}])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m1","messages":[]}',
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + MASTER,
                              "User-Agent": "some-client/1.0"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertEqual(up.agents[-1], "station-only/3.1")

    def test_stream_first_byte_timeout_is_separate_from_plain_requests(self):
        slow, good = self.upstream(delay=1.2), self.upstream()
        self.start([{"id": "s", "name": "S", "baseUrl": slow.url, "key": "k1"},
                    {"id": "g", "name": "G", "baseUrl": good.url, "key": "k2"}], firstByteTimeoutSeconds=0.4)
        pool = self.server.pool
        pool.nodes["s"].last_used, pool.nodes["g"].last_used = 0, 9e12
        self.assertEqual(call(self.port, body={"model": "m1", "messages": []})[0], 200)   # 非流式：不受首字超时限制
        self.assertEqual((slow.hits, good.hits), (1, 0))
        pool.nodes["s"].last_used, pool.nodes["g"].last_used = 0, 9e12
        self.assertEqual(call(self.port, body={"model": "m1", "messages": [], "stream": True})[0], 200)
        self.assertEqual(good.hits, 1)                                                    # 流式：等不到响应头，换到 G

    def test_all_failing_returns_last_upstream_error(self):
        limited = self.upstream("429")
        self.start([{"id": "lim", "name": "L", "baseUrl": limited.url, "key": "k1"}])
        status, _ = call(self.port)
        self.assertEqual(status, 429)

    def test_stream_is_relayed_chunk_by_chunk(self):
        s = self.upstream("stream")
        self.start([{"id": "s", "name": "S", "baseUrl": s.url, "key": "ks"}])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m","stream":true}',
                     headers={"Authorization": "Bearer " + MASTER, "Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        first = response.read1(4096) if hasattr(response, "read1") else response.read(10)
        self.assertIn(b"data:", first)
        rest = response.read()
        self.assertIn(b"[DONE]", first + rest)
        conn.close()
        event = self.state()["events"][-1]
        self.assertTrue(event["stream"])
        self.assertEqual(event["model"], "m")

    def test_sequential_requests_rotate_through_every_node(self):
        ups = [self.upstream() for _ in range(4)]
        self.start([{"id": "n%d" % i, "name": "N%d" % i, "baseUrl": up.url, "key": "k%d" % i}
                    for i, up in enumerate(ups)])
        for _ in range(8):
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual([up.hits for up in ups], [2, 2, 2, 2])

    def test_pin_header_selects_node_and_is_not_forwarded(self):
        a, b = self.upstream(), self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"}])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        for _ in range(3):
            conn.request("POST", "/v1/chat/completions", body=b"{}", headers={
                "Authorization": "Bearer " + MASTER, "X-KeyPool-Node": "B", "Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
        conn.close()
        self.assertEqual((a.hits, b.hits), (0, 3))

    def test_timing_flags_stream_that_arrives_all_at_once(self):
        slow = self.upstream("stream", delay=8.2)  # 响应头和内容都在 8 秒后才一起到：不是真流式
        self.start([{"id": "s", "name": "S", "baseUrl": slow.url, "key": "k"}])
        self.server.pool.settings["timeoutSeconds"] = 30
        status, _ = call(self.port, body={"model": "m", "stream": True}, timeout=30)
        self.assertEqual(status, 200)
        node = self.state()["nodes"][0]
        event = self.state()["events"][-1]
        self.assertEqual((node["streams"], node["buffered"]), (1, 1))
        self.assertTrue(event["buffered"])
        self.assertGreaterEqual(event["ttfb"], 8000)
        self.assertEqual(event["path"], "/v1/chat/completions")

    def test_timing_real_stream_is_not_flagged(self):
        s = self.upstream("stream")
        self.start([{"id": "s", "name": "S", "baseUrl": s.url, "key": "k"}])
        call(self.port, body={"model": "m", "stream": True})
        node = self.state()["nodes"][0]
        self.assertEqual((node["streams"], node["buffered"]), (1, 0))
        self.assertIsNotNone(node["ttfbMs"])
        self.assertLessEqual(node["ttfbMs"], node["totalMs"])

    def test_disabled_nodes_are_skipped_and_config_reloads_live(self):
        a, b = self.upstream(), self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb", "enabled": False}])
        for _ in range(3):
            call(self.port)
        self.assertEqual((a.hits, b.hits), (3, 0))
        time.sleep(0.05)
        write_config(self.config, [{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka", "enabled": False},
                                   {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"}])
        for _ in range(3):
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((a.hits, b.hits), (3, 3))

    def test_no_nodes_gives_clear_503(self):
        self.start([])
        status, body = call(self.port)
        self.assertEqual(status, 503)
        self.assertIn(b"no upstream", body)

    def test_admin_needs_token_and_never_shows_keys(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "secret-upstream-key"}])
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen("http://127.0.0.1:%d/_admin/state" % self.port, timeout=5)
        self.assertEqual(caught.exception.code, 403)
        call(self.port)
        text = json.dumps(self.state())
        self.assertNotIn("secret-upstream-key", text)
        self.assertNotIn(MASTER, text)

    def test_max_concurrency_queues_instead_of_overloading(self):
        a = self.upstream(delay=0.3)
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka", "maxConcurrency": 1}])
        peak = []
        results = []

        def watch():
            for _ in range(10):
                peak.append(self.state()["nodes"][0]["inflight"])
                time.sleep(0.05)

        threads = [threading.Thread(target=lambda: results.append(call(self.port)[0])) for _ in range(3)]
        watcher = threading.Thread(target=watch)
        for t in threads + [watcher]:
            t.start()
        for t in threads + [watcher]:
            t.join()
        self.assertEqual(results, [200, 200, 200])
        self.assertLessEqual(max(peak), 1)


class NodeModelTests(ProxyCase):
    """节点指定调用模型，以及“节点不支持这个模型”时自动换线路。"""

    def ask(self, model="m1", **extra):
        code, data = call(self.port, body=dict({"model": model, "messages": [{"role": "user", "content": "hi"}]}, **extra))
        try:
            return code, json.loads(data.decode("utf-8"))
        except ValueError:
            return code, data

    def test_node_model_replaces_the_client_model_only_on_that_node(self):
        a, b = self.upstream(), self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb", "model": "other-model"}])
        for _ in range(4):
            self.assertEqual(self.ask(temperature=0.5)[0], 200)
        sent_a = [json.loads(x) for x in a.bodies]
        sent_b = [json.loads(x) for x in b.bodies]
        self.assertEqual((len(sent_a), len(sent_b)), (2, 2))
        self.assertTrue(all(x["model"] == "m1" for x in sent_a))
        self.assertTrue(all(x["model"] == "other-model" and x["temperature"] == 0.5 and x["messages"][0]["content"] == "hi"
                            for x in sent_b))
        models = {e["nodeName"]: e["model"] for e in self.state()["events"]}
        self.assertEqual(models, {"A": "m1", "B": "other-model"})

    def test_unsupported_model_fails_over_without_cooling_the_node_and_is_remembered(self):
        good, amd = self.upstream(), self.upstream()
        amd.only = {"amd-model"}
        self.start([{"id": "amd", "name": "AMD", "baseUrl": amd.url, "key": "k1"},
                    {"id": "good", "name": "Good", "baseUrl": good.url, "key": "k2"}])
        for _ in range(6):
            self.assertEqual(self.ask("tierflow_pro")[0], 200)     # 客户端一次错误都看不到
        self.assertEqual(amd.hits, 1)                              # 只在第一次碰了一下，之后记住不再分给它
        self.assertEqual(good.hits, 6)
        node = {n["name"]: n for n in self.state()["nodes"]}["AMD"]
        self.assertEqual(node["cooldownLeft"], 0)
        self.assertIn("不支持模型 tierflow_pro", node["lastError"])
        self.assertEqual(self.ask("amd-model")[0], 200)            # 它支持的模型照常分给它
        self.assertEqual(amd.hits, 2)

    def test_400_counts_as_unsupported_only_when_it_mentions_the_model(self):
        a, b = self.upstream(), self.upstream()
        a.only, a.reject_status, a.reject_text = set(), 400, "model %s is not available for this account"
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"}])
        self.server.pool.nodes["b"].last_used = 9e12               # 让 A 先被选中
        self.assertEqual(self.ask()[0], 200)
        self.assertEqual((a.hits, b.hits), (1, 1))
        a.reject_text = "bad request: messages field required%.0s"   # 客户端请求本身有问题：原样返回，不换线路
        self.server.pool.nodes["a"].unsupported.clear()
        self.server.pool.nodes["b"].last_used = 9e12
        code, data = self.ask()
        self.assertEqual(code, 400)
        self.assertIn("messages field required", json.dumps(data))
        self.assertEqual(b.hits, 1)

    def test_every_node_rejecting_the_model_returns_the_upstream_error_and_node_model_fixes_it(self):
        a = self.upstream()
        a.only = {"amd-model"}
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"}])
        code, data = self.ask("tierflow_pro")
        self.assertEqual(code, 404)
        self.assertIn("does not exist", json.dumps(data))
        write_config(self.config, [{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka", "model": "amd-model"}])
        deadline = time.time() + 5
        while time.time() < deadline and not self.server.pool.nodes["a"].model:
            time.sleep(0.1)
            self.server.pool.reload()
        self.assertEqual(self.ask("tierflow_pro")[0], 200)
        self.assertEqual(json.loads(a.bodies[-1])["model"], "amd-model")


class OutputCapTests(ProxyCase):
    """站点有输出上限时，超出的 max_tokens 压到上限再发，不让对方整个请求 400。"""

    ask = NodeModelTests.ask

    def cap(self, up, value):
        data = json.loads(self.config.read_text(encoding="utf-8"))
        data["stations"] = {kp.Node({"id": "x", "baseUrl": up.url, "key": "k"}).station: {"maxOutputTokens": value}}
        self.config.write_text(json.dumps(data), encoding="utf-8")

    def test_output_limit_above_the_station_cap_is_lowered_and_the_rest_is_untouched(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"}])
        self.cap(a, 1000)
        self.assertEqual(self.ask(max_tokens=5000, temperature=0.5)[0], 200)
        self.assertEqual(self.ask(max_completion_tokens=1001)[0], 200)
        self.assertEqual(self.ask(max_tokens=1000)[0], 200)
        self.assertEqual(self.ask()[0], 200)
        sent = [json.loads(x) for x in a.bodies]
        self.assertEqual((sent[0]["max_tokens"], sent[0]["temperature"], sent[0]["model"]), (1000, 0.5, "m1"))
        self.assertEqual(sent[1]["max_completion_tokens"], 1000)
        self.assertEqual(sent[2]["max_tokens"], 1000)
        self.assertNotIn("max_tokens", sent[3])

    def test_without_a_station_cap_the_output_limit_is_sent_as_is(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"}])
        self.assertEqual(self.ask(max_tokens=999999)[0], 200)
        self.assertEqual(json.loads(a.bodies[0])["max_tokens"], 999999)


class ModuleTests(unittest.TestCase):
    """监看中心里的 Key 池模块：只测接口与配置文件，不启动代理进程。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.port = free_port()
        config = cfg.load(root / "none.json", {"port": self.port, "log": {"dir": str(root / "logs")},
                                               "keypool": {"port": free_port(), "file": str(root / "keypool.json")}})
        config["root"] = str(root)
        self.ctx = Context(config, logs.setup(None), self.port)
        self.app = App(self.ctx)
        self.module = KeyPoolModule(self.ctx, manage=False)
        self.app.register(self.module)
        self.app.start()
        self.httpd = self.app.server()
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.up = FakeUpstream()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.up.close()
        self.tmp.cleanup()

    def post(self, path, body, token=True):
        headers = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % self.port,
                   "Host": "127.0.0.1:%d" % self.port}
        if token:
            headers["X-Xiaoba-Token"] = self.ctx.page_token
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                         data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def get_state(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/api/keypool/state" % self.port, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_file_is_created_private_with_a_master_key(self):
        data = json.loads(self.module.file.read_text(encoding="utf-8"))
        self.assertTrue(data["masterKey"].startswith("sk-xb-"))
        self.assertEqual(stat.S_IMODE(os.stat(self.module.file).st_mode), 0o600)

    def test_pay_key_order_and_floor_are_saved_and_never_returned(self):
        code, made = self.post("/api/keypool/nodes", {"node": {"name": "双路", "baseUrl": self.up.url, "key": "plan-secret-1",
                                                               "payKey": "pay-secret-22", "payFloor": "50"}})
        self.assertEqual(code, 200)
        node_id = made["node"]["id"]
        self.assertEqual((made["node"]["hasKey"], made["node"]["hasPayKey"], made["node"]["payFirst"], made["node"]["payFloor"]),
                         (True, True, False, 50))
        self.assertNotIn("pay-secret-22", json.dumps(made) + json.dumps(self.get_state()))
        saved = json.loads(Path(self.module.file).read_text(encoding="utf-8"))["nodes"][0]
        self.assertEqual((saved["key"], saved["payKey"], saved["payFloor"]), ("plan-secret-1", "pay-secret-22", 50))
        # 留空不修改；只改顺序
        code, edited = self.post("/api/keypool/nodes", {"node": {"id": node_id, "payFirst": True, "payFloor": 0}})
        self.assertEqual((code, edited["node"]["payFirst"], edited["node"]["hasPayKey"], edited["node"]["payFloor"]),
                         (200, True, True, 0))
        # 两个 Key 分别测试连通
        self.assertTrue(self.post("/api/keypool/nodes/test", {"id": node_id, "which": "pay"})[1]["ok"])
        self.assertEqual(self.up.auth[-1], "Bearer pay-secret-22")
        self.assertTrue(self.post("/api/keypool/nodes/test", {"id": node_id})[1]["ok"])
        self.assertEqual(self.up.auth[-1], "Bearer plan-secret-1")
        # 去掉余额 Key；最后一个 Key 不能去掉
        code, cleared = self.post("/api/keypool/nodes", {"node": {"id": node_id, "payKeyClear": True}})
        self.assertEqual((code, cleared["node"]["hasPayKey"]), (200, False))
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node_id, "keyClear": True}})[0], 400)
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node_id, "payFloor": -1}})[0], 400)

    def test_add_edit_disable_delete_node(self):
        code, added = self.post("/api/keypool/nodes", {"node": {"name": "中转A", "baseUrl": self.up.url + "/v1/",
                                                                "key": "sk-upstream-secret-1234", "weight": 2,
                                                                "maxConcurrency": 5}})
        self.assertEqual(code, 200, added)
        node_id = added["node"]["id"]
        self.assertEqual(added["node"]["baseUrl"], self.up.url + "/v1")
        state = self.get_state()
        self.assertEqual(len(state["nodes"]), 1)
        self.assertNotIn("sk-upstream-secret-1234", json.dumps(state))
        self.assertNotIn(self.module.load()["masterKey"], json.dumps(state))
        # 编辑时不填 Key 就保留原来的
        code, _ = self.post("/api/keypool/nodes", {"node": {"id": node_id, "name": "中转A2", "enabled": False}})
        self.assertEqual(code, 200)
        saved = self.module.load()["nodes"][0]
        self.assertEqual((saved["name"], saved["enabled"], saved["key"]), ("中转A2", False, "sk-upstream-secret-1234"))
        self.assertEqual(self.post("/api/keypool/layout", {"positions": {node_id: {"x": 10, "y": 20}}})[0], 200)
        self.assertEqual(self.module.load()["nodes"][0]["pos"], {"x": 10.0, "y": 20.0})
        # 格位：存成第几列第几行，旧的像素位置随之作废；节点信息里带回给页面
        self.assertEqual(self.post("/api/keypool/layout", {"positions": {node_id: {"col": 1, "row": 3}}})[0], 200)
        saved = self.module.load()["nodes"][0]
        self.assertEqual(saved["slot"], {"col": 1, "row": 3})
        self.assertNotIn("pos", saved)
        self.assertEqual(self.get_state()["nodes"][0]["slot"], {"col": 1, "row": 3})
        self.assertEqual(self.post("/api/keypool/layout", {"positions": {node_id: {"col": 99, "row": 0}}})[0], 400)
        self.assertEqual(self.post("/api/keypool/nodes/delete", {"id": node_id})[0], 200)
        self.assertEqual(self.module.load()["nodes"], [])

    def test_bad_input_is_rejected(self):
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"name": "x", "baseUrl": "ftp://a", "key": "k"}})[0], 400)
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"name": "x", "baseUrl": self.up.url}})[0], 400)
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"name": "", "baseUrl": self.up.url, "key": "k"}})[0], 400)
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"name": "x", "baseUrl": self.up.url, "key": "k",
                                                                   "weight": -1}})[0], 400)

    def test_writes_need_page_token(self):
        self.assertEqual(self.post("/api/keypool/master-key", {}, token=False)[0], 403)
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"name": "x", "baseUrl": self.up.url, "key": "k"}},
                                   token=False)[0], 403)
        code, data = self.post("/api/keypool/master-key", {})
        self.assertEqual(code, 200)
        self.assertEqual(data["key"], self.module.load()["masterKey"])

    def test_rotate_changes_master_key(self):
        before = self.module.load()["masterKey"]
        code, data = self.post("/api/keypool/master-key/rotate", {})
        self.assertEqual(code, 200)
        self.assertNotEqual(data["key"], before)

    def test_connectivity_test(self):
        code, result = self.post("/api/keypool/nodes/test", {"baseUrl": self.up.url, "key": "k"})
        self.assertEqual(code, 200)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["models"], 2)
        code, result = self.post("/api/keypool/nodes/test", {"baseUrl": "http://127.0.0.1:%d" % free_port(), "key": "k"})
        self.assertFalse(result["ok"])

    def test_connectivity_test_is_not_blocked_as_python_urllib(self):
        self.up.block_agent = "Python-urllib"       # 像 Cloudflare 那样拒绝 Python 自带的客户端标识
        code, result = self.post("/api/keypool/nodes/test", {"baseUrl": self.up.url, "key": "k"})
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.up.agents[-1].startswith("Python-urllib"), self.up.agents)

    def test_node_client_identity_is_saved_and_used_for_testing(self):
        code, result = self.post("/api/keypool/nodes", {"node": {"name": "x", "baseUrl": self.up.url, "key": "k",
                                                                 "userAgent": "station-only/3.1"}})
        self.assertEqual(code, 200, result)
        node_id = result["node"]["id"]
        self.assertEqual(result["node"]["userAgent"], "station-only/3.1")
        self.assertEqual(self.post("/api/keypool/nodes/test", {"id": node_id})[1]["ok"], True)
        self.assertEqual(self.up.agents[-1], "station-only/3.1")          # 已保存的节点：用它自己的标识
        self.post("/api/keypool/nodes/test", {"baseUrl": self.up.url, "key": "k", "userAgent": "typed/1"})
        self.assertEqual(self.up.agents[-1], "typed/1")                   # 编辑框里刚填、还没保存的也算
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node_id, "userAgent": "中文"}})[0], 400)
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node_id, "userAgent": ""}})[0], 200)
        self.assertNotIn("userAgent", self.module.load()["nodes"][0])

    def test_connectivity_test_names_a_site_shield_block(self):
        self.up.block_agent = "xiaoba-keypool"      # 防护连 Key 池自己的标识也拒绝时，提示不能说成 Key 的问题
        code, result = self.post("/api/keypool/nodes/test", {"baseUrl": self.up.url, "key": "k"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 403)
        self.assertIn("防护", result["detail"])

    def test_state_when_proxy_is_down_is_clear_not_crash(self):
        state = self.get_state()
        self.assertFalse(state["online"])
        self.assertEqual(state["nodes"], [])


if __name__ == "__main__":
    unittest.main()


class DualRouteTests(ProxyCase):
    """一个节点两个 Key：套餐 Key 与余额 Key。"""

    def hint(self, node, **amounts):
        query = "&".join("%s=%s" % item for item in amounts.items())
        request = urllib.request.Request("http://127.0.0.1:%d/_admin/balance?node=%s&%s" % (self.port, node, query),
                                         headers={"X-KeyPool-Admin": ADMIN})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))["ok"]

    def node(self, name="A"):
        return {n["name"]: n for n in self.state()["nodes"]}[name]

    def test_plan_key_goes_first_and_a_single_key_node_is_unchanged(self):
        a, b = self.upstream(), self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "only-b"}])
        for _ in range(4):
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(set(a.auth), {"Bearer plan-a"})
        self.assertEqual(set(b.auth), {"Bearer only-b"})
        self.assertEqual((self.node("A")["dual"], self.node("A")["route"], self.node("B")["dual"]), (True, "plan", False))
        routes = {e["nodeName"]: e["route"] for e in self.state()["events"]}
        self.assertEqual(routes, {"A": "plan", "B": None})

    def test_pay_first_reverses_the_order(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a", "payFirst": True}])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(a.auth, ["Bearer pay-a"])

    def test_refused_plan_key_retries_the_same_node_on_the_pay_key_and_is_remembered(self):
        a = self.upstream()
        a.deny["plan-a"] = (403, "用户额度不足")
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a"}])
        for _ in range(3):
            self.assertEqual(call(self.port)[0], 200)             # 客户端一次错误都看不到
        self.assertEqual(a.auth, ["Bearer plan-a", "Bearer pay-a", "Bearer pay-a", "Bearer pay-a"])
        node = self.node()
        self.assertEqual((node["route"], node["lastRoute"], node["cooldownLeft"], node["inflight"]), ("pay", "pay", 0, 0))
        blocked = {r["id"]: r["blocked"] for r in node["routes"]}
        self.assertIn("套餐 Key 被拒（403）", blocked["plan"])
        self.assertIsNone(blocked["pay"])
        self.assertEqual([(e["route"], e["ok"]) for e in self.state()["events"]][:2], [("plan", False), ("pay", True)])

    def test_plain_429_is_rate_limiting_not_quota(self):
        a = self.upstream()
        a.deny["plan-a"] = (429, "too many requests")
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a"}])
        self.assertEqual(call(self.port)[0], 429)
        self.assertEqual(a.auth, ["Bearer plan-a"])
        a.deny["plan-a"] = (429, "insufficient_quota")            # 429 但写明是额度：换路
        self.server.pool.nodes["a"].cooldown_until = 0
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(a.auth[-1], "Bearer pay-a")

    def test_both_keys_refused_fails_over_to_another_node(self):
        a, b = self.upstream(), self.upstream()
        a.deny.update({"plan-a": (403, "额度不足"), "pay-a": (403, "余额不足")})
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"}])
        self.server.pool.nodes["b"].last_used = 9e12
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((a.auth, b.hits), (["Bearer plan-a", "Bearer pay-a"], 1))
        self.assertEqual(self.node("A")["status"], "down")
        self.assertEqual(self.node("A")["inflight"], 0)

    def test_balance_reading_switches_routes_before_any_error(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a"}])
        self.assertTrue(self.hint("a", plan=0, pay=120.5))
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(a.auth, ["Bearer pay-a"])
        self.assertTrue(self.hint("a", plan=30, pay=120.5))       # 套餐续上了：回到套餐
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(a.auth[-1], "Bearer plan-a")
        self.server.pool.nodes["a"].hint["at"] -= 10000           # 读数过期：不再据此判断
        self.assertTrue(self.hint("a", plan=0, pay=0) and True)
        self.server.pool.nodes["a"].hint["at"] -= 10000
        self.assertEqual(self.node()["route"], "plan")
        self.assertFalse(self.hint("nobody", plan=1, pay=1))

    def test_pay_floor_is_a_hard_stop(self):
        a, b = self.upstream(), self.upstream()
        a.deny["plan-a"] = (403, "套餐额度不足")
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a", "payFloor": 50},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"}])
        self.server.pool.nodes["b"].last_used = 9e12
        self.assertTrue(self.hint("a", plan=5, pay=49.9))         # 按量余额已在底线以下
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((a.auth, b.hits), (["Bearer plan-a"], 1))  # 套餐被拒也不碰余额 Key，直接换节点
        self.assertIn("底线", {r["id"]: r["blocked"] for r in self.node("A")["routes"]}["pay"])
        self.assertTrue(self.hint("a", plan=5, pay=80))           # 充值后高于底线：余额路恢复
        self.server.pool.nodes["a"].cooldown_until = 0
        self.server.pool.nodes["b"].last_used = 9e12
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(a.auth[-1], "Bearer pay-a")

    def test_pay_floor_without_a_fresh_reading_keeps_the_pay_key_unused(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "payKey": "pay-a", "payFloor": 50}])
        self.assertEqual(call(self.port)[0], 503)
        self.assertEqual(a.hits, 0)
        self.assertTrue(self.hint("a", pay=60))
        self.assertEqual(call(self.port)[0], 200)

    def test_keys_never_appear_in_state(self):
        a = self.upstream()
        a.deny["plan-secret-a"] = (403, "额度不足")
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-secret-a", "payKey": "pay-secret-a"}])
        call(self.port)
        text = json.dumps(self.state())
        self.assertNotIn("plan-secret-a", text)
        self.assertNotIn("pay-secret-a", text)

    def test_each_route_can_send_its_own_model(self):
        a = self.upstream()
        a.deny["plan-a"] = (403, "额度不足")
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a",
                     "model": "plan-model", "payModel": "pay-model"}])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual([json.loads(x)["model"] for x in a.bodies], ["plan-model", "pay-model"])
        self.assertEqual(a.auth, ["Bearer plan-a", "Bearer pay-a"])
        self.assertEqual([e["model"] for e in self.state()["events"]], ["plan-model", "pay-model"])

    def test_pay_model_defaults_to_the_plan_model(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a",
                     "model": "plan-model", "payFirst": True}])
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(json.loads(a.bodies[0])["model"], "plan-model")

    def test_model_missing_on_one_route_uses_the_other_route_and_is_remembered(self):
        a = self.upstream()
        a.only = {"big-model"}                                    # 套餐路发 m1 → 404；余额路改发 big-model → 有
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a",
                     "payModel": "big-model"}])
        for _ in range(3):
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(a.auth, ["Bearer plan-a", "Bearer pay-a", "Bearer pay-a", "Bearer pay-a"])
        node = self.node()
        self.assertEqual((node["cooldownLeft"], node["inflight"]), (0, 0))


class BalanceEndpointTests(ProxyCase):
    """GET /user/balance：DeepSeek 格式的全池余额合计，给只认 DeepSeek 余额接口的客户端（如 DSH 的余额挂件）用。"""

    hint = DualRouteTests.hint

    def balance(self, path="/user/balance", key=MASTER):
        status, data = call(self.port, path, key=key, method="GET")
        return status, json.loads(data.decode("utf-8"))

    def test_sums_fresh_readings_of_enabled_nodes_in_deepseek_format(self):
        a, b, c = self.upstream(), self.upstream(), self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "plan-a", "payKey": "pay-a"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"},
                    {"id": "c", "name": "C", "baseUrl": c.url, "key": "kc", "enabled": False}])
        self.assertTrue(self.hint("a", plan=10.5, pay=2.25))
        self.assertTrue(self.hint("b", plan=0, pay=7.1234))
        self.assertTrue(self.hint("c", plan=100, pay=100))        # 停用的节点不算：这笔钱池子用不到
        for path in ("/user/balance", "/v1/user/balance"):
            status, body = self.balance(path)
            self.assertEqual(status, 200)
            self.assertTrue(body["is_available"])
            self.assertEqual(body["balance_infos"], [{"currency": "CNY", "total_balance": "19.8734",
                                                      "granted_balance": "10.5000", "topped_up_balance": "9.3734"}])
            self.assertEqual(body["key_pool"], {"nodes": 2, "fresh": 2, "plan_missing": 0})
        self.assertEqual((a.hits, b.hits, c.hits), (0, 0, 0))     # 不转发给任何节点

    def test_needs_the_master_key(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"}])
        self.hint("a", plan=1, pay=1)
        self.assertEqual(self.balance(key="bad")[0], 401)
        self.assertEqual(self.balance(key=None)[0], 401)
        self.assertEqual(a.hits, 0)

    def test_stale_or_partial_readings(self):
        a, b = self.upstream(), self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "kb"}])
        self.assertEqual(self.balance()[0], 503)                  # 还没有任何读数：报错，不报 0
        self.hint("a", pay=3)                                     # 套餐接口失败时监看中心只送按量
        self.hint("b", plan=5, pay=1)
        self.server.pool.nodes["b"].hint["at"] -= 10000           # 过期的读数不算
        status, body = self.balance()
        self.assertEqual((status, body["balance_infos"][0]["total_balance"]), (200, "3.0000"))
        self.assertEqual(body["key_pool"], {"nodes": 2, "fresh": 1, "plan_missing": 1})
        self.hint("a", plan=0, pay=0)
        self.assertFalse(self.balance()[1]["is_available"])
        self.assertEqual(a.hits + b.hits, 0)


class StatsRouteTests(ModuleTests):
    """统计接口：汇总、明细分页、导出。"""

    def test_stats_usage_and_csv(self):
        from monitor_core import keypool_stats as ks
        now = time.time()
        for i in range(3):
            ks.append_usage(self.module.usage_dir, {"t": now - 60 * i, "node": "a", "name": "A", "model": "m1", "asked": "m1",
                                                     "path": "/v1/chat/completions", "stream": False, "status": 200 if i else 500,
                                                     "ok": bool(i), "attempt": 1, "ms": 10, "ttfb": 20, "total": 30,
                                                     "in": 5, "out": 7, "cache": 0})
        ks.append_balance(self.module.usage_dir, "a", 10.0, 2.0, now - 500)
        ks.append_balance(self.module.usage_dir, "a", 9.5, 2.0, now - 100)
        base = "http://127.0.0.1:%d/api/keypool/" % self.port
        read = lambda path: urllib.request.urlopen(base + path, timeout=10)   # noqa: E731
        data = json.loads(read("stats?range=1h").read())
        self.assertEqual((data["ok"], data["requests"], data["succeeded"], data["fail"], data["tokensIn"], data["tokensOut"]),
                         (True, 3, 2, 1, 10, 14))
        self.assertEqual((data["spend"]["total"], data["spend"]["nodes"]), (0.5, [{"key": "a", "label": "a", "amount": 0.5}]))
        self.assertEqual(data["bucket"], 3600)
        self.assertEqual(json.loads(read("stats?range=7d").read())["bucket"], 86400)
        self.assertEqual(json.loads(read("stats?range=1h&result=fail").read())["requests"], 1)
        page = json.loads(read("usage?range=1h&limit=2&offset=0").read())
        self.assertEqual((page["total"], len(page["rows"]), page["rows"][0]["status"]), (3, 2, 500))   # 新的在前
        self.assertEqual(len(json.loads(read("usage?range=1h&limit=2&offset=2").read())["rows"]), 1)
        response = read("usage.csv?range=1h&result=ok")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(len(response.read().decode("utf-8").strip().splitlines()), 3)
        # 从现在起重新统计：数字归零，记录文件一条不少；恢复后又都回来
        self.assertIsNone(data["baseline"])
        self.assertEqual(self.post("/api/keypool/stats/baseline", {"action": "set"})[0], 200)
        zeroed = json.loads(read("stats?range=1h").read())
        self.assertEqual((zeroed["requests"], zeroed["spend"]["total"]), (0, 0))
        self.assertIsNotNone(zeroed["baseline"])
        self.assertEqual(json.loads(read("usage?range=1h").read())["total"], 0)
        self.assertEqual(len(list(ks.load(self.module.usage_dir, now - 3600, now + 60))), 3)
        ks.append_usage(self.module.usage_dir, {"t": time.time(), "node": "a", "name": "A", "model": "m1", "asked": "m1",
                                                 "path": "/v1/chat/completions", "stream": False, "status": 200, "ok": True,
                                                 "attempt": 1, "ms": 10, "ttfb": 20, "total": 30, "in": 1, "out": 1, "cache": 0})
        self.assertEqual(json.loads(read("stats?range=1h").read())["requests"], 1)
        self.assertEqual(self.post("/api/keypool/stats/baseline", {"action": "clear"})[0], 200)
        self.assertEqual(json.loads(read("stats?range=1h").read())["requests"], 4)
        self.assertEqual(self.post("/api/keypool/stats/baseline", {"action": "wipe"})[0], 400)
        self.assertEqual(self.post("/api/keypool/stats/baseline", {"action": "set"}, token=False)[0], 403)


class ModelRoutingTests(ProxyCase):
    """按客户端要的模型选路：哪一路有这个模型就走哪一路，模型名不改；别名；模型列表由 Key 池自己回答。"""

    PLAN, PAY = ["tierflow", "tierflow_pro"], ["DeepSeek-V4.1-Flash", "Qwen3.8-Flash", "tierflow_pro"]

    def ask(self, model):
        return call(self.port, body={"model": model, "messages": []})[0]

    def pool_nodes(self):
        tf, amd = self.upstream(), self.upstream()
        tf.only, amd.only = set(self.PLAN + self.PAY), {"DeepSeek-V4.1-Flash", "Qwen3.8-Flash-Next"}
        nodes = [{"id": "tf", "name": "TF", "baseUrl": tf.url, "key": "plan", "payKey": "pay", "models": self.PLAN,
                  "payModels": self.PAY, "model": "tierflow_pro", "payModel": "DeepSeek-V4.1-Flash"},
                 {"id": "amd", "name": "AMD", "baseUrl": amd.url, "key": "ka", "model": "DeepSeek-V4.1-Flash",
                  "models": ["DeepSeek-V4.1-Flash", "Qwen3.8-Flash-Next"]}]
        return tf, amd, nodes

    def test_the_client_model_picks_the_route_and_is_sent_unchanged(self):
        tf, amd, nodes = self.pool_nodes()
        self.start(nodes)
        for _ in range(4):
            self.assertEqual(self.ask("tierflow_pro"), 200)        # 只有 TF 有：全走 TF 的套餐 Key，AMD 不拿替代模型顶上
        self.assertEqual((tf.auth, amd.hits), (["Bearer plan"] * 4, 0))
        self.assertEqual({json.loads(x)["model"] for x in tf.bodies}, {"tierflow_pro"})
        for _ in range(4):
            self.assertEqual(self.ask("DeepSeek-V4.1-Flash"), 200)  # 套餐路没有：TF 走余额 Key，AMD 也参与
        self.assertEqual((tf.auth[4:], amd.hits), (["Bearer pay"] * 2, 2))
        self.assertEqual({json.loads(x)["model"] for x in tf.bodies[4:] + amd.bodies}, {"DeepSeek-V4.1-Flash"})
        self.assertEqual(tf.hits + amd.hits, 8)                    # 没有一次是撞了 404 才换路的

    def test_plan_model_falls_back_to_the_pay_key_with_the_same_model(self):
        tf, amd, nodes = self.pool_nodes()
        tf.deny = {"plan": (403, "套餐额度已用完")}
        self.start(nodes)
        self.assertEqual(self.ask("tierflow_pro"), 200)
        self.assertEqual(tf.auth, ["Bearer plan", "Bearer pay"])
        self.assertEqual(json.loads(tf.bodies[-1])["model"], "tierflow_pro")

    def test_alias_lets_a_differently_named_model_join_and_is_recorded(self):
        tf, amd, nodes = self.pool_nodes()
        self.start(nodes)
        for _ in range(4):
            self.assertEqual(self.ask("Qwen3.8-Flash"), 200)
        self.assertEqual((tf.hits, amd.hits), (4, 0))               # 没配别名：AMD 没有这个名字，不参与
        nodes[1]["aliases"] = {"Qwen3.8-Flash": "Qwen3.8-Flash-Next"}
        write_config(self.config, nodes)
        self.server.pool.reload()
        for _ in range(4):
            self.assertEqual(self.ask("Qwen3.8-Flash"), 200)
        self.assertEqual((tf.hits, amd.hits), (6, 2))
        self.assertEqual({json.loads(x)["model"] for x in amd.bodies}, {"Qwen3.8-Flash-Next"})
        sent = [(e["asked"], e["model"]) for e in self.state()["events"] if e["nodeName"] == "AMD"]
        self.assertEqual(sent, [("Qwen3.8-Flash", "Qwen3.8-Flash-Next")] * 2)

    def test_a_model_nobody_has_still_uses_each_nodes_substitute(self):
        tf, amd, nodes = self.pool_nodes()
        self.start(nodes)
        for _ in range(2):
            self.assertEqual(self.ask("gpt-unknown"), 200)
        self.assertEqual({json.loads(x)["model"] for x in tf.bodies + amd.bodies}, {"tierflow_pro", "DeepSeek-V4.1-Flash"})

    def test_route_without_a_model_list_is_tried_last(self):
        tf, amd, nodes = self.pool_nodes()
        del nodes[1]["models"]                                     # AMD 的清单还没读到
        self.start(nodes)
        for _ in range(3):
            self.assertEqual(self.ask("DeepSeek-V4.1-Flash"), 200)
        self.assertEqual(amd.hits, 0)                              # 有确定有这个模型的线路时，先用它们
        self.server.pool.nodes["tf"].quarantined = True            # 只剩它了：原样发过去试
        self.assertEqual(self.ask("DeepSeek-V4.1-Flash"), 200)
        self.assertEqual(json.loads(amd.bodies[-1])["model"], "DeepSeek-V4.1-Flash")

    def test_pool_answers_the_model_list_itself(self):
        tf, amd, nodes = self.pool_nodes()
        nodes[1]["aliases"] = {"Qwen3.8-Flash": "Qwen3.8-Flash-Next"}
        self.start(nodes)

        def listed(path="/v1/models", **kw):
            code, data = call(self.port, path, method="GET", **kw)
            self.assertEqual(code, 200)
            return [m["id"] for m in json.loads(data.decode("utf-8"))["data"]]
        union = sorted(set(self.PLAN + self.PAY + ["DeepSeek-V4.1-Flash", "Qwen3.8-Flash-Next", "Qwen3.8-Flash"]))
        self.assertEqual(listed(), union)                          # 没设公布清单：所有线路的模型合在一起
        self.assertEqual(listed("/models", header="x-api-key"), union)
        write_config(self.config, nodes, publishedModels=["tierflow_pro", "Qwen3.8-Flash"])
        self.server.pool.reload()
        self.assertEqual(listed(), ["tierflow_pro", "Qwen3.8-Flash"])
        self.assertEqual(tf.hits + amd.hits, 0)                    # 一次都没有转给上游
        self.assertEqual(call(self.port, "/v1/models", method="GET", key="wrong")[0], 401)

    def test_model_list_is_forwarded_when_no_route_has_been_read_yet(self):
        a = self.upstream()
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "ka"}])
        code, data = call(self.port, "/v1/models", method="GET")
        self.assertEqual((code, a.hits), (200, 1))
        self.assertEqual([m["id"] for m in json.loads(data.decode("utf-8"))["data"]], ["m1", "m2"])


class ModelCatalogTests(ModuleTests):
    """监看中心这一侧：读每一路的模型清单、别名、对外公布的清单。"""

    def add(self, **extra):
        code, made = self.post("/api/keypool/nodes", {"node": dict({"name": "双路", "baseUrl": self.up.url, "key": "plan-key",
                                                                   "payKey": "pay-key"}, **extra)})
        self.assertEqual(code, 200, made)
        return made["node"]["id"]

    def saved(self):
        return json.loads(Path(self.module.file).read_text(encoding="utf-8"))

    def test_model_lists_are_read_per_key_and_only_when_due(self):
        self.add()
        self.assertEqual(self.module.refresh_models(), (2, 0))
        node = self.saved()["nodes"][0]
        self.assertEqual((node["models"], node["payModels"]), (["m1", "m2"], ["m1", "m2"]))
        self.assertEqual(sorted(self.up.auth), ["Bearer pay-key", "Bearer plan-key"])
        self.assertEqual(self.module.refresh_models(), (0, 0))      # 刚读过：不重复读
        state = self.get_state()
        self.assertEqual(state["nodes"][0]["models"], ["m1", "m2"])
        self.assertEqual(state["modelCatalog"], [{"id": "m1", "routes": 2}, {"id": "m2", "routes": 2}])
        self.assertEqual(self.post("/api/keypool/models/refresh", {})[1]["refreshed"], 2)

    def test_changing_a_key_drops_its_list_and_a_failed_read_keeps_the_old_one(self):
        node_id = self.add()
        self.module.refresh_models()
        self.post("/api/keypool/nodes", {"node": {"id": node_id, "payKey": "pay-key-2"}})
        node = self.saved()["nodes"][0]
        self.assertEqual((node["models"], node.get("payModels")), (["m1", "m2"], None))
        self.up.mode = "429"
        self.assertEqual(self.module.refresh_models(force=True), (0, 2))
        self.assertEqual(self.saved()["nodes"][0]["models"], ["m1", "m2"])
        self.assertEqual(self.module.refresh_models(), (0, 0))      # 失败后先停一会儿

    def test_testing_a_saved_key_records_its_models(self):
        node_id = self.add()
        self.assertTrue(self.post("/api/keypool/nodes/test", {"id": node_id, "which": "pay"})[1]["ok"])
        node = self.saved()["nodes"][0]
        self.assertEqual((node.get("models"), node["payModels"]), (None, ["m1", "m2"]))
        self.post("/api/keypool/nodes/test", {"id": node_id, "key": "some-other-key"})   # 测的不是已保存的 Key：不记
        self.assertIsNone(self.saved()["nodes"][0].get("models"))

    def test_aliases_are_saved_validated_and_cleared(self):
        node_id = self.add(aliases={" Qwen3.8-Flash ": "Qwen3.8-Flash-Next"})
        self.assertEqual(self.saved()["nodes"][0]["aliases"], {"Qwen3.8-Flash": "Qwen3.8-Flash-Next"})
        self.assertEqual(self.get_state()["nodes"][0]["aliases"], {"Qwen3.8-Flash": "Qwen3.8-Flash-Next"})
        self.assertEqual(self.get_state()["modelCatalog"], [{"id": "Qwen3.8-Flash", "routes": 2}])
        for bad in ({"a": "a"}, {"a": ""}, {"a": 1}, ["a"]):
            self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node_id, "aliases": bad}})[0], 400, bad)
        self.post("/api/keypool/nodes", {"node": {"id": node_id, "name": "改名"}})         # 不带 aliases：保留
        self.assertIn("aliases", self.saved()["nodes"][0])
        self.post("/api/keypool/nodes", {"node": {"id": node_id, "aliases": {}}})
        self.assertNotIn("aliases", self.saved()["nodes"][0])

    def test_published_models_are_saved_and_an_empty_list_means_all(self):
        code, data = self.post("/api/keypool/published-models", {"models": ["tierflow_pro", " m1 ", "tierflow_pro", ""]})
        self.assertEqual((code, data["publishedModels"]), (200, ["tierflow_pro", "m1"]))
        self.assertEqual(self.get_state()["publishedModels"], ["tierflow_pro", "m1"])
        self.assertEqual(self.post("/api/keypool/published-models", {"models": []})[0], 200)
        self.assertNotIn("publishedModels", self.saved())
        self.assertEqual(self.post("/api/keypool/published-models", {"models": "x"})[0], 400)
        self.assertEqual(self.post("/api/keypool/published-models", {"models": ["x"]}, token=False)[0], 403)


class ContentFilterTests(ProxyCase):
    """中转站的内容过滤拒绝请求：不算节点故障，同一个站的其它节点直接跳过。"""

    REFUSAL = (500, "sensitive_words_detected (request id: x)")

    def station(self):
        tf = self.upstream()
        tf.deny = {"k1": self.REFUSAL, "k2": self.REFUSAL, "k3": self.REFUSAL}
        return tf, [{"id": "t%d" % i, "name": "T%d" % i, "baseUrl": tf.url, "key": "k%d" % i} for i in (1, 2, 3)]

    def test_refused_content_skips_the_rest_of_that_station_without_cooling_it(self):
        tf, nodes = self.station()
        other = self.upstream()
        self.start(nodes + [{"id": "o", "name": "Other", "baseUrl": other.url, "key": "ko"}])
        self.server.pool.nodes["o"].last_used = 9e12               # 让那个站先被选中
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((tf.hits, other.hits), (1, 1))            # 同站另外两个节点没有再白试
        state = {n["id"]: n for n in self.state()["nodes"]}
        self.assertEqual([(state[i]["cooldownLeft"], state[i]["fail"]) for i in ("t1", "t2", "t3")], [(0, 0)] * 3)
        self.assertEqual(sum("内容被这个站的过滤拒绝" in (state[i]["lastError"] or "") for i in ("t1", "t2", "t3")), 1)

    def test_refusal_is_returned_as_is_when_no_other_station_exists(self):
        tf, nodes = self.station()
        self.start(nodes)
        code, data = call(self.port)
        self.assertEqual(code, 500)
        self.assertIn("sensitive_words_detected", data.decode("utf-8"))
        self.assertEqual(tf.hits, 1)

    def test_an_ordinary_500_still_fails_over_within_the_station(self):
        tf, nodes = self.station()
        tf.deny = {"k1": (500, "internal error"), "k2": (500, "internal error")}
        self.start(nodes)
        self.server.pool.nodes["t3"].last_used = 9e12              # 好的那个排到最后
        self.assertEqual(call(self.port)[0], 200)
        self.assertEqual(tf.hits, 3)

    def test_capture_switch_keeps_one_refused_request_and_is_off_by_default(self):
        tf, nodes = self.station()
        usage = Path(self.tmp.name) / "usage"
        write_config(self.config, nodes)
        self.port = free_port()
        self.server = kp.make_server(str(self.config), self.port, ADMIN, usage_dir=str(usage))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        target = usage / "refused-request.json"
        self.server.pool.usage_dir = None                           # 第一次连用量记录都不写：目录还不存在
        call(self.port, body={"model": "m1", "messages": [{"role": "user", "content": "第一次"}]})
        self.assertFalse(target.exists())                           # 默认不存
        self.server.pool.usage_dir = str(usage)
        self.server.pool.settings["captureRefused"] = True
        call(self.port, body={"model": "m1", "messages": [{"role": "user", "content": "第二次"}]})
        call(self.port, body={"model": "m1", "messages": [{"role": "user", "content": "第三次"}]})
        saved = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(saved["body"])["messages"][0]["content"], "第二次")   # 只存第一份
        self.assertIn("sensitive_words_detected", saved["reply"])
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertNotIn("k1", target.read_text(encoding="utf-8"))


class RedirectAndErrorLogTests(ProxyCase):
    """3xx 不当成功转给客户端；上游报错正文写进日志并给 Key 打码。"""

    def test_redirect_fails_over_and_stops_the_moved_node(self):
        moved, good = self.upstream("301"), self.upstream()
        self.start([{"id": "m", "name": "M", "baseUrl": moved.url, "key": "k1"},
                    {"id": "g", "name": "G", "baseUrl": good.url, "key": "k2"}])
        pool = self.server.pool
        pool.nodes["m"].last_used, pool.nodes["g"].last_used = 0, 9e12   # 让搬家的先被选中
        code, data = call(self.port)
        self.assertEqual(code, 200)
        self.assertEqual((moved.hits, good.hits), (1, 1))
        node = {n["id"]: n for n in self.state()["nodes"]}["m"]
        self.assertEqual(node["breaker"], "open")   # 立刻停用，不等连续失败
        self.assertIn("moved.example", node["lastError"])
        call(self.port)
        self.assertEqual(moved.hits, 1)   # 停用期间不再分到它

    def test_redirect_on_the_only_node_is_a_clear_502_not_the_raw_redirect(self):
        moved = self.upstream("301")
        self.start([{"id": "m", "name": "M", "baseUrl": moved.url, "key": "k1"}])
        code, data = call(self.port)
        self.assertEqual(code, 502)
        self.assertIn(b"redirected", data)
        self.assertIn(b"moved.example", data)

    def test_upstream_error_body_is_logged_with_keys_masked(self):
        bad = self.upstream()
        bad.deny["secret-key-123"] = (400, "bad request for key secret-key-123: max_tokens too large")
        self.start([{"id": "b", "name": "B", "baseUrl": bad.url, "key": "secret-key-123"}])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code, data = call(self.port)
        self.assertEqual(code, 400)
        self.assertIn(b"max_tokens too large", data)   # 客户端照样拿到原话
        log = buf.getvalue()
        self.assertIn("upstream error: B", log)
        self.assertIn("status=400", log)
        self.assertIn("max_tokens too large", log)
        self.assertNotIn("secret-key-123", log)
        self.assertIn("sec***", log)


class StreamFirstEventTests(ProxyCase):
    """流式回复 200，但第一条事件就是上游的错误（比如 RequestBurstTooFast）：客户端还没收到东西，换节点重发。"""

    BURST = (b'data: {"error":{"code":"RequestBurstTooFast","message":"request burst too fast",'
             b'"type":"TooManyRequests"}}\n\n')

    def stream_call(self):
        return call(self.port, body={"model": "m1", "messages": [], "stream": True})

    def two_nodes(self, bad_events):
        bad, good = self.upstream("stream"), self.upstream("stream")
        bad.events = bad_events
        self.start([{"id": "b", "name": "B", "baseUrl": bad.url, "key": "k1"},
                    {"id": "g", "name": "G", "baseUrl": good.url, "key": "k2"}])
        pool = self.server.pool
        pool.nodes["b"].last_used, pool.nodes["g"].last_used = 0, 9e12   # 让出错的先被选中
        return bad, good

    def test_burst_error_as_first_event_fails_over_and_client_never_sees_it(self):
        bad, good = self.two_nodes([b": keep-alive\n\n", self.BURST])
        code, data = self.stream_call()
        self.assertEqual(code, 200)
        self.assertNotIn(b"RequestBurstTooFast", data)
        self.assertIn(b'data: {"i": 0}', data)
        self.assertEqual((bad.hits, good.hits), (1, 1))
        node = {n["id"]: n for n in self.state()["nodes"]}["b"]
        self.assertEqual(node["failStreak"], 1)
        self.assertIn("RequestBurstTooFast", node["lastError"])

    def test_anthropic_error_event_also_fails_over(self):
        bad, good = self.two_nodes([b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error",'
                                    b'"message":"Overloaded"}}\n\n'])
        code, data = self.stream_call()
        self.assertEqual(code, 200)
        self.assertNotIn(b"overloaded_error", data)
        self.assertEqual((bad.hits, good.hits), (1, 1))

    def test_last_node_passes_the_error_through_as_before(self):
        bad = self.upstream("stream")
        bad.events = [self.BURST]
        self.start([{"id": "b", "name": "B", "baseUrl": bad.url, "key": "k1"}])
        code, data = self.stream_call()
        self.assertEqual(code, 200)
        self.assertIn(b"RequestBurstTooFast", data)
        node = {n["id"]: n for n in self.state()["nodes"]}["b"]
        self.assertEqual((node["ok"], node["failStreak"]), (0, 1))     # 原样转了，但不算成功

    def test_normal_stream_is_untouched(self):
        s = self.upstream("stream")
        self.start([{"id": "s", "name": "S", "baseUrl": s.url, "key": "ks"}])
        code, data = self.stream_call()
        self.assertEqual(code, 200)
        self.assertEqual(data.count(b"data: "), 4)
        self.assertIn(b"[DONE]", data)
        self.assertEqual({n["id"]: n for n in self.state()["nodes"]}["s"]["ok"], 1)

    def test_error_after_content_is_not_retried(self):
        bad, good = self.two_nodes([b'data: {"i": 0}\n\n', self.BURST])
        code, data = self.stream_call()
        self.assertEqual(code, 200)
        self.assertIn(b"RequestBurstTooFast", data)                   # 正文已经发出去了，没法重来
        self.assertEqual((bad.hits, good.hits), (1, 0))

    def test_stream_error_helper(self):
        self.assertIsNone(kp.stream_error(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'))
        self.assertIsNone(kp.stream_error(b"data: [DONE]\n\n"))
        self.assertIsNone(kp.stream_error(b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n"))
        self.assertIn("RequestBurstTooFast", kp.stream_error(self.BURST))
        self.assertIsNotNone(kp.stream_error(b'{"error":{"message":"busy"}}'))   # 没按 SSE 回，直接给 JSON 错误


class StreamTimeoutTests(ProxyCase):
    """2026-10-23：中转站回了响应头和一条只带角色的事件，然后几百秒不出字。首段 180 秒、断流 120 秒（用户定）。"""

    ROLE = b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'

    @staticmethod
    def content(text):
        return b'data: {"choices":[{"index":0,"delta":{"content":"%s"}}]}\n\n' % text

    def stream_call(self, timeout=10):
        return call(self.port, body={"model": "m1", "messages": [], "stream": True}, timeout=timeout)

    def test_role_only_event_then_silence_fails_over_before_anything_reaches_the_client(self):
        bad, good = self.upstream("stream"), self.upstream("stream")
        bad.events = [self.ROLE, 3.0, self.content(b"late"), b"data: [DONE]\n\n"]
        self.start([{"id": "b", "name": "B", "baseUrl": bad.url, "key": "k1"},
                    {"id": "g", "name": "G", "baseUrl": good.url, "key": "k2"}], firstByteTimeoutSeconds=0.8)
        pool = self.server.pool
        pool.nodes["b"].last_used, pool.nodes["g"].last_used = 0, 9e12
        code, data = self.stream_call()
        self.assertEqual(code, 200)
        self.assertNotIn(b"late", data)
        self.assertNotIn(b'"role"', data)                 # 坏节点的角色事件没有漏给客户端
        self.assertIn(b'data: {"i": 0}', data)
        self.assertEqual((bad.hits, good.hits), (1, 1))
        self.assertIn("首段", {n["id"]: n for n in self.state()["nodes"]}["b"]["lastError"])

    def test_every_node_without_content_gives_a_clear_timeout_error(self):
        a, b = self.upstream("stream"), self.upstream("stream")
        a.events = b.events = [self.ROLE, 3.0, b"data: [DONE]\n\n"]
        self.start([{"id": "a", "name": "A", "baseUrl": a.url, "key": "k1"},
                    {"id": "b", "name": "B", "baseUrl": b.url, "key": "k2"}], firstByteTimeoutSeconds=0.5)
        code, data = self.stream_call()
        self.assertEqual(code, 504)
        self.assertIn(b"no content within", data)
        self.assertEqual((a.hits, b.hits), (1, 1))       # 每个节点只试一次，不无限重试

    def test_silence_after_content_is_cut_with_an_error_event(self):
        bad, good = self.upstream("stream"), self.upstream("stream")
        bad.events = [self.content(b"part1"), 3.0, self.content(b"part2"), b"data: [DONE]\n\n"]
        self.start([{"id": "b", "name": "B", "baseUrl": bad.url, "key": "k1"},
                    {"id": "g", "name": "G", "baseUrl": good.url, "key": "k2"}], streamIdleTimeoutSeconds=0.6)
        pool = self.server.pool
        pool.nodes["b"].last_used, pool.nodes["g"].last_used = 0, 9e12
        code, data = self.stream_call()
        self.assertEqual(code, 200)
        self.assertIn(b"part1", data)
        self.assertNotIn(b"part2", data)
        self.assertIn(b"mid-stream", data)               # 客户端收到一条明确的错误，不是悄悄断开
        self.assertEqual(good.hits, 0)                   # 正文已经发出去了，不能换节点重发
        self.assertIn("断流", {n["id"]: n for n in self.state()["nodes"]}["b"]["lastError"])

    def test_largest_gap_between_chunks_is_recorded(self):
        s = self.upstream("stream")
        s.events = [self.content(b"a"), 0.4, self.content(b"b"), b"data: [DONE]\n\n"]
        self.start([{"id": "s", "name": "S", "baseUrl": s.url, "key": "k"}])
        code, _ = self.stream_call()
        self.assertEqual(code, 200)
        event = self.state()["events"][-1]
        self.assertGreaterEqual(event["gap"], 300)

    def test_zero_or_missing_settings_use_the_built_in_waits(self):
        self.assertEqual((180.0, 120.0), kp.stream_waits({"firstByteTimeoutSeconds": 0, "streamIdleTimeoutSeconds": 0}))
        self.assertEqual((180.0, 120.0), kp.stream_waits({}))
        self.assertEqual((30.0, 5.0), kp.stream_waits({"firstByteTimeoutSeconds": 30, "streamIdleTimeoutSeconds": 5}))
        self.assertEqual((180, 120), (kp.DEFAULT_SETTINGS["firstByteTimeoutSeconds"],
                                      kp.DEFAULT_SETTINGS["streamIdleTimeoutSeconds"]))

    def test_content_detection(self):
        self.assertFalse(kp.event_has_content(b"", b'{"choices":[{"delta":{"role":"assistant"}}]}'))
        self.assertFalse(kp.event_has_content(b"", b'{"choices":[{"delta":{"content":""}}]}'))
        self.assertFalse(kp.event_has_content(b"message_start", b'{"type":"message_start"}'))
        self.assertTrue(kp.event_has_content(b"", b'{"choices":[{"delta":{"reasoning_content":"x"}}]}'))
        self.assertTrue(kp.event_has_content(b"", b'{"choices":[{"delta":{},"finish_reason":"stop"}]}'))
        self.assertTrue(kp.event_has_content(b"content_block_delta", b'{"type":"content_block_delta"}'))
        self.assertTrue(kp.event_has_content(b"", b"[DONE]"))
        self.assertTrue(kp.event_has_content(b"", b'{"error":{"message":"busy"}}'))
        self.assertTrue(kp.event_has_content(b"", b'{"i": 0}'))          # 认不出的格式不拦着
        self.assertTrue(kp.event_has_content(b"", b'{"choices":[],"usage":{"total_tokens":3}}'))


class StationLimitTests(ProxyCase):
    """整个站一起限速，以及对方按 IP 限流时的处理。"""

    IP_LIMIT = (429, "too many requests from your client ip")

    def start_with(self, nodes, settings=None, **extra):
        write_config(self.config, nodes, settings=dict({"queueTimeoutSeconds": 6, "timeoutSeconds": 10}, **(settings or {})),
                     **extra)
        self.port = free_port()
        self.server = kp.make_server(str(self.config), self.port, ADMIN)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def station(self, up):
        return "127.0.0.1:%d" % up.port

    def burst(self, count):
        results, started = [], time.time()
        threads = [threading.Thread(target=lambda: results.append(call(self.port)[0])) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results, time.time() - started

    def test_station_concurrency_is_shared_by_all_its_nodes(self):
        tf = self.upstream(delay=0.3)
        nodes = [{"id": "t%d" % i, "name": "T%d" % i, "baseUrl": tf.url, "key": "k%d" % i} for i in (1, 2, 3)]
        self.start_with(nodes, stations={self.station(tf): {"maxConcurrency": 1}})
        results, elapsed = self.burst(3)
        self.assertEqual(results, [200] * 3)
        self.assertGreaterEqual(elapsed, 0.9)                      # 三个节点也只能一个一个来
        self.assertEqual(len(set(tf.auth)), 3)                     # 但还是轮流用三个 Key
        self.assertEqual(self.state()["stations"][0]["inflight"], 0)

    def test_station_interval_spaces_requests_out(self):
        tf = self.upstream()
        nodes = [{"id": "t%d" % i, "name": "T%d" % i, "baseUrl": tf.url, "key": "k%d" % i} for i in (1, 2)]
        self.start_with(nodes, stations={self.station(tf): {"minIntervalSeconds": 0.4}})
        results, elapsed = self.burst(3)
        self.assertEqual(results, [200] * 3)
        self.assertGreaterEqual(elapsed, 0.8)

    def test_overflow_goes_to_another_station_instead_of_waiting(self):
        tf, other = self.upstream(delay=0.4), self.upstream(delay=0.4)
        nodes = [{"id": "t1", "name": "T1", "baseUrl": tf.url, "key": "k1"}, {"id": "t2", "name": "T2", "baseUrl": tf.url, "key": "k2"},
                 {"id": "o", "name": "Other", "baseUrl": other.url, "key": "ko"}]
        self.start_with(nodes, stations={self.station(tf): {"maxConcurrency": 1}})
        results, elapsed = self.burst(4)
        self.assertEqual(results, [200] * 4)
        self.assertLessEqual(tf.hits, 2)
        self.assertGreaterEqual(other.hits, 2)
        self.assertLess(elapsed, 1.5)

    def test_ip_limit_pauses_the_whole_station_and_uses_another_one(self):
        tf, other = self.upstream(), self.upstream()
        tf.deny = {"k1": self.IP_LIMIT, "k2": self.IP_LIMIT, "k3": self.IP_LIMIT}
        nodes = [{"id": "t%d" % i, "name": "T%d" % i, "baseUrl": tf.url, "key": "k%d" % i} for i in (1, 2, 3)]
        self.start_with(nodes + [{"id": "o", "name": "Other", "baseUrl": other.url, "key": "ko"}])
        self.server.pool.nodes["o"].last_used = 9e12
        for _ in range(3):
            self.assertEqual(call(self.port)[0], 200)
        self.assertEqual((tf.hits, other.hits), (1, 3))            # 只撞了一次，暂停期间不再去碰那个站
        state = self.state()
        nodes = {n["id"]: n for n in state["nodes"]}
        self.assertEqual([(nodes[i]["cooldownLeft"], nodes[i]["fail"]) for i in ("t1", "t2", "t3")], [(0, 0)] * 3)
        held = {s["name"]: s["holdLeft"] for s in state["stations"]}
        self.assertGreater(held[self.station(tf)], 0)
        self.assertEqual(held[self.station(other)], 0)

    def test_ip_limit_with_no_other_station_waits_and_retries(self):
        tf = self.upstream()
        tf.deny = {"k1": self.IP_LIMIT, "k2": self.IP_LIMIT}
        nodes = [{"id": "t%d" % i, "name": "T%d" % i, "baseUrl": tf.url, "key": "k%d" % i} for i in (1, 2)]
        self.start_with(nodes, settings={"ipLimitPauseSeconds": 1})
        threading.Timer(0.5, tf.deny.clear).start()                # 半秒后对方不再限流
        started = time.time()
        self.assertEqual(call(self.port)[0], 200)                  # 客户端没看到 429
        self.assertGreaterEqual(time.time() - started, 0.9)        # 等暂停结束才重发
        self.assertEqual(tf.hits, 2)

    def test_pinned_request_goes_to_the_route_that_has_the_model(self):
        tf = self.upstream()
        self.start_with([{"id": "t", "name": "T", "baseUrl": tf.url, "key": "plan", "payKey": "pay", "model": "plan-model",
                          "models": ["plan-model"], "payModels": ["pay-only"]}])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=json.dumps({"model": "pay-only", "messages": []}),
                     headers={"Authorization": "Bearer " + MASTER, "X-KeyPool-Node": "t"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertEqual((tf.auth[-1], json.loads(tf.bodies[-1])["model"]), ("Bearer pay", "pay-only"))


class StationSettingsTests(ModuleTests):
    def test_station_limits_are_saved_listed_and_cleared(self):
        self.post("/api/keypool/nodes", {"node": {"name": "A", "baseUrl": self.up.url + "/v1", "key": "k1"}})
        self.post("/api/keypool/nodes", {"node": {"name": "B", "baseUrl": self.up.url, "key": "k2", "enabled": False}})
        name = "127.0.0.1:%d" % self.up.port
        self.assertEqual(self.get_state()["stations"], [{"name": name, "nodes": 1, "maxConcurrency": 0, "minIntervalSeconds": 0,
                                                         "inflight": 0, "holdLeft": 0}])
        code, _ = self.post("/api/keypool/stations", {"station": name, "maxConcurrency": 2, "minIntervalSeconds": "2.5"})
        self.assertEqual(code, 200)
        saved = json.loads(Path(self.module.file).read_text(encoding="utf-8"))
        self.assertEqual(saved["stations"], {name: {"maxConcurrency": 2, "minIntervalSeconds": 2.5}})
        self.assertEqual((self.get_state()["stations"][0]["maxConcurrency"], self.get_state()["stations"][0]["minIntervalSeconds"]),
                         (2, 2.5))
        self.assertEqual(self.post("/api/keypool/stations", {"station": "nobody.example", "maxConcurrency": 1})[0], 404)
        self.assertEqual(self.post("/api/keypool/stations", {"station": name, "maxConcurrency": -1})[0], 400)
        self.assertEqual(self.post("/api/keypool/stations", {"station": name, "maxConcurrency": 1}, token=False)[0], 403)
        self.post("/api/keypool/stations", {"station": name, "maxConcurrency": 0, "minIntervalSeconds": 0})
        self.assertNotIn("stations", json.loads(Path(self.module.file).read_text(encoding="utf-8")))
