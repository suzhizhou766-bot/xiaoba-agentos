"""冻结任务列表与单个恢复的测试（只用标准库）。运行：/usr/bin/python3 -m unittest discover -s tests -v

全部用一个假的命令行入口：不接触真实的小八，不会恢复任何真实任务。
"""
import json
import socket
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
from monitor_core.modules.frozen import FrozenModule  # noqa: E402
from monitor_core.modules.system import SystemModule  # noqa: E402
from monitor_core.web import App, Context  # noqa: E402


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(method, url, headers=None, body=None, timeout=5):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


HASH_A, HASH_B = "a" * 64, "b" * 64
ID_A = "11111111-2222-3333-4444-555555555555"
ID_B = "66666666-7777-8888-9999-000000000000"

FAKE_CLI = """#!/usr/bin/python3
import json, sys, time
plan = json.load(open(%(plan)r, encoding="utf-8"))
open(%(calls)r, "a").write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[3:]
if args[0] == "status":
    limit = int(args[args.index("--limit") + 1])
    out = dict(plan["status"])
    if "tasks" in out:
        out["tasks"] = out["tasks"][:limit]
    print(json.dumps(out)); sys.exit(0)
time.sleep(plan.get("sleep", 0))
if plan.get("garbage"):
    sys.stderr.write(plan["garbage"]); sys.exit(2)
print(json.dumps(plan["resume"])); sys.exit(plan.get("code", 0))
"""


def task(task_id, status="frozen_paused", freeze_hash=HASH_A, failures=None, receipts=1, prompt="整理十月拍摄素材\n第二行不该出现"):
    frozen = {
        "version": 2, "task_id": task_id, "frozen_at": 1791000000.5, "failure_class": "external_upstream",
        "completed_runs": [{"run_id": "r1", "agent": "dsh_review", "output_sha256": "c" * 64}],
        "execution_receipts": [{"name": "dsh_review.json", "sha256": "d" * 64}] * receipts,
        "failures": failures if failures is not None else [
            {"agent": "agy_review", "error": "AGY returned HTTP 503", "failure_class": "external_upstream"}],
    }
    return {"id": task_id, "prompt": prompt, "status": status, "freeze_hash": freeze_hash,
            "freeze_json": json.dumps(frozen), "failure_class": "external_upstream", "resume_generation": 0,
            "updated_at": 1791000001.0, "result": "成功侧的输出正文，不该下发给页面"}


class FrozenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = cls.root = Path(cls.tmp.name)
        (root / "page.html").write_text("<p>页面</p>", encoding="utf-8")
        cls.plan, cls.calls_file, cls.script = root / "plan.json", root / "calls.jsonl", root / "fake-xiaoba"
        cls.script.write_text(FAKE_CLI % {"plan": str(cls.plan), "calls": str(cls.calls_file)}, encoding="utf-8")
        cls.script.chmod(0o700)
        cls.port = free_port()
        config = cfg.load(root / "none.json", {
            "port": cls.port, "page_file": str(root / "page.html"), "agentos_project": str(root),
            "agentos_config": str(root / "agentos.json"), "review": {"cli": str(cls.script)}})
        cls.ctx = Context(config, logs.setup(None), cls.port)
        cls.app = App(cls.ctx)
        cls.frozen = FrozenModule(cls.ctx)
        for module in (SystemModule(cls.ctx), cls.frozen):
            cls.app.register(module)
        cls.httpd = cls.app.server()
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.url = cls.base + "/api/review/frozen/resume"
        cls.page_headers = {"Origin": cls.base, "X-Xiaoba-Token": cls.ctx.page_token, "Content-Type": "application/json"}

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.frozen.cli = self.script

    def arrange(self, tasks, resume=None, code=0, counts=None, health=None, **extra):
        if counts is None:
            counts = {}
            for item in tasks:
                counts[item["status"]] = counts.get(item["status"], 0) + 1
        status = {"summary": {"tasks": counts}, "tasks": tasks,
                  "agent_health": [{"agent_name": k, "status": v} for k, v in (health or {}).items()]}
        self.plan.write_text(json.dumps(dict({"status": status, "resume": resume or {}, "code": code}, **extra)), encoding="utf-8")
        self.calls_file.write_text("", encoding="utf-8")

    def calls(self, command=None):
        rows = [json.loads(line) for line in self.calls_file.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if command is None or row[2] == command]

    def listing(self):
        code, _, body = fetch("GET", self.base + "/api/review/frozen")
        return code, json.loads(body), body

    def post(self, body, headers=None):
        code, _, raw = fetch("POST", self.url, dict(self.page_headers) if headers is None else headers, body, timeout=20)
        return code, json.loads(raw)

    # ---- 列出 ----
    def test_listing_keeps_only_frozen_tasks_and_trims_them(self):
        self.arrange([task(ID_A), task(ID_B, status="resume_ready", freeze_hash=HASH_B, receipts=0),
                      dict(task("done"), status="completed")], health={"agy_review": "healthy"})
        code, data, body = self.listing()
        self.assertEqual((code, data["count"], data["complete"]), (200, 2, True))
        first, legacy = data["items"]
        self.assertEqual((first["id"], first["shortId"], first["freezeHash"]), (ID_A, ID_A[:8], HASH_A))
        self.assertEqual(first["title"], "整理十月拍摄素材 第二行不该出现"[:80])
        self.assertEqual(first["frozenAt"], 1791000000.5)
        self.assertEqual(first["failureClass"], "external_upstream")
        self.assertEqual(first["failures"], [{"agent": "agy_review", "failureClass": "external_upstream",
                                              "error": "AGY returned HTTP 503", "outputBudget": False, "probe": "healthy"}])
        self.assertEqual((first["succeeded"], first["hasReceipt"], first["legacy"]), (["dsh_review"], True, False))
        self.assertEqual((legacy["status"], legacy["legacy"], legacy["hasReceipt"]), ("resume_ready", True, False))
        self.assertNotIn("成功侧的输出正文".encode("utf-8"), body)          # 任务产出与冻结记录原文不下发给页面
        self.assertNotIn(b"freeze_json", body)
        # 用的是与 Worker 相同的那份配置，而且只是读状态
        self.assertEqual(self.calls(), [["--config", str(self.root / "agentos.json"), "status", "--json", "--limit", "50"]])

    def test_listing_is_empty_when_nothing_is_frozen(self):
        self.arrange([dict(task(ID_A), status="completed")])
        self.assertEqual(self.listing()[1], {"ok": True, "items": [], "count": 0, "complete": True})

    def test_listing_marks_output_budget_and_missing_probe_and_unreadable_record(self):
        budget = [{"agent": "dsh_review", "error": "output limit", "failure_class": "output_limit", "output_limit_exhausted": True}]
        self.arrange([task(ID_A, failures=budget), dict(task(ID_B), freeze_json="{broken")])
        items = self.listing()[1]["items"]
        self.assertEqual((items[0]["failures"][0]["outputBudget"], items[0]["failures"][0]["probe"]), (True, "none"))
        self.assertEqual(items[1]["failures"], [])
        self.assertIn("读不出来", items[1]["problem"])
        self.assertEqual(items[1]["frozenAt"], 1791000001.0)

    def test_listing_widens_once_and_says_so_when_still_incomplete(self):
        filler = [dict(task("t%d" % i), status="completed") for i in range(60)]
        self.arrange(filler + [task(ID_A)])                                # 冻结的那条排在最近 50 条之外
        code, data, _ = self.listing()
        self.assertEqual((data["count"], data["complete"]), (1, True))
        self.assertEqual([call[-1] for call in self.calls()], ["50", "1000"])
        self.arrange([task(ID_A)], counts={"frozen_paused": 2})            # 汇总说有 2 条，只读到 1 条
        self.assertEqual(self.listing()[1]["complete"], False)

    def test_listing_failure_is_reported_not_shown_as_empty(self):
        self.frozen.cli = self.root / "no-such-entry"
        code, data, _ = self.listing()
        self.assertEqual((code, data["ok"]), (502, False))
        self.frozen.cli = self.script
        self.plan.write_text(json.dumps({"status": {"database_schema": {"ok": False}}}), encoding="utf-8")
        self.calls_file.write_text("", encoding="utf-8")
        code, data, _ = self.listing()
        self.assertEqual(code, 502)
        self.assertIn("迁移", data["error"])

    # ---- 恢复：门禁 ----
    def test_resume_is_refused_unless_it_comes_from_the_local_page(self):
        self.arrange([task(ID_A)], resume={"status": "retry"})
        ask = {"taskId": ID_A, "freezeHash": HASH_A}
        json_only = {"Content-Type": "application/json"}
        self.assertEqual(self.post(ask, dict(json_only, Origin=self.base))[0], 403)                      # 没有页面令牌
        self.assertEqual(self.post(ask, dict(self.page_headers, Origin="http://evil.example"))[0], 403)   # 别的网站发起
        self.assertEqual(self.post(ask, dict(self.page_headers, **{"Sec-Fetch-Site": "cross-site"}))[0], 403)
        self.assertEqual(self.post(ask, dict(self.page_headers, Host="evil.example:%d" % self.port))[0], 403)  # 不是本机地址
        self.assertEqual(self.post(ask, dict(self.page_headers, **{"Content-Type": "text/plain"}))[0], 403)
        self.assertEqual(fetch("GET", self.url)[0], 404)                                                  # 只接受 POST
        self.assertEqual(fetch("GET", self.base + "/api/review/frozen", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.calls(), [])                         # 被门禁拦下的请求不会碰命令行

    # ---- 恢复：哈希与单个 ----
    def test_resume_needs_the_hash_shown_on_the_page(self):
        self.arrange([task(ID_A)], resume={"status": "retry"})
        for body in ({"taskId": ID_A}, {"taskId": ID_A, "freezeHash": ""}, {"taskId": ID_A, "freezeHash": "abc"},
                     {"taskId": ID_A, "freezeHash": HASH_A.upper()}, {"freezeHash": HASH_A}, {"taskId": "", "freezeHash": HASH_A}):
            self.assertEqual(self.post(body)[0], 400, body)
        self.assertEqual(self.calls(), [])                         # 请求本身不合格时连清单都不读
        code, data = self.post({"taskId": ID_A, "freezeHash": HASH_B})          # 哈希与此刻的记录对不上
        self.assertEqual((code, data["kind"]), (409, "hash_mismatch"))
        code, data = self.post({"taskId": ID_B, "freezeHash": HASH_A})          # 不在冻结清单里
        self.assertEqual((code, data["kind"]), (409, "not_frozen"))
        self.assertEqual(self.calls("task-resume"), [])

    def test_resume_takes_one_task_never_a_batch(self):
        self.arrange([task(ID_A), task(ID_B, freeze_hash=HASH_B)], resume={"status": "retry"})
        for body in ({"taskId": [ID_A, ID_B], "freezeHash": HASH_A},
                     {"items": [{"taskId": ID_A, "freezeHash": HASH_A}, {"taskId": ID_B, "freezeHash": HASH_B}]},
                     {"taskId": ID_A, "freezeHash": HASH_A, "taskIds": [ID_B]},
                     {"taskId": ID_A, "freezeHash": HASH_A, "tasks": [ID_B]}):
            code, data = self.post(body)
            self.assertEqual(code, 400, body)
            self.assertIn("一次只能恢复一个", data["error"])
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.post({"taskId": ID_A, "freezeHash": HASH_A})[0], 200)
        self.assertEqual(self.calls("task-resume"),                # 只恢复了点的那一个，另一个冻结任务没有被碰
                         [["--config", str(self.root / "agentos.json"), "task-resume", ID_A, "--freeze-hash", HASH_A]])

    def test_second_resume_is_refused_while_one_is_running(self):
        self.arrange([task(ID_A), task(ID_B, freeze_hash=HASH_B)], resume={"status": "retry"}, sleep=1.5)
        first = {}
        thread = threading.Thread(target=lambda: first.update(zip(("code", "data"), self.post({"taskId": ID_A, "freezeHash": HASH_A}))))
        thread.start()
        deadline = time.time() + 10
        while not self.calls("task-resume") and time.time() < deadline:
            time.sleep(0.05)
        code, data = self.post({"taskId": ID_B, "freezeHash": HASH_B})
        thread.join(15)
        self.assertEqual((code, data["kind"]), (409, "busy"))
        self.assertEqual(first["code"], 200)
        self.assertEqual(len(self.calls("task-resume")), 1)

    # ---- 恢复：命令行的结果原样带回 ----
    def test_success_is_passed_through_with_split_warning(self):
        result = {"task_id": ID_A, "status": "retry", "freeze_hash": HASH_A, "resume_generation": 2,
                  "resumed_from": "frozen_paused", "output_budget_exhausted": True, "warning": "建议先拆小任务。涉及 Agent：dsh_review"}
        self.arrange([task(ID_A)], resume=result)
        code, data = self.post({"taskId": ID_A, "freezeHash": HASH_A})
        self.assertEqual((code, data["ok"], data["status"]), (200, True, "retry"))
        self.assertEqual((data["resumeGeneration"], data["resumedFrom"], data["outputBudgetExhausted"]), (2, "frozen_paused", True))
        self.assertIn("拆小任务", data["warning"])
        self.assertEqual(data["result"], result)
        self.assertEqual(len(self.calls("task-resume")), 1)

    def test_refusals_are_passed_through_and_never_retried(self):
        cases = (
            ("failed Agent has no current healthy probe; run session-start first", "needs_probe", "上班"),
            ("frozen execution receipt is missing", "evidence_gap", "执行回执"),
            ("frozen execution receipt hash mismatch", "evidence_gap", "执行回执"),
            ("execution snapshot changed while task was frozen", "evidence_gap", "执行快照"),
            ("frozen Agent output hash mismatch", "evidence_gap", "输出"),
            ("frozen Provider attempt event hash mismatch", "evidence_gap", "事件记录"),
            ("frozen task record hash mismatch", "evidence_gap", "冻结记录"),
            ("freeze hash mismatch", "hash_mismatch", "冻结哈希"),
            ("task is not frozen", "not_frozen", "不在冻结状态"),
            ("'%s'" % ID_A, "not_found", "找不到"),
            ("something new the monitor has never seen", "blocked", "something new the monitor has never seen"),
        )
        for error, kind, words in cases:
            result = {"status": "blocked", "error": error, "task_id": ID_A}
            self.arrange([task(ID_A)], resume=result, code=1)
            code, data = self.post({"taskId": ID_A, "freezeHash": HASH_A})
            self.assertEqual((code, data["ok"], data["kind"]), (409, False, kind), error)
            self.assertIn(words, data["error"])
            self.assertEqual(data["result"], result)               # 命令行说了什么，原样带给页面
            self.assertEqual(len(self.calls("task-resume")), 1)    # 被拒绝后不自动重试

    def test_unreadable_cli_output_is_an_error_not_a_success(self):
        self.arrange([task(ID_A)], garbage='{"error": "release_unverified"}')
        code, data = self.post({"taskId": ID_A, "freezeHash": HASH_A})
        self.assertEqual((code, data["ok"], data["kind"]), (409, False, "blocked"))
        self.assertIn("release_unverified", data["error"])
        self.arrange([task(ID_A)], resume={"status": "blocked", "error": "task is not frozen"}, code=0)
        self.assertEqual(self.post({"taskId": ID_A, "freezeHash": HASH_A})[0], 409)   # 退出码是 0 但没有恢复，也不算成功
        self.arrange([task(ID_A)], resume={"status": "retry"}, code=1)
        self.assertEqual(self.post({"taskId": ID_A, "freezeHash": HASH_A})[0], 409)


if __name__ == "__main__":
    unittest.main()
