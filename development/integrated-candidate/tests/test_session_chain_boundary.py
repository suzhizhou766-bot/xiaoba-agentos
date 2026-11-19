"""Broken audit chains block dispatch, but not ordinary session bookkeeping."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_card37 import _isolated_config
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore


class SessionChainBoundaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = _isolated_config(self.root, [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.session = self.store.start_session()

    def cli(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("PYTHONOPTIMIZE", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        run = subprocess.run(
            [sys.executable, "-B", "-m", "xiaoba_agentos.cli", "--config", str(self.config.path), *args],
            cwd=self.root, env=env, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        return json.loads(run.stdout)

    def break_chain(self):
        with self.store.connect() as conn:
            conn.execute("UPDATE events SET event_hash=? WHERE sequence=(SELECT MIN(sequence) FROM events)",
                         ("0" * 64,))
        self.assertFalse(self.store.verify_event_chain()["ok"])

    def session_status(self):
        with self.store.read_only() as conn:
            return conn.execute("SELECT status FROM work_sessions WHERE id=?", (self.session["id"],)).fetchone()[0]

    def test_close_cli_keeps_known_work_and_reports_unknown_value_queue(self):
        task = self.store.submit("TEST pending work", 1, session_id=self.session["id"])
        self.break_chain()
        packet = self.cli("session-close", "--session-id", self.session["id"])
        self.assertEqual("closing", packet["session"]["status"])
        self.assertEqual(1, packet["known_unresolved_tasks"])
        self.assertEqual([task], [row["id"] for row in packet["tasks"]])
        self.assertIsNone(packet["pending_value_reviews"])
        self.assertEqual("unverifiable", packet["value_queue_status"]["status"])
        self.assertIn("价值队列无法验证", packet["value_queue_status"]["warning"])
        self.assertEqual([], self.store.close_resolved_sessions())
        self.assertEqual("closing", self.session_status())

    def test_cross_day_and_closing_session_allow_public_direct_intake(self):
        self.break_chain()
        with self.store.connect() as conn:
            conn.execute("UPDATE work_sessions SET local_date='1900-01-01' WHERE id=?", (self.session["id"],))
        new = self.store.start_session()
        self.assertNotEqual(self.session["id"], new["id"])
        self.assertEqual("closing", self.session_status())
        self.assertFalse(new["value_queue_status"]["ok"])
        with self.store.connect() as conn:
            conn.execute("UPDATE work_sessions SET status='closing' WHERE id=?", (new["id"],))
        direct = self.cli("intake", "今天天气如何？")
        self.assertEqual("direct", direct["route"])
        self.assertEqual("routed", direct["status"])
        self.assertFalse(direct["value_queue_status"]["ok"])
        self.assertEqual("closing", self.session_status())

    def test_broken_chain_still_blocks_dispatch_queue_and_value_approval(self):
        task = self.store.submit("TEST queued work", 1, session_id=self.session["id"])
        self.break_chain()
        with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value={"ok": True}):
            result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual("blocked_configuration", result["status"])
        self.assertIn("审计链校验失败", result["error"])
        self.assertEqual("pending", self.store.get_task(task)["status"])
        self.assertEqual([], self.store.task_runs(task))
        with self.assertRaisesRegex(ValueError, "审计事件链损坏"):
            self.store.value_review_queue()
        with self.assertRaisesRegex(ValueError, "审计事件链损坏"):
            self.store.assess_value_candidate(task, "TEST review request")

    def test_healthy_empty_session_closes_and_unscored_session_waits(self):
        packet = self.cli("session-close", "--session-id", self.session["id"])
        self.assertEqual("closed", packet["session"]["status"])
        self.assertEqual(0, packet["pending_value_reviews"])
        self.assertTrue(packet["value_queue_status"]["ok"])
        session = self.store.start_session()
        task = self.store.submit("TEST unscored method", 1, session_id=session["id"], snapshot={
            "experience_policy": {"version": 1, "kind": "method"},
        })
        path = self.root / "candidate.md"
        path.write_text("TEST candidate")
        self.store.complete_with_memory_candidate(
            task, "TEST answer", str(path), hashlib.sha256(path.read_bytes()).hexdigest(), [], expected_attempt=0,
        )
        packet = self.cli("session-close", "--session-id", session["id"])
        self.assertEqual("closing", packet["session"]["status"])
        self.assertEqual(1, packet["pending_value_reviews"])
        self.assertEqual("awaiting_value_review", packet["tasks"][0]["memory_value_status"])
        self.assertTrue(packet["value_queue_status"]["ok"])


if __name__ == "__main__":
    unittest.main()
