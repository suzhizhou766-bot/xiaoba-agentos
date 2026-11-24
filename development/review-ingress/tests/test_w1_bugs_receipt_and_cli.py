"""W1-Bug-1, W1-Bug-3 & staged-material evidence_refs regression tests."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from xiaoba_review.agent_workspace import AgentWorkspace
from xiaoba_review.resource_agy import ReviewReceiptError, parse_review_receipt
from xiaoba_review.resource_cli import CliWorklogResource
from xiaoba_review.workday import strip_ref_anchor, verified_staged_evidence_map


class W1BugsIngressTests(unittest.TestCase):
    def _sample_card(self):
        return {
            "receipt_contract": {"version": 2},
            "evidence_refs": [
                {"ref": "/tmp/outbox/ba304a1e.md", "sha256": "a" * 64},
            ],
        }

    def _sample_value(self, refs):
        return {
            "status": "completed",
            "body": "Valid review body.",
            "evidence_refs": refs,
            "metadata": {
                "审查结论": "PASS",
                "审查类型": "new_experience",
                "评分": "7",
                "适用条件": "适用于日常任务台账记录，不适用于未验证草稿。",
                "重复经验": "无",
            },
        }

    def test_w1_bug1_accepts_line_anchors_and_dispatch_card_supplement(self):
        """W1-Bug-1 (← 2026-11-23): #L1-L44 anchor and supplemental 派工卡-*.md are accepted & normalized."""
        card = self._sample_card()
        val = self._sample_value([
            "/tmp/outbox/ba304a1e.md#L1-L44",
            "/tmp/work/派工卡-41e080d0047d456297859320014ac68a.md#L1-L80",
        ])
        parsed = parse_review_receipt(val, card, {"call_id": "c1"})
        self.assertEqual(parsed["verdict"], "PASS")
        self.assertEqual(parsed["score"], 7)
        self.assertEqual(parsed["evidence_refs"], ["/tmp/outbox/ba304a1e.md"])

    def test_w1_bug1_rejects_dispatch_card_only_without_real_evidence_ref(self):
        """Security boundary: citing ONLY 派工卡-*.md cannot substitute for candidate evidence."""
        card = self._sample_card()
        val = self._sample_value(["/tmp/work/派工卡-41e080d0047d456297859320014ac68a.md"])
        with self.assertRaises(ReviewReceiptError) as ctx:
            parse_review_receipt(val, card, {"call_id": "c2"})
        self.assertEqual(ctx.exception.code, "review_fields_missing")
        self.assertIn("证据引用未匹配任务卡", str(ctx.exception))

    def test_w1_bug1_rejects_unlisted_ref_with_specific_diagnostic(self):
        """Unlisted evidence path still fails closed and reports the exact invalid ref."""
        card = self._sample_card()
        val = self._sample_value(["/tmp/other/unrelated.md#L1-L10"])
        with self.assertRaises(ReviewReceiptError) as ctx:
            parse_review_receipt(val, card, {"call_id": "c3"})
        self.assertIn("/tmp/other/unrelated.md#L1-L10", str(ctx.exception))

    def test_staged_material_copy_paths_and_anchors_normalize_to_canonical_card_ref(self):
        """Verified staged read-only 材料/ copies (abs, rel, backticks, Chinese anchors) map back to canonical card_ref."""
        card = self._sample_card()
        staged_abs = "/tmp/ws/AGY-Flash/任务记录/review-123/材料/ba304a1e.md"
        staged_refs = {
            staged_abs: "/tmp/outbox/ba304a1e.md",
            "任务记录/review-123/材料/ba304a1e.md": "/tmp/outbox/ba304a1e.md",
            "材料/ba304a1e.md": "/tmp/outbox/ba304a1e.md",
        }
        val = self._sample_value([
            staged_abs,
            "`材料/ba304a1e.md`（L1–L44）",
            "./材料/ba304a1e.md:1-44",
            "/tmp/ws/AGY-Flash/任务记录/review-123/派工卡-abc.md",
        ])
        parsed = parse_review_receipt(val, card, {"call_id": "c4"}, staged_refs=staged_refs)
        self.assertEqual(parsed["verdict"], "PASS")
        self.assertEqual(parsed["evidence_refs"], ["/tmp/outbox/ba304a1e.md"])

    def test_staged_material_not_in_card_evidence_refs_is_still_rejected(self):
        """Security boundary: a staged material_file not listed in card.evidence_refs cannot bypass card_refs."""
        card = self._sample_card()
        staged_refs = {
            "/tmp/ws/AGY-Flash/任务记录/review-123/材料/extra-frame.png": "/tmp/outbox/extra-frame.png",
        }
        val = self._sample_value(["/tmp/ws/AGY-Flash/任务记录/review-123/材料/extra-frame.png"])
        with self.assertRaises(ReviewReceiptError) as ctx:
            parse_review_receipt(val, card, {"call_id": "c5"}, staged_refs=staged_refs)
        self.assertIn("extra-frame.png", str(ctx.exception))

    def test_strip_ref_anchor_preserves_non_path_colon_identifiers(self):
        self.assertEqual(strip_ref_anchor("task:12345"), "task:12345")
        self.assertEqual(strip_ref_anchor("video-sha256:0123456789"), "video-sha256:0123456789")
        self.assertEqual(strip_ref_anchor("/tmp/a.md:1-44"), "/tmp/a.md")
        self.assertEqual(strip_ref_anchor("`/tmp/a.md`（L1–L44）"), "/tmp/a.md")

    def test_cli_run_and_workday_inputs_accept_verified_staged_copy_and_reject_tampered_copy(self):
        """End-to-end: CliWorklogResource.run normalizes verified 材料/ copy and workday_inputs agrees."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outbox = root / "outbox"
            ws_parent = root / "agent-ws"
            outbox.mkdir()
            ws_parent.mkdir()
            cand = outbox / "ba304a1e.md"
            cand_bytes = b"# Candidate\nRule body.\n"
            cand.write_bytes(cand_bytes)
            cand_sha = hashlib.sha256(cand_bytes).hexdigest()

            source = {
                "kind": "peer",
                "reviewer_id": "node-agy",
                "source_ref": "agent-entry:agy-test",
                "node": {"name": "AGY-Test", "entry": "/bin/echo", "model": "Gemini-Test", "agent_type": "agy"},
            }
            card = {
                "assignment_id": "asg-staged-1",
                "review_request_id": "req-staged-1",
                "step_code": "REVIEW-01",
                "review_type": "new_experience",
                "receipt_contract": {"version": 2},
                "evidence_refs": [{"ref": str(cand), "sha256": cand_sha}],
            }

            class _FakeCliResource(CliWorklogResource):
                label = "AGY-Test"

                def resource_identity(self):
                    return {"provider": "antigravity_cli"}

                def _evidence(self, call_id):
                    return {"call_id": call_id, "provider": "antigravity_cli", "model": self.model}

                def _start(self, directory, ticket, timeout, health_only):
                    self._ticket = ticket

                def _wait(self, ticket, timeout):
                    card_text = ticket["card"].read_text(encoding="utf-8")
                    staged_lines = [
                        line[2:].strip()
                        for line in card_text.split("## 材料副本（只读）\n", 1)[1].splitlines()
                        if line.startswith("- ")
                    ]
                    assert len(staged_lines) == 1
                    log_path = ticket["log"]
                    receipt_block = (
                        ticket["header"]
                        + "\n"
                        + ticket["marker"]
                        + f"\n任务ID: {ticket['task_id']}\n步骤码: {ticket['step_code']}\n状态: completed\n"
                        + "审查结论: PASS\n"
                        + f"证据引用: {staged_lines[0]}\n"
                        + f"证据引用: {ticket['card']}\n"
                        + "审查类型: new_experience\n评分: 8\n适用条件: 仅用于回归测试。\n重复经验: \n"
                        + "---\n完整独立审查正文。\n"
                        + ticket["done"]
                        + "\n"
                    )
                    log_path.write_text(receipt_block, encoding="utf-8")
                    return self.workday.read(ticket), False

            adapter = _FakeCliResource(source, workspace_root=ws_parent)
            parsed = adapter.run(card)
            self.assertEqual(parsed["verdict"], "PASS")
            self.assertEqual(parsed["score"], 8)
            self.assertEqual(parsed["evidence_refs"], [str(cand)])

            # Daily workday_inputs must agree with parse_review_receipt on the saved receipt.
            aw = AgentWorkspace(ws_parent, source)
            day = parsed["execution_evidence"]["worklog"]["date"]
            items = aw.workday_inputs(day)
            self.assertEqual(len(items), 1)
            self.assertTrue(items[0]["review_qualified"])

            # Security boundary: if the staged copy on disk is tampered with, verified_staged_evidence_map refuses it.
            record = items[0]["record"]
            staged_dir = aw.path / "任务记录" / record / "材料"
            staged_file = staged_dir / cand.name
            os.chmod(staged_dir, 0o700)
            os.chmod(staged_file, 0o600)
            staged_file.write_bytes(b"TAMPERED")
            self.assertEqual(verified_staged_evidence_map(aw.path, record, card), {})
            tampered_items = aw.workday_inputs(day)
            self.assertFalse(tampered_items[0]["review_qualified"])

    def test_cli_run_task_normalizes_staged_copy_back_to_declared_path(self):
        """CliWorklogResource.run_task maps verified 材料/ copy back to original declared path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outbox = root / "outbox"
            ws_parent = root / "agent-ws"
            outbox.mkdir()
            ws_parent.mkdir()
            cand = outbox / "doc.md"
            cand_bytes = b"Declared material.\n"
            cand.write_bytes(cand_bytes)
            cand_sha = hashlib.sha256(cand_bytes).hexdigest()

            source = {
                "kind": "both",
                "reviewer_id": "node-qoder",
                "source_ref": "agent-entry:qoder-test",
                "node": {"name": "Qoder-Test", "entry": "/bin/echo", "model": "Qwen-Test", "agent_type": "qoder"},
            }
            materials = {"material_files": [{"path": str(cand), "sha256": cand_sha}]}

            class _FakeTaskCli(CliWorklogResource):
                label = "Qoder-Test"

                def resource_identity(self):
                    return {"provider": "qoder_cli"}

                def _evidence(self, call_id):
                    return {"call_id": call_id, "provider": "qoder_cli", "model": self.model}

                def _start(self, directory, ticket, timeout, health_only):
                    pass

                def _wait(self, ticket, timeout):
                    card_text = ticket["card"].read_text(encoding="utf-8")
                    staged_path = [
                        line[2:].strip()
                        for line in card_text.split("## 材料副本（只读）\n", 1)[1].splitlines()
                        if line.startswith("- ")
                    ][0]
                    ticket["log"].write_text(
                        ticket["header"]
                        + "\n"
                        + ticket["marker"]
                        + f"\n任务ID: {ticket['task_id']}\n步骤码: {ticket['step_code']}\n状态: completed\n"
                        + f"证据引用: {staged_path}#L1-L1\n"
                        + "证据引用: task:t-1\n"
                        + "---\nDone.\n"
                        + ticket["done"]
                        + "\n",
                        encoding="utf-8",
                    )
                    return self.workday.read(ticket), False

            adapter = _FakeTaskCli(source, workspace_root=ws_parent)
            res = adapter.run_task("t-1", "WORK-01", "Check material", materials)
            self.assertEqual(res["evidence_refs"], [str(cand), "task:t-1"])

    def test_w1_bug3_failure_evidence_retains_exit_code_without_raw_output(self):
        """W1-Bug-3 (← 2026-11-23): _failure_evidence preserves integer exit_code cleanly."""
        dummy = object.__new__(CliWorklogResource)
        dummy._evidence = lambda call_id: {"call_id": call_id, "provider": "antigravity_cli"}
        exc = RuntimeError("cli_failed")
        exc.failure_class = "permanent"
        exc.exit_code = 1
        ev = dummy._failure_evidence(exc, "call-1")
        self.assertEqual(ev["failure_class"], "permanent")
        self.assertEqual(ev["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
