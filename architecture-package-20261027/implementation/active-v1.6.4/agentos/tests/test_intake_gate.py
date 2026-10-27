import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _config, _raw_config, _run_cli
from xiaoba_agentos.bridge import dispatch
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.health import execution_blockers
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore


def _task_count(store: EventStore) -> int:
    with store.read_only() as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


def _no_skills(module):
    stub = patch("xiaoba_agentos." + module + ".SkillRegistry")
    registry = stub.start()
    registry.return_value.select_context.return_value = {"selected": [], "context": "", "context_sha256": "test"}
    return stub


class IntakeGateTest(unittest.TestCase):
    ARGS = ["intake", "请多后台分析", "--explicit-agentos", "--requires-action", "--submit"]

    def setUp(self):
        for module in ("cli", "bridge"):
            self.addCleanup(_no_skills(module).stop)
        # Task snapshots record the expected release; the minimal mock fixture has none.
        version = patch.object(AppConfig, "expected_release_version", "test")
        version.start()
        self.addCleanup(version.stop)

    def test_blocked_backend_is_not_queued_silently(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            with patch("xiaoba_agentos.cli.execution_blockers", return_value=["agy_review: 桌面未运行"]):
                code, payload = _run_cli(["--config", str(config.path), *self.ARGS, "--message-id", "m1"])
            self.assertEqual(1, code)
            self.assertEqual("blocked_local_health", payload["status"])
            self.assertEqual(["agy_review: 桌面未运行"], payload["blockers"])
            self.assertNotIn("task_id", payload)
            self.assertEqual(0, _task_count(store))

    def test_explicit_flag_queues_and_says_it_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            with patch("xiaoba_agentos.cli.execution_blockers", return_value=["agy_review: 桌面未运行"]):
                code, payload = _run_cli(
                    ["--config", str(config.path), *self.ARGS, "--message-id", "m2", "--queue-when-blocked"])
            self.assertEqual(0, code)
            self.assertEqual("pending", payload["status"])
            self.assertTrue(payload["execution_blocked"])
            self.assertEqual(1, _task_count(store))

    def test_healthy_backend_queues_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            code, payload = _run_cli(["--config", str(config.path), *self.ARGS, "--message-id", "m3"])
            self.assertEqual(0, code)
            self.assertEqual("pending", payload["status"])
            self.assertNotIn("execution_blocked", payload)

    def test_blockers_mirror_the_worker_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            self.assertEqual([], execution_blockers(config, store))
            store.record_agent_health("a", "degraded", error="probe failed")
            blockers = execution_blockers(config, store)
            self.assertTrue(any(item.startswith("a: ") for item in blockers), blockers)
            # The worker refuses the same state, so intake and worker agree.
            store.submit("任务", 2)
            self.assertEqual("degraded_health", Orchestrator(config, store).run_once()["status"])

            raw = _raw_config(root)
            for agent in raw["agents"]:
                agent["enabled"] = False
            disabled = AppConfig(root / "disabled.json", raw)
            self.assertEqual(["没有启用任何 Agent"], execution_blockers(disabled, store))

    def test_bridge_intake_applies_the_same_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            request = {"command": "intake", "prompt": "请多后台分析", "submit": True,
                       "requires_action": True, "explicit_agentos": True, "force_route": "agentos_multi"}
            with patch("xiaoba_agentos.bridge.execution_blockers", return_value=["dsh_review: 不可用"]):
                blocked = dispatch(dict(request, message_id="b1"), store, config)
                self.assertEqual(0, _task_count(store))
                queued = dispatch(dict(request, message_id="b2", queue_when_blocked=True), store, config)
            if blocked["ok"]:
                self.assertNotEqual("pending", blocked["result"]["status"])  # routed elsewhere, nothing queued
            else:
                self.assertIn("blocked_local_health", blocked["error"])
                self.assertTrue(queued["ok"], queued)
                self.assertTrue(queued["result"]["execution_blocked"])


if __name__ == "__main__":
    unittest.main()
