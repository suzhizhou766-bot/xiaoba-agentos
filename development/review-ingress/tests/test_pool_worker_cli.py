"""Card 36: Worker must call page-created pool nodes through a fake CLI.

The entry is a real executable. Nodes are created via AgentPool.add (the page
API), with no adapter_type and no template. Tests do not call a real model.
"""
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
CORE = Path(__file__).resolve().parents[2] / "integrated-candidate" / "src"
sys.path[:0] = [str(CORE), str(PACKAGE), str(Path(__file__).resolve().parent)]

from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore
from xiaoba_review.agent_pool import AgentPool, node_state
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.store import ReviewStore


FAKE_CLI = r'''
import json, os, re, sys
from pathlib import Path
root = Path(__file__).resolve().parent
stdin = sys.stdin.read()
calls = root / (Path(__file__).name + ".calls")
with calls.open("a") as out:
    out.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": stdin[:800]}) + "\n")
if (root / (Path(__file__).name + ".no-receipt")).exists():
    sys.exit(0)
if "XIAOBA_TASK_CARD=" not in stdin:
    sys.exit(2)
path = json.loads(stdin.split("XIAOBA_TASK_CARD=", 1)[1].split("\n", 1)[0])
card = Path(path).read_text()
logpath = Path(re.search(r"^工作回执文件: (.+)$", card, re.M)[1])
taskid = re.search(r"^任务ID: (.+)$", card, re.M)[1]
step = re.search(r"^步骤码: (.+)$", card, re.M)[1]
nonce = re.search(r"^本轮标识: (.+)$", card, re.M)[1]
ctx = json.loads(card.split("## 材料\n", 1)[1].split("\n\n## 范围与回执", 1)[0])
body = ctx["challenge"] if "challenge" in ctx else "RECEIPT_BODY_FROM_" + Path(__file__).name
if not logpath.exists():
    header = card.split("先写以下文件头：\n\n", 1)[1].split("\n文件已存在时", 1)[0]
    logpath.write_text(header)
meta = "任务ID: " + taskid + "\n步骤码: " + step + "\n状态: completed\n"
if step == "REVIEW-01":
    options_path = root / "card56-review.json"
    options = json.loads(options_path.read_text()) if options_path.exists() else {}
    meta += ("审查结论: PASS\n证据引用: TEST fake cli receipt\n审查类型: new_experience\n"
             "评分: " + str(options.get("score", 8)) + "\n适用条件: 仅测试夹具\n重复经验: "
             + options.get("duplicate", "") + "\n")
    body += options.get("body_fields", "")
with logpath.open("a") as out:
    out.write("\n<!-- task:" + nonce + " -->\n" + meta + "---\n" + body + "\n<!-- done:" + nonce + " -->\n")
print(json.dumps({"event": "result", "result": {"status": "SUCCESS"}}))
'''


def _config(root, agents):
    template = Path(__file__).resolve().parents[2] / "integrated-candidate" / "config" / "agentos.json"
    raw = json.loads(template.read_text(encoding="utf-8"))
    raw["runtime_dir"] = str(root / "runtime")
    raw["project_dir"] = str(root)
    raw["agents"] = agents
    raw["memory"]["knowlp_enabled"] = False
    raw["memory"]["publish_enabled"] = False
    raw["memory"]["obsidian_vault"] = str(root)
    path = root / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return AppConfig.load(path)


class PoolWorkerCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _config(self.root, [])
        self.config.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.workspaces = self.root / "user-selected-workspaces"
        self.workspaces.mkdir(parents=True, exist_ok=True)
        self.native = EventStore(self.config.database_path)
        self.native.initialize()
        self.review = ReviewStore(self.native, self.config.runtime_dir / "memory-outbox", self.workspaces)
        self.backends = BackendRegistry(
            self.review, resource_factory=lambda source: json_stdio_factory(source, self.workspaces)
        )
        self.addCleanup(self.backends.close)
        self.pool = AgentPool(self.review, self.backends)

    def _fake(self, name):
        path = self.root / name
        path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(FAKE_CLI))
        path.chmod(0o700)
        return path

    def _add_ready(self, node_id, entry, model, agent_type, kind="review"):
        source = self.pool.add({
            "node_id": node_id, "name": node_id, "agent": agent_type, "kind": kind,
            "interface": "cli", "entry": str(entry), "model": model, "agent_type": agent_type,
        })
        self.assertNotIn("adapter_type", source["node"])
        self.assertNotIn("template", source["node"])
        enabled = self.pool.change(node_id, node_state(source)["revision"], "toggle", True)
        result = self.pool.runtime.test_run(node_id, node_state(enabled)["revision"], "TEST-" + node_id)
        self.assertEqual("passed", result["status"], result)
        return node_id

    def _calls(self, name):
        path = self.root / (name + ".calls")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_two_page_nodes_run_once_calls_fake_cli_and_uses_receipt(self):
        north = self._fake("codex")
        south = self._fake("claude")
        self._add_ready("north-node", north, "alpha", "codex")
        self._add_ready("south-node", south, "beta", "claude")
        before = {name: len(self._calls(name)) for name in ("codex", "claude")}
        task_id = self.native.submit("审查走通", 2, snapshot={"prompt": "审查走通", "needs_review": True})
        with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
            result = Orchestrator(self.config, self.native).run_once()
        self.assertEqual("completed", result.get("status"), result)
        self.assertGreater(len(self._calls("codex")), before["codex"])
        self.assertGreater(len(self._calls("claude")), before["claude"])
        self.assertTrue(any("XIAOBA_TASK_CARD" in item.get("stdin", "")
                            for item in self._calls("codex") + self._calls("claude")))
        result_text = self.native.get_task(task_id)["result"] or ""
        self.assertIn("RECEIPT_BODY_FROM_", result_text)
        self.assertNotIn("完成验证", result_text)

    def test_card56_header_only_vote_completes_host_and_scores_one(self):
        import time
        from xiaoba_agentos.cli import _submit_task
        from xiaoba_agentos.host_completion import complete_host
        from xiaoba_agentos.pool_dispatch import review_vote_runs
        self._add_ready("north-node", self._fake("codex"), "alpha", "codex")
        task = _submit_task(self.config, self.native, "TEST card56 header vote", experience_kind="audit",
                            host={"id": "host", "entry": "cli:host", "model": "TEST host"})
        with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
            self.assertEqual("awaiting_host", Orchestrator(self.config, self.native).run_once()["status"])
        receipt = {"receipt_id": "TEST-host", "task_id": task,
                   "snapshot_sha256": self.native.get_task(task)["execution_snapshot_hash"],
                   "reviewer_id": "host", "verdict": "PASS", "findings": "TEST host review",
                   "evidence_refs": ["task:" + task], "review_type": "new_experience", "score": 8,
                   "applicability": "TEST only", "duplicate_of": None,
                   "started_at": time.time() - 1, "finished_at": time.time()}
        complete_host(self.config, self.native, task, receipt)
        with self.native.read_only() as db:
            vote = review_vote_runs(db, task)["north-node"]
            rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM ri_task_assessments")]
            finished = json.loads(db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='agent.finished'", (task,)).fetchone()[0])
        for field in ("审查结论: PASS", "证据引用: TEST fake cli receipt", "审查类型: new_experience",
                      "评分: 8", "适用条件: 仅测试夹具", "重复经验: "):
            self.assertIn(field, vote["output"])
        self.assertIn("---\nRECEIPT_BODY_FROM_codex", vote["output"])
        self.assertNotIn("FINAL_ACCEPTANCE", vote["output"])
        self.assertEqual([1], [r["points"] for r in rows])
        self.assertTrue(rows[0]["receipt_accepted"])
        self.assertEqual(finished["receipt_sha256"], rows[0]["receipt_sha256"])
        self.assertTrue(self.native.verify_event_chain()["ok"])

    def card56_next_value_vote(self, kind, options, host_score=5):
        import time
        from xiaoba_agentos.cli import _submit_task
        (self.root / "card56-review.json").write_text(json.dumps(options))
        task = _submit_task(self.config, self.native, "TEST card56 value " + kind,
                            experience_kind=kind,
                            host={"id": "host", "entry": "cli:host", "model": "TEST host"})
        with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
            self.assertEqual("awaiting_host", Orchestrator(self.config, self.native).run_once()["status"])
        receipt = {"receipt_id": "TEST-host-" + task, "task_id": task,
                   "snapshot_sha256": self.native.get_task(task)["execution_snapshot_hash"],
                   "reviewer_id": "host", "verdict": "PASS", "findings": "TEST host review",
                   "evidence_refs": ["task:" + task], "review_type": "new_experience", "score": host_score,
                   "applicability": "TEST only", "duplicate_of": None,
                   "started_at": time.time() - 1, "finished_at": time.time()}
        return task, receipt

    def test_card56_method_rule_empty_duplicate_does_not_promote_low_value(self):
        from xiaoba_agentos.host_completion import complete_host
        self._add_ready("north-node", self._fake("codex"), "alpha", "codex")
        for kind in ("method", "rule"):
            with self.subTest(kind=kind):
                task, receipt = self.card56_next_value_vote(kind, {"score": 5})
                result = complete_host(self.config, self.native, task, receipt)
                value = result["value_assessment"]
                self.assertEqual("below_threshold", value["reason"])
                self.assertEqual("discard", value["decision"])
                self.assertEqual([], value["duplicate_of"])
                self.assertIsNone(self.native.get_memory_candidate(task))
                self.assertEqual("completed", self.native.get_task(task)["status"])
                with self.native.read_only() as db:
                    output = db.execute("SELECT output FROM agent_runs WHERE task_id=?", (task,)).fetchone()[0]
                self.assertIn("重复经验: \n证据引用:", output)

    def test_card56_method_rule_matching_header_body_fields_settle_normally(self):
        from xiaoba_agentos.host_completion import complete_host
        self._add_ready("north-node", self._fake("codex"), "alpha", "codex")
        for kind in ("method", "rule"):
            for duplicate, body_duplicate, score in (("", "无", 5), ("", "无", 8),
                                                       ("TEST-existing", "TEST-existing", 1)):
                with self.subTest(kind=kind, duplicate=duplicate):
                    task, receipt = self.card56_next_value_vote(kind, {
                        "score": score, "duplicate": duplicate,
                        "body_fields": "\nscore: " + str(float(score)) + "\nduplicate_of: " + body_duplicate}, score)
                    value = complete_host(self.config, self.native, task, receipt)["value_assessment"]
                    self.assertEqual("duplicate" if duplicate else "average" if score >= 6 else "below_threshold", value["reason"])
                    self.assertEqual([duplicate] if duplicate else [], value["duplicate_of"])
                    candidate = self.native.get_memory_candidate(task)
                    if duplicate or score >= 6:
                        self.assertEqual("pending_delivery_review", candidate["status"])
                    else:
                        self.assertIsNone(candidate)

    def test_card56_method_rule_conflicting_score_or_duplicate_rolls_back(self):
        from xiaoba_agentos.host_completion import complete_host
        self._add_ready("north-node", self._fake("codex"), "alpha", "codex")
        for kind in ("method", "rule"):
            for body, field in (("\n评分: 8", "评分"), ("\n重复经验: TEST-other", "重复经验")):
                with self.subTest(kind=kind, field=field):
                    task, receipt = self.card56_next_value_vote(kind, {"score": 5, "body_fields": body})
                    with self.assertRaisesRegex(ValueError, field + ".*自相矛盾"):
                        complete_host(self.config, self.native, task, receipt)
                    self.assertEqual("pending", self.native.get_task(task)["status"])
                    self.assertIsNone(self.native.get_memory_candidate(task))
                    with self.native.read_only() as db:
                        self.assertIsNone(db.execute("SELECT 1 FROM events WHERE task_id=? AND event_type IN ('host.receipt_accepted','memory.value_assessed','task_scoring.assessed')", (task,)).fetchone())

    def test_missing_receipt_does_not_complete(self):
        north = self._fake("codex")
        south = self._fake("claude")
        self._add_ready("north-node", north, "alpha", "codex")
        self._add_ready("south-node", south, "beta", "claude")
        (self.root / "codex.no-receipt").touch()
        (self.root / "claude.no-receipt").touch()
        task_id = self.native.submit("缺回执", 2, snapshot={"prompt": "缺回执", "needs_review": True})
        with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
            result = Orchestrator(self.config, self.native).run_once()
        self.assertNotEqual("completed", result.get("status"), result)
        self.assertNotEqual("completed", self.native.get_task(task_id)["status"])


if __name__ == "__main__":
    unittest.main()
