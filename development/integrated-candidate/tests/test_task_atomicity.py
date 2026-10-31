import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from xiaoba_agentos.store import EventStore


class TaskAtomicityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "agentos.sqlite3"
        self.store = EventStore(self.path)
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def _event_count(self, task_id, event_type):
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type=?",
                (task_id, event_type),
            ).fetchone()[0]

    def test_complete_missing_task_does_not_append_an_event(self):
        with self.assertRaises(KeyError):
            self.store.complete("missing-task", "result")

        self.assertEqual(0, self._event_count("missing-task", "task.completed"))
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_terminal_task_rejects_a_stale_retry_snapshot_without_changing_result(self):
        task_id = self.store.submit("settle once", 3)
        leased = self.store.lease_next(60)
        self.assertEqual(task_id, leased["id"])
        stale = dict(leased)
        self.store.complete(task_id, "original result")

        with self.assertRaises(ValueError):
            self.store.retry_or_fail(stale, "late worker result", 0)

        current = self.store.get_task(task_id)
        self.assertEqual("completed", current["status"])
        self.assertEqual("original result", current["result"])
        self.assertEqual(0, self._event_count(task_id, "task.retry"))
        self.assertEqual(0, self._event_count(task_id, "task.failed"))
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_retry_uses_attempts_and_limit_from_the_locked_database_row(self):
        task_id = self.store.submit("retry from current row", 3)
        leased = self.store.lease_next(60)
        forged_snapshot = dict(leased)
        forged_snapshot.update(attempts=99, max_attempts=99)

        self.assertEqual("retry", self.store.retry_or_fail(forged_snapshot, "temporary", 1))
        current = self.store.get_task(task_id)
        self.assertEqual("retry", current["status"])
        self.assertEqual(1, current["attempts"])
        self.assertGreaterEqual(current["run_after"], current["updated_at"])

    def test_retry_rejects_an_expired_current_lease(self):
        task_id = self.store.submit("expired lease", 1)
        leased = self.store.lease_next(-1)

        with self.assertRaises(ValueError):
            self.store.retry_or_fail(leased, "late result", 0)

        self.assertEqual("running", self.store.get_task(task_id)["status"])
        self.assertEqual(0, self._event_count(task_id, "task.retry"))

    def test_eight_same_key_submissions_return_one_task(self):
        results = []
        errors = []
        lock = threading.Lock()

        def submit_once():
            try:
                task_id = self.store.submit(
                    "same prompt",
                    2,
                    snapshot={"prompt": "same prompt", "input": {"n": 1}},
                    idempotency_key="same-key",
                )
                with lock:
                    results.append(task_id)
            except Exception as exc:  # pragma: no cover - assertion reports it
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=submit_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            self.assertFalse(thread.is_alive(), "submission thread did not finish")

        self.assertEqual([], errors)
        self.assertEqual(8, len(results))
        self.assertEqual(1, len(set(results)))
        with self.store.read_only() as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            self.assertEqual(
                1,
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='task.submitted'"
                ).fetchone()[0],
            )
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_lease_clock_is_read_after_waiting_for_the_writer_lock(self):
        task_id = self.store.submit("lock race", 1)
        holder = sqlite3.connect(str(self.path), timeout=30)
        holder.execute("BEGIN IMMEDIATE")
        outcome = {}

        def lease():
            try:
                outcome["task"] = self.store.lease_next(0.2)
            except Exception as exc:  # pragma: no cover - assertion reports it
                outcome["error"] = exc

        worker = threading.Thread(target=lease)
        worker.start()
        time.sleep(0.35)
        holder.rollback()
        holder.close()
        worker.join(30)
        self.assertFalse(worker.is_alive(), "lease thread did not finish")
        self.assertNotIn("error", outcome)
        self.assertEqual(task_id, outcome["task"]["id"])
        self.assertGreater(outcome["task"]["lease_until"], time.time())

    def test_normal_lease_retry_fail_and_complete_flow_remains_intact(self):
        retry_id = self.store.submit("temporary failure", 2)
        retry_task = self.store.lease_next(60)
        self.assertEqual(retry_id, retry_task["id"])
        self.assertEqual("retry", self.store.retry_or_fail(retry_task, "temporary", 0))
        retry_again = self.store.lease_next(60)
        self.assertEqual(retry_id, retry_again["id"])
        self.assertEqual("failed", self.store.retry_or_fail(retry_again, "permanent", 0))

        fail_id = self.store.submit("direct failure", 1)
        fail_task = self.store.lease_next(60)
        self.assertEqual(fail_id, fail_task["id"])
        self.assertEqual("failed", self.store.fail(fail_id, "invalid input"))

        complete_id = self.store.submit("success", 1)
        complete_task = self.store.lease_next(60)
        self.assertEqual(complete_id, complete_task["id"])
        self.store.complete(complete_id, "done")
        self.assertEqual("completed", self.store.get_task(complete_id)["status"])
        self.assertTrue(self.store.verify_event_chain()["ok"])


if __name__ == "__main__":
    unittest.main()
