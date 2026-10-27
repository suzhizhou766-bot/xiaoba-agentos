import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_orchestrator
from xiaoba_agentos.adapters import AgentResult, TransientAdapterError
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.task_context import freeze_context
from xiaoba_agentos.workflow import work_plan, review_plan


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        agents = [{"name": n, "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1}
                  for n in ("a", "b", "c")]
        self.config = test_orchestrator.OrchestratorTest()._config(self.root, agents, minimum=1, budget=9)
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.calls = []

    def submit(self, plan, context):
        return self.store.submit("fix", 3, snapshot={"prompt": "fix", "workflow": plan,
                                 "task_context": freeze_context(context)})

    def test_work_then_separate_review_with_artifact_drift_gate(self):
        owner = self
        class Adapter:
            def __init__(self, agent): self.agent = agent
            def run(self, prompt, context):
                owner.calls.append((self.agent.name, prompt, context))
                return AgentResult(self.agent.name, "specific patch and validation " + self.agent.name, 1)
        plan = work_plan(self.config, {"a": "storage", "b": "guards", "c": "video"}, "original source")
        work = self.submit(plan, "original source")
        with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=Adapter):
            outcome = Orchestrator(self.config, self.store).run_once()
        self.assertEqual("completed", outcome["status"])
        self.assertEqual(3, len(self.calls))
        self.assertIn("storage", next(item[1] for item in self.calls if item[0] == "a"))
        self.assertIsNone(self.store.get_memory_candidate(work))
        self.assertEqual("agentos_collaboration_work", json.loads(self.store.get_task(work)["provenance_json"])["kind"])
        artifact = self.root / "fixed.ts"
        artifact.write_text("fixed source")
        review = review_plan(self.config, self.store, work, "integrated source and tests", [str(artifact)])
        task = self.submit(review, "integrated source and tests")
        self.calls.clear()
        with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=Adapter):
            outcome = Orchestrator(self.config, self.store).run_once()
        self.assertEqual("completed", outcome["status"])
        self.assertIn("winner", outcome)
        self.assertEqual(3, len(self.calls))
        self.assertEqual(1, len({item[1] for item in self.calls}))
        self.assertEqual(1, len({item[2] for item in self.calls}))
        self.assertNotIn("specific patch", self.calls[0][2])
        drift_task = self.submit(review, "integrated source and tests")
        artifact.write_text("changed after review freeze")
        with patch("xiaoba_agentos.orchestrator.build_adapter") as adapter:
            self.assertEqual("failed", Orchestrator(self.config, self.store).run_once()["status"])
            adapter.assert_not_called()
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_incomplete_roster_and_same_context_rejected(self):
        with self.assertRaises(ValueError):
            work_plan(self.config, {"a": "storage"}, "source")
        plan = work_plan(self.config, {"a": "storage", "b": "guards", "c": "video"}, "source")
        task = self.submit(plan, "source")
        with self.assertRaises(ValueError):
            review_plan(self.config, self.store, task, "integrated", [__file__])
        Orchestrator(self.config, self.store).run_once()
        with self.assertRaises(ValueError):
            review_plan(self.config, self.store, task, "source", [__file__])

    def test_work_partial_retry_preserves_contributions_without_judging(self):
        calls = {name: 0 for name in ("a", "b", "c")}
        class Adapter:
            def __init__(self, agent): self.agent = agent
            def run(self, prompt, context):
                name = self.agent.name
                calls[name] += 1
                if name == "c" and calls[name] == 1:
                    raise TransientAdapterError("temporary")
                return AgentResult(name, "patch " + name, 1)
        task = self.submit(work_plan(self.config, {n: "fix " + n for n in calls}, "source"), "source")
        with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=Adapter):
            self.assertEqual("retry", Orchestrator(self.config, self.store).run_once()["status"])
            self.assertEqual("completed", Orchestrator(self.config, self.store).run_once()["status"])
        self.assertEqual({"a": 1, "b": 1, "c": 2}, calls)
        self.assertIsNone(self.store.get_memory_candidate(task))
