"""Actual-task evidence loop: real Seatbelt cases, explicitly TEST reviewers/users."""
import concurrent.futures
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _config, _run_cli
from fixtures_validation import native_trial
from xiaoba_agentos.experience_loop import (
    pending_index, read_pending, record_application, record_delivery, application_packet,
)
from xiaoba_agentos.experience_workflow import digest, verify_final_qualification, match_trials, trial_summary
from xiaoba_agentos.memory import MemoryBridge
from xiaoba_agentos.store import EventStore


class ExperienceLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = _config(self.root)
        self.store = EventStore(self.cfg.database_path)
        self.store.initialize()
        self.store.apply_validation_extension()
        self.project = self.root / "real-task"
        self.project.mkdir()
        self.artifact = self.file("product.txt", "TEST delivered product v1")
        self.artifact["version"] = "v1"
        self.evidence = self.file("execution.txt", "TEST actual application assertions and execution log")
        self.delivery_record = self.file("delivery.md", "TEST user acceptance and task delivery record")

    def file(self, name, content):
        path = self.project / name
        path.write_text(content)
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def candidate(self, kind="method", approve=True):
        c = self.store.create_authored_candidate("TEST parse-fixture method", "TEST reusable method steps",
            self.cfg.runtime_dir / "memory-outbox", author="test", experience_kind=kind, problem_key="parse-fixture")
        if approve:
            self.store.review_memory(c["task_id"], "delivery", "approve", "TEST human initial review", c["evidence_sha256"])
        return c

    def application(self, c):
        with self.store.read_only() as conn:
            review = conn.execute("SELECT id FROM memory_reviews WHERE task_id=? ORDER BY rowid DESC LIMIT 1", (c["task_id"],)).fetchone()[0]
        return {"candidate_id": c["task_id"], "candidate_sha256": c["evidence_sha256"], "delivery_review_id": review,
            "real_task": {"id": "host:TEST-task-1", "problem_key": "parse-fixture", "task_type": "TEST parsing",
                "description": "TEST similar real task, not the candidate-authoring task", "workspace": str(self.project),
                "authorization_ref": "TEST user request 1"},
            "conditions_checked": {"applicability": "TEST same input format", "exclusions": "TEST no excluded conditions",
                "environment": "TEST local fixture environment"},
            "artifact": self.artifact, "application_location": "TEST parser step 2",
            "steps_applied": "TEST applied the candidate normalization steps", "deviations": "TEST no deviations",
            "execution_evidence": [self.evidence]}

    def delivery(self, **changes):
        return dict({"origin": "user", "user_message": "交付", "message_id": "TEST-message-1",
            "source_ref": "TEST explicit human event", "real_task_id": "host:TEST-task-1", "artifact": self.artifact,
            "delivery_record": self.delivery_record}, **changes)

    def trial(self, c, app, passed=True):
        return native_trial(self.store, c, passed, card={"problem_key": "parse-fixture", "experience_application": app["trial_binding"]})

    def gate(self, c):
        with self.store.read_only() as conn:
            return verify_final_qualification(conn, c["task_id"])

    def packet(self, c):
        with self.store.read_only() as conn:
            return application_packet(conn, c)

    def count(self, table):
        with self.store.read_only() as conn:
            return conn.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def event_count(self, kind):
        with self.store.read_only() as conn:
            return conn.execute("SELECT count(*) FROM events WHERE event_type=?", (kind,)).fetchone()[0]

    def test_index_is_verifiable_read_only_and_separate_from_published_retrieval(self):
        c = self.candidate()
        before = self.count("events")
        index = pending_index(self.store, "parse-fixture")
        self.assertEqual(len(index["entries"]), 1)
        self.assertEqual(index["index_sha256"], digest({k: v for k, v in index.items() if k != "index_sha256"}))
        entry = index["entries"][0]
        self.assertEqual(entry["match_basis"], "problem_key")
        self.assertFalse(entry["normal_retrieval"])
        read = read_pending(self.store, c["task_id"], entry["candidate_sha256"], entry["delivery_review_id"])
        self.assertIn("TEST reusable method steps", read["body"])
        self.assertFalse(read["applied"])
        self.assertEqual(self.count("events"), before)
        self.assertEqual(self.count("memory_reuse"), 0)
        self.assertFalse(MemoryBridge(self.cfg, self.store)._database_active({"source_task_id": c["task_id"]}))
        self.assertEqual(match_trials(self.store, "parse-fixture"), [{k: entry[k] for k in ("task_id", "title", "candidate_sha256")}])

    def test_index_excludes_unapproved_rule_drift_and_ready_candidates(self):
        self.candidate(approve=False)
        self.candidate(kind="rule")
        drift = self.candidate()
        Path(drift["staged_path"]).write_text("TEST drift")
        ready = self.candidate()
        native_trial(self.store, ready, card={"problem_key": "parse-fixture"})
        self.assertEqual(pending_index(self.store)["entries"], [])

    def test_read_refuses_changed_initial_review(self):
        c = self.candidate()
        old = pending_index(self.store)["entries"][0]
        self.store.review_memory(c["task_id"], "delivery", "approve", "TEST renewed review", c["evidence_sha256"])
        with self.assertRaisesRegex(ValueError, "binding changed"):
            read_pending(self.store, c["task_id"], old["candidate_sha256"], old["delivery_review_id"])

    def test_index_title_search_does_not_count_application(self):
        self.candidate()
        self.assertEqual(len(pending_index(self.store, query="parse-fixture")["entries"]), 1)
        self.assertEqual(self.event_count("memory.application_recorded"), 0)
        with self.assertRaisesRegex(ValueError, "no actual applications"):
            record_delivery(self.store, self.delivery())

    def test_application_rejects_wrong_problem_or_missing_evidence_or_scope(self):
        c = self.candidate()
        for change in ("problem", "evidence", "scope", "conditions"):
            value = copy.deepcopy(self.application(c))
            if change == "problem": value["real_task"]["problem_key"] = "unrelated"
            if change == "evidence": value["execution_evidence"] = []
            if change == "scope": value["real_task"]["workspace"] = str(self.root / "different-project")
            if change == "conditions": value["conditions_checked"]["applicability"] = ""
            with self.subTest(change=change), self.assertRaises(ValueError):
                record_application(self.store, value)
        self.assertEqual(self.event_count("memory.application_recorded"), 0)

    def test_real_execution_and_delivery_return_to_original_for_human_final(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        trial = self.trial(c, app)
        self.assertFalse(self.gate(c)["qualified"])
        before = self.count("memory_reviews")
        receipt = record_delivery(self.store, self.delivery())
        self.assertTrue(self.gate(c)["qualified"], self.gate(c))
        packet = self.packet(c)
        attachment = packet["attachments"][-1]
        self.assertEqual(attachment["delivery_event_id"], receipt["event_id"])
        self.assertEqual(len(attachment["independent_reviews"]), 2)
        self.assertTrue(all(r["validation_rating"] == "PASS" and r["numeric_score"] is None for r in attachment["independent_reviews"]))
        self.assertEqual(self.count("memory_reviews"), before)
        self.assertEqual(self.count("memory_candidates"), 1)
        self.assertIsNone(self.store.get_memory_candidate(trial["task_id"]))
        self.assertEqual(self.count("memory_reuse"), 0)
        self.assertIsNone(self.store.get_memory_candidate(c["task_id"])["published_path"])
        summary = self.store.review_queue("all")[0]["experience_validation"]
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["experience_loop"]["applications"][0]["application_id"], app["application_id"])
        self.assertEqual(len(self.store.review_queue("final")), 1)

    def test_delivery_before_independent_completion_refreshes_original_evidence(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        record_delivery(self.store, self.delivery())
        self.assertFalse(self.gate(c)["qualified"])
        self.trial(c, app)
        self.assertTrue(self.gate(c)["qualified"])
        self.assertTrue(self.packet(c)["attachments"][-1]["validation"]["qualified"])
        self.assertEqual(self.count("memory_reviews"), 1)

    def test_product_acceptance_cannot_replace_failed_isolated_validation(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app, False)
        record_delivery(self.store, self.delivery())
        self.assertFalse(self.gate(c)["qualified"])
        with self.assertRaises(ValueError):
            self.store.review_memory(c["task_id"], "final", "approve", "TEST forbidden bypass", c["evidence_sha256"])
        self.assertEqual(self.count("memory_reviews"), 1)

    def test_old_trial_without_application_binding_is_refused_before_execution(self):
        c = self.candidate()
        record_application(self.store, self.application(c))
        card = {"title": "TEST unbound", "problem_key": "parse-fixture", "objective": "TEST objective",
                "scope": "TEST scope", "acceptance": "TEST assertions", "stop_conditions": "TEST fail stop"}
        path = self.root / "unbound-card.json"
        path.write_text(json.dumps(card))
        with patch("xiaoba_agentos.experience_workflow.execute_trial") as execute:
            code, blocked = _run_cli(["--config", str(self.cfg.path), "memory-trial-run", c["task_id"], "--card", str(path)])
        self.assertEqual(code, 1, blocked)
        self.assertIn("application binding", blocked["error"])
        execute.assert_not_called()
        self.assertEqual(len(self.store.get_validation_records(c["task_id"])), 0)

    def test_artifact_or_execution_evidence_drift_invalidates_acceptance(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app)
        record_delivery(self.store, self.delivery())
        for ref in (self.artifact, self.evidence, self.delivery_record):
            path = Path(ref["path"])
            old = path.read_bytes()
            path.write_bytes(old + b" changed")
            self.assertFalse(self.gate(c)["qualified"])
            path.write_bytes(old)
            self.assertTrue(self.gate(c)["qualified"])

    def test_quoted_negated_or_agent_delivery_cannot_trigger_acceptance(self):
        c = self.candidate()
        record_application(self.store, self.application(c))
        for text in ('“交付”', '不要交付', '交付是什么意思', '交付?', '交付？'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                record_delivery(self.store, self.delivery(user_message=text))
        with self.assertRaises(ValueError):
            record_delivery(self.store, self.delivery(origin="agent"))
        self.assertEqual(self.event_count("task.delivery_recorded"), 0)

    def test_repeat_and_concurrent_delivery_are_once_without_approval(self):
        c = self.candidate()
        value = self.application(c)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            apps = list(pool.map(lambda _: record_application(self.store, value), range(3)))
        self.assertEqual(len({a["application_id"] for a in apps}), 1)
        self.trial(c, apps[0])
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            deliveries = list(pool.map(lambda _: record_delivery(self.store, self.delivery()), range(3)))
        self.assertEqual(len({d["event_id"] for d in deliveries}), 1)
        record_delivery(self.store, self.delivery(message_id="TEST-repeated-user-message"))
        self.assertEqual(self.event_count("task.delivery_recorded"), 1)
        self.assertEqual(self.event_count("memory.delivery_attached"), 1)
        self.assertEqual(self.event_count("memory.application_recorded"), 1)
        self.assertEqual(self.count("memory_reviews"), 1)
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_acceptance_withdrawal_prevents_publication_even_after_test_human_final(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app)
        record_delivery(self.store, self.delivery())
        self.store.review_memory(c["task_id"], "final", "approve", "TEST human final only", c["evidence_sha256"])
        record_delivery(self.store, self.delivery(message_id="TEST-withdraw", user_message="撤回验收", decision="withdraw"))
        with self.assertRaises(ValueError), self.store.publication_transaction(c["task_id"]):
            pass

    def test_multiple_experiences_return_individually_without_new_candidate(self):
        candidates = [self.candidate(), self.candidate()]
        for c in candidates:
            app = record_application(self.store, self.application(c))
            self.trial(c, app)
        receipt = record_delivery(self.store, self.delivery())
        self.assertEqual(len(receipt["application_ids"]), 2)
        for c in candidates:
            self.assertTrue(self.gate(c)["qualified"])
            self.assertEqual(len(self.packet(c)["attachments"]), 1)
        self.assertEqual(self.count("memory_candidates"), 2)
        self.assertEqual(self.count("memory_reviews"), 2)

    def test_receipt_conflict_and_transaction_failure_leave_no_partial_delivery(self):
        c = self.candidate()
        record_application(self.store, self.application(c))
        with patch("xiaoba_agentos.experience_loop.attach_delivery_tx", side_effect=OSError("TEST write fault")):
            with self.assertRaises(OSError):
                record_delivery(self.store, self.delivery())
        self.assertEqual(self.event_count("task.delivery_recorded"), 0)
        record_delivery(self.store, self.delivery())
        with self.assertRaisesRegex(ValueError, "identity"):
            record_delivery(self.store, self.delivery(source_ref="TEST conflicting reference"))
        self.assertEqual(self.event_count("task.delivery_recorded"), 1)

    def test_cli_direct_host_path_does_not_start_session_or_backend(self):
        c = self.candidate()
        code, index = _run_cli(["--config", str(self.cfg.path), "memory-pending-index", "--problem-key", "parse-fixture"])
        self.assertEqual(code, 0)
        entry = index["entries"][0]
        code, result = _run_cli(["--config", str(self.cfg.path), "memory-pending-read", c["task_id"],
            "--candidate-sha256", entry["candidate_sha256"], "--delivery-review-id", entry["delivery_review_id"]])
        self.assertEqual(code, 0)
        path = self.root / "application.json"
        path.write_text(json.dumps(self.application(c)))
        code, app = _run_cli(["--config", str(self.cfg.path), "memory-application-record", "--receipt", str(path)])
        self.assertEqual(code, 0, app)
        path.write_text(json.dumps(self.delivery()))
        code, receipt = _run_cli(["--config", str(self.cfg.path), "memory-delivery-record", "--receipt", str(path)])
        self.assertEqual(code, 0, receipt)
        self.assertEqual(self.count("work_sessions"), 0)
        self.assertEqual(self.count("agent_runs"), 0)

    def test_new_initial_approval_invalidates_existing_application_and_delivery(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app)
        record_delivery(self.store, self.delivery())
        self.store.review_memory(c["task_id"], "delivery", "approve", "TEST renewed human initial review", c["evidence_sha256"])
        self.assertFalse(self.gate(c)["qualified"])

    def test_event_chain_damage_blocks_index_and_application(self):
        c = self.candidate()
        value = self.application(c)
        with self.store.connect() as conn:
            conn.execute("UPDATE events SET event_hash='broken' WHERE sequence=1")
        with self.assertRaisesRegex(ValueError, "event chain"):
            pending_index(self.store)
        with self.assertRaisesRegex(ValueError, "event chain"):
            record_application(self.store, value)

    def test_reacceptance_requires_new_human_final_approval(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app)
        record_delivery(self.store, self.delivery())
        self.store.review_memory(c["task_id"], "final", "approve", "TEST human final", c["evidence_sha256"])
        record_delivery(self.store, self.delivery(message_id="TEST-withdraw", user_message="撤回验收", decision="withdraw"))
        record_delivery(self.store, self.delivery(message_id="TEST-reaccept"))
        self.assertTrue(self.gate(c)["qualified"])
        with self.assertRaisesRegex(ValueError, "current application"), self.store.publication_transaction(c["task_id"]):
            pass

    def test_failed_reviews_and_execution_remain_attached_to_original(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app, False)
        record_delivery(self.store, self.delivery())
        attachment = self.packet(c)["attachments"][-1]
        self.assertFalse(attachment["execution"]["passed"])
        self.assertEqual(len(attachment["independent_reviews"]), 2)
        self.assertFalse(attachment["validation"]["qualified"])

    def test_delivery_invalidates_stale_browser_snapshot_and_withdrawal_survives_drift(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app)
        before = self.store.get_memory_candidate(c["task_id"])["updated_at"]
        record_delivery(self.store, self.delivery())
        self.assertGreater(self.store.get_memory_candidate(c["task_id"])["updated_at"], before)
        Path(self.artifact["path"]).write_text("TEST reworked product")
        record_delivery(self.store, self.delivery(message_id="TEST-withdraw", user_message="撤回验收", decision="withdraw"))
        self.assertEqual(self.packet(c)["deliveries"][-1]["decision"], "withdraw")
        self.assertFalse(self.gate(c)["qualified"])

    def test_existing_intake_delivery_hook_binds_exact_user_message_without_backend(self):
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app)
        path = self.root / "delivery.json"
        value = self.delivery()
        for key in ("origin", "message_id", "user_message"):
            value.pop(key)
        path.write_text(json.dumps(value))
        args = ["--config", str(self.cfg.path), "intake", "交付", "--message-id", "TEST-intake-delivery", "--delivery-receipt", str(path)]
        code, receipt = _run_cli(args)
        self.assertEqual(code, 0, receipt)
        self.assertEqual(receipt["status"], "delivery_recorded")
        self.assertTrue(self.gate(c)["qualified"])
        self.assertEqual(self.count("work_sessions"), 0)
        self.assertEqual(self.count("memory_reviews"), 1)
        code, rejected = _run_cli([*args[:3], '讨论“交付”', *args[4:]])
        self.assertEqual(code, 1, rejected)
        self.assertEqual(self.event_count("task.delivery_recorded"), 1)

    def test_index_refuses_an_initial_receipt_without_matching_audit_event(self):
        c = self.candidate()
        with self.store.connect() as conn:
            conn.execute("UPDATE memory_reviews SET id='TEST-unverifiable' WHERE task_id=?", (c["task_id"],))
        self.assertTrue(self.store.verify_event_chain()["ok"])
        self.assertEqual(pending_index(self.store)["entries"], [])

    def test_native_card_disables_final_until_delivery_and_after_withdrawal(self):
        from test_trial_review_center import module
        center = object.__new__(module("server").Center)
        center.vault = self.root / "vault"
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        self.trial(c, app)
        def view():
            with self.store.read_only() as conn:
                current = dict(conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (c["task_id"],)).fetchone())
                current.update(prompt="TEST original method", experience_validation=trial_summary(conn, current))
                return center.item(current)
        waiting = view()
        self.assertEqual(waiting["status"], "pending_final_review")
        self.assertNotIn("approve", waiting["allowed_decisions"])
        self.assertEqual(waiting["status_label"], "待应用或用户交付验收证据")
        record_delivery(self.store, self.delivery())
        self.assertIn("approve", view()["allowed_decisions"])
        record_delivery(self.store, self.delivery(message_id="TEST-withdraw", user_message="撤回验收", decision="withdraw"))
        self.assertNotIn("approve", view()["allowed_decisions"])

    def test_external_review_history_remains_visible_after_later_failure(self):
        from test_review_ingress_native_integration import ReviewIngressNativeIntegrationTests as Fixture
        from xiaoba_review.store import ReviewStore, DecisionInbox
        fixture = Fixture()
        fixture.store = self.store
        fixture.rs = ReviewStore(self.store, self.cfg.runtime_dir / "memory-outbox")
        fixture.inbox = DecisionInbox()
        for name, kind in (("TEST-dsh", "dsh"), ("TEST-peer", "peer")):
            fixture.rs.register_source({"reviewer_id": name, "kind": kind, "source_ref": "test://"+name, "provenance": "TEST synthetic source"})
        c = self.candidate()
        app = record_application(self.store, self.application(c))
        trial = self.trial(c, app)
        receipts = fixture.receipts(fixture.request(c, trial))
        record_delivery(self.store, self.delivery())
        self.assertEqual(len(self.packet(c)["attachments"][-1]["external_reviews"]), 2)
        old = receipts["peer"]
        self.store.review_memory(c["task_id"], "final", "approve", "TEST human final with original reviews", c["evidence_sha256"])
        revised = dict(old, receipt_id=old["receipt_id"]+"-new-pass", findings="TEST revised limitations with PASS")
        received = fixture.rs.receive(revised)
        fixture.rs.attest(received["id"], received["digest"], "TEST source confirmation")
        fixture.rs.deliver(fixture.inbox)
        self.assertTrue(self.gate(c)["qualified"])
        with self.assertRaisesRegex(ValueError, "current application"), self.store.publication_transaction(c["task_id"]):
            pass
        failed = dict(old, receipt_id=old["receipt_id"]+"-later-fail", verdict="FAIL", findings="TEST later independent finding")
        received = fixture.rs.receive(failed)
        fixture.rs.attest(received["id"], received["digest"], "TEST source confirmation")
        fixture.rs.deliver(fixture.inbox)
        packet = self.packet(c)
        self.assertEqual(len(packet["external_reviews"]), 4)
        self.assertEqual(packet["external_reviews"][-1]["original_receipt"]["verdict"], "FAIL")
        self.assertFalse(self.gate(c)["qualified"])
