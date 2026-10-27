from concurrent.futures import ProcessPoolExecutor
import ast
import hashlib
import json
import re
import tempfile
import tokenize
import unittest
import os
import sqlite3
import time
from pathlib import Path

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from fixtures_review import _candidate_count, _config, _event_count, _run_cli
from xiaoba_agentos.cli import main
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore


def _initialize_store(path: str) -> bool:
    store = EventStore(Path(path))
    store.initialize()
    return store.schema_status()["ok"]


class EventStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.tmp.name) / "test.sqlite3")
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def test_submit_lease_complete_and_chain(self):
        task_id = self.store.submit("hello", max_attempts=3)
        task = self.store.lease_next(60)
        self.assertEqual(task_id, task["id"])
        self.store.complete(task_id, "done")
        self.assertEqual("completed", self.store.get_task(task_id)["status"])
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_constraint_hold_withholds_result_until_release(self):
        task_id = self.store.submit("只输出数字", max_attempts=1)
        self.store.hold_constraint_task(
            task_id,
            "1517",
            {"status": "unresolved_partial", "numeral_signal": True},
        )
        self.assertEqual("completed", self.store.get_task(task_id)["status"])
        self.assertIsNone(self.store.get_task(task_id)["result"])
        held = self.store.get_constraint_hold(task_id, include_result=True)
        self.assertEqual("pending", held["status"])
        self.assertEqual("1517", held["result"])

        released = self.store.release_constraint_hold(task_id)
        self.assertEqual("1517", released["result"])
        self.assertEqual("released", self.store.get_constraint_hold(task_id)["status"])
        self.assertEqual("1517", self.store.get_task(task_id)["result"])

    def test_candidate_status_sidecar_preserves_immutable_evidence(self):
        task_id = self.store.submit("source", 1)
        staged = Path(self.tmp.name) / "memory.md"
        staged.write_text("# immutable evidence\n", encoding="utf-8")
        evidence_hash = hashlib.sha256(staged.read_bytes()).hexdigest()
        self.store.stage_memory_candidate(task_id, str(staged), evidence_hash)
        sidecar = Path(str(staged) + ".status.json")
        self.assertEqual("pending_delivery_review", json.loads(sidecar.read_text())["status"])
        before = staged.read_bytes()
        candidate = self.store.review_memory(task_id, "delivery", "reject", "reject", evidence_hash)
        self.assertEqual("delivery_rejected", candidate["status"])
        self.assertEqual(before, staged.read_bytes())
        self.assertEqual("delivery_rejected", json.loads(sidecar.read_text())["status"])

    def test_pending_constraint_hold_keeps_session_open_for_review(self):
        session = self.store.start_session()
        task_id = self.store.submit("只输出数字", 1, session_id=session["id"])
        self.store.hold_constraint_task(task_id, "1517", {"status": "unresolved_partial"})
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE work_sessions SET local_date='2000-01-01' WHERE id=?", (session["id"],)
            )
        next_session = self.store.start_session()
        with self.store.read_only() as connection:
            old_status = connection.execute(
                "SELECT status FROM work_sessions WHERE id=?", (session["id"],)
            ).fetchone()["status"]
        self.assertEqual("closing", old_status)
        self.assertNotEqual(session["id"], next_session["id"])

    def test_expired_lease_recovers(self):
        task_id = self.store.submit("recover", max_attempts=3)
        self.store.lease_next(-1)
        run_id = self.store.start_agent_run(task_id, "agent")
        self.assertEqual(1, self.store.recover_expired())
        self.assertEqual("retry", self.store.get_task(task_id)["status"])
        run = next(item for item in self.store.task_runs(task_id) if item["id"] == run_id)
        self.assertEqual("interrupted", run["status"])
        self.assertIsNotNone(run["finished_at"])

    def _legacy_repair_job(self, job_id, status, lease_until=None):
        """Write a repair row the way the retired auto-repair used to.

        The write API is gone on purpose, so history has to be seeded directly.
        """
        now = time.time()
        with self.store.connect() as connection:
            connection.execute(
                """INSERT INTO repair_jobs(id,failed_agent,repair_agent,status,failure_class,error,
                       attempts,max_attempts,run_after,lease_until,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, "b", "a", status, "internal_headroom", "Headroom unavailable",
                 1, 2, now - 10, lease_until, now - 10, now - 10),
            )

    def _repair_rows(self):
        with self.store.read_only() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM repair_jobs ORDER BY id")]

    def test_legacy_repair_jobs_are_history_not_work_and_task_leases_still_recover(self):
        self._legacy_repair_job("job-pending", "pending")
        self._legacy_repair_job("job-running", "running", lease_until=time.time() - 1)
        seeded = self._repair_rows()
        self.assertFalse(self.store.has_runnable_work())
        self.assertIsNone(self.store.lease_next(60))
        self.assertEqual(0, self.store.recover_expired())

        task_id = self.store.submit("recover", 3)
        self.store.lease_next(-1)
        self.assertTrue(self.store.has_runnable_work())
        self.assertEqual(1, self.store.recover_expired())
        self.assertEqual("retry", self.store.get_task(task_id)["status"])

        self.assertEqual(seeded, self._repair_rows())
        self.assertEqual(
            {"job-pending": "pending", "job-running": "running"},
            {item["id"]: item["status"] for item in self.store.repair_jobs()},
        )
        with self.store.read_only() as connection:
            self.assertEqual(
                EventStore.DATABASE_SCHEMA_VERSION,
                connection.execute("PRAGMA user_version").fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type LIKE 'repair.%'"
                ).fetchone()[0],
            )
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_notification_pending_acknowledged_lifecycle_and_deduplication(self):
        payload = {"repair_job_id": "job"}
        first_id = self.store.create_notification("repair.failed", payload, task_id="task")
        second_id = self.store.create_notification("repair.failed", payload, task_id="task")
        self.assertEqual(first_id, second_id)
        pending = self.store.pending_notifications()
        self.assertEqual(1, len(pending))
        self.assertEqual("pending", pending[0]["status"])
        acknowledged = self.store.pending_notifications(acknowledge=True)
        self.assertEqual(first_id, acknowledged[0]["id"])
        self.assertEqual([], self.store.pending_notifications())

    def test_resume_rejects_tampered_freeze_output_snapshot_and_receipt(self):
        task_id = self.store.submit("recover", 3, snapshot={"evidence": "fixed"})
        self.store.lease_next(60)
        run_id = self.store.start_agent_run(task_id, "a")
        self.store.finish_agent_run(run_id, "completed", "kept output", None, 1)
        receipt_root = Path(self.tmp.name) / "receipts"
        receipt_dir = receipt_root / task_id
        receipt_dir.mkdir(parents=True)
        receipt = receipt_dir / f"{run_id}.json"
        receipt.write_text('{"status":"succeeded"}', encoding="utf-8")
        frozen = self.store.freeze_task(
            task_id,
            failure_class="internal_headroom",
            failures=[{"agent": "b", "error": "down"}],
            receipts=[{
                "agent": "a", "run_id": run_id, "name": receipt.name,
                "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            }],
        )
        self.store.record_agent_health("b", "healthy")
        receipt.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "receipt hash mismatch"):
            self._resume(task_id, frozen["freeze_hash"], receipt_root)
        receipt.write_text('{"status":"succeeded"}', encoding="utf-8")
        with self.store.connect() as connection:
            connection.execute("UPDATE agent_runs SET output='tampered' WHERE id=?", (run_id,))
        with self.assertRaisesRegex(ValueError, "output hash mismatch"):
            self._resume(task_id, frozen["freeze_hash"], receipt_root)

    def _resume(self, task_id, freeze_hash, receipt_root=None, names=("a", "b"), window=86400):
        """Call resume_task the way the CLI does: enabled names plus the window."""
        return self.store.resume_task(
            task_id, freeze_hash, receipt_root,
            enabled_names=list(names), health_max_age_seconds=window,
        )

    def test_resume_rejects_nonfrozen_and_modified_freeze_record(self):
        task_id = self.store.submit("recover", 3, snapshot={"evidence": "fixed"})
        with self.assertRaisesRegex(ValueError, "not frozen"):
            self._resume(task_id, None)
        self.store.lease_next(60)
        frozen = self.store.freeze_task(
            task_id, failure_class="internal_headroom",
            failures=[{"agent": "b", "error": "down"}],
        )
        self.store.record_agent_health("b", "healthy")
        with self.store.connect() as connection:
            record = json.loads(connection.execute(
                "SELECT freeze_json FROM tasks WHERE id=?", (task_id,)
            ).fetchone()[0])
            record["failure_class"] = "tampered"
            connection.execute(
                "UPDATE tasks SET freeze_json=? WHERE id=?",
                (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")), task_id),
            )
        with self.assertRaisesRegex(ValueError, "record hash mismatch"):
            self._resume(task_id, frozen["freeze_hash"])

    def test_resume_rejects_missing_hash_and_modified_evidence_bodies(self):
        for mutation in ("missing_hash", "snapshot", "provider_event", "receipt_root"):
            with self.subTest(mutation=mutation):
                task_id = self.store.submit("recover", 3, snapshot={"evidence": "fixed"})
                self.store.append_event(task_id, "agent.provider_attempt", {"provider": "original"})
                frozen = self.store.freeze_task(
                    task_id, failure_class="internal_headroom",
                    failures=[{"agent": "b", "error": "down"}],
                    receipts=[{"name": "receipt.json", "sha256": "a" * 64}]
                    if mutation == "receipt_root" else [],
                )
                self.store.record_agent_health("b", "healthy")
                with self.store.connect() as connection:
                    if mutation == "snapshot":
                        connection.execute(
                            "UPDATE tasks SET execution_snapshot_json='{}' WHERE id=?", (task_id,)
                        )
                    elif mutation == "provider_event":
                        connection.execute(
                            "UPDATE events SET payload_json='{}' WHERE task_id=? "
                            "AND event_type='agent.provider_attempt'", (task_id,)
                        )
                with self.assertRaises(ValueError):
                    self._resume(
                        task_id, None if mutation == "missing_hash" else frozen["freeze_hash"]
                    )
                self.assertEqual("frozen_paused", self.store.get_task(task_id)["status"])
                self.assertFalse(any(
                    row["event_type"] == "task.resumed"
                    for row in self._task_events(task_id)
                ))

    def _task_events(self, task_id):
        with self.store.read_only() as connection:
            return connection.execute("SELECT * FROM events WHERE task_id=?", (task_id,)).fetchall()

    def test_health_reprobe_never_unfreezes_task(self):
        task_id, frozen = self._freeze_with_failure("b")
        for _round in range(2):
            self.store.record_agent_health("b", "healthy")
        self.assertEqual("frozen_paused", self.store.get_task(task_id)["status"])
        self.assertEqual(frozen["freeze_hash"], self.store.get_task(task_id)["freeze_hash"])
        self.assertEqual([], self.store.pending_notifications())
        self.assertFalse(any(
            row["event_type"] == "task.resumed" for row in self._task_events(task_id)
        ))
        self.assertEqual("frozen_paused", self._resume(task_id, frozen["freeze_hash"])["resumed_from"])

    def _freeze_with_failure(self, agent_name):
        task_id = self.store.submit("recover", 3)
        self.store.lease_next(60)
        frozen = self.store.freeze_task(
            task_id, failure_class="internal_headroom",
            failures=[{"agent": agent_name, "error": "down"}],
        )
        self.store.record_agent_health(agent_name, "healthy")
        return task_id, frozen

    def test_stale_health_record_does_not_authorize_resume(self):
        task_id, frozen = self._freeze_with_failure("b")
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE agent_health SET last_probe_at=? WHERE agent_name='b'",
                (time.time() - 7 * 86400,),
            )
        with self.assertRaisesRegex(ValueError, "no current healthy probe"):
            self._resume(task_id, frozen["freeze_hash"])
        self.assertEqual("frozen_paused", self.store.get_task(task_id)["status"])
        self.assertEqual(
            "retry",
            self._resume(task_id, frozen["freeze_hash"], window=30 * 86400)["status"],
        )

    def test_disabled_agent_health_does_not_authorize_resume(self):
        # Models the retired gpt_codex / claude_code records: still marked
        # healthy, but no longer enabled, so they are not resumable capacity.
        task_id, frozen = self._freeze_with_failure("retired")
        with self.assertRaisesRegex(ValueError, "no current healthy probe"):
            self._resume(task_id, frozen["freeze_hash"], names=["live"])
        self.assertEqual("frozen_paused", self.store.get_task(task_id)["status"])
        self.assertEqual(
            "retry",
            self._resume(task_id, frozen["freeze_hash"], names=["retired"])["status"],
        )

    def test_frozen_memory_reuse_remains_pending_until_task_settlement(self):
        source = self.store.submit("source", 1)
        self.store.complete(source, "source result")
        task_id = self.store.submit("recover", 3)
        self.store.record_memory_reuse(task_id, source, "/vault/source.md")
        self.store.lease_next(60)
        self.store.freeze_task(
            task_id, failure_class="external_upstream",
            failures=[{"agent": "b", "error": "429"}],
        )
        self.assertEqual(1, len(self.store.memory_reuses(task_id, outcome="pending")))
        self.store.decide_reuse_outcomes(task_id, True)
        self.assertEqual(1, len(self.store.memory_reuses(task_id, outcome="validated")))

    def test_runtime_permissions_are_private(self):
        self.assertEqual(0o700, os.stat(self.store.path.parent).st_mode & 0o777)
        self.assertEqual(0o600, os.stat(self.store.path).st_mode & 0o777)

    def test_schema_version_is_recorded(self):
        status = self.store.schema_status()
        self.assertTrue(status["ok"])
        self.assertEqual(EventStore.DATABASE_SCHEMA_VERSION, status["current_version"])

    def test_concurrent_processes_safely_migrate_legacy_database(self):
        legacy_path = Path(self.tmp.name) / "legacy.sqlite3"
        connection = sqlite3.connect(str(legacy_path))
        connection.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, prompt TEXT NOT NULL, status TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL, run_after REAL NOT NULL, lease_until REAL,
                strategy_version INTEGER NOT NULL DEFAULT 1, result TEXT, last_error TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )"""
        )
        connection.commit()
        connection.close()

        with ProcessPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(_initialize_store, [str(legacy_path)] * 12))

        self.assertEqual([True] * 12, results)
        migrated = EventStore(legacy_path)
        self.assertTrue(migrated.schema_status()["ok"])
        with sqlite3.connect(str(legacy_path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        self.assertTrue(
            {
                "execution_snapshot_json", "execution_snapshot_hash", "execution_mode", "idempotency_key",
                "session_id", "route_decision_id", "provenance_json",
            }.issubset(columns)
        )

    def test_schema_six_adds_review_content_binding_to_schema_three(self):
        with self.store.connect() as connection:
            connection.execute("ALTER TABLE memory_reviews RENAME TO memory_reviews_v4")
            connection.execute(
                """CREATE TABLE memory_reviews (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    stage TEXT NOT NULL, decision TEXT NOT NULL, note TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )
            connection.execute("DROP TABLE memory_reviews_v4")
            connection.execute("PRAGMA user_version=3")
        self.store.initialize()
        with sqlite3.connect(str(self.store.path)) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(memory_reviews)")}
        self.assertIn("candidate_sha256", columns)
        self.assertEqual(7, self.store.schema_status()["current_version"])

    def test_route_decision_is_audited_without_storing_prompt(self):
        session = self.store.start_session()
        decision = self.store.record_route(
            prompt="这是不应原文写入路由表的消息",
            route="direct",
            reason_code="test",
            confidence=1.0,
            signals={"relation": "unrelated"},
            session_id=session["id"],
            message_id="message-1",
        )
        self.assertNotIn("prompt", decision)
        self.assertEqual(64, len(decision["prompt_sha256"]))
        self.assertEqual("v1.3", decision["policy_version"])
        repeated = self.store.record_route(
            prompt="这是不应原文写入路由表的消息",
            route="direct",
            reason_code="test",
            confidence=1.0,
            signals={"relation": "unrelated"},
            session_id=session["id"],
            message_id="message-1",
        )
        self.assertEqual(decision["id"], repeated["id"])

    def test_two_stage_review_and_reuse_lifecycle(self):
        source_task = self.store.submit("source", 1)
        self.store.complete(source_task, "source result")
        self.store.stage_memory_candidate(source_task, "/tmp/source.md", "a" * 64)
        self.store.review_memory(source_task, "delivery", "approve", "第一次审核通过", "a" * 64)
        self.store.review_memory(source_task, "final", "approve", "最终审核通过", "a" * 64)
        self.store.mark_memory_published(source_task, "/vault/source.md")

        current_task = self.store.submit("reuse", 1)
        self.store.record_memory_reuse(current_task, source_task, "source.md")
        affected = self.store.decide_reuse_outcomes(current_task, False)
        self.assertEqual([source_task], affected)
        source = self.store.get_memory_candidate(source_task)
        self.assertEqual("revalidation_required", source["status"])
        self.assertEqual(1, source["reuse_count"])
        self.assertEqual(1, source["failed_reuse_count"])

    def test_successful_reuse_resets_consecutive_failure_count(self):
        source_task = self.store.submit("source", 1)
        self.store.complete(source_task, "source result")
        self.store.stage_memory_candidate(source_task, "/tmp/source.md", "e" * 64)
        self.store.review_memory(source_task, "delivery", "approve", "first", "e" * 64)
        self.store.review_memory(source_task, "final", "approve", "final", "e" * 64)
        self.store.mark_memory_published(source_task, "/vault/source.md")

        failed_once = self.store.submit("failed once", 1)
        self.store.record_memory_reuse(failed_once, source_task, "source.md")
        self.store.decide_reuse_outcomes(failed_once, False)
        self.assertEqual(1, self.store.get_memory_candidate(source_task)["failed_reuse_count"])

        succeeded = self.store.submit("succeeded", 1)
        self.store.record_memory_reuse(succeeded, source_task, "source.md")
        self.store.decide_reuse_outcomes(succeeded, True)
        after_success = self.store.get_memory_candidate(source_task)
        self.assertEqual(0, after_success["failed_reuse_count"])
        self.assertEqual("published", after_success["status"])

        failed_again = self.store.submit("failed again", 1)
        self.store.record_memory_reuse(failed_again, source_task, "source.md")
        self.store.decide_reuse_outcomes(failed_again, False)
        after_second_failure = self.store.get_memory_candidate(source_task)
        self.assertEqual(1, after_second_failure["failed_reuse_count"])
        self.assertEqual("revalidation_required", after_second_failure["status"])
        self.assertEqual("failed", self.store.memory_reuses(failed_again)[0]["outcome"])

    def test_final_review_cannot_skip_delivery_reapproval_after_revision(self):
        task_id = self.store.submit("revise", 1)
        self.store.complete(task_id, "result")
        self.store.stage_memory_candidate(task_id, "/tmp/revise.md", "b" * 64)
        self.store.review_memory(task_id, "delivery", "revise", "需要修改", "b" * 64)
        with self.assertRaises(ValueError):
            self.store.review_memory(task_id, "final", "approve", "不应跳级", "b" * 64)

    def test_final_review_is_bound_to_delivery_candidate_hash(self):
        task_id = self.store.submit("hash", 1)
        self.store.complete(task_id, "result")
        self.store.stage_memory_candidate(task_id, "/tmp/hash.md", "c" * 64)
        self.store.review_memory(task_id, "delivery", "approve", "通过", "c" * 64)
        with self.assertRaises(ValueError):
            self.store.review_memory(task_id, "final", "approve", "被篡改", "d" * 64)

    def test_duplicate_message_id_with_different_content_is_rejected(self):
        session = self.store.start_session()
        self.store.record_route(
            prompt="first", route="direct", reason_code="test", confidence=1.0,
            signals={}, session_id=session["id"], message_id="same",
        )
        with self.assertRaises(ValueError):
            self.store.record_route(
                prompt="second", route="direct", reason_code="test", confidence=1.0,
                signals={}, session_id=session["id"], message_id="same",
            )

    def test_duplicate_message_id_with_different_session_is_rejected(self):
        self.store.record_route(
            prompt="same", route="direct", reason_code="test", confidence=1.0,
            signals={}, session_id="session-a", message_id="same-session",
        )
        with self.assertRaises(ValueError):
            self.store.record_route(
                prompt="same", route="direct", reason_code="test", confidence=1.0,
                signals={}, session_id="session-b", message_id="same-session",
            )

    def test_duplicate_message_id_with_different_active_task_is_rejected(self):
        self.store.record_route(
            prompt="same", route="direct", reason_code="test", confidence=1.0,
            signals={}, session_id="session", message_id="same-task", active_task_id="task-a",
        )
        with self.assertRaises(ValueError):
            self.store.record_route(
                prompt="same", route="direct", reason_code="test", confidence=1.0,
                signals={}, session_id="session", message_id="same-task", active_task_id="task-b",
            )

    def test_session_close_reports_routes_without_direct_message_text(self):
        session = self.store.start_session()
        self.store.record_route(
            prompt="private conversational aside", route="direct", reason_code="test",
            confidence=1.0, signals={}, session_id=session["id"], message_id="aside",
        )
        packet = self.store.session_review_packet(session["id"])
        self.assertEqual(1, len(packet["routes"]))
        self.assertNotIn("prompt", packet["routes"][0])

    def test_session_start_rolls_over_stale_active_day_without_losing_it(self):
        first = self.store.start_session()
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE work_sessions SET local_date='2000-01-01' WHERE id=?", (first["id"],)
            )
        second = self.store.start_session()
        self.assertNotEqual(first["id"], second["id"])
        with self.store.read_only() as connection:
            old_status = connection.execute(
                "SELECT status FROM work_sessions WHERE id=?", (first["id"],)
            ).fetchone()[0]
        self.assertEqual("closed", old_status)


class MemoryLifecycleStateMachineTest(unittest.TestCase):
    """provisional -> active 晋升：状态转移必须显式、可审计、可重复。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.tmp.name) / "test.sqlite3")
        self.store.initialize()
        self.sha = "a" * 64

    def tearDown(self):
        self.tmp.cleanup()

    def _candidate_in_state(self, status: str) -> str:
        task_id = self.store.submit("source", 1)
        self.store.complete(task_id, "result")
        self.store.stage_memory_candidate(task_id, "/tmp/candidate.md", self.sha)
        if status == "pending_delivery_review":
            return task_id
        if status == "delivery_approved":
            self.store.review_memory(task_id, "delivery", "approve", "ok", self.sha)
            return task_id
        if status == "delivery_rejected":
            self.store.review_memory(task_id, "delivery", "reject", "no", self.sha)
            return task_id
        if status == "revision_requested":
            self.store.review_memory(task_id, "delivery", "revise", "调整", self.sha)
            return task_id
        if status == "final_rejected":
            self.store.review_memory(task_id, "delivery", "approve", "ok", self.sha)
            self.store.review_memory(task_id, "final", "reject", "no", self.sha)
            return task_id
        if status == "pending_final_review":
            self.store.review_memory(task_id, "delivery", "approve", "ok", self.sha)
            self.store.review_memory(task_id, "final", "approve", "ok", self.sha)
            return task_id
        if status == "published":
            self.store.review_memory(task_id, "delivery", "approve", "ok", self.sha)
            self.store.review_memory(task_id, "final", "approve", "ok", self.sha)
            self.store.mark_memory_published(task_id, "/vault/candidate.md")
            return task_id
        if status == "publication_failed":
            self.store.review_memory(task_id, "delivery", "approve", "ok", self.sha)
            self.store.review_memory(task_id, "final", "approve", "ok", self.sha)
            self.store.mark_memory_publication_failed(task_id, "index rebuild failed")
            return task_id
        if status == "revalidation_required":
            task_id = self._candidate_in_state("published")
            reuse_task = self.store.submit("reuse", 1)
            self.store.record_memory_reuse(reuse_task, task_id, "candidate.md")
            self.store.decide_reuse_outcomes(reuse_task, False)
            return task_id
        if status == "retired":
            task_id = self._candidate_in_state("published")
            self.store.mark_memory_retired(task_id, "/archive/candidate.md", "test")
            return task_id
        raise AssertionError("unsupported fixture status: " + status)

    def test_renamed_note_updates_only_the_recorded_path_and_leaves_an_event(self):
        task_id = self._candidate_in_state("published")
        with self.assertRaises(ValueError):                       # 记录的旧路径对不上就不改
            self.store.record_memory_note_renamed(task_id, "/vault/other.md", "/vault/readable.md")
        row = self.store.record_memory_note_renamed(task_id, "/vault/candidate.md", "/vault/readable.md")
        self.assertEqual(("published", "/vault/readable.md"), (row["status"], row["published_path"]))
        with self.store.connect() as conn:
            payloads = [r[0] for r in conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.note_renamed'", (task_id,))]
        self.assertEqual(1, len(payloads))
        self.assertIn("/vault/readable.md", payloads[0])
        self.assertTrue(self.store.verify_event_chain()["ok"])
        with self.assertRaises(ValueError):                       # 没发布的候选不能改名
            self.store.record_memory_note_renamed(self._candidate_in_state("final_rejected"), "", "/vault/x.md")

    def test_transition_table_covers_every_declared_status(self):
        from xiaoba_agentos.store import (
            MEMORY_ACTIVE_RETRIEVAL_STATES,
            MEMORY_CANDIDATE_STATES,
            MEMORY_CANDIDATE_TRANSITIONS,
        )

        self.assertEqual(10, len(MEMORY_CANDIDATE_STATES))
        self.assertEqual({"published"}, set(MEMORY_ACTIVE_RETRIEVAL_STATES))
        with self.store.read_only() as connection:
            ddl = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_candidates'"
            ).fetchone()[0]
        for status in MEMORY_CANDIDATE_STATES:
            with self.subTest(status=status):
                self.assertIn(status, ddl, "状态常量与数据库 CHECK 约束已漂移")
        for action, sources in MEMORY_CANDIDATE_TRANSITIONS.items():
            with self.subTest(action=action):
                self.assertTrue(sources <= MEMORY_CANDIDATE_STATES)

    def test_invalid_transitions_are_rejected(self):
        from xiaoba_agentos.store import validate_memory_transition

        validate_memory_transition("publish", "pending_final_review")
        validate_memory_transition("retire", "published")
        validate_memory_transition("delivery_review", "pending_delivery_review")
        for action, status in (
            ("publish", "pending_delivery_review"),
            ("retire", "pending_delivery_review"),
            ("delivery_review", "retired"),
            ("final_review", "revision_requested"),
            ("final_review", "pending_delivery_review"),
        ):
            with self.subTest(action=action, status=status):
                with self.assertRaises(ValueError):
                    validate_memory_transition(action, status)

    def test_retired_candidate_can_never_be_reviewed_back_to_life(self):
        task_id = self._candidate_in_state("retired")
        for stage, decision in (("delivery", "approve"), ("final", "approve")):
            with self.subTest(stage=stage, decision=decision):
                with self.assertRaises(ValueError):
                    self.store.review_memory(task_id, stage, decision, "resurrect", self.sha)
        self.assertEqual("retired", self.store.get_memory_candidate(task_id)["status"])

    def test_retirement_requires_a_published_candidate(self):
        task_id = self._candidate_in_state("pending_delivery_review")
        with self.assertRaises(ValueError):
            self.store.mark_memory_retired(task_id, "/archive/x.md", "too early")
        self.assertEqual(
            "pending_delivery_review", self.store.get_memory_candidate(task_id)["status"]
        )

    def test_publication_failure_requires_a_final_approval_state(self):
        task_id = self._candidate_in_state("pending_delivery_review")
        with self.assertRaises(ValueError):
            self.store.mark_memory_publication_failed(task_id, "should not happen")
        self.assertEqual(
            "pending_delivery_review", self.store.get_memory_candidate(task_id)["status"]
        )

    def test_review_receipt_records_both_approvals_and_active_state(self):
        task_id = self._candidate_in_state("published")
        receipt = self.store.memory_review_receipt(task_id)
        self.assertEqual("published", receipt["status"])
        self.assertTrue(receipt["active_retrieval"])
        self.assertTrue(receipt["fully_approved"])
        self.assertFalse(receipt["publish_ready"], "已发布的经验不需要再次发布")
        self.assertEqual(1, receipt["delivery_approved_count"])
        self.assertEqual(1, receipt["final_approved_count"])
        self.assertEqual(
            [("delivery", "approve"), ("final", "approve")],
            [(item["stage"], item["decision"]) for item in receipt["reviews"]],
        )
        self.assertEqual(
            ["memory.published"],
            [item["event_type"] for item in receipt["publications"]],
        )

    def test_review_receipt_marks_provisional_candidate_inactive(self):
        task_id = self._candidate_in_state("delivery_approved")
        receipt = self.store.memory_review_receipt(task_id)
        self.assertFalse(receipt["active_retrieval"], "只有 published 允许进入活动检索")
        self.assertFalse(receipt["fully_approved"], "仅有交付审核不得视为终审通过")
        self.assertFalse(receipt["publish_ready"])
        self.assertEqual(0, receipt["final_approved_count"])
        self.assertTrue(receipt["stopped_after_delivery_review"])

    def test_publication_failure_after_file_write_stays_out_of_retrieval(self):
        task_id = self._candidate_in_state("pending_final_review")
        self.store.mark_memory_publication_failed(task_id, "database settlement failed")
        self.assertEqual("publication_failed", self.store.get_memory_candidate(task_id)["status"])
        receipt = self.store.memory_review_receipt(task_id)
        self.assertFalse(receipt["active_retrieval"], "发布未结算的经验不得进入活动检索")
        self.assertTrue(receipt["publish_ready"], "发布失败必须可以重试")
        self.assertEqual("memory.publication_failed", receipt["publications"][-1]["event_type"])
        recovered = self.store.mark_memory_published(task_id, "/vault/candidate.md")
        self.assertEqual("published", recovered["status"])

    def test_review_is_atomic_when_event_append_fails(self):
        task_id = self._candidate_in_state("pending_delivery_review")
        original = self.store._append_event_tx

        def failing(*args, **kwargs):
            raise sqlite3.OperationalError("simulated process exit mid-review")

        self.store._append_event_tx = failing
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.store.review_memory(task_id, "delivery", "approve", "ok", self.sha)
        finally:
            self.store._append_event_tx = original
        self.assertEqual(
            "pending_delivery_review",
            self.store.get_memory_candidate(task_id)["status"],
            "审核回执写入失败时必须整体回滚",
        )
        with self.store.read_only() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM memory_reviews WHERE task_id=?", (task_id,)
            ).fetchone()[0]
        self.assertEqual(0, count, "失败的审核不得留下半条 review 记录")
        recovered = self.store.review_memory(task_id, "delivery", "approve", "retry", self.sha)
        self.assertEqual("delivery_approved", recovered["status"])
        self.assertEqual("pending_delivery_review", recovered["previous_status"])
        self.assertEqual("delivery_approved", recovered["reviewed_status"])

    def test_legacy_database_initializes_and_passes_schema_check(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite3"
        first = EventStore(legacy)
        first.initialize()
        version = first.schema_status()["current_version"]
        reopened = EventStore(legacy)
        status = reopened.schema_status()
        self.assertTrue(status["ok"], "旧库必须直接通过 schema 检查：" + str(status))
        self.assertEqual(7, version)
        self.assertEqual(version, status["current_version"], "本次补强不引入 schema 变更")


# 策略进化 2026-10-11 由用户裁决退役，6 张表只读保留、结构版本仍是 7。
# 这一组测试守 B12「退役的不复活」：表还在（历史数据不丢、退回 v1.6.2 时它的
# 结构自检还能过），但新代码一行都不再读写它们。
_RETIRED_STRATEGY_TABLES = (
    "strategies",
    "evaluation_cases",
    "evaluations",
    "evaluation_runs",
    "promotion_gates",
    "strategy_audit",
)

# 基线 v1.6.2 建库时 sqlite_master 里存下的建表原文（去掉 IF NOT EXISTS 后）。
# 逐字比对，是为了证明「结构版本留在 7」不是嘴上说说：退回 v1.6.2 时，
# 旧版按这份定义自检，任何一处列定义漂移都会让它判成坏库、小八起不来。
_BASELINE_TABLE_DDL = {
    "strategies": """CREATE TABLE strategies (
                    version INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL CHECK(status IN ('candidate','active','retained','retired','rejected')),
                    body_json TEXT NOT NULL,
                    score REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )""",
    "evaluation_cases": """CREATE TABLE evaluation_cases (
                    id TEXT PRIMARY KEY, dataset TEXT NOT NULL, prompt TEXT NOT NULL,
                    expected_json TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
                )""",
    "evaluations": """CREATE TABLE evaluations (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('offline','shadow','replay','regression')),
                    dataset TEXT, case_id TEXT, prompt TEXT NOT NULL, output TEXT, judge_json TEXT,
                    strategy_version INTEGER, status TEXT NOT NULL, source_task_id TEXT,
                    created_at REAL NOT NULL, finished_at REAL
                )""",
    "evaluation_runs": """CREATE TABLE evaluation_runs (
                    id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
                    agent_name TEXT NOT NULL, output TEXT, status TEXT NOT NULL, latency_ms INTEGER,
                    created_at REAL NOT NULL
                )""",
    "promotion_gates": """CREATE TABLE promotion_gates (
                    id TEXT PRIMARY KEY, strategy_version INTEGER NOT NULL, gate_type TEXT NOT NULL,
                    status TEXT NOT NULL, details_json TEXT NOT NULL, created_at REAL NOT NULL
                )""",
    "strategy_audit": """CREATE TABLE strategy_audit (
                    id TEXT PRIMARY KEY, strategy_version INTEGER NOT NULL, action TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at REAL NOT NULL
                )""",
}

# 6 张表里各放几行历史数据。表由建库语句建出来，这里只塞行。
_RETIRED_STRATEGY_ROWS = """
INSERT INTO strategies(version,status,body_json,score,sample_count,created_at)
    VALUES(1,'active','{"agent_weights": {}}',0.0,0,100.0);
INSERT INTO strategies(version,status,body_json,score,sample_count,created_at)
    VALUES(2,'candidate','{"agent_weights": {"a": 0.5}}',1.0,3,200.0);
INSERT INTO evaluation_cases(id,dataset,prompt,expected_json,metadata_json,created_at)
    VALUES('case-1','judge','计算 37 乘以 41','"1517"','{}',300.0);
INSERT INTO evaluation_cases(id,dataset,prompt,expected_json,metadata_json,created_at)
    VALUES('case-2','judge','只输出数字','"1517"','{}',301.0);
INSERT INTO evaluations(id,mode,dataset,case_id,prompt,output,judge_json,strategy_version,status,source_task_id,created_at,finished_at)
    VALUES('eval-1','offline','judge','case-1','计算 37 乘以 41','1517','{}',1,'completed',NULL,400.0,400.0);
INSERT INTO evaluations(id,mode,dataset,case_id,prompt,output,judge_json,strategy_version,status,source_task_id,created_at,finished_at)
    VALUES('eval-2','replay','judge','case-2','只输出数字','1517','{}',2,'completed',NULL,401.0,401.0);
INSERT INTO evaluation_runs(id,evaluation_id,agent_name,output,status,latency_ms,created_at)
    VALUES('run-1','eval-1','agy_review','1517','completed',10,500.0);
INSERT INTO evaluation_runs(id,evaluation_id,agent_name,output,status,latency_ms,created_at)
    VALUES('run-2','eval-1','dsh_review','1517','completed',11,501.0);
INSERT INTO evaluation_runs(id,evaluation_id,agent_name,output,status,latency_ms,created_at)
    VALUES('run-3','eval-2','agy_review','1517','completed',12,502.0);
INSERT INTO promotion_gates(id,strategy_version,gate_type,status,details_json,created_at)
    VALUES('gate-1',2,'regression','passed','{}',600.0);
INSERT INTO promotion_gates(id,strategy_version,gate_type,status,details_json,created_at)
    VALUES('gate-2',2,'statistical_margin','passed','{}',601.0);
INSERT INTO strategy_audit(id,strategy_version,action,reason,created_at)
    VALUES('audit-1',1,'approve','initial',700.0);
"""

# 源码里出现 SQL 的地方只有这些关键字开头才算 SQL 语句；其余字符串（报错文案、
# 文档字符串）不参与扫描，注释也不参与。
_SQL_KEYWORDS = (
    "SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE",
    "CREATE", "DROP", "ALTER", "PRAGMA", "WITH",
)


def _normalize_ddl(text):
    """把建表语句里的空白折叠成单个空格，避免缩进影响逐字比对。"""
    return " ".join(str(text).split())


def _sql_statements(source):
    """把源码里的字符串字面量按分号切成语句，只留看起来像 SQL 的那些。

    用 tokenize 取字面量而不是正则扫全文，是为了区分「SQL」和「注释」：
    注释里提到表名是允许的，SQL 里读写表名不允许。
    """
    statements = []
    readline = io.StringIO(source).readline
    for token in tokenize.generate_tokens(readline):
        if token.type != tokenize.STRING:
            continue
        try:
            literal = ast.literal_eval(token.string)
        except (SyntaxError, ValueError):
            # f-string 等无法直接取值的字面量：退回原文扫，宁可多报不可漏报。
            literal = token.string
        if not isinstance(literal, str):
            continue
        for chunk in literal.split(";"):
            if any(keyword in chunk.upper() for keyword in _SQL_KEYWORDS):
                statements.append((token.start[0], chunk))
    return statements


class RetiredStrategyTablesReadOnlyTest(unittest.TestCase):
    """B12「退役的不复活」：6 张表只读保留，结构版本留在 7。

    三条各守一件事：
    - 跑完一整轮真实工作（建库、提交、裁判、status、7 个退役命令）后，6 张表
      逐行不变，user_version 仍是 7，事件链仍然通过；
    - 用新代码操作过的库，6 张表的列定义与基线建表语句逐字一致，退回 v1.6.2
      时旧版的结构自检还能过；
    - 源码里除了建表语句，再没有任何对这 6 张表的 SQL 读写。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        self.config = _config(self.root)
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def tearDown(self):
        self.tmp.cleanup()

    def _seed_history(self):
        with sqlite3.connect(str(self.store.path)) as conn:
            conn.executescript(_RETIRED_STRATEGY_ROWS)

    def _retired_rows(self):
        """按 rowid 顺序整行读出 6 张表，用来证明「一行都没动」。"""
        with sqlite3.connect(str(self.store.path)) as conn:
            return {
                table: [tuple(row) for row in conn.execute(
                    "SELECT * FROM {} ORDER BY rowid".format(table))]
                for table in _RETIRED_STRATEGY_TABLES
            }

    def _user_version(self):
        with sqlite3.connect(str(self.store.path)) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])

    def _run_main(self, argv):
        """跑一次命令行，返回 (退出码, 标准输出, 标准错误)。"""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_full_round_of_work_leaves_the_six_tables_row_for_row_unchanged(self):
        self._seed_history()
        before = self._retired_rows()
        for table, rows in before.items():
            self.assertTrue(rows, "夹具必须先给这张表塞行：" + table)

        self.store.initialize()  # 再跑一次建库：对已有库必须是空操作
        task_id = self.store.submit("总结今天的进展", max_attempts=1)
        result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual("completed", result["status"], result)

        code, out, _ = self._run_main(["--config", str(self.config_path), "status"])
        self.assertEqual(0, code, out)
        summary = json.loads(out)["summary"]
        self.assertEqual(7, summary["database_schema_version"])
        self.assertNotIn("active_strategy", summary)

        for name in ("evolve", "eval", "replay", "regression",
                     "judge-baseline", "promote", "rollback"):
            with self.subTest(command=name):
                code, out, err = self._run_main(
                    ["--config", str(self.config_path), name])
                self.assertEqual(2, code, "退役命令不得返回成功")
                self.assertIn("已退役", err)
                self.assertEqual("", out)

        self.assertEqual(before, self._retired_rows(), "6 张表的内容必须逐行不变")
        self.assertEqual(7, self._user_version())
        chain = self.store.verify_event_chain()
        self.assertTrue(chain["ok"], chain)
        # 这一轮确实动了库：任务与它的事件都在，否则上面的「没变」没有意义。
        self.assertEqual("completed", self.store.get_task(task_id)["status"])
        self.assertGreater(chain["events"], 0)

    def test_a_database_this_build_touched_still_passes_the_v162_self_check(self):
        self._seed_history()
        self.store.submit("历史任务", max_attempts=1)
        self.store.initialize()

        self.assertEqual(7, self._user_version(), "结构版本必须留在 7")
        with sqlite3.connect(str(self.store.path)) as conn:
            stored = {row[0]: row[1] for row in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'")}
        for table, baseline_ddl in _BASELINE_TABLE_DDL.items():
            with self.subTest(table=table):
                self.assertIn(table, stored, "退回 v1.6.2 时这张表必须在：" + table)
                self.assertEqual(
                    _normalize_ddl(baseline_ddl), _normalize_ddl(stored[table]),
                    "列定义与基线建表语句不一致：" + table)
        # v1.6.2 的 schema_status 要求这 6 张表都在，缺一张就判坏库。
        status = self.store.schema_status()
        self.assertTrue(status["ok"], status)
        self.assertEqual(7, status["current_version"])

    def test_source_never_reads_or_writes_the_six_tables(self):
        source_root = Path(__file__).resolve().parents[1] / "src"
        hits = {}
        for path in sorted(source_root.rglob("*.py")):
            for lineno, statement in _sql_statements(
                    path.read_text(encoding="utf-8")):
                for table in _RETIRED_STRATEGY_TABLES:
                    if re.search(r"\b{}\b".format(table), statement, re.IGNORECASE):
                        hits.setdefault(table, []).append(
                            (path.name, lineno, _normalize_ddl(statement)[:70]))
        # 6 张表都必须还能在建表语句里找到：找不到说明表被删了，不是「没读写」。
        self.assertEqual(sorted(_RETIRED_STRATEGY_TABLES), sorted(hits),
                         "6 张表必须仍在建表语句里，扫描结果：" + str(hits))
        for table, found in hits.items():
            for name, lineno, head in found:
                with self.subTest(table=table, file=name, line=lineno):
                    self.assertTrue(
                        head.upper().startswith(("CREATE TABLE", "CREATE INDEX")),
                        "{}:{} 出现了对这 6 张表的 SQL 读写，只允许建表语句：{}".format(
                            name, lineno, head))


class SafetyRegressionTest(unittest.TestCase):
    def test_blocked_failed_do_not_enter_long_term_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": 1,
                "runtime_dir": str(root / "runtime"),
                "project_dir": str(root),
                "worker": {"poll_seconds": 1, "lease_seconds": 60, "max_attempts": 2,
                           "retry_base_seconds": 0, "retry_max_seconds": 0, "parallelism": 1,
                           "minimum_successful_agents": 1},
                "memory": {"knowlp_enabled": False, "obsidian_vault": str(root),
                           "obsidian_subdir": "AgentOS", "publish_enabled": False},
                "integrations": {},
                "agents": [{"name": "a", "type": "mock", "enabled": True, "role": "",
                            "timeout_seconds": 1}],
                "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
            }
            (root / "c.json").write_text(json.dumps(raw), encoding="utf-8")
            cfg = AppConfig.load(root / "c.json")
            store = EventStore(cfg.database_path)
            store.initialize()
            task_id = store.submit("x", 2)
            store.fail(task_id, "permanent error")
            self.assertEqual("failed", store.get_task(task_id)["status"])
            candidates = store.list_memory_candidates(task_id) if hasattr(store, "list_memory_candidates") else []
            self.assertEqual([], candidates)

    def test_registration_refuses_staged_evidence_that_does_not_match_its_declared_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "agentos.sqlite3")
            store.initialize()
            task_id = store.submit("hash binding", 1)
            staged = root / "candidate.md"
            staged.write_text("original evidence", encoding="utf-8")
            original_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
            staged.write_text("changed evidence", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "does not match its evidence hash"):
                store.complete_with_memory_candidate(
                    task_id, "result", str(staged), original_sha256, []
                )
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertEqual(0, _event_count(store, task_id, "task.completed"))
            self.assertEqual(0, _event_count(store, task_id, "memory.candidate_staged"))


class AtomicCompletionTest(unittest.TestCase):
    def test_store_rolls_back_completion_when_candidate_cannot_be_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "runtime" / "agentos.sqlite3")
            store.initialize()
            task_id = store.submit("原子性", 2)
            staged = root / "c.md"
            staged.write_text("x", encoding="utf-8")
            evidence = hashlib.sha256(staged.read_bytes()).hexdigest()
            with patch.object(
                store, "_write_candidate_status_sidecar_values", side_effect=OSError("disk full")
            ):
                with self.assertRaises(OSError):
                    store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, [])
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertEqual(0, _event_count(store, task_id, "task.completed"))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_staging_failure_leaves_task_retryable_then_completes_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("测试", 3)

            with patch.object(MemoryBridge, "stage_experience", side_effect=OSError("disk full")):
                first = Orchestrator(config, store).run_once()
            self.assertEqual("retry", first["status"])
            self.assertIn("memory candidate staging failed", first["errors"][0])
            task = store.get_task(task_id)
            self.assertNotEqual("completed", task["status"])
            self.assertIsNone(task["result"])
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertEqual(0, _event_count(store, task_id, "task.completed"))

            second = Orchestrator(config, store).run_once()
            self.assertEqual("completed", second["status"])
            self.assertEqual("completed", store.get_task(task_id)["status"])
            self.assertEqual("pending_delivery_review", store.get_memory_candidate(task_id)["status"])
            self.assertEqual(1, _candidate_count(store, task_id))
            self.assertEqual(1, _event_count(store, task_id, "task.completed"))
            self.assertEqual(1, _event_count(store, task_id, "memory.candidate_staged"))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_database_failure_during_completion_is_also_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("测试", 3)
            with patch.object(
                EventStore, "_write_candidate_status_sidecar_values", side_effect=OSError("read-only")
            ):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("retry", result["status"])
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertEqual(0, _candidate_count(store, task_id))

    def test_completion_is_idempotent_and_writes_one_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "runtime" / "agentos.sqlite3")
            store.initialize()
            task_id = store.submit("幂等", 2)
            staged = root / "c.md"
            staged.write_text("x", encoding="utf-8")
            evidence = hashlib.sha256(staged.read_bytes()).hexdigest()
            self.assertTrue(store.complete_with_memory_candidate(task_id, "结果", str(staged), evidence, []))
            self.assertFalse(store.complete_with_memory_candidate(task_id, "另一个结果", str(staged), evidence, []))
            self.assertEqual("结果", store.get_task(task_id)["result"])
            self.assertEqual(evidence, store.get_memory_candidate(task_id)["evidence_sha256"])
            self.assertEqual(1, _event_count(store, task_id, "task.completed"))
            self.assertEqual(1, _event_count(store, task_id, "memory.candidate_staged"))
            with self.assertRaises(KeyError):
                store.complete_with_memory_candidate("missing", "r", str(staged), evidence, [])

    def test_pending_constraint_hold_cannot_be_repaired_into_review_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "runtime" / "agentos.sqlite3")
            store.initialize()
            task_id = store.submit("扣留候选", 1)
            store.hold_constraint_task(task_id, "1517", {"status": "unresolved_partial"})
            staged = root / "candidate.md"
            staged.write_text("held result", encoding="utf-8")
            evidence = hashlib.sha256(staged.read_bytes()).hexdigest()

            self.assertFalse(store.complete_with_memory_candidate(
                task_id, "1517", str(staged), evidence, []
            ))
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertFalse(staged.with_suffix(".md.status.json").exists())
            self.assertEqual(0, _event_count(store, task_id, "memory.candidate_staged"))
            self.assertTrue(store.verify_event_chain()["ok"])

            store.release_constraint_hold(task_id)
            self.assertFalse(store.complete_with_memory_candidate(
                task_id, "1517", str(staged), evidence, []
            ))
            self.assertEqual("pending_delivery_review", store.get_memory_candidate(task_id)["status"])
            self.assertEqual(1, _event_count(store, task_id, "memory.candidate_staged"))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_pending_hold_blocks_candidate_even_with_legacy_retry_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = EventStore(root / "runtime" / "agentos.sqlite3")
            store.initialize()
            task_id = store.submit("held legacy status", 1)
            store.hold_constraint_task(task_id, "withheld", {"status": "unresolved_partial"})
            with store.connect() as conn:
                conn.execute("UPDATE tasks SET status='retry' WHERE id=?", (task_id,))
            staged = root / "candidate.md"
            staged.write_text("withheld evidence", encoding="utf-8")
            evidence = hashlib.sha256(staged.read_bytes()).hexdigest()

            self.assertFalse(store.complete_with_memory_candidate(
                task_id, "withheld", str(staged), evidence, []
            ))
            self.assertEqual("retry", store.get_task(task_id)["status"])
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertEqual(0, _event_count(store, task_id, "memory.candidate_staged"))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_error_after_commit_is_not_downgraded_to_retry(self):
        """connect() runs permission maintenance after commit; that failure is not a rollback."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("postcommit", 3)
            original_complete = store.complete_with_memory_candidate

            def fail_after_commit(*args, **kwargs):
                real_permissions = store._secure_permissions
                calls = [0]

                def permissions():
                    calls[0] += 1
                    if calls[0] == 3:  # before open, after open, then finally after commit
                        raise PermissionError("postcommit chmod denied")
                    return real_permissions()

                with patch.object(store, "_secure_permissions", side_effect=permissions):
                    return original_complete(*args, **kwargs)

            with patch.object(store, "complete_with_memory_candidate", side_effect=fail_after_commit):
                first = Orchestrator(config, store).run_once()
            self.assertEqual("completed", first["status"])
            task = store.get_task(task_id)
            self.assertEqual("completed", task["status"])
            self.assertTrue(task["result"])
            self.assertEqual("pending_delivery_review", store.get_memory_candidate(task_id)["status"])

            second = Orchestrator(config, store).run_once()
            self.assertEqual("idle", second["status"])
            self.assertEqual(1, _event_count(store, task_id, "task.completed"))
            self.assertEqual(1, _event_count(store, task_id, "memory.candidate_staged"))
            self.assertEqual(0, _event_count(store, task_id, "task.retry"))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_unreadable_state_after_failure_does_not_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("unknown", 3)
            orchestrator = Orchestrator(config, store)
            with patch.object(store, "complete_with_memory_candidate", side_effect=OSError("io")), patch.object(
                orchestrator, "_completion_committed", side_effect=OSError("cannot read")
            ), patch.object(store, "retry_or_fail") as retry:
                with self.assertRaises(OSError):
                    orchestrator.run_once()
            retry.assert_not_called()
            self.assertEqual(0, _event_count(store, task_id, "task.retry"))

    def test_worker_loop_reports_unexpected_errors_instead_of_exiting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _config(root)
            output = io.StringIO()
            with patch("xiaoba_agentos.cli.Orchestrator") as orchestrator, patch(
                "xiaoba_agentos.cli.time.sleep"
            ), redirect_stdout(output):
                orchestrator.return_value.run_once.side_effect = RuntimeError("boom")
                code = main(["--config", str(config.path), "run", "--max-idle-polls", "2"])
            self.assertEqual(0, code)
            self.assertEqual(2, orchestrator.return_value.run_once.call_count)
            reports = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith("{")]
            errors = [item for item in reports if item.get("status") == "worker_error"]
            self.assertEqual(1, len(errors))  # repeated identical errors are not spammed
            self.assertEqual("RuntimeError", errors[0]["error_type"])


class ConstraintHoldReleaseTest(unittest.TestCase):
    def _held(self, root: Path):
        config = _config(root)
        store = EventStore(config.database_path)
        store.initialize()
        task_id = store.submit("受约束任务", 2)
        store.hold_constraint_task(task_id, "被扣留的结果", {"reason": "test"})
        return config, store, task_id

    def test_release_and_candidate_roll_back_together(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _config_, store, task_id = self._held(root)
            staged = root / "c.md"
            staged.write_text("x", encoding="utf-8")
            evidence = hashlib.sha256(staged.read_bytes()).hexdigest()
            with patch.object(store, "_write_candidate_status_sidecar_values", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.release_constraint_hold(task_id, str(staged), evidence)
            self.assertEqual("pending", store.get_constraint_hold(task_id)["status"])
            self.assertIsNone(store.get_task(task_id)["result"])
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertEqual(0, _event_count(store, task_id, "constraint_hold_released"))

            store.release_constraint_hold(task_id, str(staged), evidence)
            self.assertEqual("released", store.get_constraint_hold(task_id)["status"])
            self.assertEqual("被扣留的结果", store.get_task(task_id)["result"])
            self.assertEqual("pending_delivery_review", store.get_memory_candidate(task_id)["status"])
            self.assertEqual(1, _event_count(store, task_id, "memory.candidate_staged"))
            self.assertTrue(store.verify_event_chain()["ok"])
            with self.assertRaises(ValueError):
                store.release_constraint_hold(task_id, str(staged), evidence)
            with self.assertRaises(ValueError):
                store.release_constraint_hold(task_id, str(staged))

    def test_command_failure_keeps_the_hold_and_can_be_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, store, task_id = self._held(root)
            with patch.object(MemoryBridge, "stage_experience", side_effect=OSError("disk full")):
                code, payload = _run_cli(["--config", str(config.path), "constraint-hold-release", task_id])
            self.assertEqual(1, code)
            self.assertEqual("blocked", payload["status"])
            self.assertTrue(payload["retry_safe"])
            self.assertEqual("pending", store.get_constraint_hold(task_id)["status"])
            self.assertIsNone(store.get_task(task_id)["result"])
            self.assertEqual(0, _candidate_count(store, task_id))

            code, payload = _run_cli(["--config", str(config.path), "constraint-hold-release", task_id])
            self.assertEqual(0, code)
            self.assertEqual("released", payload["status"])
            candidate = store.get_memory_candidate(task_id)
            self.assertEqual(payload["candidate_sha256"], candidate["evidence_sha256"])
            self.assertEqual(
                candidate["evidence_sha256"],
                hashlib.sha256(Path(candidate["staged_path"]).read_bytes()).hexdigest(),
            )
            self.assertEqual(1, _candidate_count(store, task_id))

    def test_error_after_commit_is_reported_as_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _store, task_id = self._held(root)
            original = EventStore.release_constraint_hold

            def commit_then_fail(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise PermissionError("postcommit chmod denied")

            with patch.object(EventStore, "release_constraint_hold", commit_then_fail):
                code, payload = _run_cli(["--config", str(config.path), "constraint-hold-release", task_id])
            self.assertEqual(0, code)
            self.assertEqual("released", payload["status"])


if __name__ == "__main__":
    unittest.main()
