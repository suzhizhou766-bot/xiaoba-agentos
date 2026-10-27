import json
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.handoff import (
    block_with_reason,
    empty_handoff,
    read_handoff,
    require_real_evidence,
    verify_completed,
    write_handoff,
)


class HandoffTest(unittest.TestCase):
    def test_completed_is_reverified_before_trust(self):
        state = empty_handoff("t1", "dsh_review", "agy_review")
        state["completed"] = ["step_a", "step_b"]
        verified, reverted = verify_completed(state, lambda item: item == "step_a")
        self.assertEqual(["step_a"], verified)
        self.assertEqual(["step_b"], reverted)
        self.assertIn("step_b", state["pending"])

    def test_reverify_failure_moves_item_to_pending(self):
        state = empty_handoff("t1", "dsh_review", "agy_review")
        state["completed"] = ["step_x"]
        verify_completed(state, lambda item: False)
        self.assertEqual([], state["completed"])
        self.assertEqual(["step_x"], state["pending"])

    def test_block_records_concrete_reason(self):
        state = empty_handoff("t1", "a", "b")
        state["pending"] = ["publish"]
        block_with_reason(state, "publish", "requires_native_web_search")
        self.assertEqual([], state["pending"])
        self.assertEqual("requires_native_web_search", state["blocked"][0]["reason"])

    def test_require_real_evidence_fails_closed(self):
        state = empty_handoff("t1", "a", "b")
        ok = require_real_evidence(state, "step_z", has_evidence=False,
                                   reason="missing_real_tool_execution")
        self.assertFalse(ok)
        self.assertTrue(any(b["item"] == "step_z" and "missing_real_tool_execution" in b["reason"]
                            for b in state["blocked"]))

    def test_read_write_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "HANDOFF.json"
            state = empty_handoff("t9", "a", "b")
            state["completed"] = ["c"]
            write_handoff(path, state)
            loaded = read_handoff(path)
            self.assertEqual("t9", loaded["task_id"])
            self.assertEqual(["c"], loaded["completed"])

    def test_read_missing_required_field_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "HANDOFF.json"
            path.write_text(json.dumps({"task_id": "x"}), encoding="utf-8")
            with self.assertRaises(Exception):
                read_handoff(path)


if __name__ == "__main__":
    unittest.main()
