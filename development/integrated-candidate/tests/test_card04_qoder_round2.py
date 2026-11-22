"""卡04 Qoder 二轮复核明确缺陷：先红后绿的正反例（临时库，3.14.7 -B）。"""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures_review import _admit_authored_candidate, _config
from fixtures_validation import make_execution, native_trial
from test_pool_dispatch import _seed, _source
from test_validation_rounds_v2 import mixed_trial, review_output, trial_card
from xiaoba_agentos.experience_workflow import complete_audit
from xiaoba_agentos.pool_dispatch import independence_pair, retested_nodes, select_reviewers
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.task_participants import execution_context
from xiaoba_agentos.validation_evidence import (
    decide_round,
    review_independence,
    role_verdicts,
)

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "review-ingress"))
from xiaoba_review.store import DecisionInbox, ReviewStore  # noqa: E402


class Card04QoderRound2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.cfg = _config(self.root)
        self.store = EventStore(self.cfg.database_path)
        self.store.initialize()
        self.store.apply_validation_extension()
        self.outbox = self.cfg.runtime_dir / "memory-outbox"
        self.rs = ReviewStore(self.store, self.outbox)
        self.inbox = DecisionInbox()
        for name, kind in (("TEST-dsh", "dsh"), ("TEST-peer", "peer")):
            self.rs.register_source({
                "reviewer_id": name, "kind": kind, "source_ref": "test://" + name,
                "provenance": "TEST synthetic independent source",
            })

    def candidate(self):
        c = self.store.create_authored_candidate(
            "TEST method", "TEST steps", self.outbox,
            author="test", experience_kind="method", problem_key="parse-fixture",
        )
        c = _admit_authored_candidate(self.store, c)
        self.store.review_memory(
            c["task_id"], "delivery", "approve", "TEST human", c["evidence_sha256"],
        )
        return c

    def request(self, c, task):
        return self.rs.create(
            c["task_id"], "TEST-dsh", "TEST-peer", "TEST真实执行审查",
            [{"ref": "test://execution", "sha256": task["execution_digest"]}],
        )

    def receipts(self, req, verdicts):
        out = {}
        for assignment in req["assignments"]:
            card = assignment["card"]
            data = {k: card[k] for k in (
                "review_request_id", "assignment_id", "reviewer_role", "plan_revision",
                "reviewer_id", "source_ref", "candidate_sha256", "input_digest", "card_digest",
            )}
            data.update(
                receipt_id=assignment["id"] + "-r",
                verdict=verdicts[assignment["role"]],
                findings="TEST证据独立核查",
                evidence_refs=card["evidence_refs"],
                started_at=time.time(), finished_at=time.time(),
            )
            received = self.rs.receive(data)
            self.rs.attest(received["id"], received["digest"], "TEST来源核对")
            out[assignment["role"]] = data
        self.rs.deliver(self.inbox)
        return out

    def test_round2_native_all_fail_with_valid_external_needs_info_pauses(self):
        """P1-A：第二轮原生 FAIL/FAIL + 有效外部 NEEDS_INFO/PASS → 暂停，不退役。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())
        task = mixed_trial(
            self.store, c,
            {"dsh_review": review_output("FAIL"), "agy_review": review_output("FAIL")},
            settle=False,
        )
        req = self.request(c, task)
        self.receipts(req, {"dsh": "NEEDS_INFO", "peer": "PASS"})
        complete_audit(self.store, task["task_id"], "TEST external needs_info must pause")
        lineage = self.store.get_validation_lineage(c["task_id"])
        record = self.store.get_validation_records(c["task_id"])[1]
        self.assertEqual("paused", lineage["lineage_status"])
        self.assertEqual(1, lineage["rounds_used"])
        self.assertEqual("paused", record["status"])
        self.assertEqual("needs_info", record["failure_class"])
        self.assertNotEqual("retired", self.store.get_memory_candidate(c["task_id"])["status"])

    def test_round1_native_needs_info_with_external_fail_consumes_round(self):
        """P1-A：首轮原生 NEEDS_INFO/PASS + 外部 FAIL → 耗失败轮，不是 paused。"""
        c = self.candidate()
        task = mixed_trial(
            self.store, c,
            {"dsh_review": review_output("NEEDS_INFO"), "agy_review": review_output("PASS")},
            settle=False,
        )
        req = self.request(c, task)
        self.receipts(req, {"dsh": "FAIL", "peer": "PASS"})
        complete_audit(self.store, task["task_id"], "TEST external fail consumes round")
        lineage = self.store.get_validation_lineage(c["task_id"])
        record = self.store.get_validation_records(c["task_id"])[0]
        self.assertEqual("pending_round_2", lineage["lineage_status"])
        self.assertEqual(1, lineage["rounds_used"])
        self.assertEqual("failed", record["status"])

    def test_round2_native_all_fail_without_formed_external_still_retires(self):
        """保留：原生全 FAIL + 未形成有效外部包 → 直接退役。"""
        c = self.candidate()
        native_trial(self.store, c, False, card=trial_card())
        task = mixed_trial(
            self.store, c,
            {"dsh_review": review_output("FAIL"), "agy_review": review_output("FAIL")},
            settle=False,
        )
        self.request(c, task)  # waiting_receipts, no receipts
        complete_audit(self.store, task["task_id"], "TEST unformed external")
        self.assertEqual("retired", self.store.get_memory_candidate(c["task_id"])["status"])

    def test_external_real_task_without_local_row_still_excludes_registered(self):
        """P1-B：外部 real_task 可无本机 tasks 行；已登记身份仍排除；本机源缺失才硬拒。"""
        source_id = self.store.submit("TEST source", 1)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.store._append_event_tx(
                db, source_id, "task.participant_recorded",
                {"actor_id": "author-A", "role": "author"},
            )
        trial_id = self.store.submit("TEST trial", 1)
        snap = {
            "prompt": "TEST",
            "experience_trial": {
                "source_task_id": source_id,
                "real_task": {"id": "host:EXT-TASK-1"},
            },
            "review_context": {
                "needs_review": True,
                "host": {"id": "host-H", "entry": "cli:host", "model": "m"},
            },
        }
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET execution_snapshot_json=?,execution_snapshot_hash=? WHERE id=?",
                (json.dumps(snap), "x" * 64, trial_id),
            )
            # Register exclusion for external identity on the candidate itself.
            self.store._append_event_tx(
                db, trial_id, "task.participant_recorded",
                {"actor_id": "ext-executor-X", "role": "executor"},
            )
        context = execution_context(self.store, self.store.get_task(trial_id))
        self.assertIn("author-A", context["excluded_ids"])
        self.assertIn("ext-executor-X", context["excluded_ids"])
        self.assertIn("host:EXT-TASK-1", context["missing_related_task_ids"])

        # Local source_task_ids missing → hard reject.
        broken = self.store.submit("TEST broken", 1)
        bad = {
            "prompt": "TEST",
            "source_task_ids": ["missing-local-source"],
            "review_context": {"needs_review": True},
        }
        with self.store.connect() as db:
            db.execute(
                "UPDATE tasks SET execution_snapshot_json=?,execution_snapshot_hash=? WHERE id=?",
                (json.dumps(bad), "y" * 64, broken),
            )
        with self.assertRaisesRegex(ValueError, "来源任务记录缺失"):
            execution_context(self.store, self.store.get_task(broken))

    def test_dsh_entry_variants_ignore_model(self):
        """DSH：cli:dsh / 路径 / 裸 dsh 一律忽略模型；不同模型仍同源。"""
        a = review_independence("cli:dsh", "model-a", kind="dsh")
        b = review_independence("cli:dsh", "model-b", kind="dsh")
        self.assertEqual(a, b)
        path_a = independence_pair({
            "interface": "cli", "entry": "/opt/dsh", "model": "model-a", "agent_type": "dsh",
        })
        path_b = independence_pair({
            "interface": "cli", "entry": "/opt/dsh", "model": "model-b", "agent_type": "dsh",
        })
        self.assertEqual(path_a, path_b)
        bare = independence_pair({"entry": "dsh", "model": "model-c", "agent_type": "dsh"})
        self.assertEqual(bare[1], "")  # no model segment
        # Counterexample: same cli:dsh token host vs pool with different models → not independent seats.
        host_pair = independence_pair({
            "interface": "cli", "entry": "cli:dsh", "model": "model-a", "agent_type": "dsh",
        })
        pool_pair = independence_pair({
            "interface": "cli", "entry": "cli:dsh", "model": "model-b", "agent_type": "dsh",
        })
        self.assertEqual(host_pair, pool_pair)
        host = {
            "id": "host", "entry": "cli:dsh", "model": "model-a",
            "interface": "cli", "agent_type": "dsh", "independence": host_pair,
        }
        pool = {
            "node_id": "pool-dsh", "entry": "cli:dsh", "model": "model-b",
            "interface": "cli", "agent_type": "dsh", "role": "review",
            "enabled": True, "test_passed": True, "ready_review": True,
            "independence": pool_pair,
        }
        decision = select_reviewers(
            [pool], exclude_ids=set(), host=host, user_chose_host=False,
        )
        # Same DSH program → host cannot count as a second independent seat.
        self.assertNotEqual("run", decision.get("route"))
        self.assertFalse(decision.get("host"))

    def test_retested_nodes_require_ready_review(self):
        """collab-only 不可进 retested_nodes 审查替补。"""
        collab = _source("TEST collab", "cli:TEST collab", "m", kind="collaborator")
        collab["node"]["test"]["finished_at"] = time.time()
        review = _source("TEST review", "cli:TEST review", "m2", kind="review")
        review["node"]["test"]["finished_at"] = time.time()
        _seed(self.store, [collab, review])
        found = retested_nodes(self.store)
        self.assertNotIn("TEST collab", found)
        self.assertIn("TEST review", found)

    def test_fallback_role_verdicts_do_not_fill_dsh_pass(self):
        """fallback 无池票：dsh 列不填 PASS。"""
        columns = role_verdicts({"peer_host_review": "PASS"})
        self.assertIsNone(columns["dsh"])
        self.assertEqual("PASS", columns["peer"])
        # decide_round still pauses via missing_independent_review at workflow layer;
        # columns alone must not look like two-seat PASS.
        outcome, cols = decide_round(1, True, {"peer_host_review": "PASS"}, None)
        self.assertEqual("passed", outcome)  # pure function; pause is workflow gate
        self.assertIsNone(cols["dsh"])

    def test_needs_info_get_does_not_flip_to_waiting_receipts(self):
        """get/prepare：已投递的 NEEDS_INFO 不因查看改成 waiting_receipts。"""
        c = self.candidate()
        task = native_trial(self.store, c, card=trial_card(), settle=False)
        req = self.request(c, task)
        self.receipts(req, {"dsh": "NEEDS_INFO", "peer": "PASS"})
        before = self.rs.get(req["id"])
        self.assertIn(before["state"], {"submitted", "pending_center"})
        after = self.rs.get(req["id"])
        self.assertEqual(before["state"], after["state"])
        self.assertNotEqual("waiting_receipts", after["state"])

    def test_illegal_replace_role_rejected(self):
        c = self.candidate()
        task = native_trial(self.store, c, card=trial_card(), settle=False)
        req = self.request(c, task)
        with self.assertRaisesRegex(ValueError, "只能更换 dsh 或 peer"):
            self.rs.replace_reviewer(req["id"], "TEST-peer", "bad", role="collaborator")


if __name__ == "__main__":
    unittest.main()
