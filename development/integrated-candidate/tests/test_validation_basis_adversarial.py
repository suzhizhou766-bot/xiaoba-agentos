"""J11c negative evidence cases and concurrent final-confirmation settlement."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import threading
import unittest
import uuid
from unittest.mock import patch

import test_validation_basis as fixtures
from xiaoba_agentos.experience_loop import record_application, record_delivery as _record_delivery
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.validation_basis import digest, validate_basis


def record_delivery(store, value):
    from xiaoba_agentos.delivery import user_decision
    return _record_delivery(store, value, authority=user_decision(value, channel="cli_user_declaration", explicit=True))



class ValidationBasisAdversarialTests(unittest.TestCase):
    # Reuse the same public-entry fixture without inheriting and rerunning its tests.
    setUp = fixtures.ValidationBasisTests.setUp
    file_ref = fixtures.ValidationBasisTests.file_ref
    candidate = fixtures.ValidationBasisTests.candidate
    real_task = fixtures.ValidationBasisTests.real_task
    application = fixtures.ValidationBasisTests.application
    delivery = fixtures.ValidationBasisTests.delivery
    event_count = fixtures.ValidationBasisTests.event_count
    final_review_event_count = fixtures.ValidationBasisTests.final_review_event_count
    final_approval = fixtures.ValidationBasisTests.final_approval
    confirm = fixtures.ValidationBasisTests.confirm

    def artifact_basis(self):
        candidate = self.candidate()
        task = self.real_task()
        application, artifact = self.application(
            candidate, task, [{"claim": "TEST inspect the delivered result", "class": "artifact"}]
        )
        self.delivery(task, artifact, "TEST-acceptance-" + candidate["task_id"])
        with self.store.read_only() as conn:
            basis = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        return candidate, task, application, artifact, basis

    def assert_no_confirmation(self, candidate):
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))

    def assert_publication_blocked(self, candidate):
        with self.assertRaises(ValueError):
            with self.store.publication_transaction(candidate["task_id"]):
                self.fail("invalid evidence entered the publication transaction")

    def append_attack_event(self, candidate, event_type, payload):
        # Simulate a malformed import/internal writer while preserving the chain.
        # This isolates semantic validation from the separate broken-chain gate.
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            EventStore._append_event_tx(conn, candidate["task_id"], event_type, payload)
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_symlink_replacement_after_proposal_cannot_be_confirmed(self):
        candidate, _, _, artifact, basis = self.artifact_basis()
        original = Path(artifact["path"])
        target = self.file_ref("replacement-artifact.txt", original.read_text())
        original.unlink()
        original.symlink_to(target["path"])
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.final_approval(candidate, basis, self.confirm(basis))
        self.assert_no_confirmation(candidate)

    def test_same_hash_copy_in_other_workspace_cannot_replace_registered_basis(self):
        """P1反例保留在J11c依据机制上，合法摘要不能代替登记来源绑定。"""
        import shutil

        candidate, _, _, artifact, basis = self.artifact_basis()
        foreign = self.root / "lookalike-workspace"
        shutil.copytree(self.project, foreign)
        forged = copy.deepcopy(basis)
        forged["basis_id"] = str(uuid.uuid4())
        forged["real_task"]["workspace"] = str(foreign)
        forged["artifact"]["path"] = str(foreign / Path(artifact["path"]).name)
        for ref in forged["execution_evidence"]:
            ref["path"] = str(foreign / Path(ref["path"]).name)
        for item in forged["evidence_items"]:
            if item["ref"].startswith(str(self.project) + "/"):
                item["ref"] = str(foreign / Path(item["ref"]).name)
        forged["basis_sha256"] = digest({
            key: value for key, value in forged.items() if key != "basis_sha256"
        })
        self.append_attack_event(candidate, "memory.validation_basis_proposed", forged)
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            self.final_approval(candidate, forged, self.confirm(forged))
        self.assert_no_confirmation(candidate)
        self.assertTrue(self.store.verify_event_chain()["ok"])

    def test_unknown_event_versions_fail_closed_for_all_basis_event_types(self):
        for event_type in (
            "memory.validation_basis_proposed",
            "memory.validation_basis_confirmed",
            "memory.validation_basis_revoked",
        ):
            with self.subTest(event_type=event_type):
                candidate, _, _, _, basis = self.artifact_basis()
                self.append_attack_event(candidate, event_type, {"version": 99})
                with self.assertRaisesRegex(ValueError, "event version"):
                    self.final_approval(candidate, basis, self.confirm(basis))
                self.assertEqual(0, self.final_review_event_count(candidate["task_id"]))

    def test_old_confirmation_replay_cannot_confirm_a_newer_proposal(self):
        candidate, _, _, _, first = self.artifact_basis()
        task = first["real_task"]["id"]
        application, _ = self.application(
            candidate, task, [{"claim": "TEST inspect the delivered result", "class": "artifact"}]
            , record=False
        )
        application["artifact"] = self.file_ref("artifact-v2.txt", "TEST delivered artifact v2") | {"version": "v2"}
        application = record_application(self.store, application)
        self.final_approval(candidate, first, self.confirm(first))
        with self.store.read_only() as conn:
            confirmed = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("confirmed", confirmed["status"])
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))

        # A previously recorded application can receive user acceptance later.
        # A new artifact version produces a distinct, later acceptance proposal.
        self.delivery(task, application["artifact"], "TEST-newer-acceptance")
        with self.store.read_only() as conn:
            current = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertNotEqual(first["basis_id"], current["basis_id"])
        self.assertEqual("proposed", current["status"])
        with self.assertRaisesRegex(ValueError, "does not match the current evidence"):
            self.final_approval(candidate, current, self.confirm(first))
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))
        self.assert_publication_blocked(candidate)

    def test_client_cannot_supply_or_reuse_a_confirmation_id(self):
        candidate, _, _, _, basis = self.artifact_basis()
        supplied = dict(self.confirm(basis), confirmation_id="TEST-reused-client-id")
        with self.assertRaisesRegex(ValueError, "explicit real-task evidence confirmation"):
            self.final_approval(candidate, basis, supplied)
        self.assert_no_confirmation(candidate)
        self.final_approval(candidate, basis, self.confirm(basis))
        with self.store.read_only() as conn:
            row = conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.validation_basis_confirmed'",
                (candidate["task_id"],),
            ).fetchone()
        confirmation = json.loads(row[0])
        self.assertNotEqual("TEST-reused-client-id", confirmation["confirmation_id"])
        # Reusing an existing server id for different evidence cannot revive it.
        changed = dict(confirmation, basis_sha256="0" * 64)
        self.append_attack_event(candidate, "memory.validation_basis_confirmed", changed)
        with self.store.read_only() as conn:
            with self.assertRaisesRegex(ValueError, "confirmation binding changed"):
                validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assert_publication_blocked(candidate)

    def test_concurrent_confirmations_settle_one_confirmation_and_a_publishable_binding(self):
        candidate, _, _, _, basis = self.artifact_basis()
        late_store = EventStore(self.config.database_path)
        original_connect = late_store.connect
        first_started = threading.Event()
        second_committed = threading.Event()

        @contextmanager
        def delayed_connect(*args, **kwargs):
            # The earlier request reaches the transaction after the later request.
            first_started.set()
            if not second_committed.wait(10):
                raise RuntimeError("TEST concurrent review did not complete")
            with original_connect(*args, **kwargs) as conn:
                yield conn

        def first_review():
            with patch.object(late_store, "connect", delayed_connect):
                return late_store.review_memory(
                    candidate["task_id"], "final", "approve", "TEST first concurrent decision",
                    candidate["evidence_sha256"], validation_basis_confirmation=self.confirm(basis),
                )

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(first_review)
            self.assertTrue(first_started.wait(10))
            try:
                self.final_approval(candidate, basis, self.confirm(basis))
            finally:
                second_committed.set()
            pending.result(timeout=10)
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertTrue(self.store.verify_event_chain()["ok"])
        with self.store.publication_transaction(candidate["task_id"]):
            pass

    def test_route_replacement_after_final_approval_cannot_publish(self):
        candidate, _, _, _, basis = self.artifact_basis()
        task = self.real_task()
        _, artifact = self.application(
            candidate, task, [{"claim": "TEST deterministic code execution", "class": "code"}]
        )
        self.final_approval(candidate, basis, self.confirm(basis))
        # Accepting the second application through the ordinary delivery entry
        # adds its code claim and derives a structurally valid combined route.
        self.delivery(task, artifact, "TEST-code-application-accepted-after-final")
        with self.store.read_only() as conn:
            replaced = validate_basis(conn, self.store.get_memory_candidate(candidate["task_id"]))
        self.assertEqual("combined", replaced["route"])
        self.assertEqual(["real_task_acceptance", "sandbox"], replaced["required_checks"])
        self.assertEqual({"artifact", "code"}, {claim["class"] for claim in replaced["method_claims"]})
        self.assertNotEqual(basis["basis_sha256"], replaced["basis_sha256"])
        self.assertEqual("proposed", replaced["status"])
        self.assertTrue(self.store.verify_event_chain()["ok"])
        self.assert_publication_blocked(candidate)
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))
        self.assertEqual(1, self.final_review_event_count(candidate["task_id"]))

    def test_real_task_identity_change_after_final_approval_cannot_publish(self):
        candidate, task, _, _, basis = self.artifact_basis()
        self.final_approval(candidate, basis, self.confirm(basis))
        # Simulate a changed task record without creating a new evidence approval.
        with self.store.connect() as conn:
            conn.execute("UPDATE tasks SET prompt=? WHERE id=?", ("TEST replaced task identity", task))
        self.assert_publication_blocked(candidate)

    def test_latest_invalid_proposal_cannot_fall_back_to_an_older_confirmed_basis(self):
        candidate, _, _, _, basis = self.artifact_basis()
        self.final_approval(candidate, basis, self.confirm(basis))
        invalid = copy.deepcopy(basis)
        invalid["basis_id"] = str(uuid.uuid4())
        invalid["artifact"]["sha256"] = "0" * 64
        invalid["basis_sha256"] = digest({key: value for key, value in invalid.items() if key != "basis_sha256"})
        self.append_attack_event(candidate, "memory.validation_basis_proposed", invalid)
        self.assert_publication_blocked(candidate)
        self.assertEqual(1, self.event_count(candidate["task_id"], "memory.validation_basis_confirmed"))

    def test_proposed_task_execution_and_acceptance_mismatches_cannot_be_confirmed(self):
        for field in ("real_task", "execution_evidence", "delivery_event_id"):
            with self.subTest(field=field):
                candidate, _, _, _, basis = self.artifact_basis()
                altered = copy.deepcopy(basis)
                if field == "real_task":
                    altered[field]["id"] = "TEST-unrelated-task"
                elif field == "execution_evidence":
                    altered[field][0]["sha256"] = "0" * 64
                else:
                    altered[field] = "TEST-unrelated-acceptance-event"
                altered["basis_sha256"] = digest({
                    key: value for key, value in altered.items() if key != "basis_sha256"
                })
                self.append_attack_event(candidate, "memory.validation_basis_proposed", altered)
                with self.assertRaises(ValueError):
                    self.final_approval(candidate, altered, self.confirm(altered))
                self.assert_no_confirmation(candidate)

    def test_renewed_initial_review_rejects_old_application_and_confirmation(self):
        candidate, _, application, _, basis = self.artifact_basis()
        self.store.review_memory(
            candidate["task_id"], "delivery", "approve", "TEST renewed initial review", candidate["evidence_sha256"]
        )
        with self.assertRaisesRegex(ValueError, "current candidate and initial approval"):
            record_application(self.store, application)
        with self.assertRaisesRegex(ValueError, "candidate or initial review changed"):
            self.final_approval(candidate, basis, self.confirm(basis))
        self.assert_no_confirmation(candidate)

    def test_origin_user_without_direct_delivery_and_evidence_does_not_create_a_basis(self):
        candidate = self.candidate()
        task = self.real_task()
        _, artifact = self.application(
            candidate, task, [{"claim": "TEST inspect the adopted result", "class": "artifact"}]
        )
        with self.assertRaisesRegex(ValueError, "only a direct user delivery"):
            record_delivery(self.store, {
                "origin": "user", "message_id": "TEST-forged-origin", "source_ref": "TEST origin field only",
                "real_task_id": task, "artifact": artifact,
            })
        self.assertEqual(0, self.event_count(candidate["task_id"], "memory.validation_basis_proposed"))
        with self.assertRaises(ValueError):
            self.final_approval(candidate, {"status": "missing"})
        self.assert_no_confirmation(candidate)


if __name__ == "__main__":
    unittest.main()
