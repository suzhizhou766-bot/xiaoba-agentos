"""Unit tests for authoritative verification gate and two-round state machine.

Tests cover:
1. Unified evidence contract (real execution + independent dual receipts).
2. Two-round state machine:
   - Round 1 PASS -> pending_final_review
   - Round 1 FAIL -> revalidation_required
   - Round 2 PASS with valid diff -> pending_final_review
   - Round 2 FAIL -> retired (excluded from reuse)
   - Round 2 PASS without valid diff -> rejected / not ready
3. No third-round loophole: MaxValidationRoundsExceeded enforced.
4. Content drift invalidates old PASS while preserving failure and round history.
5. External pause failures (tools, network, auth) pause and preserve round without consuming it.
6. Substantive FAIL or hygiene leaks block qualification.
7. Final approval remains an independent user action (no auto-approval).
8. Legacy records migration marks ambiguous history as needs_human_check.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from xiaoba_agentos.store import EventStore
from xiaoba_agentos.review_flow import execute_review
from xiaoba_agentos.experience_workflow import (
    verify_final_qualification,
    advance_validation_round,
    resume_paused_validation,
    submit_diff_materials,
    validate_diff_materials,
    migrate_legacy_validation_records,
    trial_summary,
    require_final_trial,
    link_trial_tx,
    validate_diff_explanation,
    MaxValidationRoundsExceeded,
)


class AuthoritativeGateAndStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "agentos.sqlite3"
        self.outbox = self.root / "memory-outbox"
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.store = EventStore(self.db_path)
        self.store.initialize()
        self.store.apply_validation_extension()

    def create_candidate(self, title="测试方法经验", body="# 方法正文\n测试实现步骤\n", kind="method"):
        cand = self.store.create_authored_candidate(
            title=title,
            body=body,
            outbox=self.outbox,
            author="tester",
            experience_kind=kind,
        )
        # 初始初审通过
        self.store.review_memory(
            task_id=cand["task_id"],
            stage="delivery",
            decision="approve",
            note="初审合格，准予开展验证",
            candidate_sha256=cand["evidence_sha256"],
        )
        return self.store.get_memory_candidate(cand["task_id"])

    def dummy_execution(self, passed=True):
        return {
            "passed": passed,
            "started_at": time.time(),
            "finished_at": time.time() + 1,
            "cases": [
                {
                    "name": "case-1",
                    "passed": passed,
                    "returncode": 0 if passed else 1,
                    "expect_stdout": "SUCCESS",
                    "output": "SUCCESS" if passed else "FAILED",
                }
            ],
        }

    def test_round1_pass_advances_to_pending_final_review(self):
        """首轮验证全面通过：推进到待终审 (pending_final_review)。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res = advance_validation_round(
                self.store, conn, cand_id,
                {
                    "execution": self.dummy_execution(True),
                    "dsh_verdict": "PASS",
                    "peer_verdict": "PASS",
                    "dsh_output": "审查合格，无外泄",
                    "peer_output": "复核通过，符合预期",
                }
            )
            conn.commit()

        self.assertTrue(res["ok"])
        self.assertEqual(res["round"], 1)
        self.assertEqual(res["status"], "passed")
        self.assertEqual(res["candidate_status"], "pending_final_review")

        # 检查权威门禁
        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertTrue(verdict["qualified"])
            self.assertTrue(verdict["ready"])
            self.assertEqual(verdict["round"], 1)

            # require_final_trial 正常放行
            task_id = require_final_trial(conn, cand)
            self.assertIsNotNone(task_id)

    def test_round1_fail_advances_to_revalidation_required(self):
        """首轮验证失败：推进到待重新验证 (revalidation_required)。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res = advance_validation_round(
                self.store, conn, cand_id,
                {
                    "execution": self.dummy_execution(False),
                    "dsh_verdict": "FAIL",
                    "peer_verdict": "PASS",
                }
            )
            conn.commit()

        self.assertTrue(res["ok"])
        self.assertEqual(res["round"], 1)
        self.assertEqual(res["status"], "failed")
        self.assertEqual(res["candidate_status"], "revalidation_required")

        # 权威门禁拒绝终审
        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])
            self.assertFalse(verdict["ready"])

            with self.assertRaises(ValueError) as ctx:
                require_final_trial(conn, cand)
            self.assertIn("门禁拒绝", str(ctx.exception))

    def test_round2_pass_with_valid_diff_advances_to_pending_final(self):
        """次轮验证通过且附带有效差异材料：推进到待终审 (pending_final_review)。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        # 第 1 轮失败
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "FAIL"}
            )
            conn.commit()

        # 第 2 轮整改，提交有效差异材料（包含五个必要方面）
        valid_diff = {
            "initial_failure": "首轮由于测试用例超时未通过",
            "remediation": "调整了等待策略和执行参数，补齐了依赖夹具",
            "evidence_changes": "次轮补充了独立断言日志和双审回执",
            "outcome_changes": "用例执行全部通过，退出码 0",
            "remaining_limitations": "仅适用于沙箱执行环境",
        }
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res2 = advance_validation_round(
                self.store, conn, cand_id,
                {
                    "execution": self.dummy_execution(True),
                    "dsh_verdict": "PASS",
                    "peer_verdict": "PASS",
                    "diff_explanation": valid_diff,
                }
            )
            conn.commit()

        self.assertTrue(res2["ok"])
        self.assertEqual(res2["round"], 2)
        self.assertEqual(res2["status"], "passed")
        self.assertEqual(res2["candidate_status"], "pending_final_review")
        self.assertEqual(res2["lineage_status"], "ready_for_final")

        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertTrue(verdict["qualified"])
            self.assertEqual(verdict["round"], 2)

    def test_round2_pass_without_valid_diff_blocks_qualification_without_retiring(self):
        """次轮验证通过但缺少有效差异材料：保留通过证据，阻断待终审，补齐后重新检查资格，不新增验证轮次，绝不退役。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        # 第 1 轮失败
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "PASS"}
            )
            conn.commit()

        # 第 2 轮执行及双审通过，但无有效差异材料（传入无效说明）
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res2 = advance_validation_round(
                self.store, conn, cand_id,
                {
                    "execution": self.dummy_execution(True),
                    "dsh_verdict": "PASS",
                    "peer_verdict": "PASS",
                    "diff_explanation": "none",  # 无效说明
                }
            )
            conn.commit()

        # 核心断言：核验记录本身判定为 passed，轮次耗用为 2，但谱系为 awaiting_diff_materials，未退役！
        self.assertEqual(res2["status"], "passed")
        self.assertEqual(res2["candidate_status"], "revalidation_required")
        self.assertEqual(res2["lineage_status"], "awaiting_diff_materials")

        # 门禁阻断终审，状态为 diff_materials_required，绝非 retired
        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])
            self.assertFalse(verdict["ready"])
            self.assertEqual(verdict["state"], "diff_materials_required")
            self.assertIn("差异材料", verdict["reasons"][0])

        # 候选当前状态依然为 revalidation_required，绝非 retired
        cand_now = self.store.get_memory_candidate(cand_id)
        self.assertNotEqual(cand_now["status"], "retired")

        # 补齐材料：调用 submit_diff_materials
        valid_diff = {
            "initial_failure": "首轮由于测试用例断言不符导致失败",
            "remediation": "修复断言匹配逻辑并同步参数",
            "evidence_changes": "执行日志证明断言通过，双审 PASS",
            "outcome_changes": "测试用例通过率 100%",
            "remaining_limitations": "无剩余阻断限制",
        }
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            sub_res = submit_diff_materials(self.store, conn, cand_id, valid_diff)
            conn.commit()

        self.assertTrue(sub_res["ok"])
        self.assertEqual(sub_res["round"], 2)
        self.assertEqual(sub_res["rounds_used"], 2)  # 保持 2 轮，未消耗第 3 轮！
        self.assertEqual(sub_res["candidate_status"], "pending_final_review")
        self.assertEqual(sub_res["lineage_status"], "ready_for_final")

        # 补齐材料后重新核验证格：终审资格顺利通过
        with self.store.read_only() as conn:
            verdict_after = verify_final_qualification(conn, cand_id)
            self.assertTrue(verdict_after["qualified"])
            self.assertTrue(verdict_after["ready"])
            self.assertEqual(verdict_after["state"], "ready_for_final")

    def test_round2_fail_retires_candidate(self):
        """次轮验证明确失败：退役并排除复用 (retired)。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        # 第 1 轮失败
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "FAIL"}
            )
            conn.commit()

        # 第 2 轮再次失败
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res2 = advance_validation_round(
                self.store, conn, cand_id,
                {
                    "execution": self.dummy_execution(False),
                    "dsh_verdict": "FAIL",
                    "peer_verdict": "PASS",
                    "diff_explanation": "重新调整了参数但依然执行失败",
                }
            )
            conn.commit()

        self.assertEqual(res2["round"], 2)
        self.assertEqual(res2["status"], "failed")
        self.assertEqual(res2["candidate_status"], "retired")

        updated = self.store.get_memory_candidate(cand_id)
        self.assertEqual(updated["status"], "retired")

    def test_no_third_round_allowed(self):
        """已耗尽两轮验证的候选，禁止尝试第 3 轮。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "FAIL"}
            )
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(False), "dsh_verdict": "FAIL", "peer_verdict": "FAIL", "diff_explanation": "调整参数尝试"}
            )
            conn.commit()

        # 试图执行第 3 轮
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(MaxValidationRoundsExceeded):
                advance_validation_round(
                    self.store, conn, cand_id,
                    {"execution": self.dummy_execution(True), "dsh_verdict": "PASS", "peer_verdict": "PASS"}
                )

        # 试图通过 link_trial_tx 关联新任务同样被拒绝
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(MaxValidationRoundsExceeded):
                link_trial_tx(self.store, conn, cand_id, "some-new-trial-task")

    def test_external_pause_failure_preserves_round_without_consuming(self):
        """工具、网络或认证异常触发暂停，保留当前轮次，不增加 rounds_used。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res = advance_validation_round(
                self.store, conn, cand_id,
                {
                    "failure_class": "tool_unavailable",
                    "pause_reason": "沙箱工具 sandbox-exec 临时缺失",
                }
            )
            conn.commit()

        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "paused")
        self.assertEqual(res["round"], 1)
        self.assertEqual(res["rounds_used"], 0)  # 未消耗机会！

        # 处于暂停状态，门禁判定为未就绪
        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])
            self.assertEqual(verdict["state"], "validation_paused")

        # 人工恢复
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res_resume = resume_paused_validation(self.store, conn, cand_id)
            conn.commit()

        self.assertTrue(res_resume["ok"])
        self.assertEqual(res_resume["resumed_round"], 1)
        self.assertEqual(res_resume["status"], "pending_round_1")

        # 恢复后正常执行第 1 轮并通过
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

    def test_content_drift_invalidates_prior_pass_evidence(self):
        """候选正文被修改后，哈希不匹配导致历史 PASS 证据失效。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(True), "dsh_verdict": "PASS", "peer_verdict": "PASS"}
            )
            conn.commit()

        # 修改磁盘上的暂存文件
        staged = Path(cand["staged_path"])
        staged.write_text("# 篡改后的正文\n增加了未受审内容\n", encoding="utf-8")

        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])
            self.assertEqual(verdict["state"], "content_drift")

    def test_substantive_fail_or_reasoning_leak_blocks_qualification(self):
        """审查回执包含实质 FAIL 或泄漏思考过程时，门禁坚决拦截。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {
                    "execution": self.dummy_execution(True),
                    "dsh_verdict": "PASS",
                    "peer_verdict": "PASS",
                    "dsh_output": "审查分析：<think>模型内部思考泄露</think>\nTRIAL_VALIDATION: PASS",
                }
            )
            conn.commit()

        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])

    def test_final_review_requires_independent_user_action(self):
        """即便验证已进入 pending_final_review，系统不自动批准，终审仍为独立操作。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            advance_validation_round(
                self.store, conn, cand_id,
                {"execution": self.dummy_execution(True), "dsh_verdict": "PASS", "peer_verdict": "PASS"}
            )
            conn.commit()

        # 检查候选状态是 pending_final_review，绝非 published
        current = self.store.get_memory_candidate(cand_id)
        self.assertEqual(current["status"], "pending_final_review")
        self.assertIsNone(current["published_path"])

    def test_legacy_records_migration_flags_ambiguous_data(self):
        """旧数据迁移：无法证明有效性的记录标记 needs_human_check，不猜测补齐。"""
        cand = self.create_candidate()
        cand_id = cand["task_id"]

        # 制造一条模糊的旧事件
        with self.store.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.store._append_event_tx(conn, cand_id, "memory.trial_linked", {"task_id": "legacy-task-1"})
            self.store._append_event_tx(conn, cand_id, "memory.trial_linked", {"task_id": "legacy-task-2"})
            res = migrate_legacy_validation_records(conn)
            conn.commit()

        self.assertEqual(res["flagged_for_human_check"], 1)

        with self.store.read_only() as conn:
            verdict = verify_final_qualification(conn, cand_id)
            self.assertFalse(verdict["qualified"])
            self.assertEqual(verdict["state"], "awaiting_check")

    def test_validation_extension_migration_and_rollback(self):
        """显式数据库扩展迁移验证：验证旧版兼容、干跑、幂等应用与回滚机制。"""
        unmigrated_db = self.root / "unmigrated.sqlite3"
        raw_store = EventStore(unmigrated_db)
        raw_store.initialize()

        # 1. 普通初始化绝不会隐式创建扩展表或修改 schema
        self.assertFalse(raw_store.is_validation_extension_installed())
        self.assertIsNone(raw_store.get_validation_lineage("dummy-id"))
        self.assertEqual(raw_store.get_validation_records("dummy-id"), [])

        # 2. 干跑（dry_run）只报告计划改动，不产生副作用
        dry = raw_store.apply_validation_extension(dry_run=True)
        self.assertTrue(dry["ok"])
        self.assertTrue(dry["dry_run"])
        self.assertFalse(dry["already_installed"])
        self.assertFalse(raw_store.is_validation_extension_installed())

        # 3. 显式迁移安装
        applied = raw_store.apply_validation_extension(dry_run=False)
        self.assertTrue(applied["ok"])
        self.assertEqual(applied["version"], 1)
        self.assertTrue(raw_store.is_validation_extension_installed())

        # 4. 幂等再次执行
        reapplied = raw_store.apply_validation_extension(dry_run=False)
        self.assertTrue(reapplied["ok"])
        self.assertTrue(reapplied["already_installed"])

        # 5. 回滚恢复
        rolled = raw_store.rollback_validation_extension()
        self.assertTrue(rolled["ok"])
        self.assertTrue(rolled["rolled_back"])
        self.assertFalse(raw_store.is_validation_extension_installed())


if __name__ == "__main__":
    unittest.main()
