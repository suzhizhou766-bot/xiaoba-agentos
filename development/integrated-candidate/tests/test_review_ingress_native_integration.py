"""Integration tests connecting review-ingress receipts to native final review qualification.

Verifies:
1. Loaded source path audit: ensures tests execute against the isolated worktree candidate.
2. Real review-ingress receipt ingestion -> native state machine advance -> native authoritative gate.
3. End-to-end promotion from delivery review -> dual receipt ingress -> final review -> publication.
4. Idempotency: duplicate receipts and re-submitted diff materials do not corrupt state.
5. Concurrency: serialized database transactions prevent race conditions.
6. Restart persistence: reopening the database preserves complete validation lineages and records.
7. Pause & resume: external environmental failures pause without consuming rounds.
8. Expired & drifted evidence rejection: candidate body or initial review changes invalidate evidence.
9. Retired candidate exclusion: two failed rounds retire candidate and exclude it from reuse or new trials.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

import xiaoba_agentos
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.experience_workflow import (
    verify_final_qualification,
    advance_validation_round,
    resume_paused_validation,
    submit_diff_materials,
    validate_diff_materials,
    require_final_trial,
    link_trial_tx,
    MaxValidationRoundsExceeded,
)
import xiaoba_review
from xiaoba_review.store import ReviewStore, DecisionInbox, digest


class ReviewIngressNativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "agentos.sqlite3"
        self.outbox = self.root / "memory-outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.vault = self.root / "vault"
        self.vault.mkdir(parents=True, exist_ok=True)

        self.store = EventStore(self.db_path)
        self.store.initialize()
        self.store.apply_validation_extension()

        self.review_store = ReviewStore(self.store, self.outbox)
        self.inbox = DecisionInbox(online=True)

        # 登记两家真实、独立的审查方身份（一 DSH、一 Peer）
        self.dsh_source = self.review_store.register_source({
            "reviewer_id": "dsh-reviewer-primary",
            "source_ref": "agentos://dsh/independent-audit",
            "provenance": "local-dsh-service",
            "kind": "dsh",
        })
        self.peer_source = self.review_store.register_source({
            "reviewer_id": "peer-reviewer-cross",
            "source_ref": "agentos://peer/external-peer",
            "provenance": "external-sandbox-peer",
            "kind": "peer",
        })

    def test_loaded_source_paths_audit(self):
        """审计记录：记录测试实际加载的源码路径，确保测试在独立候选目录下执行。"""
        agentos_path = Path(xiaoba_agentos.__file__).resolve()
        review_path = Path(xiaoba_review.__file__).resolve()
        sys.stderr.write(f"\n[SOURCE AUDIT] xiaoba_agentos loaded from: {agentos_path}\n")
        sys.stderr.write(f"[SOURCE AUDIT] xiaoba_review loaded from: {review_path}\n")
        self.assertIn("worktree-v165-audit", str(agentos_path))
        self.assertIn("worktree-v165-audit", str(review_path))

    def create_candidate_with_delivery_approval(self, title="方法经验卡", body="# 正文\n步骤说明\n"):
        cand = self.store.create_authored_candidate(
            title=title,
            body=body,
            outbox=self.outbox,
            author="author-agent",
            experience_kind="method",
        )
        self.store.review_memory(
            task_id=cand["task_id"],
            stage="delivery",
            decision="approve",
            note="交付初审合格，准许开展双审与隔离试验",
            candidate_sha256=cand["evidence_sha256"],
        )
        return self.store.get_memory_candidate(cand["task_id"])

    def dummy_execution(self, passed=True):
        return {
            "passed": passed,
            "started_at": time.time(),
            "finished_at": time.time() + 2,
            "cases": [
                {
                    "name": "integration-case-1",
                    "passed": passed,
                    "returncode": 0 if passed else 1,
                    "expect_stdout": "SUCCESS_VERIFIED",
                    "output": "SUCCESS_VERIFIED\n",
                }
            ],
        }

    def simulate_review_ingress_flow(self, cand, dsh_verdict="PASS", peer_verdict="PASS"):
        """模拟真实的审查接入流程并产出经过 attestation 确认的双审回执。"""
        evidence_item = {
            "ref": "trial://execution-log/run-01",
            "sha256": hashlib.sha256(b"dummy-execution-evidence").hexdigest(),
        }
        req = self.review_store.create(
            candidate_id=cand["task_id"],
            dsh_id="dsh-reviewer-primary",
            peer_id="peer-reviewer-cross",
            scope="全流程规范与隔离执行核验",
            evidence=[evidence_item],
        )

        assignments = {a["role"]: a for a in req["assignments"]}
        dsh_card = assignments["dsh"]["card"]
        peer_card = assignments["peer"]["card"]
        now = time.time()

        dsh_receipt_payload = {
            "receipt_id": "rcpt-dsh-" + cand["task_id"],
            "assignment_id": dsh_card["assignment_id"],
            "review_request_id": req["id"],
            "reviewer_role": "dsh",
            "plan_revision": 1,
            "reviewer_id": "dsh-reviewer-primary",
            "source_ref": self.dsh_source["source_ref"],
            "candidate_sha256": cand["evidence_sha256"],
            "input_digest": dsh_card["input_digest"],
            "card_digest": dsh_card["card_digest"],
            "verdict": dsh_verdict,
            "findings": "DSH审查结论：符合规范，执行正确，无推理外泄" if dsh_verdict == "PASS" else "DSH审查结论：不合格",
            "evidence_refs": [evidence_item],
            "started_at": now,
            "finished_at": now + 5,
        }

        peer_receipt_payload = {
            "receipt_id": "rcpt-peer-" + cand["task_id"],
            "assignment_id": peer_card["assignment_id"],
            "review_request_id": req["id"],
            "reviewer_role": "peer",
            "plan_revision": 1,
            "reviewer_id": "peer-reviewer-cross",
            "source_ref": self.peer_source["source_ref"],
            "candidate_sha256": cand["evidence_sha256"],
            "input_digest": peer_card["input_digest"],
            "card_digest": peer_card["card_digest"],
            "verdict": peer_verdict,
            "findings": "Peer复核结论：执行验证全数通过" if peer_verdict == "PASS" else "Peer复核结论：未达标",
            "evidence_refs": [evidence_item],
            "started_at": now,
            "finished_at": now + 5,
        }

        # 接收回执（初始为 identity_pending）
        dsh_res = self.review_store.receive(dsh_receipt_payload)
        peer_res = self.review_store.receive(peer_receipt_payload)

        # 确权证明（attestation 确认为 valid）
        self.review_store.attest(dsh_res["id"], dsh_res["digest"], "DSH 独立审查凭据核对通过")
        self.review_store.attest(peer_res["id"], peer_res["digest"], "Peer 跨机构复核凭据核对通过")

        # 交付材料箱
        self.review_store.deliver(self.inbox)

        return req, dsh_receipt_payload, peer_receipt_payload

    def test_end_to_end_receipt_ingress_to_native_qualification_and_publication(self):
        """端到端验证：真实 review-ingress 回执接入 -> 原生核验状态机 -> 终审门禁资格 -> 批准终审 -> 正式发布。"""
        cand = self.create_candidate_with_delivery_approval()
        cand_id = cand["task_id"]

        # 1. 产生真实双审回执
        req, dsh_rcpt, peer_rcpt = self.simulate_review_ingress_flow(cand, "PASS", "PASS")

        # 2. 调用原生状态机写入第 1 轮核验结果
        exec_data = self.dummy_execution(True)
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            round_res = advance_validation_round(
                self.store,
                conn,
                cand_id,
                {
                    "candidate_sha256": cand["evidence_sha256"],
                    "task_id": "trial-task-e2e-01",
                    "execution": exec_data,
                    "plan_digest": req["fingerprint"],
                    "execution_digest": digest(exec_data),
                    "dsh_receipt_id": dsh_rcpt["receipt_id"],
                    "peer_receipt_id": peer_rcpt["receipt_id"],
                    "dsh_verdict": dsh_rcpt["verdict"],
                    "peer_verdict": peer_rcpt["verdict"],
                    "dsh_output": dsh_rcpt["findings"],
                    "peer_output": peer_rcpt["findings"],
                }
            )
            conn.commit()

        self.assertTrue(round_res["ok"])
        self.assertEqual(round_res["status"], "passed")
        self.assertEqual(round_res["candidate_status"], "pending_final_review")

        # 3. 权威门禁判断终审资格
        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertTrue(verdict["qualified"])
            self.assertTrue(verdict["ready"])
            self.assertEqual(verdict["round"], 1)

            # require_final_trial 校验
            bound_task = require_final_trial(conn, cand)
            self.assertEqual(bound_task, "trial-task-e2e-01")

        # 4. 执行终审批准
        approved = self.store.review_memory(
            task_id=cand_id,
            stage="final",
            decision="approve",
            note="双审回执与隔离执行证据齐备，予以终审通过",
            candidate_sha256=cand["evidence_sha256"],
        )
        self.assertEqual(approved["status"], "pending_final_review")
        with self.store.read_only() as conn:
            reviews = [dict(r) for r in conn.execute(
                "SELECT * FROM memory_reviews WHERE task_id=? ORDER BY created_at", (cand_id,)
            ).fetchall()]
        self.assertTrue(any(r["stage"] == "final" and r["decision"] == "approve" for r in reviews))

        # 5. 执行正式发布事务（锁定、验质、归档、落盘）
        with self.store.publication_transaction(cand_id, cand["evidence_sha256"]) as pub:
            vault_file = self.vault / f"{cand_id}.md"
            vault_file.write_text(Path(cand["staged_path"]).read_text(encoding="utf-8"), encoding="utf-8")
            pub.settle(str(vault_file))

        published_cand = self.store.get_memory_candidate(cand_id)
        self.assertEqual(published_cand["status"], "published")
        self.assertEqual(published_cand["published_path"], str(vault_file))

    def test_idempotency_of_receipts_and_diff_submission(self):
        """幂等性测试：重复回执接收、重复确权与重复补齐差异材料均不引发冲突或破坏轮次。"""
        cand = self.create_candidate_with_delivery_approval()
        cand_id = cand["task_id"]

        req, dsh_rcpt, _ = self.simulate_review_ingress_flow(cand, "PASS", "PASS")

        # 1. 重复调用 review_store.receive 相同回执
        dup_res = self.review_store.receive(dsh_rcpt)
        self.assertEqual(dup_res["id"], dsh_rcpt["receipt_id"])

        # 2. 推进首轮为失败
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "PASS"}
            )
            conn.commit()

        # 3. 推进第 2 轮通过但缺少差异材料
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(True), "dsh_verdict": "PASS", "peer_verdict": "PASS", "diff_explanation": "none"}
            )
            conn.commit()

        # 4. 补齐合规材料
        diff_payload = {
            "initial_failure": "首轮由于测试用例断言不符导致失败",
            "remediation": "修复断言匹配逻辑并同步参数",
            "evidence_changes": "执行日志证明断言通过，双审 PASS",
            "outcome_changes": "测试用例通过率 100%",
            "remaining_limitations": "无剩余阻断限制",
        }
        res1 = self.store.submit_validation_diff_materials(cand_id, diff_payload)
        self.assertTrue(res1["ok"])
        self.assertEqual(res1["rounds_used"], 2)

        # 重复调用补齐材料（幂等更新）
        res2 = self.store.submit_validation_diff_materials(cand_id, diff_payload)
        self.assertTrue(res2["ok"])
        self.assertEqual(res2["rounds_used"], 2)  # 轮次始终保持 2，不泄露第 3 轮！

    def test_concurrency_serialization(self):
        """并发测试：多线程同时尝试推进轮次或审查候选，在 IMMEDIATE 事务锁下安全串行化。"""
        cand = self.create_candidate_with_delivery_approval()
        cand_id = cand["task_id"]

        def worker_attempt(index):
            try:
                with self.store.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    res = advance_validation_round(
                        self.store,
                        conn,
                        cand_id,
                        {
                            "execution": self.dummy_execution(True),
                            "dsh_verdict": "PASS",
                            "peer_verdict": "PASS",
                            "task_id": f"concurrent-trial-{index}",
                        }
                    )
                    conn.commit()
                    return ("ok", res)
            except Exception as exc:
                return ("error", str(exc))

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker_attempt, i) for i in range(4)]
            results = [f.result() for f in futures]

        # 至少有一次成功推进，且整体状态机保持一致
        successes = [r for r in results if r[0] == "ok"]
        self.assertGreaterEqual(len(successes), 1)

        with self.store.read_only() as conn:
            lineage = conn.execute("SELECT * FROM experience_validation_lineages WHERE candidate_id=?", (cand_id,)).fetchone()
            self.assertIsNotNone(lineage)
            self.assertIn(lineage["lineage_status"], {"ready_for_final", "pending_round_2", "awaiting_diff_materials"})

    def test_restart_and_persistence_recovery(self):
        """重启恢复测试：重新连接数据库后，谱系、轮次记录与门禁判定完好无损。"""
        cand = self.create_candidate_with_delivery_approval()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(True), "dsh_verdict": "PASS", "peer_verdict": "PASS"}
            )
            conn.commit()

        # 重新创建 Store 实例模拟服务重启
        restarted_store = EventStore(self.db_path)
        with restarted_store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertTrue(verdict["qualified"])
            self.assertTrue(verdict["ready"])
            self.assertEqual(verdict["round"], 1)

            lineage = restarted_store.get_validation_lineage(cand_id)
            self.assertIsNotNone(lineage)
            self.assertEqual(lineage["lineage_status"], "ready_for_final")
            self.assertEqual(lineage["rounds_used"], 1)

            records = restarted_store.get_validation_records(cand_id)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "passed")

    def test_pause_and_resume_preserves_round(self):
        """暂停恢复测试：外部工具缺失引发暂停，恢复后轮次不递增，重新验证成功放行。"""
        cand = self.create_candidate_with_delivery_approval()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res_pause = advance_validation_round(
                self.store, conn, cand_id,
                {"failure_class": "network_partition", "pause_reason": "沙箱与节点网络中断"}
            )
            conn.commit()

        self.assertEqual(res_pause["status"], "paused")
        self.assertEqual(res_pause["rounds_used"], 0)

        with self.store.read_only() as conn:
            verdict_paused = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict_paused["qualified"])
            self.assertEqual(verdict_paused["state"], "validation_paused")

        # 恢复暂停
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res_resume = resume_paused_validation(self.store, conn, cand_id)
            conn.commit()

        self.assertEqual(res_resume["status"], "pending_round_1")
        self.assertEqual(res_resume["resumed_round"], 1)

        # 恢复后正常执行首轮
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res_after = advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(True), "dsh_verdict": "PASS", "peer_verdict": "PASS"}
            )
            conn.commit()

        self.assertEqual(res_after["round"], 1)
        self.assertEqual(res_after["rounds_used"], 1)
        self.assertEqual(res_after["status"], "passed")

    def test_expired_and_drifted_evidence_rejection(self):
        """过期及漂移证据拒绝：候选正文变动或初审失效使历史证据全部失效。"""
        cand = self.create_candidate_with_delivery_approval()
        cand_id = cand["task_id"]

        req, _, _ = self.simulate_review_ingress_flow(cand, "PASS", "PASS")

        # 推进首轮通过
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(True), "dsh_verdict": "PASS", "peer_verdict": "PASS"}
            )
            conn.commit()

        # 篡改磁盘暂存文件（发生正文漂移）
        staged_path = Path(cand["staged_path"])
        staged_path.write_text("# 篡改内容\n未经审核的代码注入\n", encoding="utf-8")

        # 1. review-ingress 判定过期
        with self.review_store.transaction() as db:
            req_row = db.execute("SELECT * FROM ri_requests WHERE id=?", (req["id"],)).fetchone()
            is_fresh = self.review_store.fresh(db, req_row)
            self.assertFalse(is_fresh)
            self.review_store.refresh(db, req_row)

        # 2. 原生门禁坚决拒绝
        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])
            self.assertEqual(verdict["state"], "content_drift")
            self.assertIn("正文发生变动", verdict["reasons"][0])

    def test_retired_candidate_exclusion_from_reuse_and_ingress(self):
        """退役候选排除测试：两轮验证明确失败后被退役，排除复用，禁止再补审或启动新试验。"""
        cand = self.create_candidate_with_delivery_approval()
        cand_id = cand["task_id"]

        # 第 1 轮失败
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "FAIL"}
            )
            conn.commit()

        # 第 2 轮再次失败 -> 退役
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res2 = advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "PASS"}
            )
            conn.commit()

        self.assertEqual(res2["candidate_status"], "retired")
        self.assertEqual(res2["lineage_status"], "retired")

        # 1. 终审门禁判定退役并拒绝
        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])
            self.assertEqual(verdict["state"], "retired")

        # 2. 试图通过 link_trial_tx 开启新试验被硬性拦截
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(MaxValidationRoundsExceeded):
                link_trial_tx(self.store, conn, cand_id, "new-rogue-trial")

        # 3. 审查中心试图再次对其创建补审被拒绝
        with self.assertRaises(ValueError) as ctx:
            self.review_store.create(
                candidate_id=cand_id,
                dsh_id="dsh-reviewer-primary",
                peer_id="peer-reviewer-cross",
                scope="试图复活退役经验",
                evidence=[{"ref": "some-ref", "sha256": hashlib.sha256(b"x").hexdigest()}],
            )
        self.assertIn("不能补审", str(ctx.exception))

        # 4. 终审批准拒绝退役候选
        with self.assertRaises(ValueError):
            self.store.review_memory(
                task_id=cand_id,
                stage="final",
                decision="approve",
                note="强行批准退役候选",
                candidate_sha256=cand["evidence_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
