import io
import hashlib
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.bridge import dispatch
from xiaoba_agentos.cli import execute_review, main
from xiaoba_agentos.store import EventStore

from fixtures_review import _config


class UnifiedReviewFlowTest(unittest.TestCase):
    def _staged(self, root: Path):
        config = _config(root)
        store = EventStore(config.database_path)
        store.initialize()
        task_id = store.submit("审核流程", 1)
        staged = root / "runtime" / "memory-outbox" / f"{task_id}.md"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text("# 候选\n\n证据。\n", encoding="utf-8")
        sha = hashlib.sha256(staged.read_bytes()).hexdigest()
        store.complete_with_memory_candidate(task_id, "结果", str(staged), sha, [], expected_attempt=0)
        return config, store, task_id, sha

    def _packet(self, task_id: str, sha: str, stage: str, decision: str) -> dict:
        return {"command": "review_decision", "packet": {
            "candidate_id": task_id, "stage": stage, "decision": decision,
            "evidence_sha256": sha, "user_note": "ok",
        }}

    def test_bridge_final_approval_publishes_like_the_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, store, task_id, sha = self._staged(root)
            delivery = dispatch(self._packet(task_id, sha, "delivery", "approve"), store, config)
            self.assertTrue(delivery["ok"], delivery)
            self.assertEqual("delivery_approved", store.get_memory_candidate(task_id)["status"])

            published = root / "vault" / "AgentOS" / f"{task_id}.md"
            with patch("xiaoba_agentos.review_flow.MemoryBridge") as bridge_class:
                bridge_class.return_value.publish_candidate.return_value = {"path": str(published)}
                final = dispatch(self._packet(task_id, sha, "final", "approve"), store, config)
            self.assertTrue(final["ok"], final)
            bridge_class.return_value.publish_candidate.assert_called_once_with(task_id, sha)
            candidate = store.get_memory_candidate(task_id)
            self.assertEqual("published", candidate["status"])
            self.assertEqual(str(published), candidate["published_path"])
            self.assertEqual("published", final["result"]["review"]["status"])
            self.assertEqual("published", final["result"]["outcome"]["memory_status"])

    def test_bridge_final_retirement_moves_the_published_note(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, store, task_id, sha = self._staged(root)
            dispatch(self._packet(task_id, sha, "delivery", "approve"), store, config)
            published = root / "vault" / "AgentOS" / f"{task_id}.md"
            archive = root / "vault" / "archive" / f"{task_id}.md"
            with patch("xiaoba_agentos.review_flow.MemoryBridge") as bridge_class:
                bridge_class.return_value.publish_candidate.return_value = {"path": str(published)}
                dispatch(self._packet(task_id, sha, "final", "approve"), store, config)
                bridge_class.return_value.retire_candidate.return_value = {"archive_path": str(archive)}
                retired = dispatch(self._packet(task_id, sha, "final", "retire"), store, config)
            self.assertTrue(retired["ok"], retired)
            bridge_class.return_value.retire_candidate.assert_called_once_with(task_id, str(published))
            self.assertEqual("retired", store.get_memory_candidate(task_id)["status"])

    def test_bridge_reports_publication_failure_instead_of_silent_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, store, task_id, sha = self._staged(root)
            dispatch(self._packet(task_id, sha, "delivery", "approve"), store, config)
            with patch("xiaoba_agentos.review_flow.MemoryBridge") as bridge_class:
                bridge_class.return_value.publish_candidate.side_effect = RuntimeError("vault offline")
                final = dispatch(self._packet(task_id, sha, "final", "approve"), store, config)
            self.assertFalse(final["ok"])
            self.assertIn("memory_publication", final["error"])
            self.assertNotEqual("published", store.get_memory_candidate(task_id)["status"])

    def test_bridge_still_rejects_a_stale_evidence_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, store, task_id, _sha = self._staged(root)
            response = dispatch(self._packet(task_id, "f" * 64, "delivery", "approve"), store, config)
            self.assertFalse(response["ok"])
            self.assertEqual("pending_delivery_review", store.get_memory_candidate(task_id)["status"])

    def test_execute_review_guards_against_a_changed_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, store, task_id, _sha = self._staged(root)
            code, payload = execute_review(
                config, store, task_id, "delivery", "approve", "", expected_sha256="e" * 64
            )
            self.assertEqual(1, code)
            self.assertEqual("blocked", payload["status"])
            self.assertEqual("pending_delivery_review", store.get_memory_candidate(task_id)["status"])

    def test_cli_review_command_uses_the_shared_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, store, task_id, _sha = self._staged(root)
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(config.path), "review", task_id,
                             "--stage", "delivery", "--decision", "approve"])
            self.assertEqual(0, code)
            self.assertEqual("delivery_approved", json.loads(output.getvalue())["memory_status"])


if __name__ == "__main__":
    unittest.main()
