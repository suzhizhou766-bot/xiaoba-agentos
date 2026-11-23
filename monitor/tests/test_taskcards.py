"""任务卡页的测试（只用标准库）。运行：/usr/bin/python3 -m unittest discover -s tests -v

全部在临时目录里造卡、台账和回执，不读真实工作区。
"""
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from monitor_core import config as cfg, logs, taskcards  # noqa: E402
from monitor_core.modules.system import SystemModule  # noqa: E402
from monitor_core.modules.taskcards import TaskCardsModule  # noqa: E402
from monitor_core.web import App, Context  # noqa: E402
import taskcard  # noqa: E402


def card_text(number, title, start="现在可发", who="任何 Agent"):
    return "# 小八修复任务卡 %d：%s\n\n开始条件：%s\n适合谁接：%s\n\n## 目标\n做事。\n" % (number, title, start, who)


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cards = self.root / "工作区" / "修复" / "任务卡"
        self.cards.mkdir(parents=True)
        self.ledger = self.root / "工作区" / "任务卡台账.jsonl"
        self.write(43, "v1.6.8节点测试期限", "现在可发")
        self.write(42, "v1.6.7验收", "已完成，不用再发")
        self.write(49, "v1.6.8验收", "卡 43～47 合并后可发")
        (self.cards / "说明.md").write_text("不是卡", encoding="utf-8")

    def write(self, number, title, start):
        path = self.cards / ("卡%d-%s.md" % (number, title))
        path.write_text(card_text(number, title, start), encoding="utf-8")
        return path

    def board(self):
        return taskcards.Board([self.cards], self.ledger, self.root / "工作区", 3)

    def close(self):
        self.tmp.cleanup()


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.board = self.fx.board()

    def tearDown(self):
        self.fx.close()

    def rows(self):
        return {row["id"]: row for row in self.board.snapshot()["cards"]}

    def test_cards_are_parsed_and_state_inferred_without_ledger(self):
        rows = self.rows()
        self.assertEqual(sorted(rows), [42, 43, 49])  # 说明.md 不是卡
        self.assertEqual(rows[43]["state"], "ready")
        self.assertEqual(rows[43]["version"], "v1.6.8")
        self.assertEqual(rows[43]["who"], "任何 Agent")
        self.assertEqual(rows[42]["state"], "history")
        self.assertEqual(rows[49]["state"], "waiting")
        self.assertEqual(rows[43]["state_source"], "inferred")

    def test_ledger_events_drive_state_and_latest_wins(self):
        ledger = self.board.ledger
        ledger.append(43, "issue", to="Codex", by="小克", now=100)
        self.assertEqual(self.rows()[43]["state"], "issued")
        self.assertEqual(self.rows()[43]["to"], "Codex")
        receipt = self.fx.root / "回执.md"
        receipt.write_text("【回执】卡 43", encoding="utf-8")
        record = ledger.append(43, "receipt", path=str(receipt), now=200)
        self.assertEqual(len(record["sha256"]), 64)
        row = self.rows()[43]
        self.assertEqual((row["state"], row["receipt_count"], row["latest_receipt"]), ("receipt", 1, 200))
        ledger.append(43, "returned", note="反例没跑", now=300)
        self.assertEqual(self.rows()[43]["state"], "returned")
        ledger.append(43, "note", note="只是备注", now=350)
        self.assertEqual(self.rows()[43]["state"], "returned")  # 备注不改状态
        ledger.append(43, "verified", now=400)
        ledger.append(43, "done", now=500)
        row = self.rows()[43]
        self.assertEqual(row["state"], "done")
        self.assertEqual([e["event"] for e in row["events"]], ["issue", "receipt", "returned", "note", "verified", "done"])

    def test_auto_found_receipt_newer_than_issue_flags_pending_check(self):
        self.board.ledger.append(43, "issue", to="DSH", now=time.time() - 60)
        found = self.fx.root / "工作区" / "v1.6.8-发版" / "回执-卡43-第一次.md"
        found.parent.mkdir(parents=True)
        found.write_text("回执", encoding="utf-8")
        (found.parent / "回执-卡430-别的.md").write_text("别的卡", encoding="utf-8")
        row = self.rows()[43]
        self.assertEqual((row["state"], row["state_source"]), ("receipt", "found"))
        self.assertEqual([item["name"] for item in row["receipts_found"]], ["回执-卡43-第一次.md"])
        self.assertEqual(row["receipt_count"], 1)

    def test_auto_found_receipt_older_than_verification_does_not_reopen(self):
        found = self.fx.root / "工作区" / "回执-卡43.md"
        found.write_text("回执", encoding="utf-8")
        os.utime(found, (1000, 1000))
        self.board.ledger.append(43, "issue", now=900)
        self.board.ledger.append(43, "verified", now=1100)
        self.assertEqual(self.rows()[43]["state"], "verified")

    def test_unregistered_receipt_without_issue_keeps_inferred_state(self):
        (self.fx.root / "工作区" / "回执-卡42.md").write_text("旧回执", encoding="utf-8")
        row = self.rows()[42]
        self.assertEqual(row["state"], "history")
        self.assertEqual(row["receipt_count"], 1)

    def test_bad_ledger_lines_and_unknown_cards_are_reported_not_fatal(self):
        self.fx.ledger.parent.mkdir(parents=True, exist_ok=True)
        with open(self.fx.ledger, "a", encoding="utf-8") as handle:
            handle.write("不是 JSON\n")
            handle.write(json.dumps({"at": 1, "card": 77, "event": "issue"}) + "\n")
            handle.write(json.dumps({"at": 2, "card": 43, "event": "unknown"}) + "\n")
        data = self.board.snapshot()
        self.assertTrue(data["ok"])
        text = "；".join(data["warnings"])
        self.assertIn("2 行格式不对", text)
        self.assertIn("卡 77", text)

    def test_append_validation(self):
        ledger = self.board.ledger
        with self.assertRaises(ValueError):
            ledger.append(43, "approve")
        with self.assertRaises(ValueError):
            ledger.append(0, "issue")
        with self.assertRaises(ValueError):
            ledger.append(43, "receipt")  # 没有文件也没有摘要
        with self.assertRaises(ValueError):
            ledger.append(43, "receipt", path=str(self.fx.root / "不存在.md"))
        with self.assertRaises(ValueError):
            ledger.append(43, "note", note="长" * 2001)
        self.assertFalse(self.fx.ledger.exists())

    def test_read_text_only_allows_cards_and_known_receipts(self):
        card = self.fx.cards / "卡43-v1.6.8节点测试期限.md"
        self.assertIn("任务卡 43", self.board.read_text(str(card))["text"])
        secret = self.fx.root / "keypool.json"
        secret.write_text("{}", encoding="utf-8")
        for path in (secret, self.fx.cards / "说明.md", self.fx.cards / ".." / ".." / ".." / "keypool.json"):
            with self.assertRaises(PermissionError):
                self.board.read_text(str(path))
        link = self.fx.cards / "卡50-链接.md"
        link.symlink_to(secret)
        with self.assertRaises(PermissionError):
            self.board.read_text(str(link))
        self.assertNotIn(50, self.rows())  # 符号链接不算卡
        receipt = self.fx.root / "外面的回执.md"
        receipt.write_text("回执正文", encoding="utf-8")
        with self.assertRaises(PermissionError):
            self.board.read_text(str(receipt))
        self.board.ledger.append(43, "receipt", path=str(receipt))
        self.assertEqual(self.board.read_text(str(receipt))["text"], "回执正文")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.config = self.fx.root / "monitor.json"
        self.config.write_text(json.dumps({"taskcards": {
            "dirs": [str(self.fx.cards)], "ledger": str(self.fx.ledger),
            "receipt_root": str(self.fx.root / "工作区")}}), encoding="utf-8")

    def tearDown(self):
        self.fx.close()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = taskcard.main(["--config", str(self.config)] + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_issue_receipt_and_list(self):
        code, out, _ = self.run_cli("issue", "43", "--to", "Codex", "--by", "小克")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["record"]["to"], "Codex")
        code, _, _ = self.run_cli("receipt", "43", "--note", "回执贴在对话里")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"卡43\s+回执待核对")

    def test_unknown_card_and_missing_note_are_refused(self):
        code, _, err = self.run_cli("issue", "99")
        self.assertEqual(code, 2)
        self.assertIn("找不到卡 99", err)
        code, _, _ = self.run_cli("note", "43")
        self.assertEqual(code, 2)
        self.assertFalse(self.fx.ledger.exists())


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixture()
        root = cls.fx.root
        (root / "page.html").write_text("<p>页面</p>", encoding="utf-8")
        cls.port = free_port()
        config = cfg.load(root / "none.json", {
            "port": cls.port, "page_file": str(root / "page.html"), "agentos_project": str(root),
            "taskcards": {"dirs": [str(cls.fx.cards)], "ledger": str(cls.fx.ledger),
                          "receipt_root": str(root / "工作区")}})
        cls.ctx = Context(config, logs.setup(None), cls.port)
        cls.app = App(cls.ctx)
        cls.module = TaskCardsModule(cls.ctx)
        for module in (SystemModule(cls.ctx), cls.module):
            cls.app.register(module)
        cls.httpd = cls.app.server()
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.fx.close()

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def test_listing_and_file(self):
        code, data = self.get("/api/taskcards")
        self.assertEqual(code, 200)
        self.assertEqual([row["id"] for row in data["cards"]], [49, 43, 42])
        path = urllib.parse.quote(str(self.fx.cards / "卡43-v1.6.8节点测试期限.md"))
        code, data = self.get("/api/taskcards/file?path=" + path)
        self.assertEqual(code, 200)
        self.assertIn("开始条件", data["text"])

    def test_file_outside_is_forbidden_and_cross_site_is_refused(self):
        code, _ = self.get("/api/taskcards/file?path=" + urllib.parse.quote("/etc/hosts"))
        self.assertEqual(code, 403)
        code, _ = self.get("/api/taskcards/file")
        self.assertEqual(code, 400)
        code, _ = self.get("/api/taskcards", {"Sec-Fetch-Site": "cross-site", "Origin": "http://evil.example"})
        self.assertNotEqual(code, 200)

    def test_page_cannot_write(self):
        request = urllib.request.Request(self.base + "/api/taskcards", data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertIn(caught.exception.code, (404, 405))

    def test_health(self):
        self.assertEqual(self.module.health()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
