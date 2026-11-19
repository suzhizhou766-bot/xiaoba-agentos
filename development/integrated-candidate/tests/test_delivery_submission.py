"""Delivery submission response classification without external services."""
import contextlib
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xiaoba_agentos.delivery import submit, user_decision  # noqa: E402


class DeliverySubmissionClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.artifact_path = self.workspace / "result.md"
        self.artifact_path.write_text("Verified delivery result", encoding="utf-8")
        self.artifact = {
            "path": str(self.artifact_path),
            "sha256": hashlib.sha256(self.artifact_path.read_bytes()).hexdigest(),
            "version": "v1",
        }
        self.real_task = {
            "id": "task-1", "workspace": str(self.workspace),
            "authorization_ref": "local-test", "description": "TEST delivery",
        }
        self.expected = {"version": 1, "result_sha256": self.artifact["sha256"]}
        self.request = {
            "version": 1, "decision": "accept", "user_selected": True,
            "expected": self.expected, "message_id": "message-1", "source_ref": "test:source",
            "task_id": self.real_task["id"], "real_task": self.real_task,
            "artifact": self.artifact,
        }
        self.config = SimpleNamespace(runtime_dir=root / "runtime")
        self.store = SimpleNamespace(read_only=lambda: contextlib.nullcontext(object()))
        self.current = {
            "expected": self.expected, "real_task": self.real_task,
            "allowed_decisions": ["accept"],
        }

    def run_submit(self, *, current=None, record_side_effect=None, archive_side_effect=None):
        with (
            patch("xiaoba_agentos.experience_loop._chain"),
            patch("xiaoba_agentos.experience_loop._events", return_value=[]),
            patch("xiaoba_agentos.delivery.detail_tx", return_value=current or self.current),
            patch("xiaoba_agentos.experience_loop.record_delivery", side_effect=record_side_effect) as record,
            patch("xiaoba_agentos.delivery.archive_delivery", side_effect=archive_side_effect) as archive,
        ):
            response = submit(
                self.config, self.store, self.request,
                authority=user_decision(self.request, channel="local_csrf_post", explicit=True),
            )
        return response, record, archive

    def test_stale_frozen_version_is_a_deterministic_rejection(self):
        stale = dict(self.current, expected={"version": 1, "result_sha256": "0" * 64})
        response, record, archive = self.run_submit(current=stale)
        self.assertEqual((False, False), (response["ok"], response["pending"]))
        self.assertEqual("message-1", response["message_id"])
        record.assert_not_called()
        archive.assert_not_called()

    def test_validation_rejection_from_recording_is_not_pending(self):
        response, record, archive = self.run_submit(
            record_side_effect=ValueError("withdrawal is stale"),
        )
        self.assertEqual((False, False), (response["ok"], response["pending"]))
        record.assert_called_once()
        archive.assert_not_called()

    def test_database_failure_during_recording_remains_retryable(self):
        import sqlite3

        response, record, archive = self.run_submit(
            record_side_effect=sqlite3.OperationalError("commit outcome unknown"),
        )
        self.assertEqual((False, True), (response["ok"], response["pending"]))
        self.assertEqual("message-1", response["message_id"])
        record.assert_called_once()
        archive.assert_not_called()

    def test_archive_failure_after_recording_remains_retryable(self):
        receipt = {"event_id": "delivery-event-1", "real_task_id": "task-1", "artifact": self.artifact,
                   "decision": "accept"}
        response, record, archive = self.run_submit(
            record_side_effect=lambda *args, **kwargs: receipt,
            archive_side_effect=OSError("archive write failed"),
        )
        self.assertEqual((False, True), (response["ok"], response["pending"]))
        self.assertEqual("message-1", response["message_id"])
        record.assert_called_once()
        archive.assert_called_once_with(self.config, self.store, receipt)

    def test_success_is_not_reported_pending(self):
        receipt = {"event_id": "delivery-event-1", "real_task_id": "task-1", "artifact": self.artifact,
                   "decision": "accept"}
        response, record, archive = self.run_submit(
            record_side_effect=lambda *args, **kwargs: receipt,
            archive_side_effect=lambda *args, **kwargs: {"path": "archive.md", "sha256": "a" * 64},
        )
        self.assertTrue(response["ok"])
        self.assertNotIn("pending", response)
        record.assert_called_once()
        archive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
