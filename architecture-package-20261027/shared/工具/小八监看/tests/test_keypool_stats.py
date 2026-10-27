"""Key 池用量统计测试：提取 token 数、写读记录、汇总、估算花费、导出。只用临时目录。"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor_core import keypool_proxy as kp  # noqa: E402
from monitor_core import keypool_stats as ks  # noqa: E402
from tests.test_keypool import ADMIN, FakeUpstream, call, free_port, write_config  # noqa: E402


class ExtractTests(unittest.TestCase):
    def test_plain_json_openai_and_anthropic(self):
        self.assertEqual(ks.extract_usage(b'{"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":30,'
                                          b'"prompt_tokens_details":{"cached_tokens":100}}}'),
                         {"in": 120, "out": 30, "cache": 100})
        self.assertEqual(ks.extract_usage(b'{"content":[],"usage":{"input_tokens":7,"output_tokens":9,'
                                          b'"cache_read_input_tokens":3}}'), {"in": 7, "out": 9, "cache": 3})
        self.assertEqual(ks.extract_usage(b'{"usage":{"prompt_tokens":5,"completion_tokens":1,"prompt_cache_hit_tokens":4}}'),
                         {"in": 5, "out": 1, "cache": 4})

    def test_streams(self):
        openai = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                  b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":22}}\n\ndata: [DONE]\n\n')
        self.assertEqual(ks.extract_usage(openai), {"in": 11, "out": 22, "cache": None})
        anthropic = (b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":40,"output_tokens":1}}}\n\n'
                     b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":55}}\n\n')
        self.assertEqual(ks.extract_usage(anthropic), {"in": 40, "out": 55, "cache": None})

    def test_missing_or_broken_usage_is_none_not_zero(self):
        for data in (b"", b"not json", b'{"choices":[]}', b'data: {"usage":{"prompt_tok', b'{"usage":{"prompt_tokens":"x"}}',
                     b'{"usage":{"prompt_tokens":true,"completion_tokens":-3}}'):
            self.assertEqual(ks.extract_usage(data), {"in": None, "out": None, "cache": None}, data)

    def test_tap_keeps_head_and_tail_of_a_long_stream(self):
        tap = ks.UsageTap()
        tap.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":9}}}\n\n')
        filler = b'data: {"type":"content_block_delta","delta":{"text":"' + b"x" * 900 + b'"}}\n\n'
        for _ in range(2000):                                   # 约 1.9 MB，超过整段保留的上限
            tap.feed(filler)
        tap.feed(b'data: {"type":"message_delta","usage":{"output_tokens":77}}\n\n')
        self.assertGreater(tap.size, ks.WHOLE_BYTES)
        self.assertLessEqual(len(tap.head) + len(tap.tail), ks.HEAD_BYTES + ks.WHOLE_BYTES)
        self.assertEqual(tap.usage(), {"in": 9, "out": 77, "cache": None})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "usage")
        self.addCleanup(self.tmp.cleanup)
        self.now = time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1))

    def row(self, ago, **extra):
        base = {"t": self.now - ago, "node": "a", "name": "A", "route": None, "model": "m1", "asked": "m1",
                "path": "/v1/chat/completions", "stream": True, "status": 200, "ok": True, "attempt": 1,
                "ms": 100, "ttfb": 300, "total": 1000, "in": 10, "out": 5, "cache": 2}
        base.update(extra)
        ks.append_usage(self.dir, base)

    def test_rows_roundtrip_across_days_with_private_files(self):
        self.row(10)
        self.row(86400 + 10, model="m2")
        self.row(3 * 86400)
        rows = ks.load(self.dir, self.now - 2 * 86400, self.now)
        self.assertEqual([r["model"] for r in rows], ["m2", "m1"])
        self.assertEqual(set(rows[0]), set(ks.FIELDS))
        self.assertEqual(len(os.listdir(self.dir)), 3)
        for name in os.listdir(self.dir):
            self.assertEqual(os.stat(os.path.join(self.dir, name)).st_mode & 0o077, 0)
        with open(ks.day_file(self.dir, self.now), "a", encoding="utf-8") as handle:
            handle.write("broken line\n[1,2]\n")                # 坏行跳过，不影响别的
        self.assertEqual(len(ks.load(self.dir, self.now - 100, self.now)), 1)

    def test_summary_counts_tokens_latency_shares_and_trend(self):
        self.row(30)
        self.row(40, node="b", name="B", model="m2", **{"in": 100, "out": 50, "cache": 0, "total": 3000, "ttfb": 500})
        self.row(50, ok=False, status=429, attempt=1, **{"in": None, "out": None, "cache": None, "total": 80, "ttfb": None})
        self.row(49, attempt=2, route="pay", **{"in": None, "out": None, "cache": None})   # 同一次请求换线路后成功，但上游没给用量
        self.row(2 * 3600 + 5)
        rows = ks.load(self.dir, self.now - 86400, self.now)
        out = ks.summarize(rows, self.now - 86400, self.now, 3600)
        self.assertEqual((out["requests"], out["calls"], out["succeeded"], out["fail"]), (5, 4, 4, 1))
        self.assertEqual((out["tokensIn"], out["tokensOut"], out["tokensCache"], out["tokenCounted"]), (120, 60, 4, 3))
        self.assertEqual((out["avgMs"], out["avgTtfbMs"]), (1500, 350))
        self.assertEqual([(g["label"], g["requests"], g["tokens"]) for g in out["byNode"]], [("A", 4, 30), ("B", 1, 150)])
        self.assertEqual(out["byModel"][0]["key"], "m1")
        self.assertEqual([g["key"] for g in out["byRoute"]], ["pay"])
        self.assertEqual(len(out["trend"]), 25)
        self.assertEqual(sum(b["requests"] for b in out["trend"]), 5)
        last = out["trend"][-2]            # 现在是 12:00 整，这几条落在 11 点那一格
        self.assertEqual((last["requests"], last["fail"], last["in"], last["out"]), (4, 1, 110, 55))
        self.assertEqual(time.localtime(last["t"]).tm_min, 0)                # 对齐到本机整点

    def test_filters(self):
        self.row(10)
        self.row(20, node="b", name="B", model="m2", ok=False, status=500)
        rows = ks.load(self.dir, self.now - 100, self.now)
        pick = lambda **f: [r["node"] for r in rows if ks.matches(r, f)]   # noqa: E731
        self.assertEqual(pick(node="b"), ["b"])
        self.assertEqual(pick(model="m1"), ["a"])
        self.assertEqual(pick(result="fail"), ["b"])
        self.assertEqual(pick(result="ok", node="b"), [])
        self.assertEqual(len(pick()), 2)

    def test_spend_adds_only_the_drops(self):
        for ago, node, pay, plan in ((5000, "a", 100.0, 30.0),      # 范围之前：只当起点
                                     (900, "a", 99.5, 29.0), (800, "a", 199.5, 29.0),   # 充值 100：不算
                                     (700, "a", 199.0, 28.5), (600, "b", 50.0, None), (500, "b", 49.75, None)):
            ks.append_balance(self.dir, node, pay, plan, self.now - ago)
        out = ks.spend(self.dir, self.now - 1000, self.now)
        self.assertEqual((out["pay"], out["plan"], out["total"]), (1.25, 1.5, 2.75))
        self.assertEqual(out["nodes"], {"a": 2.5, "b": 0.25})
        self.assertEqual(ks.spend(self.dir, self.now - 100, self.now)["total"], 0)
        self.assertEqual(ks.spend(os.path.join(self.dir, "none"), 0)["total"], 0)

    def test_csv_and_prune(self):
        self.row(10, route="pay")
        text = ks.to_csv(ks.load(self.dir, self.now - 100, self.now))
        self.assertTrue(text.startswith("﻿时间,节点,线路"))
        self.assertIn(",A,余额,m1,m1,/v1/chat/completions,是,200,是,1,100,300,1000,10,5,2", text)
        self.row(100 * 86400)
        self.assertEqual(ks.prune(self.dir, 90, self.now), 1)
        self.assertEqual(len([n for n in os.listdir(self.dir) if n.startswith("usage-")]), 1)


class ProxyRecordsTests(unittest.TestCase):
    """代理真的把每次尝试写进用量目录，带上从回复里读到的 token 数；内容和 Key 不落盘。"""

    def test_requests_are_recorded_with_tokens_and_without_secrets(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        good, bad = FakeUpstream(), FakeUpstream("429")
        self.addCleanup(good.close)
        self.addCleanup(bad.close)
        config, usage = Path(tmp.name) / "keypool.json", os.path.join(tmp.name, "usage")
        write_config(config, [{"id": "bad", "name": "坏的", "baseUrl": bad.url, "key": "key-bad-secret"},
                              {"id": "good", "name": "好的", "baseUrl": good.url, "key": "key-good-secret"}])
        port = free_port()
        server = kp.make_server(str(config), port, ADMIN, usage_dir=usage)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        server.pool.nodes["good"].last_used = 9e12                    # 先分到坏的那个
        body = {"model": "m1", "messages": [{"role": "user", "content": "这是请求内容 secret-prompt"}]}
        self.assertEqual(call(port, body=body)[0], 200)
        rows = ks.load(usage, 0)
        self.assertEqual([(r["name"], r["status"], r["ok"], r["attempt"], r["path"], r["asked"]) for r in rows],
                         [("坏的", 429, False, 1, "/v1/chat/completions", "m1"),
                          ("好的", 200, True, 2, "/v1/chat/completions", "m1")])
        self.assertEqual((rows[1]["in"], rows[1]["out"]), (None, None))   # 假上游没给用量：记为未知，不是 0
        raw = "".join(Path(usage, n).read_text(encoding="utf-8") for n in os.listdir(usage))
        for secret in ("secret-prompt", "key-good-secret", "key-bad-secret"):
            self.assertNotIn(secret, raw)
        request = urllib.request.Request("http://127.0.0.1:%d/_admin/state" % port, headers={"X-KeyPool-Admin": ADMIN})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertIsNone(json.loads(response.read())["usageError"])


if __name__ == "__main__":
    unittest.main()
