import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import AgentResult
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.receipts import ExecutionReceiptStore, ReceiptError
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.task_context import freeze_context
from xiaoba_agentos.workflow import work_plan

import test_orchestrator


class ReceiptGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _create_config_and_store(self, agents=None, minimum=2, budget=2):
        if agents is None:
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1, "template": "SAME_OUTPUT"},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1, "template": "SAME_OUTPUT"},
            ]
        config = test_orchestrator.OrchestratorTest()._config(self.root, agents, minimum=minimum, budget=budget)
        store = EventStore(config.database_path)
        store.initialize()
        return config, store

    def test_corrupt_receipt_file_rejects_overwrite_and_preserves_bytes(self):
        """损坏的收据文件再写入：报错，文件字节不变，且不自动修复。"""
        receipt_path = self.root / "corrupt_receipt.json"
        corrupt_content = b'{"receipts": {"invalid_json_tail": '
        receipt_path.write_bytes(corrupt_content)

        # 1. 尝试直接加载损坏文件：抛出 ReceiptError
        with self.assertRaises(ReceiptError):
            ExecutionReceiptStore(receipt_path)
        self.assertEqual(receipt_path.read_bytes(), corrupt_content)

        # 2. 模拟一个实例在内存中，而磁盘文件被破坏后的写入操作：抛出 ReceiptError，文件字节不变
        valid_path = self.root / "valid_receipt.json"
        store = ExecutionReceiptStore(valid_path)
        store.record_call({
            "task_id": "t1",
            "step_id": "step1",
            "agent": "agent1",
            "tool": "cli",
            "tool_call_id": "call1",
            "command_hash": ExecutionReceiptStore.command_hash("echo 1"),
        })
        self.assertTrue(valid_path.exists())

        # 外部篡改损坏该文件
        valid_path.write_bytes(corrupt_content)
        # 尝试通过现有实例再次写入：必须报错且文件字节不变
        with self.assertRaises(ReceiptError):
            store.associate_result("call1", "res1", exit_code=0)
        self.assertEqual(valid_path.read_bytes(), corrupt_content)

        # 3. 损坏的收据结构（非 dict 或 receipts 字段损坏）
        bad_struct_path = self.root / "bad_struct.json"
        bad_struct_path.write_text(json.dumps(["not", "a", "dict"]))
        with self.assertRaises(ReceiptError):
            ExecutionReceiptStore(bad_struct_path)

    def test_injected_association_failure_prevents_completed_task(self):
        """注入关联失败：任务不是 completed，收据门禁必须校验 is_success 并判定失败。"""
        config, store = self._create_config_and_store(minimum=2, budget=2)
        task_id = store.submit("TEST association failure injection", 2)

        # 模拟关联失败（associate_result 返回 False）
        # 验证门禁在结算时调用 is_success，且任务不被标记为 completed
        with patch.object(ExecutionReceiptStore, "associate_result", return_value=False) as mock_assoc:
            result = Orchestrator(config, store).run_once()

        self.assertNotEqual("completed", result["status"])
        self.assertEqual("failed", result["status"])
        db_task = store.get_task(task_id)
        self.assertNotEqual("completed", db_task["status"])
        self.assertEqual("failed", db_task["status"])
        self.assertTrue(mock_assoc.called)

    def test_gate_calls_is_success_and_fails_if_is_success_false(self):
        """普通任务结算前验证收据：is_success 为 False 时任务必须判定为失败，不写完成。"""
        config, store = self._create_config_and_store(minimum=2, budget=2)
        task_id = store.submit("TEST is_success gate", 2)

        is_success_calls = []
        real_is_success = ExecutionReceiptStore.is_success

        def mock_is_success(store_self, tool_call_id):
            is_success_calls.append(tool_call_id)
            return False

        with patch.object(ExecutionReceiptStore, "is_success", new=mock_is_success):
            result = Orchestrator(config, store).run_once()

        self.assertGreater(len(is_success_calls), 0, "is_success must be called as a gate before completion")
        self.assertEqual("failed", result["status"])
        self.assertEqual("failed", store.get_task(task_id)["status"])
        self.assertIn("execution receipt not successful", result.get("errors", [""])[0])

    def test_receipts_are_distinguishable_across_agents_even_with_identical_output(self):
        """两个 Agent 输出相同文本：收据能区分。"""
        agents = [
            {"name": "agent_alpha", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1, "template": "IDENTICAL_RESPONSE"},
            {"name": "agent_beta", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1, "template": "IDENTICAL_RESPONSE"},
        ]
        config, store = self._create_config_and_store(agents=agents, minimum=2, budget=2)
        task_id = store.submit("TEST same output different agents", 2)

        result = Orchestrator(config, store).run_once()
        self.assertEqual("completed", result["status"])
        self.assertEqual("completed", store.get_task(task_id)["status"])

        receipt_files = list((config.runtime_dir / "receipts" / task_id).glob("*.json"))
        self.assertEqual(2, len(receipt_files))

        entries = []
        for p in receipt_files:
            data = json.loads(p.read_text(encoding="utf-8"))
            for entry in data.get("receipts", {}).values():
                entries.append(entry)

        self.assertEqual(2, len(entries))
        agents_in_receipts = {e["agent"] for e in entries}
        self.assertEqual({"agent_alpha", "agent_beta"}, agents_in_receipts)

        # 核心断言：两个 Agent 哪怕输出相同文本，command_hash 必须能够区分彼此，不能相同
        hashes = {e["command_hash"] for e in entries}
        self.assertEqual(2, len(hashes), "Receipt command_hash must be distinguishable across different agents")

        # 验证关联与绑定字段
        for e in entries:
            self.assertEqual(task_id, e["task_id"])
            self.assertTrue(bool(e["tool_result_id"]))
            self.assertEqual("succeeded", e["status"])
            self.assertEqual(0, e["exit_code"])
            self.assertTrue(bool(e["tool_call_id"]))

    def test_normal_task_settlement_succeeds_with_valid_receipts(self):
        """正常结算不受影响，合格收据正常放行并完成任务。"""
        config, store = self._create_config_and_store(minimum=2, budget=2)
        task_id = store.submit("TEST normal task completion", 2)

        result = Orchestrator(config, store).run_once()
        self.assertEqual("completed", result["status"])
        self.assertEqual("completed", store.get_task(task_id)["status"])
        self.assertIn("winner", result)

    def test_workflow_collaboration_contribution_settlement_unchanged(self):
        """贡献结算保持不变，正常通过。"""
        agents = [
            {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
        ]
        config = test_orchestrator.OrchestratorTest()._config(self.root, agents, minimum=1, budget=4)
        store = EventStore(config.database_path)
        store.initialize()

        plan = work_plan(config, {"a": "part1", "b": "part2"}, "source context")
        task_id = store.submit("workflow fix", 2, snapshot={
            "prompt": "workflow fix",
            "workflow": plan,
            "task_context": freeze_context("source context"),
        })

        class Adapter:
            def __init__(self, agent):
                self.agent = agent
            def run(self, prompt, context):
                return AgentResult(self.agent.name, "patch for " + self.agent.name, 1)

        with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=Adapter):
            outcome = Orchestrator(config, store).run_once()

        self.assertEqual("completed", outcome["status"])
        self.assertEqual("completed", store.get_task(task_id)["status"])
        self.assertEqual("integration", outcome.get("next_stage"))
        self.assertIn("contributions", outcome)
        self.assertEqual(2, len(outcome["contributions"]))

    def test_reused_task_without_receipt_is_not_blocked(self):
        """没有收据的旧任务和重用旧数据不追溯，只管新结算。"""
        agents = [
            {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
        ]
        config = test_orchestrator.OrchestratorTest()._config(self.root, agents, minimum=2, budget=4)
        store = EventStore(config.database_path)
        store.initialize()

        task_id = store.submit("resume task", 3, snapshot={"evidence": "data"})
        store.lease_next(60)

        # 手工造旧 run 数据，不创建收据文件
        run_a = store.start_agent_run(task_id, "a")
        store.finish_agent_run(run_a, "completed", "old completed result A", None, 1)
        run_b = store.start_agent_run(task_id, "b")
        store.finish_agent_run(run_b, "permanent_error", None, "err", 1)

        frozen = store.freeze_task(task_id, failure_class="internal_headroom", failures=[{"agent": "b", "error": "err"}])
        store.record_agent_health("a", "healthy")
        store.record_agent_health("b", "healthy")
        store.resume_task(task_id, frozen["freeze_hash"], enabled_names=["a", "b"], health_max_age_seconds=86400)

        # 这次运行只重跑 b，a 为旧数据重用
        result = Orchestrator(config, store).run_once()
        self.assertEqual("completed", result["status"])
        self.assertEqual("completed", store.get_task(task_id)["status"])

    def test_reused_old_receipts_with_identical_hash_do_not_fail_the_gate(self):
        """升级前的旧收据没带 Agent 和模型，同题时哈希相同；重用它们不能让任务失败。"""
        config, store = self._create_config_and_store()
        task_id = store.submit("TEST reuse old receipts", 2)
        store.lease_next(60)
        old_hash = ExecutionReceiptStore.command_hash("same prompt\ncontext")
        results = []
        for name in ("a", "b"):
            run = store.start_agent_run(task_id, name)
            store.finish_agent_run(run, "completed", "old output", None, 1)
            receipt = ExecutionReceiptStore(config.runtime_dir / "receipts" / task_id / (str(run) + ".json"))
            receipt.record_call({"task_id": task_id, "step_id": "agent_run:" + name, "agent": name,
                                 "tool": "agent_adapter", "tool_call_id": str(run), "command_hash": old_hash})
            receipt.associate_result(str(run), "adapter-result:" + str(run), exit_code=0)
            reused = AgentResult(name, "old output", 1)
            reused.run_id = run
            reused.is_new_run = False
            results.append(reused)
        self.assertIsNone(Orchestrator(config, store)._verify_settlement_receipts(store.get_task(task_id), results))

    def test_corrupt_receipt_error_is_a_value_error_for_evidence_checks(self):
        """协作证据检查按 ValueError 走失败路径；损坏的收据不能绕过它变成 Worker 错误。"""
        path = self.root / "corrupt.json"
        path.write_bytes(b"{")
        with self.assertRaises(ValueError):
            ExecutionReceiptStore(path)

    def test_card7_item14_reproduction_exact(self):
        """完全还原卡 7 第 14 项复现逻辑，验证所有缺陷点均已修复。"""
        config, store = self._create_config_and_store(minimum=2, budget=2)
        tid = store.submit("TEST receipt audit", 2)
        with patch.object(ExecutionReceiptStore, "is_success", side_effect=AssertionError("must gate")) as gate, \
             patch.object(ExecutionReceiptStore, "associate_result", return_value=False) as associate:
            result = Orchestrator(config, store).run_once()
        files = list((config.runtime_dir / "receipts" / tid).glob("*.json"))
        entries = [next(iter(json.loads(p.read_text(encoding="utf-8"))["receipts"].values())) for p in files]

        # 修复前：status 是 completed，任务被错误结算完成；修复后：状态为 failed
        self.assertEqual("failed", result["status"])
        self.assertEqual("failed", store.get_task(tid)["status"])

        # 修复前：is_success_calls 为 0；修复后：is_success 作为完成门禁被显式调用
        self.assertGreater(gate.call_count, 0)
        self.assertEqual(2, associate.call_count)

        # 修复前：same_hash_across_agents 为 True；修复后：能区分不同 Agent，为 False
        same_hash = (len({v["command_hash"] for v in entries}) == 1)
        self.assertFalse(same_hash)


if __name__ == "__main__":
    unittest.main()
