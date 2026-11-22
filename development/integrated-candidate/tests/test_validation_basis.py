"""J11b real-task evidence routing and final-review transaction gates."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _admit_authored_candidate, _config, _run_cli
from fixtures_validation import native_trial
from xiaoba_agentos.experience_loop import record_application, record_delivery as _record_delivery
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.validation_basis import (
    revoke_basis,
    confirm_basis_tx,
    basis_binding,
    normalize_method_claims,
    propose_historical_basis_tx,
    revoke_basis_tx,
    validate_basis,
)


def record_delivery(store, value):
    from xiaoba_agentos.delivery import user_decision
    return _record_delivery(store, value, authority=user_decision(value, channel="cli_user_declaration", explicit=True))



class ValidationBasisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = _config(self.root)
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.project = self.root / "actual-project"
        self.project.mkdir()

    def file_ref(self, name, content):
        path = self.project / name
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def candidate(self):
        value = self.store.create_authored_candidate(
            "TEST method candidate", "TEST reusable method and bounded claims", self.config.runtime_dir / "memory-outbox",
            author="test", experience_kind="method", problem_key="parse-fixture",
        )
        value = _admit_authored_candidate(self.store, value)
        self.store.review_memory(value["task_id"], "delivery", "approve", "TEST initial user decision", value["evidence_sha256"])
        return value

    def real_task(self, purpose="user_task", *, completed=True, result="TEST real task result"):
        kind = "audit" if purpose in {"release_smoke", "release_acceptance", "experience_trial"} else "method"
        task_id = self.store.submit(
            "TEST native real task prompt", 2,
            snapshot={"task_purpose": purpose, "experience_policy": {"version": 1, "kind": kind, "problem_key": "parse-fixture"}},
        )
        if completed:
            self.store.complete(task_id, result, expected_attempt=0)
        return task_id

    def application(self, candidate, real_task_id, claims=None, *, record=True):
        with self.store.read_only() as conn:
            review_id = conn.execute(
                "SELECT id FROM memory_reviews WHERE task_id=? AND stage='delivery' ORDER BY rowid DESC LIMIT 1",
                (candidate["task_id"],),
            ).fetchone()[0]
        artifact = self.file_ref("artifact.txt", "TEST delivered artifact v1") | {"version": "v1"}
        execution = self.file_ref("execution.txt", "TEST actual application execution record")
        value = {
            "candidate_id": candidate["task_id"], "candidate_sha256": candidate["evidence_sha256"],
            "delivery_review_id": review_id,
            "real_task": {"id": real_task_id, "problem_key": "parse-fixture", "task_type": "TEST parse/render",
                "description": "TEST actual task using a versioned product", "workspace": str(self.project),
                "authorization_ref": "TEST direct user task"},
            "conditions_checked": {"applicability": "TEST same input family", "exclusions": "TEST none omitted",
                "environment": "TEST local project"},
            "artifact": artifact, "application_location": "TEST actual project output",
            "steps_applied": "TEST applied the candidate steps to the actual task",
            "deviations": "TEST no deviations", "execution_evidence": [execution],
        }
        if claims is not None:
            value["method_claims"] = claims
        return (record_application(self.store, value) if record else value), artifact

    def delivery(self, real_task_id, artifact, message_id="TEST-delivery"):
        return record_delivery(self.store, {
            "origin": "user", "user_message": "交付", "message_id": message_id,
            "source_ref": "TEST direct user message", "real_task_id": real_task_id, "artifact": artifact,
            "delivery_record": self.file_ref("delivery.md", "TEST delivered product and user acceptance"),
        })

    def proposed(self, candidate, claims):
        task_id = self.real_task()
        app, artifact = self.application(candidate, task_id, claims)
        self.delivery(task_id, artifact)
        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        return task_id, app, artifact, basis

    def event_count(self, candidate_id, event_type):
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type=?", (candidate_id, event_type)
            ).fetchone()[0]

    def final_review_event_count(self, candidate_id):
        with self.store.read_only() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='memory.reviewed' "
                "AND json_extract(payload_json,'$.stage')='final'",
                (candidate_id,),
            ).fetchone()[0]

    def final_approval(self, candidate, basis, confirmation=None):
        return self.store.review_memory(
            candidate["task_id"], "final", "approve", "TEST final user decision", candidate["evidence_sha256"],
            validation_basis_confirmation=confirmation,
        )

    def confirm(self, basis):
        return {
            "basis_id": basis["basis_id"], "basis_sha256": basis["basis_sha256"],
            "confirmed": True, "classification_confirmed": True,
        }

    def test_artifact_route_confirms_in_final_transaction_and_publish_rechecks_same_basis(self):
        candidate = self.candidate()
        task_id, _, artifact, basis = self.proposed(candidate, [{"claim": "TEST inspect the rendered artifact", "class": "artifact"}])
        self.assertEqual("real_task_acceptance", basis["route"])
        self.assertEqual(["real_task_acceptance"], basis["required_checks"])
        self.assertIn("TEST native real task prompt", basis["real_task"]["prompt"])
        self.assertTrue(any(item["label"] == "交付认可原件" for item in basis["evidence_items"]))
        stale = self.confirm(basis) | {"basis_sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "does not match the current evidence"):
            self.final_approval(candidate, basis, stale)
        with self.assertRaisesRegex(ValueError, "确认方法主张分类"):
            self.final_approval(candidate, basis)
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))

        self.final_approval(candidate, basis, self.confirm(basis))
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        with self.store.read_only() as conn:
            events = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type IN ('memory.validation_basis_confirmed','memory.reviewed') ORDER BY sequence",
                (candidate["task_id"],),
            )]
            current = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("confirmed", current["status"])
        self.assertEqual(basis_binding(current), events[-1]["validation_basis"])
        self.assertIs(events[-1]["method_classification_confirmed"], True)
        self.final_approval(candidate, basis, self.confirm(basis))
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        with self.store.publication_transaction(candidate["task_id"]):
            pass

        path = Path(artifact["path"])
        path.write_text("TEST changed output", encoding="utf-8")
        with self.assertRaises(ValueError):
            with self.store.publication_transaction(candidate["task_id"]):
                pass

    def test_native_artifact_public_cli_records_basis_without_running_sandbox(self):
        """公开应用与交付入口形成J11c提议，确认由裁决页面显式完成。"""
        candidate = self.candidate()
        task_id = self.real_task()
        application, artifact = self.application(
            candidate, task_id,
            [{"claim": "TEST inspect the delivered artifact", "class": "artifact"}],
            record=False,
        )
        application_path = self.root / "application.json"
        application_path.write_text(json.dumps(application), encoding="utf-8")
        delivery = {
            "source_ref": "TEST direct user message", "real_task_id": task_id,
            "artifact": artifact,
            "delivery_record": self.file_ref("delivery.md", "TEST user accepted the delivered artifact"),
        }
        delivery_path = self.root / "delivery.json"
        delivery_path.write_text(json.dumps(delivery), encoding="utf-8")
        cli = ["--config", str(self.config.path)]
        with patch("xiaoba_agentos.experience_workflow.execute_trial") as sandbox:
            code, applied = _run_cli(cli + ["memory-application-record", "--receipt", str(application_path)])
            self.assertEqual(0, code, applied)
            code, delivered = _run_cli(cli + [
                "intake", "交付", "--user-decision", "--message-id", "TEST-native-artifact-cli-delivery",
                "--delivery-receipt", str(delivery_path),
            ])
            self.assertEqual(0, code, delivered)
            sandbox.assert_not_called()
        self.assertFalse(self.store.is_validation_extension_installed())
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_proposed"))
        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("real_task_acceptance", basis["route"])
        self.assertEqual("proposed", basis["status"])
        code, queue = _run_cli(cli + ["review-list", "--stage", "final"])
        self.assertEqual(0, code, queue)
        self.assertIn("validation_basis_confirmation_required", json.dumps(queue))
        code, blocked = _run_cli(cli + [
            "review", candidate["task_id"], "--stage", "final", "--decision", "approve",
            "--note", "TEST no explicit basis confirmation",
        ])
        self.assertNotEqual(0, code, blocked)
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))

    def test_publication_settlement_revalidates_basis_after_staged_file_moves(self):
        candidate = self.candidate()
        _, _, _, basis = self.proposed(
            candidate, [{"claim": "TEST inspect the delivered artifact", "class": "artifact"}]
        )
        self.final_approval(candidate, basis, self.confirm(basis))

        staged = Path(candidate["staged_path"])
        marker = staged.with_suffix(".published")
        with self.store.publication_transaction(candidate["task_id"]) as publication:
            staged.replace(marker)
            settled = publication.settle(str(self.root / "vault" / "AgentOS" / "TEST-published.md"))

        self.assertEqual("published", settled["status"])
        with self.store.read_only() as conn:
            current = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='memory.published'",
                (candidate["task_id"],),
            ).fetchone()[0]
        self.assertEqual("confirmed", current["status"])
        self.assertEqual(1, event_count)

    def test_review_flow_requires_a_matching_confirmation_and_passes_it_to_the_transaction(self):
        candidate = self.candidate()
        _, _, _, basis = self.proposed(candidate, [{"claim": "TEST inspect the delivered artifact", "class": "artifact"}])
        stale = self.confirm(basis) | {"basis_sha256": "0" * 64}
        old_packet = {key: value for key, value in self.confirm(basis).items() if key != "classification_confirmed"}
        for confirmation in (None, stale, old_packet, self.confirm(basis) | {"classification_confirmed": 1}):
            with self.subTest(confirmation=confirmation):
                code, payload = execute_review(
                    self.config, self.store, candidate["task_id"], "final", "approve", "TEST final decision",
                    expected_sha256=candidate["evidence_sha256"], validation_basis_confirmation=confirmation,
                )
                self.assertNotEqual(0, code)
                self.assertEqual("blocked", payload["status"])
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))

        with patch("xiaoba_agentos.review_flow.MemoryBridge") as bridge_class:
            bridge_class.return_value.publish_candidate.return_value = {
                "path": str(self.root / "vault" / "AgentOS" / f"{candidate['task_id']}.md")
            }
            code, payload = execute_review(
                self.config, self.store, candidate["task_id"], "final", "approve", "TEST final decision",
                expected_sha256=candidate["evidence_sha256"],
                validation_basis_confirmation=self.confirm(basis),
            )

        self.assertEqual(0, code, payload)
        self.assertEqual("published", payload["memory_status"])
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))

    def test_mixed_route_requires_sandbox_and_confirmation_atomically(self):
        self.store.apply_validation_extension()
        candidate = self.candidate()
        task_id = self.real_task()
        app, artifact = self.application(candidate, task_id, [
            {"claim": "TEST run the normalization command", "class": "code"},
            {"claim": "TEST inspect the output artifact", "class": "artifact"},
        ])
        self.delivery(task_id, artifact)
        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("combined", basis["route"])
        self.assertEqual(["real_task_acceptance", "sandbox"], basis["required_checks"])
        with self.assertRaisesRegex(ValueError, "尚未.*隔离验证"):
            self.final_approval(candidate, basis, self.confirm(basis))
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))

        native_trial(self.store, candidate, card={
            "problem_key": "parse-fixture", "experience_application": app["trial_binding"],
        })
        with self.assertRaisesRegex(ValueError, "确认方法主张分类"):
            self.final_approval(candidate, basis)
        with self.assertRaisesRegex(ValueError, "lacks explicit method classification confirmation"):
            self.final_approval(candidate, basis, self.confirm(basis) | {"classification_confirmed": False})
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))
        self.final_approval(candidate, basis, self.confirm(basis))
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))

    def test_code_only_stays_on_existing_sandbox_route_and_cannot_be_downgraded(self):
        self.store.apply_validation_extension()
        candidate = self.candidate()
        task_id = self.real_task()
        app, artifact = self.application(candidate, task_id, [
            {"claim": "TEST run the normalization command", "class": "code"},
        ])
        self.delivery(task_id, artifact)
        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("sandbox", basis["route"])
        self.assertEqual(["sandbox"], basis["required_checks"])
        self.assertEqual("not_required", basis["status"])
        with self.assertRaisesRegex(ValueError, "尚未.*隔离验证"):
            self.final_approval(candidate, basis, self.confirm(basis))
        with self.assertRaisesRegex(ValueError, "确认方法主张分类"):
            self.final_approval(candidate, basis)
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))

        native_trial(self.store, candidate, card={
            "problem_key": "parse-fixture", "experience_application": app["trial_binding"],
        })
        stale = self.confirm(basis) | {"basis_sha256": "0" * 64}
        old_packet = {key: value for key, value in self.confirm(basis).items() if key != "classification_confirmed"}
        for confirmation in (None, stale, old_packet, self.confirm(basis) | {"classification_confirmed": False}):
            with self.subTest(confirmation=confirmation):
                with self.assertRaises(ValueError):
                    self.final_approval(candidate, basis, confirmation)
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))
        self.final_approval(candidate, basis, self.confirm(basis))
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))
        with self.store.read_only() as conn:
            event = json.loads(conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.reviewed' "
                "AND json_extract(payload_json,'$.stage')='final'", (candidate["task_id"],),
            ).fetchone()[0])
        self.assertEqual(basis_binding(basis), event["validation_basis"])
        self.assertIs(event["method_classification_confirmed"], True)
        with self.store.publication_transaction(candidate["task_id"]):
            pass

    def test_existing_sandbox_final_approval_remains_publishable_without_classification_marker(self):
        self.store.apply_validation_extension()
        candidate = self.candidate()
        _, application, _, basis = self.proposed(candidate, [
            {"claim": "TEST run the normalization command", "class": "code"},
        ])
        native_trial(self.store, candidate, card={
            "problem_key": "parse-fixture", "experience_application": application["trial_binding"],
        })
        append_event = self.store._append_event_tx

        def append_legacy_final_review(conn, task_id, event_type, payload):
            if event_type == "memory.reviewed" and payload["stage"] == "final":
                # Record the exact shape emitted before explicit classification confirmation.
                payload = {key: value for key, value in payload.items() if key != "method_classification_confirmed"}
            return append_event(conn, task_id, event_type, payload)

        with patch.object(self.store, "_append_event_tx", side_effect=append_legacy_final_review):
            self.final_approval(candidate, basis, self.confirm(basis))
        self.assertTrue(self.store.verify_event_chain()["ok"])
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        with self.store.publication_transaction(candidate["task_id"]):
            pass

    def test_missing_method_claims_require_evidence_and_cannot_select_a_route(self):
        candidate = self.candidate()
        task_id = self.real_task()
        _, artifact = self.application(candidate, task_id)
        app_events = self.event_count(candidate["task_id"], "memory.application_recorded")
        self.assertEqual(1, app_events)
        self.delivery(task_id, artifact)

        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
            from xiaoba_agentos.experience_workflow import _verify_trial_qualification
            verdict = _verify_trial_qualification(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("awaiting_evidence", basis["status"])
        self.assertEqual([], basis["method_claims"])
        self.assertEqual("awaiting_evidence", verdict["state"])
        self.assertFalse(verdict["ready"])
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_proposed"))
        code, queue = _run_cli(["--config", str(self.config.path), "review-list", "--stage", "final"])
        self.assertEqual(0, code, queue)
        shown = next(item for item in queue["items"] if item["task_id"] == candidate["task_id"])
        self.assertEqual("awaiting_evidence", shown["experience_validation"]["state"])
        with self.assertRaises(ValueError):
            self.final_approval(candidate, basis)
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))

        with self.assertRaisesRegex(ValueError, "awaiting_evidence.*artifact or code"):
            normalize_method_claims([{"claim": "TEST ambiguous method", "class": "unknown"}], "unused")
        with self.assertRaisesRegex(ValueError, "awaiting_evidence"):
            normalize_method_claims(None, "TEST steps do not determine route")

    def test_publication_cli_cleanly_rejects_stale_incomplete_basis(self):
        import uuid

        candidate = self.candidate()
        task_id = self.real_task()
        _, artifact = self.application(candidate, task_id)
        self.delivery(task_id, artifact)
        review_id = str(uuid.uuid4())
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE memory_candidates SET status='pending_final_review' WHERE task_id=?",
                         (candidate["task_id"],))
            conn.execute(
                "INSERT INTO memory_reviews(id,task_id,stage,decision,note,candidate_sha256,created_at) "
                "VALUES(?,?,'final','approve','TEST stale imported decision',?,?)",
                (review_id, candidate["task_id"], candidate["evidence_sha256"], 1.0),
            )
            EventStore._append_event_tx(conn, candidate["task_id"], "memory.reviewed", {
                "review_id": review_id, "stage": "final", "decision": "approve",
                "candidate_sha256": candidate["evidence_sha256"], "validation_basis": None,
                "trial_task_id": None,
            })
            conn.commit()

        with self.store.read_only() as conn:
            incomplete = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("awaiting_evidence", incomplete["status"])
        code, result = _run_cli(["--config", str(self.config.path), "publish-memory",
                                 "--task-id", candidate["task_id"]])
        self.assertEqual(1, code, result)
        self.assertNotEqual("published", self.store.get_memory_candidate(candidate["task_id"])["status"])
        self.assertIsNone(self.store.get_memory_candidate(candidate["task_id"])["published_path"])
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.published"))
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_basis_binding_rejects_missing_required_fields_without_keyerror(self):
        with self.assertRaisesRegex(ValueError, "missing binding fields: policy_version"):
            basis_binding({"status": "awaiting_evidence", "reason": "TEST incomplete"})

    def test_legacy_application_without_claims_keeps_the_old_sandbox_gate(self):
        self.store.apply_validation_extension()
        candidate = self.candidate()
        task_id = self.real_task()
        application, artifact = self.application(candidate, task_id, record=False)
        application.pop("method_claims", None)
        application.pop("validation_basis_policy_version", None)
        from xiaoba_agentos.experience_workflow import digest, verify_final_qualification
        application["application_id"] = digest(application)
        with self.store.connect() as conn:
            EventStore._append_event_tx(conn, candidate["task_id"], "memory.application_recorded", application)
        binding = {"application_id": application["application_id"], "real_task_id": task_id,
                   "artifact_sha256": artifact["sha256"]}
        native_trial(self.store, candidate, card={
            "problem_key": "parse-fixture", "experience_application": binding,
        })
        self.delivery(task_id, artifact)
        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
            verdict = verify_final_qualification(conn, candidate["task_id"])
        self.assertEqual("missing", basis["status"])
        self.assertTrue(verdict["qualified"], verdict)
        self.assertEqual("ready_for_final", verdict["state"])
        self.final_approval(candidate, basis)
        with self.store.read_only() as conn:
            event = json.loads(conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.reviewed' "
                "AND json_extract(payload_json,'$.stage')='final'", (candidate["task_id"],),
            ).fetchone()[0])
        self.assertNotIn("method_classification_confirmed", event)

    def test_claims_are_monotonic_and_unknown_classes_fail_closed(self):
        candidate = self.candidate()
        first_task = self.real_task()
        _, first_artifact = self.application(candidate, first_task, [{"claim": "TEST inspect outputs", "class": "artifact"}])
        self.delivery(first_task, first_artifact, "TEST-delivery-1")
        second_task = self.real_task()
        _, second_artifact = self.application(candidate, second_task, [{"claim": "TEST run code", "class": "code"}])
        self.delivery(second_task, second_artifact, "TEST-delivery-2")
        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("combined", basis["route"])
        self.assertEqual({"artifact", "code"}, {claim["class"] for claim in basis["method_claims"]})
        with self.assertRaisesRegex(ValueError, "artifact or code"):
            self.application(candidate, self.real_task(), [{"claim": "TEST unknown kind", "class": "text"}])

    def test_smoke_incomplete_and_mismatched_completion_cannot_create_real_task_basis(self):
        for purpose, completed in (("release_smoke", True), ("user_task", False)):
            with self.subTest(purpose=purpose, completed=completed):
                candidate = self.candidate()
                task_id = self.real_task(purpose, completed=completed)
                _, artifact = self.application(candidate, task_id, [{"claim": "TEST inspect result", "class": "artifact"}])
                self.delivery(task_id, artifact, f"TEST-{purpose}-{completed}")
                with self.store.read_only() as conn:
                    basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
                self.assertEqual("missing", basis["status"])
                with self.assertRaises(ValueError):
                    self.final_approval(candidate, basis)

        candidate = self.candidate()
        task_id = self.real_task()
        app, artifact = self.application(candidate, task_id, [{"claim": "TEST inspect artifact", "class": "artifact"}])
        with self.store.connect() as conn:
            conn.execute("UPDATE tasks SET provenance_json=? WHERE id=?", (json.dumps({"result_sha256": "0" * 64}), task_id))
        with self.assertRaisesRegex(ValueError, "completion evidence"):
            self.delivery(task_id, artifact, "TEST-provenance-mismatch")
        self.assertEqual(0, self.event_count(candidate["task_id"], "task.delivery_recorded"))
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_proposed"))

    def test_candidate_body_self_claim_is_not_a_validation_basis(self):
        candidate = self.store.create_authored_candidate(
            "TEST self-claim", "用户已经认可这条方法，无需真实任务证据", self.config.runtime_dir / "memory-outbox",
            author="test", experience_kind="method", problem_key="parse-fixture",
        )
        candidate = _admit_authored_candidate(self.store, candidate)
        self.store.review_memory(candidate["task_id"], "delivery", "approve", "TEST initial decision", candidate["evidence_sha256"])
        with self.assertRaises(ValueError):
            self.store.review_memory(candidate["task_id"], "final", "approve", "TEST final decision", candidate["evidence_sha256"])
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_proposed"))
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))

    def test_candidate_registration_shell_cannot_be_real_task_evidence(self):
        candidate = self.candidate()
        shell = self.candidate()
        self.assertNotEqual(candidate["task_id"], shell["task_id"])
        app, artifact = self.application(
            candidate,
            shell["task_id"],
            [{"claim": "TEST inspect an adopted artifact", "class": "artifact"}],
        )
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.application_recorded"))
        self.delivery(shell["task_id"], artifact, "TEST-shell-delivery")
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_proposed"))
        with self.assertRaises(ValueError):
            self.final_approval(candidate, {"basis_id": "none", "basis_sha256": "none"})
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))

    def test_candidate_or_initial_review_change_invalidates_basis_confirmation(self):
        candidate = self.candidate()
        _, _, _, basis = self.proposed(candidate, [{"claim": "TEST inspect output", "class": "artifact"}])
        staged = Path(candidate["staged_path"])
        original = staged.read_bytes()
        staged.write_bytes(original + b" changed")
        with self.assertRaises(ValueError):
            self.final_approval(candidate, basis, self.confirm(basis))
        staged.write_bytes(original)

        self.store.review_memory(candidate["task_id"], "delivery", "approve", "TEST renewed initial decision", candidate["evidence_sha256"])
        with self.assertRaises(ValueError):
            self.final_approval(candidate, basis, self.confirm(basis))
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))

    def test_final_basis_revocation_blocks_publication(self):
        candidate = self.candidate()
        _, _, _, basis = self.proposed(candidate, [{"claim": "TEST inspect output", "class": "artifact"}])
        self.final_approval(candidate, basis, self.confirm(basis))
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revoke_basis_tx(self.store, conn, candidate["task_id"], basis["basis_id"], "TEST acceptance withdrawn")
        with self.assertRaisesRegex(ValueError, "basis was revoked"):
            with self.store.publication_transaction(candidate["task_id"]):
                pass

    def test_historical_cli_freezes_complete_pack_as_proposed_without_rewriting_task(self):
        candidate = self.candidate()
        work = self.file_ref("historical-work.json", '{"task":"TEST prior user work"}')
        acceptance = self.file_ref("historical-acceptance.txt", "TEST original user acceptance record")
        artifact = self.file_ref("historical-artifact.txt", "TEST adopted output") | {"version": "v1"}
        execution = self.file_ref("historical-execution.txt", "TEST original execution evidence")
        receipt = {
            "candidate_id": candidate["task_id"], "version": 1,
            "history_pack": {"version": 1, "workspace": str(self.project), "task_id": "historical-task-1",
                "original_task_ref": work["path"], "accepted_at": "TEST asserted 2026-10-15",
                "work_record": work, "original_acceptance": acceptance, "artifact": artifact,
                "execution_evidence": [execution]},
            "method_scope": "TEST the exact artwork adjustments recorded in the work file",
            "method_claims": [{"claim": "TEST inspect the adopted final image", "class": "artifact"}],
            "application_scope": {"description": "TEST historical artwork task", "task_type": "TEST image editing",
                "authorization_ref": acceptance["path"], "application_location": str(self.project),
                "steps_applied": "TEST applied the listed adjustments", "deviations": "TEST none recorded",
                "conditions_checked": {"applicability": "TEST same subject", "exclusions": "TEST none omitted",
                    "environment": "TEST recorded project environment"}},
        }
        receipt_path = self.root / "history-pack.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        with self.store.read_only() as conn:
            before_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        code, result = _run_cli(["--config", str(self.config.path), "memory-validation-basis-propose", candidate["task_id"],
                                 "--receipt", str(receipt_path)])
        self.assertEqual(0, code, result)
        self.assertEqual("proposed", result["status"])
        self.assertEqual("real_task_acceptance", result["route"])
        self.assertEqual(receipt["history_pack"], result["history_pack"])
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))
        with self.store.read_only() as conn:
            after_tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            self.assertIsNone(conn.execute("SELECT 1 FROM tasks WHERE id='historical-task-1'").fetchone())
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual(before_tasks, after_tasks)
        self.assertEqual("proposed", basis["status"])

        Path(work["path"]).write_text('{"task":"TEST altered historical work"}', encoding="utf-8")
        with self.store.read_only() as conn:
            with self.assertRaisesRegex(ValueError, "hash changed"):
                validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))

    def test_batch_of_28_historical_proposals_has_zero_automatic_confirmations(self):
        candidates = [self.candidate() for _ in range(28)]
        proposals = []
        for index in range(28):
            work = self.file_ref(f"batch-work-{index}.json", json.dumps({"task": f"TEST historical work {index}"}))
            acceptance = self.file_ref(f"batch-acceptance-{index}.txt", f"TEST user acceptance {index}")
            artifact = self.file_ref(f"batch-artifact-{index}.txt", f"TEST adopted output {index}") | {"version": "v1"}
            execution = self.file_ref(f"batch-execution-{index}.txt", f"TEST execution evidence {index}")
            proposals.append({
                "version": 1,
                "history_pack": {
                    "version": 1,
                    "workspace": str(self.project),
                    "task_id": f"historical-task-{index}",
                    "original_task_ref": work["path"],
                    "accepted_at": "TEST asserted 2026-10-15",
                    "work_record": work,
                    "original_acceptance": acceptance,
                    "artifact": artifact,
                    "execution_evidence": [execution],
                },
                "method_scope": "TEST the exact steps captured by this historical record",
                "method_claims": [{"claim": "TEST inspect the adopted artifact", "class": "artifact"}],
                "application_scope": {
                    "description": f"TEST historical task {index}",
                    "task_type": "TEST artifact creation",
                    "authorization_ref": acceptance["path"],
                    "application_location": str(self.project),
                    "steps_applied": "TEST applied the listed steps",
                    "deviations": "TEST none recorded",
                    "conditions_checked": {
                        "applicability": "TEST same input family",
                        "exclusions": "TEST none omitted",
                        "environment": "TEST recorded project environment",
                    },
                },
            })

        staged = []
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for candidate, proposal in zip(candidates, proposals):
                basis = propose_historical_basis_tx(
                    self.store, conn, {"task_id": candidate["task_id"]}, proposal
                )
                self.assertEqual("proposed", basis["status"])
                staged.append(basis)

        self.assertEqual(28, len(staged))
        self.assertTrue(all(basis["status"] == "proposed" for basis in staged))
        with self.store.read_only() as conn:
            for candidate in candidates:
                current = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
                self.assertEqual("proposed", current["status"])
                self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))
            placeholders = ",".join("?" for _ in candidates)
            confirmed = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='memory.validation_basis_confirmed' "
                f"AND task_id IN ({placeholders})",
                tuple(candidate["task_id"] for candidate in candidates),
            ).fetchone()[0]
        self.assertEqual(0, confirmed)


    def test_first_confirmed_basis_survives_a_later_same_scope_application(self):
        candidate = self.candidate()
        first_task = self.real_task()
        _, first_artifact = self.application(
            candidate, first_task, [{"claim": "TEST inspect the delivered result", "class": "artifact"}]
        )
        self.delivery(first_task, first_artifact, "TEST-first-confirmed-delivery")
        with self.store.read_only() as conn:
            first = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))

        second_task = self.real_task()
        _, second_artifact = self.application(
            candidate, second_task, [{"claim": "TEST inspect the delivered result", "class": "artifact"}]
        )
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            confirm_basis_tx(self.store, conn, candidate, self.confirm(first))

        self.delivery(second_task, second_artifact, "TEST-second-same-scope-delivery")
        with self.store.read_only() as conn:
            selected = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual(first["basis_id"], selected["basis_id"])
        self.assertEqual("confirmed", selected["status"])
        self.assertEqual(first_task, selected["real_task"]["id"])
        with self.store.read_only() as conn:
            latest = json.loads(conn.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.validation_basis_proposed' ORDER BY sequence DESC LIMIT 1",
                (candidate['task_id'],)).fetchone()[0])
            with self.assertRaisesRegex(ValueError, 'does not match the current evidence'):
                validate_basis(conn, self.store.get_memory_candidate(candidate['task_id']), basis_id=latest['basis_id'])


    def test_revoked_code_claim_does_not_pollute_a_later_artifact_route(self):
        candidate = self.candidate()
        first_task = self.real_task()
        _, first_artifact = self.application(
            candidate, first_task, [{"claim": "TEST execute the code path", "class": "code"}]
        )
        self.delivery(first_task, first_artifact, "TEST-revoked-code-delivery")
        with self.store.read_only() as conn:
            first = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            revoke_basis_tx(self.store, conn, candidate["task_id"], first["basis_id"], "TEST revoked code claim")

        second_task = self.real_task()
        _, second_artifact = self.application(
            candidate, second_task, [{"claim": "TEST inspect the delivered image", "class": "artifact"}]
        )
        self.delivery(second_task, second_artifact, "TEST-new-artifact-after-revoke")
        with self.store.read_only() as conn:
            selected = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("real_task_acceptance", selected["route"])
        self.assertEqual(["real_task_acceptance"], selected["required_checks"])
        self.assertEqual(["artifact"], [item["class"] for item in selected["method_claims"]])


    def test_public_revoke_cli_binds_versions_and_is_idempotent(self):
        candidate = self.candidate()
        _, _, _, basis = self.proposed(
            candidate, [{"claim": "TEST inspect the delivered artifact", "class": "artifact"}]
        )
        cli = ["--config", str(self.config.path)]
        request = {
            "version": 1, "candidate_id": candidate["task_id"],
            "candidate_sha256": candidate["evidence_sha256"],
            "initial_review_id": basis["initial_review_id"], "basis_id": basis["basis_id"],
            "basis_sha256": basis["basis_sha256"], "decision": "revoke",
            "reason": "TEST user withdrew this exact validation basis",
            "user_decision_ref": "TEST user decision receipt", "idempotency_key": "TEST-revoke-key-1",
        }
        receipt_path = self.root / "revoke.json"
        stale = dict(request, basis_sha256="0" * 64)
        receipt_path.write_text(json.dumps(stale), encoding="utf-8")
        code, blocked = _run_cli(cli + ["memory-validation-basis-revoke", candidate["task_id"],
                                        "--receipt", str(receipt_path)])
        self.assertEqual(1, code)
        self.assertIn("stale validation-basis revocation", blocked["error"])
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_revoked"))

        receipt_path.write_text(json.dumps(request), encoding="utf-8")
        code, revoked = _run_cli(cli + ["memory-validation-basis-revoke", candidate["task_id"],
                                        "--receipt", str(receipt_path)])
        self.assertEqual(0, code, revoked)
        self.assertEqual(basis["basis_id"], revoked["basis_id"])
        code, replay = _run_cli(cli + ["memory-validation-basis-revoke", candidate["task_id"],
                                       "--receipt", str(receipt_path)])
        self.assertEqual(0, code, replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_revoked"))
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_revocation_requested"))

        conflicted = dict(request, reason="TEST changed reason under an existing retry identity")
        receipt_path.write_text(json.dumps(conflicted), encoding="utf-8")
        code, blocked = _run_cli(cli + ["memory-validation-basis-revoke", candidate["task_id"],
                                        "--receipt", str(receipt_path)])
        self.assertEqual(1, code)
        self.assertIn("already used for different input", blocked["error"])


    def test_public_application_strips_client_controlled_classification(self):
        candidate = self.candidate()
        value, _ = self.application(candidate, self.real_task(),
            [{"claim": "TEST inspect output", "class": "artifact"}], record=False)
        value.update(formal_reuse=True, normal_retrieval=True, application_id='TEST forged',
                     trial_binding={'TEST': 'forged'}, validation_basis_policy_version=99)
        receipt = self.root / 'forged-classification.json'
        receipt.write_text(json.dumps(value), encoding='utf-8')
        code, result = _run_cli(['--config', str(self.config.path), 'memory-application-record', '--receipt', str(receipt)])
        self.assertEqual(0, code, result)
        self.assertFalse(result.get('formal_reuse', False))
        self.assertFalse(result['normal_retrieval'])
        self.assertEqual(1, result['validation_basis_policy_version'])
        self.assertNotEqual('TEST forged', result['application_id'])
        with self.store.read_only() as conn:
            recorded = json.loads(conn.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.application_recorded'",
                (candidate['task_id'],)).fetchone()[0])
        self.assertNotIn('formal_reuse', recorded)
        self.assertNotIn('normal_retrieval', recorded)
        self.assertNotIn('trial_binding', recorded)


    def test_public_withdrawal_with_or_without_claims_never_calls_accept_only_proposer(self):
        for index, claims in enumerate((None, [{"claim": "TEST inspect output", "class": "artifact"}])):
            candidate = self.candidate()
            task = self.real_task()
            _, artifact = self.application(candidate, task, claims)
            self.delivery(task, artifact, 'TEST-accept-' + str(index))
            before = self.event_count(candidate['task_id'], 'memory.validation_basis_proposed')
            receipt = self.root / ('withdraw-%s.json' % index)
            receipt.write_text(json.dumps({
                'origin': 'user', 'user_message': '撤回验收', 'decision': 'withdraw',
                'message_id': 'TEST-withdraw-' + str(index), 'source_ref': 'TEST direct user withdrawal',
                'real_task_id': task, 'artifact': artifact,
                'delivery_record': self.file_ref('withdrawal.txt', 'TEST original withdrawal'),
            }), encoding='utf-8')
            with patch('xiaoba_agentos.validation_basis.propose_from_delivery_tx') as proposer:
                code, result = _run_cli(['--config', str(self.config.path), 'intake', '撤回验收',
                    '--user-decision', '--message-id', 'TEST-withdraw-' + str(index), '--delivery-receipt', str(receipt)])
            self.assertEqual(0, code, result)
            proposer.assert_not_called()
            self.assertEqual(before, self.event_count(candidate['task_id'], 'memory.validation_basis_proposed'))
        self.assertTrue(self.store.verify_event_chain()['ok'])


    def test_withdrawn_first_task_does_not_hide_another_valid_task_basis(self):
        candidate = self.candidate()
        first_task, _, first_artifact, first = self.proposed(candidate,
            [{"claim": "TEST inspect output", "class": "artifact"}])
        second_task = self.real_task()
        _, second_artifact = self.application(candidate, second_task,
            [{"claim": "TEST inspect output", "class": "artifact"}])
        self.delivery(second_task, second_artifact, 'TEST-second-task-delivery')
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            confirm_basis_tx(self.store, conn, candidate, self.confirm(first))
        record_delivery(self.store, {
            'origin': 'user', 'user_message': '撤回验收', 'decision': 'withdraw',
            'message_id': 'TEST-withdraw-first', 'source_ref': 'TEST direct user event',
            'real_task_id': first_task, 'artifact': first_artifact,
            'delivery_record': self.file_ref('withdrawal.txt', 'TEST user withdrew first task'),
        })
        with self.store.read_only() as conn:
            selected = validate_basis(conn, self.store.get_memory_candidate(candidate['task_id']))
        self.assertEqual(second_task, selected['real_task']['id'])
        self.assertEqual('proposed', selected['status'])

    def test_pre_v1616_completed_user_task_without_task_purpose_supports_delivery_withdrawal_basis_and_reuse(self):
        """Pre-v1.6.16 completed ordinary user tasks without task_purpose work across delivery, withdrawal, basis and reuse scoring."""
        from xiaoba_agentos.delivery import completed_task_tx, detail_tx, legacy_request, pending_tasks
        from xiaoba_agentos.reuse_scores import projection_tx

        formal_cand = self.candidate()
        vault = self.root / "vault-formal"
        vault.mkdir(exist_ok=True)
        formal_path = vault / f"formal-{formal_cand['task_id']}.md"
        formal_path.write_text(
            f"---\nstatus: active\nsource_task_id: {formal_cand['task_id']}\n---\nTEST formal method",
            encoding="utf-8",
        )
        formal_sha = hashlib.sha256(formal_path.read_bytes()).hexdigest()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE memory_candidates SET status='published',published_path=?,published_at=1 WHERE task_id=?",
                (str(formal_path), formal_cand["task_id"]),
            )

        # 1. Pre-v1.6.16 snapshot without task_purpose (both with method policy and legacy pre-v1.6.5 without policy)
        for index, snap in enumerate((
            {"experience_policy": {"version": 1, "kind": "method", "problem_key": "parse-fixture"}},
            {"prompt": "TEST legacy v1.5 user task"},
        )):
            candidate = self.candidate()
            legacy_task = self.store.submit(f"TEST pre-v1.6.16 task {index}", 2, snapshot=snap)
            self.store.complete(
                legacy_task,
                f"TEST pre-v1.6.16 completed result citing {formal_cand['task_id']}",
                expected_attempt=0,
            )
            _, artifact = self.application(
                candidate, legacy_task, [{"claim": "TEST inspect delivered output", "class": "artifact"}]
            )
            formal_app, _ = self.application(formal_cand, legacy_task, record=False)
            formal_app["candidate_sha256"] = formal_sha
            formal_app[" artifact"] = artifact
            formal_app.pop(" artifact", None)
            formal_app["artifact"] = artifact
            formal_app["reuse_assessment"] = {"outcome": "unchanged", "changed": "", "added": "", "discarded": ""}
            record_application(self.store, formal_app)

            with self.store.read_only() as conn:
                comp = completed_task_tx(conn, legacy_task)
                self.assertEqual("native", comp["completion_kind"])
                det = detail_tx(conn, legacy_task, artifact)
                self.assertIn("accept", det["allowed_decisions"])
            pending = pending_tasks(self.store)
            self.assertEqual([], pending["problems"])
            self.assertIn(legacy_task, [item["task_id"] for item in pending["items"]])
            req = legacy_request(self.store, {
                "user_message": "交付", "real_task_id": legacy_task, "artifact": artifact,
                "message_id": f"TEST-legacy-req-{index}", "source_ref": "TEST chat",
            })
            self.assertEqual("accept", req["decision"])

            self.delivery(legacy_task, artifact, f"TEST-legacy-accept-{index}")
            with self.store.read_only() as conn:
                basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
                score = projection_tx(conn).get(formal_cand["task_id"], {}).get("score", 0)
            self.assertEqual("proposed", basis["status"])
            self.assertEqual(legacy_task, basis["real_task"]["id"])
            self.assertEqual(index + 1, score)

            # Withdrawal also works and reverses the reuse score
            record_delivery(self.store, {
                "origin": "user", "user_message": "撤回验收", "decision": "withdraw",
                "message_id": f"TEST-legacy-withdraw-{index}", "source_ref": "TEST chat",
                "real_task_id": legacy_task, "artifact": artifact,
                "delivery_record": self.file_ref(f"withdraw-{index}.md", "TEST user withdrawal"),
            })
            with self.store.read_only() as conn:
                score_after = projection_tx(conn).get(formal_cand["task_id"], {}).get("score", 0)
            self.assertEqual(index, score_after)
            # Re-accept so next iteration increments from index+1
            self.delivery(legacy_task, artifact, f"TEST-legacy-reaccept-{index}")

        # 2. Pre-v1.6.16 non-user tasks without task_purpose remain strictly rejected
        non_user_snapshots = (
            ("audit_policy", "TEST audit prompt", {"experience_policy": {"version": 1, "kind": "audit", "problem_key": ""}}),
            ("release_acceptance", "TEST release acceptance", {"release_acceptance": True}),
            ("review_ctx_acceptance", "TEST review_context acceptance", {"review_context": {"release_acceptance": True}}),
            ("experience_trial", "TEST trial review", {"experience_trial": {"source_task_id": "src-1"}}),
            ("collab_review", "TEST collab review", {"workflow": {"version": 1, "phase": "review", "agents": ["a"]}}),
            ("skill_audit_prompt", "你是小八 AgentOS 的独立审查者之一，只读审查技能 demo 的改动", {}),
        )
        for label, prompt_text, bad_snap in non_user_snapshots:
            with self.subTest(label=label):
                bad_task = self.store.submit(prompt_text, 2, snapshot={})
                raw_snap = json.dumps(bad_snap, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                snap_hash = hashlib.sha256(raw_snap.encode("utf-8")).hexdigest()
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET execution_snapshot_json=?,execution_snapshot_hash=? WHERE id=?",
                        (raw_snap, snap_hash, bad_task),
                    )
                self.store.complete(bad_task, "TEST non-user result", expected_attempt=0)
                with self.store.read_only() as conn:
                    with self.assertRaisesRegex(ValueError, "completed ordinary user task"):
                        completed_task_tx(conn, bad_task)
                bad_cand = self.candidate()
                _, bad_art = self.application(
                    bad_cand, bad_task, [{"claim": "TEST inspect result", "class": "artifact"}]
                )
                self.delivery(bad_task, bad_art, f"TEST-bad-delivery-{label}")
                with self.store.read_only() as conn:
                    basis = validate_basis(conn, self.store.get_memory_candidate(bad_cand["task_id"]))
                self.assertEqual("missing", basis["status"])


if __name__ == "__main__":
    unittest.main()
