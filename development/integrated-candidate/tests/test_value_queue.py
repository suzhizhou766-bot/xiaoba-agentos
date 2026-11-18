"""Schema-7 event projection tests for candidates awaiting value assessment."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.store import EventStore
from xiaoba_agentos.value_queue import assessment_tx, defer_tx, pending_items


class ValueQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        refresh_patcher = patch("xiaoba_agentos.workday_ledger.refresh_after_write")
        refresh_patcher.start()
        self.addCleanup(refresh_patcher.stop)
        self.store = EventStore(root / "runtime" / "db.sqlite")
        self.store.initialize()
        self.task = self.store.submit("TEST reusable method", 2, snapshot={
            "experience_policy": {"version": 1, "kind": "method", "problem_key": "TEST-value-queue"},
        })
        self.candidate = root / "candidate.md"
        self.candidate.write_text("TEST candidate body", encoding="utf-8")
        self.sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

    def defer_payload(self, *, candidate_sha256=None, **updates):
        value = {
            "version": 1,
            "staged_path": str(self.candidate),
            "candidate_sha256": candidate_sha256 or self.sha,
            "source_task_ids": ["TEST-source-2", "TEST-source-1"],
            "reason": "no_scoring_source",
        }
        value.update(updates)
        return value

    @staticmethod
    def assessment_payload(candidate_sha256, scores=(6, 6)):
        from xiaoba_agentos.value_scoring import decide

        reviews = [
            {"reviewer_id": f"TEST-reviewer-{index}", "score": score}
            for index, score in enumerate(scores, 1)
        ]
        return {"version": 1, "candidate_sha256": candidate_sha256, **decide(reviews)}

    def test_authored_candidate_defaults_to_method_and_cannot_skip_value_review(self):
        outbox = Path(self.temp.name) / "runtime" / "memory-outbox"
        created = self.store.create_authored_candidate(
            "TEST default method", "TEST candidate body", outbox, author="TEST host"
        )
        self.assertEqual("awaiting_value_review", created["status"])
        self.assertEqual("method", json.loads(self.store.get_task(created["task_id"])["execution_snapshot_json"])
                         ["experience_policy"]["kind"])
        self.assertIsNone(self.store.get_memory_candidate(created["task_id"]))
        self.assertEqual("awaiting_value_review", self.store.value_candidate_status(
            created["task_id"], created["evidence_sha256"]
        ))

        before = self.store.summary()["tasks"]
        with self.assertRaisesRegex(ValueError, "must pass independent value review"):
            self.store.create_authored_candidate(
                "TEST bypass", "TEST body", outbox, author="TEST host",
                experience_kind="method", enforce_value_queue=False,
            )
        self.assertEqual(before, self.store.summary()["tasks"])
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_authored_candidate_cannot_enter_initial_review_from_forged_single_review(self):
        outbox = Path(self.temp.name) / "runtime" / "memory-outbox"
        created = self.store.create_authored_candidate(
            "TEST forged score", "TEST candidate body", outbox, author="TEST host"
        )
        from xiaoba_agentos.value_scoring import decide
        forged = {
            "version": 2,
            "candidate_sha256": created["evidence_sha256"],
            "review_request_id": "TEST-not-an-agent-pool-request",
            **decide([{"reviewer_id": "TEST-one-reviewer", "score": 10}]),
        }
        with self.store.connect() as conn:
            assessment_tx(conn, created["task_id"], forged)

        staged_path = outbox / f"{created['task_id']}.md"
        with self.assertRaisesRegex(ValueError, "independent value assessment"):
            self.store.stage_memory_candidate(
                created["task_id"], str(staged_path), created["evidence_sha256"], []
            )
        self.assertEqual("awaiting_value_review", self.store.value_candidate_status(
            created["task_id"], created["evidence_sha256"]
        ))
        self.assertEqual(1, len(self.store.value_review_queue()))
        self.assertIsNone(self.store.get_memory_candidate(created["task_id"]))

    def test_public_event_api_cannot_forge_value_assessment(self):
        from xiaoba_agentos.value_scoring import decide
        payload = {
            "version": 2,
            "candidate_sha256": self.sha,
            "review_request_id": "TEST-forged-request",
            **decide([
                {"reviewer_id": "TEST-reviewer-a", "score": 8},
                {"reviewer_id": "TEST-reviewer-b", "score": 8},
            ]),
        }
        with self.assertRaisesRegex(ValueError, "事务业务入口"):
            self.store.append_event(self.task, "memory.value_assessed", payload)
        with self.store.read_only() as conn:
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (self.task,),
            ).fetchone()[0])

    def test_deferred_item_rebuilds_from_events_without_writing(self):
        with self.store.connect() as conn:
            event_id = defer_tx(conn, self.task, self.defer_payload())
            before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            items = pending_items(conn)
            after = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        self.assertEqual(before, after)
        self.assertEqual(1, len(items))
        self.assertEqual(
            {
                "task_id": self.task,
                "staged_path": str(self.candidate),
                "candidate_sha256": self.sha,
                "source_task_ids": ["TEST-source-1", "TEST-source-2"],
                "reason": "no_scoring_source",
                "sequence": 2,
                "event_id": event_id,
                "version": 1,
                "queue_status": "awaiting_value_review",
                "dispatch": {"status": "not_started", "request_id": None, "reason": ""},
            },
            items[0],
        )
        with self.store.read_only() as conn:
            raw = conn.execute(
                "SELECT payload_json FROM events WHERE event_id=?", (event_id,)
            ).fetchone()[0]
        self.assertNotIn("TEST candidate body", raw)
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_unverifiable_v1_assessment_never_consumes_deferred_item(self):
        with self.store.connect() as conn:
            defer_tx(conn, self.task, self.defer_payload())
            assessment_tx(
                conn, self.task,
                self.assessment_payload("b" * 64),
            )
            self.assertEqual(1, len(pending_items(conn)))
            assessment_tx(conn, self.task, self.assessment_payload(self.sha))
            self.assertEqual(1, len(pending_items(conn)))

    def test_matching_candidate_row_or_later_candidate_event_consumes_item(self):
        with self.store.connect() as conn:
            defer_tx(conn, self.task, self.defer_payload())
        with self.assertRaisesRegex(ValueError, "independent value assessment"):
            self.store.stage_memory_candidate(self.task, str(self.candidate), self.sha, [])
        with self.store.read_only() as conn:
            self.assertEqual(1, len(pending_items(conn)))

        other_task = self.store.submit("TEST second reusable method", 2)
        with self.store.connect() as conn:
            defer_tx(conn, other_task, self.defer_payload())
        other_path = Path(self.temp.name) / "other.md"
        other_path.write_text("TEST other bytes", encoding="utf-8")
        other_hash = hashlib.sha256(other_path.read_bytes()).hexdigest()
        self.store.stage_memory_candidate(other_task, str(other_path), other_hash, [])
        with self.store.read_only() as conn:
            # A candidate for different bytes cannot consume the deferred item.
            self.assertEqual({self.task, other_task}, {item["task_id"] for item in pending_items(conn)})

    def test_later_matching_candidate_event_consumes_without_candidate_projection(self):
        with self.store.connect() as conn:
            defer_tx(conn, self.task, self.defer_payload())
        with self.assertRaisesRegex(ValueError, "事务业务入口"):
            self.store.append_event(self.task, "memory.candidate_staged", {"evidence_sha256": self.sha})
        with self.store.read_only() as conn:
            self.assertIsNone(self.store.get_memory_candidate(self.task))
            self.assertEqual(1, len(pending_items(conn)))

    def test_event_helpers_are_idempotent_and_reject_conflicts(self):
        with self.store.connect() as conn:
            first = defer_tx(conn, self.task, self.defer_payload())
            again = defer_tx(conn, self.task, self.defer_payload())
            self.assertEqual(first, again)
            with self.assertRaisesRegex(ValueError, "不同待评分材料"):
                defer_tx(conn, self.task, self.defer_payload(reason="changed_reason"))
            assessed = assessment_tx(conn, self.task, self.assessment_payload(self.sha))
            self.assertEqual(
                assessed,
                assessment_tx(conn, self.task, self.assessment_payload(self.sha)),
            )
            with self.assertRaisesRegex(ValueError, "冲突的价值决定"):
                assessment_tx(conn, self.task, self.assessment_payload(self.sha, scores=(8, 8)))

        with self.store.read_only() as conn:
            self.assertEqual(3, conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_unknown_versions_and_malformed_or_body_bearing_payloads_fail_closed(self):
        with self.store.connect() as conn:
            for payload in (
                self.defer_payload(version=2),
                self.defer_payload(unexpected="TEST body"),
                self.defer_payload(candidate_sha256="not-a-hash"),
                self.defer_payload(source_task_ids=["duplicate", "duplicate"]),
            ):
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    defer_tx(conn, self.task, payload)
            invalid_assessment = self.assessment_payload(self.sha)
            invalid_assessment["version"] = 2
            with self.assertRaisesRegex(ValueError, "字段|版本"):
                assessment_tx(conn, self.task, invalid_assessment)

    def test_unknown_defer_version_is_isolated_with_event_location(self):
        with self.store.connect() as conn:
            EventStore._append_event_tx(conn, self.task, "memory.value_unscored", {"version": 2})
        with self.store.read_only() as conn:
            items = pending_items(conn)
        self.assertEqual(1, len(items))
        self.assertEqual("blocked_event", items[0]["queue_status"])
        self.assertEqual(self.task, items[0]["task_id"])
        self.assertEqual("memory.value_unscored", items[0]["source_event"]["event_type"])

    def test_conflicting_duplicate_deferrals_fail_closed_during_projection(self):
        with self.store.connect() as conn:
            defer_tx(conn, self.task, self.defer_payload())
        with self.store.connect() as conn:
            EventStore._append_event_tx(
                conn, self.task, "memory.value_unscored",
                self.defer_payload(reason="TEST conflicting reason"),
            )

        with self.store.read_only() as conn:
            items = pending_items(conn)
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(all(item["queue_status"] == "blocked_event" for item in items))
        self.assertTrue(all(item["event_id"] for item in items))

    def test_malformed_assessment_is_isolated_from_other_queue_items(self):
        with self.store.connect() as conn:
            defer_tx(conn, self.task, self.defer_payload())
        with self.store.connect() as conn:
            EventStore._append_event_tx(conn, self.task, "memory.value_assessed", {"version": 3})
        with self.store.read_only() as conn:
            items = pending_items(conn)
        self.assertGreaterEqual(len(items), 2)
        self.assertTrue(any(item["queue_status"] == "awaiting_value_review" for item in items))
        self.assertTrue(any(item.get("source_event", {}).get("event_type") == "memory.value_assessed" for item in items))

    def test_legacy_unversioned_events_are_not_projected_as_v1(self):
        with self.store.connect() as conn:
            EventStore._append_event_tx(conn, self.task, "memory.value_unscored", {"reason": "no_scoring_source"})
            EventStore._append_event_tx(conn, self.task, "memory.value_assessed", {"decision": "initial_review"})
        with self.store.read_only() as conn:
            self.assertEqual([], pending_items(conn))

    def test_core_assessment_materializes_prepared_candidate_atomically(self):
        task = self.store.submit(
            "TEST scored method", 2,
            snapshot={"experience_policy": {"version": 1, "kind": "method", "problem_key": "TEST"}},
        )
        path = self.store.path.parent / "memory-outbox" / f"{task}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TEST scored candidate", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertTrue(self.store.complete_with_memory_candidate(
            task, "TEST result", str(path), digest, [], expected_attempt=0
        ))
        self.assertEqual("awaiting_value_review", self.store.value_candidate_status(task, digest))
        from xiaoba_agentos.value_scoring import decide
        assessment = {
            "version": 2,
            "review_request_id": "TEST-value-request",
            "candidate_sha256": digest,
            **decide([{"reviewer_id": "TEST-a", "score": 8}, {"reviewer_id": "TEST-b", "score": 8}]),
        }
        with patch("xiaoba_agentos.value_queue.assessment_from_review_request_tx", return_value=assessment):
            result = self.store.assess_value_candidate(task, "TEST-value-request")
        self.assertEqual("pending_delivery_review", result["status"])
        settled = self.store.get_task(task)
        self.assertEqual("TEST result", settled["result"])
        self.assertEqual(digest, result["candidate_sha256"])
        self.assertEqual("pending_delivery_review", self.store.value_candidate_status(task, digest))
        self.assertTrue(path.exists())
        self.assertEqual("pending_delivery_review", self.store.get_memory_candidate(task)["status"])
        self.assertEqual([], [item for item in self.store.value_review_queue() if item["task_id"] == task])

    def test_core_assessment_discards_low_value_without_candidate_projection(self):
        task = self.store.submit(
            "TEST low method", 2,
            snapshot={"experience_policy": {"version": 1, "kind": "method", "problem_key": "TEST"}},
        )
        path = self.store.path.parent / "memory-outbox" / f"{task}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TEST low candidate", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.complete_with_memory_candidate(task, "TEST result", str(path), digest, [], expected_attempt=0)
        from xiaoba_agentos.value_scoring import decide
        assessment = {
            "version": 2,
            "review_request_id": "TEST-low-value-request",
            "candidate_sha256": digest,
            **decide([{"reviewer_id": "TEST-a", "score": 2}, {"reviewer_id": "TEST-b", "score": 2}]),
        }
        with patch("xiaoba_agentos.value_queue.assessment_from_review_request_tx", return_value=assessment):
            result = self.store.assess_value_candidate(task, "TEST-low-value-request")
        self.assertEqual("value_discarded", result["status"])
        self.assertEqual("TEST result", self.store.get_task(task)["result"])
        self.assertEqual("value_discarded", self.store.value_candidate_status(task, digest))
        self.assertFalse(path.exists())
        self.assertFalse(path.with_suffix(path.suffix + ".status.json").exists())
        self.assertIsNone(self.store.get_memory_candidate(task))
        self.assertEqual([], [item for item in self.store.value_review_queue() if item["task_id"] == task])
        with self.store.read_only() as conn:
            event = json.loads(conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed'",
                (task,),
            ).fetchone()[0])
        self.assertNotIn("TEST low candidate", json.dumps(event))
        self.assertEqual({"TEST-a": 2, "TEST-b": 2}, event["scores"])

    def test_session_waits_for_value_review_then_closes_after_discard(self):
        session = self.store.start_session()
        task = self.store.submit(
            "TEST session value review", 2, session_id=session["id"],
            snapshot={"experience_policy": {"version": 1, "kind": "method", "problem_key": "TEST-session"}},
        )
        path = self.store.path.parent / "memory-outbox" / f"{task}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TEST session candidate", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.complete_with_memory_candidate(task, "TEST session answer", str(path), digest, [], expected_attempt=0)

        packet = self.store.session_review_packet(session["id"])
        self.assertEqual("closing", packet["session"]["status"])
        self.assertEqual(1, packet["pending_value_reviews"])
        self.assertEqual("awaiting_value_review", packet["tasks"][-1]["memory_value_status"])

        from xiaoba_agentos.value_scoring import decide
        assessment = {
            "version": 2,
            "review_request_id": "TEST-session-value-request",
            "candidate_sha256": digest,
            **decide([{"reviewer_id": "TEST-a", "score": 2}, {"reviewer_id": "TEST-b", "score": 2}]),
        }
        with patch("xiaoba_agentos.value_queue.assessment_from_review_request_tx", return_value=assessment):
            self.store.assess_value_candidate(task, "TEST-session-value-request")
        self.assertEqual([session["id"]], self.store.close_resolved_sessions())
        with self.store.read_only() as conn:
            status = conn.execute("SELECT status FROM work_sessions WHERE id=?", (session["id"],)).fetchone()[0]
        self.assertEqual("closed", status)


if __name__ == "__main__":
    unittest.main()
