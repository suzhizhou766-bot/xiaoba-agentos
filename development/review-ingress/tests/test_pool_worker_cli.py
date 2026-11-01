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
    meta += ("审查结论: PASS\n证据引用: TEST fake cli receipt\n审查类型: new_experience\n"
             "评分: 8\n适用条件: 仅测试夹具\n重复经验: \n")
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
