"""W1-Bug-1 & W1-Bug-3 regression tests (evidence_refs #L anchor normalization & cli_failed exit_code)."""
from __future__ import annotations

import unittest

from xiaoba_review.resource_agy import ReviewReceiptError, parse_review_receipt
from xiaoba_review.resource_cli import CliWorklogResource


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
