"""C11-01: reasoning markup is stripped before pool-receipt persistence and
build_release vote judgement, while unexecuted tool markers remain a hard B19
rejection (they must never be washed into "already executed").  All fixtures
are isolated; no production DB, model or port is touched.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "development/integrated-candidate/scripts"))

from xiaoba_agentos.output_hygiene import (  # noqa: E402
    contains_non_answer_markers,
    detect_reasoning_leak,
    strip_reasoning_markup,
)
from xiaoba_agentos.pool_dispatch import pool_receipt_output, review_vote_problem  # noqa: E402
from test_release import BUILD_RELEASE_MODULE  # noqa: E402


GOOD_VERDICT = "FINAL_ACCEPTANCE: PASS"


class StripReasoningOnlyTest(unittest.TestCase):
    def test_think_prefix_stripped_and_answer_survives(self):
        text = (
            "<think>the user wants me to compute a verdict</think>\n" + GOOD_VERDICT
        )
        cleaned = strip_reasoning_markup(text)
        self.assertEqual(GOOD_VERDICT, cleaned)
        self.assertEqual([], detect_reasoning_leak(cleaned))

    def test_normal_technical_prose_and_fail_verdict_survive(self):
        body = (
            "结论：FAIL。依据：integrity_check 工具返回 642 条事件不连续。\n"
            "风险：调用 bash/Read 工具时打印了 FAIL，这只是正文。\n"
            "FINAL_ACCEPTANCE: FAIL"
        )
        self.assertEqual(body, strip_reasoning_markup(body))
        self.assertFalse(contains_non_answer_markers(body))


class UnexecutedToolMarkersRejectedTest(unittest.TestCase):
    """Stripping <think> must NOT neutralize the tool-marker B19 gate."""

    def test_markers_survive_reasoning_strip_and_are_detected(self):
        cases = (
            "<invoke name='bash'>", "</invoke>", "<parameter name='x'>",
            "<｜dsml｜tool_calls>", "<|dsml|>", "tool-call serialization",
        )
        for marker in cases:
            with self.subTest(marker=marker):
                text = "<think>leak</think>\n结论：" + marker + " 后缀"
                cleaned = strip_reasoning_markup(text)
                self.assertTrue(contains_non_answer_markers(cleaned), marker)

    def test_tool_only_placeholder_is_rejected_not_washed(self):
        raw = "<invoke name='bash'><parameter name='cmd'>ls</parameter></invoke>"
        cleaned = strip_reasoning_markup(raw)
        # The marker is preserved, so the B19 gate still rejects it as a
        # non-answer; it is never silently turned into a successful verdict.
        self.assertTrue(contains_non_answer_markers(cleaned))
        self.assertNotIn(GOOD_VERDICT, cleaned)


class PoolPersistenceJudgeTest(unittest.TestCase):
    """真实回执保存 → 判票路径：清洗 think 后判票，工具标记仍拒收。"""

    def test_think_stripped_body_judged_pass(self):
        raw = "<think>leak</think>\n" + GOOD_VERDICT
        persisted = pool_receipt_output({"body": raw})
        self.assertNotIn("<think", persisted)
        self.assertIn(GOOD_VERDICT, persisted)
        self.assertTrue(BUILD_RELEASE_MODULE._passed_verdict(persisted))
        self.assertEqual("", review_vote_problem(persisted))

    def test_tool_marker_in_persisted_body_is_rejected(self):
        raw = "<think>leak</think>\n" + GOOD_VERDICT + "\n<invoke name='bash'>tool</invoke>"
        persisted = pool_receipt_output({"body": raw})
        self.assertFalse(BUILD_RELEASE_MODULE._passed_verdict(persisted))
        self.assertIn("B19", review_vote_problem(persisted))

    def test_cleaned_fail_verdict_is_not_pass(self):
        raw = "<think>leak</think>\nFINAL_ACCEPTANCE: FAIL"
        persisted = pool_receipt_output({"body": raw})
        self.assertFalse(BUILD_RELEASE_MODULE._passed_verdict(persisted))


if __name__ == "__main__":
    unittest.main()
