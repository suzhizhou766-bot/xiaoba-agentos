"""Card 36 leftovers from card 35: node timeout, step logs, tmux cancel, no false offline."""
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.resource_tmux import TmuxChannel


class NodeTimeoutAndLogTests(fixtures.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir()
        self.registry = BackendRegistry(self.store, lambda s: json_stdio_factory(s, self.workspaces))
        self.addCleanup(self.registry.close)
        self.pool = AgentPool(self.store, self.registry)
        self.entry = self.root / "qoder"
        self.entry.write_text("#!" + sys.executable + "\n" +
            "import json,os,re,sys\nfrom pathlib import Path\n"
            "stdin=sys.stdin.read()\n"
            "path=json.loads(stdin.split('XIAOBA_TASK_CARD=',1)[1].split('\\n',1)[0])\n"
            "card=Path(path).read_text()\n"
            "logpath=Path(re.search(r'^工作回执文件: (.+)$',card,re.M)[1])\n"
            "taskid=re.search(r'^任务ID: (.+)$',card,re.M)[1]\n"
            "step=re.search(r'^步骤码: (.+)$',card,re.M)[1]\n"
            "nonce=re.search(r'^本轮标识: (.+)$',card,re.M)[1]\n"
            "ctx=json.loads(card.split('## 材料\\n',1)[1].split('\\n\\n## 范围与回执',1)[0])\n"
            "body=ctx.get('challenge') or ('body-for-'+step)\n"
            "if not logpath.exists():\n"
            "    header=card.split('先写以下文件头：\\n\\n',1)[1].split('\\n文件已存在时',1)[0]\n"
            "    logpath.write_text(header)\n"
            "with logpath.open('a') as out:\n"
            "    out.write('\\n<!-- task:'+nonce+' -->\\n任务ID: '+taskid+\n"
            "              '\\n步骤码: '+step+'\\n状态: completed\\n---\\n'+body+\n"
            "              '\\n<!-- done:'+nonce+' -->\\n')\n"
            "print(json.dumps({'event':'result','result':{'status':'SUCCESS'}}))\n")
        self.entry.chmod(0o700)

    def _ready(self, node_id="step-node"):
        source = self.pool.add({
            "node_id": node_id, "name": node_id, "agent": "qoder", "agent_type": "qoder",
            "kind": "review", "interface": "cli", "entry": str(self.entry), "model": "alpha",
        })
        self.pool.change(node_id, source["node"]["revision"], "toggle", True)
        node = next(n for n in self.pool.state()["nodes"] if n["node_id"] == node_id)
        result = self.pool.runtime.test_run(node_id, node["revision"], "probe-" + node_id)
        self.assertEqual("passed", result["status"], result)
        return node_id

    def test_no_progress_seconds_comes_from_the_node_record(self):
        source = self.pool.add({
            "node_id": "timeout-node", "name": "timeout", "agent": "qoder", "agent_type": "qoder",
            "kind": "review", "interface": "cli", "entry": str(self.entry), "model": "alpha",
        })
        source["node"]["no_progress_seconds"] = 7
        adapter = json_stdio_factory(source, self.workspaces)
        self.assertEqual(adapter.no_progress_seconds, 7)

    def test_two_steps_append_to_the_same_worklog(self):
        node = self._ready()
        revision = next(n["revision"] for n in self.pool.state()["nodes"] if n["node_id"] == node)
        first = self.pool.runtime.execute_work(
            node, revision, "TASK-log", "STEP-01",
            "TEST authorized work in this owned directory", {"note": "one"})
        second = self.pool.runtime.execute_work(
            node, revision, "TASK-log", "STEP-02",
            "TEST authorized work in this owned directory", {"note": "two"})
        self.assertEqual("completed", first["status"], first)
        self.assertEqual("completed", second["status"], second)
        relative = first["execution_evidence"]["worklog"]["path"]
        self.assertEqual(relative, second["execution_evidence"]["worklog"]["path"])
        path = self.workspaces / node / relative
        text = path.read_text()
        self.assertIn("STEP-01", text)
        self.assertIn("STEP-02", text)
        self.assertIn("body-for-STEP-01", text)
        self.assertIn("body-for-STEP-02", text)


class TmuxCancelAndOfflineTests(fixtures.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.pool = AgentPool(self.store, BackendRegistry(self.store))
        self.log = self.root / "argv.jsonl"
        self.mode = self.root / "mode"
        self.mode.write_text("ok")
        self.pane = self.root / "pane"
        self.pane.write_text("agy\t" + str(self.root))
        self.fake = self.root / "fake-tmux"
        self.fake.write_text("#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
            "p=Path(" + repr(str(self.log)) + ")\n"
            "with p.open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "if sys.argv[1]=='display-message':\n"
            "    sys.stdout.write(Path(" + repr(str(self.pane)) + ").read_text());sys.exit(0)\n"
            "sys.exit(0)\n")
        self.fake.chmod(0o700)
        self.pool.tmux = TmuxChannel(self.store, str(self.fake))
        source = self.pool.add({
            "node_id": "TEST-tmux", "name": "TEST attended", "agent": "TEST CLI",
            "kind": "collaborator", "interface": "tmux", "entry": "/TEST/bin/agent",
            "model": "TEST-model", "tmux_session": "TEST_session-1",
            "working_directory": str(self.root),
        })
        self.pool.change("TEST-tmux", 1, "toggle", True)

    def _job(self):
        with self.store.native.read_only() as db:
            return next(iter(self.pool.tmux.jobs(db).values()))

    def test_remove_cancels_waiting_tmux_job(self):
        result = self.pool.runtime.execute_work(
            node_id="TEST-tmux", expected_revision=2, task_id="TEST-task",
            step_code="step1", instruction="只处理夹具材料并写文件回执。",
            materials={"fixture": True})
        self.assertEqual("waiting_user_agent", result["status"])
        self.assertEqual("waiting_user_agent", self._job()["status"])
        self.pool.change("TEST-tmux", 2, "remove")
        self.assertEqual("cancelled", self._job()["status"])

    def test_tmux_waiting_is_not_judged_offline(self):
        self.pool.runtime.execute_work(
            node_id="TEST-tmux", expected_revision=2, task_id="TEST-task",
            step_code="step1", instruction="只处理夹具材料并写文件回执。",
            materials={"fixture": True})
        with patch("time.time", return_value=time.time() + 10_000):
            self.pool.tmux.collect()
        self.assertEqual("waiting_user_agent", self._job()["status"])


if __name__ == "__main__":
    unittest.main()
