"""Key 池余额查询离线测试：假的站点响应，不访问任何真实中转站。"""
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_core import config as cfg, keypool_balance as kb, logs  # noqa: E402
from monitor_core.modules.keypool import KeyPoolModule  # noqa: E402
from monitor_core.web import App, Context  # noqa: E402

NOW = 1_800_000_000


def sub(status="active", end=NOW + 3600, total=0, used=0, tokens=0, tokens_used=0):
    return {"subscription": {"status": status, "end_time": end, "amount_total": total, "amount_used": used,
                             "basic_token_total": tokens, "basic_token_used": tokens_used}}


class ComputeTests(unittest.TestCase):
    def test_documented_example(self):
        result = kb.compute({"quota": 12500000}, {"quota_per_unit": 500000}, [sub(total=14950000)], NOW)
        self.assertEqual((result["payAsYouGo"], result["plan"], result["total"]), (25.0, 29.9, 54.9))
        self.assertFalse(result["degraded"])

    def test_only_active_unexpired_subscriptions_count_and_never_negative(self):
        subs = [sub(total=1000000, used=250000, tokens=100, tokens_used=30),
                sub(status="expired", total=5000000, tokens=50),
                sub(end=NOW - 1, total=5000000),
                sub(end=(NOW - 1) * 1000, total=5000000),
                sub(end="2020-01-01T00:00:00Z", total=5000000),
                sub(end=0, total=500000),
                sub(total=100, used=900, tokens=5, tokens_used=9),
                {"subscription": None}, "junk"]
        result = kb.compute({"quota": 0}, {"quota_per_unit": 500000}, subs, NOW)
        self.assertEqual(result["plan"], 2.5)      # 第一条 1.5，加上不限期的 1.0；用超的那条按 0 算
        self.assertEqual(result["activePlans"], 3)
        self.assertEqual(result["basicTokens"], 120)
        self.assertEqual(result["total"], 2.5)

    def test_unit_falls_back_and_result_keeps_four_decimals(self):
        for status in (None, {}, {"quota_per_unit": 0}, {"quota_per_unit": "x"}):
            self.assertEqual(kb.compute({"quota": 123457}, status, [], NOW)["total"], 0.2469)
        self.assertEqual(kb.compute({"quota": 1000}, {"quota_per_unit": 1000}, [], NOW)["total"], 1.0)

    def test_subscription_failure_degrades_and_missing_quota_is_an_error(self):
        result = kb.compute({"quota": 500000}, None, None, NOW)
        self.assertTrue(result["degraded"])
        self.assertEqual((result["total"], result["plan"]), (1.0, 0.0))
        self.assertIn("按量", result["note"])
        for user in (None, {}, {"quota": "12"}, {"quota": True}):
            with self.assertRaises(kb.BalanceError):
                kb.compute(user, None, [], NOW)


class FakeSite:
    def __init__(self):
        self.calls = []
        self.replies = {"/api/user/self": {"success": True, "data": {"quota": 12500000}},
                        "/api/status": {"success": True, "data": {"quota_per_unit": 500000}},
                        "/api/subscription/self": {"success": True, "data": {"subscriptions": [sub(end=0, total=14950000)]}}}

    def __call__(self, url, headers, timeout=12):
        self.calls.append((url, dict(headers)))
        reply = self.replies[url.split("example.test", 1)[1]]
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, bytes) else json.dumps(reply).encode("utf-8")


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.site = FakeSite()

    def query(self):
        return kb.query("https://example.test/v1", "cookie-secret", "user-secret", get=self.site, pause=0)

    def test_sends_both_headers_to_the_node_site_root_only(self):
        self.assertEqual(self.query()["total"], 54.9)
        self.assertEqual([url for url, _ in self.site.calls], ["https://example.test/api/user/self", "https://example.test/api/status",
                                                               "https://example.test/api/subscription/self"])
        for _, headers in self.site.calls:
            self.assertEqual((headers["Cookie"], headers["TF-User"]), ("session=cookie-secret", "user-secret"))
        for bad in ("http://example.test/v1", "ftp://example.test", ""):
            with self.assertRaises(kb.BalanceError):
                kb.query(bad, "c", "u", get=self.site, pause=0)

    def test_success_flag_rate_limit_retry_and_auth_errors(self):
        self.site.replies["/api/user/self"] = [b"<html>slow down</html>", {"success": True, "data": {"quota": 500000}}]
        self.assertEqual(self.query()["payAsYouGo"], 1.0)
        self.site.replies["/api/user/self"] = b"<html>slow down</html>"
        with self.assertRaisesRegex(kb.BalanceError, "限流"):
            self.query()
        self.site.replies["/api/user/self"] = {"success": False, "message": "cookie-secret leaked?"}
        with self.assertRaises(kb.BalanceError) as caught:
            self.query()
        self.assertNotIn("secret", str(caught.exception))
        for code, word in ((401, "失效"), (302, "重定向"), (500, "HTTP 500")):
            self.site.replies["/api/user/self"] = urllib.error.HTTPError("https://example.test", code, "user-secret", {}, io.BytesIO(b"cookie-secret"))
            with self.assertRaisesRegex(kb.BalanceError, word) as caught:
                self.query()
            self.assertNotIn("secret", str(caught.exception))

    def test_status_failure_uses_default_unit_and_plan_failure_degrades(self):
        self.site.replies["/api/status"] = {"success": False}
        self.site.replies["/api/subscription/self"] = OSError("cookie-secret")
        result = self.query()
        self.assertEqual((result["total"], result["degraded"]), (25.0, True))
        self.site.replies["/api/subscription/self"] = {"success": True, "data": {"subscriptions": None}}
        self.assertFalse(self.query()["degraded"])


class ModuleBalanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = cfg.load(root / "none.json", {"log": {"dir": str(root / "logs")},
                                               "keypool": {"port": 1, "file": str(root / "keypool.json")}})
        config["root"] = str(root)
        self.ctx = Context(config, logs.setup(None), 0)
        self.app = App(self.ctx)
        self.module = KeyPoolModule(self.ctx, manage=False)
        self.site = FakeSite()
        self.module.balance_get = self.site
        self.app.register(self.module)
        self.app.start()
        self.httpd = self.app.server()
        self.port = self.httpd.server_address[1]
        self.ctx.hosts = {"127.0.0.1:%d" % self.port}
        self.ctx.origins = {"http://127.0.0.1:%d" % self.port}
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def post(self, path, body, token=True):
        headers = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:%d" % self.port, "Host": "127.0.0.1:%d" % self.port}
        if token:
            headers["X-Xiaoba-Token"] = self.ctx.page_token
        request = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def state(self):
        request = urllib.request.Request("http://127.0.0.1:%d/api/keypool/state" % self.port, headers={"Host": "127.0.0.1:%d" % self.port})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def add(self, name, **extra):
        code, data = self.post("/api/keypool/nodes", {"node": dict({"name": name, "baseUrl": "https://example.test/v1", "key": "sk-node-secret"}, **extra)})
        self.assertEqual(code, 200, data)
        return data["node"]

    def test_credentials_are_saved_privately_never_returned_and_queried_only_on_request(self):
        plain = self.add("没填会话")
        node = self.add("填了会话", balanceCookie="session=cookie-secret", balanceUser="user-secret")
        self.assertEqual((plain["hasBalance"], node["hasBalance"]), (False, True))
        saved = self.module.load()["nodes"][1]
        self.assertEqual((saved["balanceCookie"], saved["balanceUser"]), ("cookie-secret", "user-secret"))
        self.assertEqual(stat.S_IMODE(os.stat(self.module.file).st_mode), 0o600)
        state = self.state()
        self.assertNotIn("secret", json.dumps(state))
        self.assertEqual([n["balance"] for n in state["nodes"]], [None, None])
        self.assertEqual(self.site.calls, [])
        self.assertEqual(self.post("/api/keypool/balance", {}, token=False)[0], 403)
        code, data = self.post("/api/keypool/balance", {})
        self.assertEqual(code, 200, data)
        self.assertEqual((data["total"], data["failed"], list(data["balances"])), (54.9, 0, [node["id"]]))
        self.assertEqual(len(self.site.calls), 3)
        state = self.state()
        self.assertEqual(state["nodes"][1]["balance"]["total"], 54.9)
        self.assertNotIn("secret", json.dumps(state) + json.dumps(data))
        self.assertEqual(self.post("/api/keypool/balance", {"id": plain["id"]})[0], 400)

    def test_edit_keeps_or_clears_credentials_and_rejects_bad_input(self):
        node = self.add("A", balanceCookie="cookie-secret", balanceUser="user-secret")
        self.post("/api/keypool/balance", {})
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node["id"], "name": "A2"}})[0], 200)
        saved = self.module.load()["nodes"][0]
        self.assertEqual((saved["name"], saved["balanceCookie"]), ("A2", "cookie-secret"))
        self.assertIsNotNone(self.state()["nodes"][0]["balance"])
        for bad in ({"balanceCookie": "a b"}, {"balanceUser": "x;y"}, {"balanceCookie": "x" * 4097}, {"balanceUser": "line\nbreak"}):
            self.assertEqual(self.post("/api/keypool/nodes", {"node": dict({"id": node["id"]}, **bad)})[0], 400, bad)
        other = self.add("B")
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": other["id"], "balanceCookie": "only-one"}})[0], 400)
        self.assertNotIn("balanceCookie", self.module.load()["nodes"][1])
        code, data = self.post("/api/keypool/nodes", {"node": {"id": node["id"], "balanceClear": True}})
        self.assertEqual((code, data["node"]["hasBalance"]), (200, False))
        self.assertNotIn("balanceCookie", self.module.load()["nodes"][0])
        self.assertIsNone(self.state()["nodes"][0]["balance"])
        self.assertEqual(self.post("/api/keypool/balance", {})[0], 400)

    def wait_for(self, predicate, timeout=3):
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("后台没有在限定时间内完成")

    def poll(self, auto=True):
        request = urllib.request.Request("http://127.0.0.1:%d/api/keypool/state%s" % (self.port, "?balance=1" if auto else ""),
                                         headers={"Host": "127.0.0.1:%d" % self.port})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_auto_refresh_runs_only_for_the_open_page_at_most_every_interval_and_reuses_slow_changing_data(self):
        node = self.add("A", balanceCookie="cookie-secret", balanceUser="user-secret")
        self.poll(auto=False)
        self.assertEqual(self.site.calls, [])
        self.poll()
        self.wait_for(lambda: self.module.balances.get(node["id"]))
        self.assertEqual(len(self.site.calls), 3)
        self.poll()
        self.poll()
        self.assertEqual(len(self.site.calls), 3)          # 3 秒内不重复查
        self.module.balance_auto_at = 0
        self.site.replies["/api/user/self"] = {"success": True, "data": {"quota": 500000}}
        self.poll()
        self.wait_for(lambda: self.module.balances[node["id"]]["payAsYouGo"] == 1.0)
        self.assertEqual(len(self.site.calls), 4)          # 这一轮只查了按量余额，单位和套餐用缓存
        self.assertEqual(self.poll(auto=False)["nodes"][0]["balance"]["total"], 30.9)
        self.assertEqual(self.post("/api/keypool/balance", {})[0], 200)
        self.assertEqual(len(self.site.calls), 7)          # 手动查询不用缓存

    def test_auto_refresh_backs_off_after_rate_limit_and_keeps_the_last_good_number(self):
        node = self.add("A", balanceCookie="cookie-secret", balanceUser="user-secret")
        self.poll()
        self.wait_for(lambda: self.module.balances.get(node["id"]))
        self.site.replies["/api/user/self"] = b"<html>slow down</html>"
        self.module.balance_auto_at = 0
        self.poll()
        self.wait_for(lambda: not self.module.balances[node["id"]]["ok"])
        shown = self.poll(auto=False)["nodes"][0]["balance"]
        self.assertIn("限流", shown["error"])
        self.assertEqual(shown["last"]["total"], 54.9)
        calls = len(self.site.calls)
        self.assertEqual(calls, 4)                         # 自动刷新被限流时不重试
        self.module.balance_auto_at = 0
        self.poll()
        self.poll()
        self.assertEqual(len(self.site.calls), calls)      # 退避期间不再查这个节点
        self.module.balance_wait[node["id"]] = 0
        self.site.replies["/api/user/self"] = {"success": True, "data": {"quota": 12500000}}
        self.module.balance_auto_at = 0
        self.poll()
        self.wait_for(lambda: self.module.balances[node["id"]]["ok"])
        self.assertNotIn("secret", json.dumps(self.poll(auto=False)))

    def test_node_model_is_saved_cleared_validated_and_shown(self):
        node = self.add("A", model="  amd-model  ")
        self.assertEqual(node["model"], "amd-model")
        self.assertEqual(self.module.load()["nodes"][0]["model"], "amd-model")
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node["id"], "name": "A2"}})[1]["node"]["model"], "amd-model")
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node["id"], "model": "x" * 161}})[0], 400)
        self.assertEqual(self.post("/api/keypool/nodes", {"node": {"id": node["id"], "model": ""}})[1]["node"]["model"], "")
        self.assertNotIn("model", self.module.load()["nodes"][0])
        self.assertEqual(self.state()["nodes"][0]["model"], "")

    def test_one_failing_node_does_not_hide_the_others(self):
        good = self.add("好的", balanceCookie="cookie-secret", balanceUser="user-secret")
        bad = self.add("坏的", baseUrl="http://example.test/v1", balanceCookie="cookie-secret", balanceUser="user-secret")
        code, data = self.post("/api/keypool/balance", {})
        self.assertEqual((code, data["total"], data["failed"]), (200, 54.9, 1))
        self.assertTrue(data["balances"][good["id"]]["ok"])
        self.assertIn("https", data["balances"][bad["id"]]["error"])
        self.assertEqual(self.post("/api/keypool/nodes/delete", {"id": good["id"]})[0], 200)
        self.assertNotIn(good["id"], self.module.balances)


if __name__ == "__main__":
    unittest.main()
