import unittest

from xiaoba_agentos.output_hygiene import (
    NON_ANSWER_MARKERS,
    contains_non_answer_markers,
    detect_non_answer_markers,
    detect_reasoning_leak,
    strip_reasoning_markup,
)


CLOSE_THINK = "</think>"

LEGITIMATE_REVIEW = """结论：审计链完整，可以交付。
依据：642 条事件的前后哈希连续，integrity_check 返回 ok。
风险：健康探针窗口将在 24 小时后过期，过期后容量会归零。
验证：重跑 doctor 并确认 ready_for_external_tasks 为 true。
步骤：先 session-start，再提交无害任务，最后核对 memory-outbox。
"""


class StripReasoningMarkupTest(unittest.TestCase):
    def test_paired_block_is_removed_and_answer_survives(self):
        text = "<think>用户想要我算加法，先分解数字。</think>\n结论：1517。依据：逐位相加。"
        self.assertEqual("结论：1517。依据：逐位相加。", strip_reasoning_markup(text))

    def test_stray_closing_tag_is_removed(self):
        self.assertEqual(
            "结论：可行。", strip_reasoning_markup("结论：可行。\n</think>")
        )

    def test_reasoning_content_field_dump_is_removed(self):
        text = 'reasoning_content: "let me work through this"\n结论：可行。'
        self.assertEqual("结论：可行。", strip_reasoning_markup(text))

    def test_prose_heading_named_reasoning_survives(self):
        text = "Reasoning: the hash chain is continuous.\n结论：可行。"
        self.assertEqual(text, strip_reasoning_markup(text))

    def test_clean_text_is_unchanged(self):
        self.assertEqual(LEGITIMATE_REVIEW, strip_reasoning_markup(LEGITIMATE_REVIEW))

    def test_reasoning_only_text_becomes_empty(self):
        self.assertEqual("", strip_reasoning_markup("<think>全部是推理，没有答案。</think>"))

    def test_answer_after_unpaired_closer_is_recovered(self):
        # Shape captured from task 302c9408: no opening tag, plain-prose
        # reasoning, a stray closer, then the deliverable answer.
        text = (
            "The user wants me to compute 37 x 41 and output only the numeral result.\n\n"
            "So the answer is 1517.\n\nI should just output 1517.\n"
            + CLOSE_THINK
            + "\n\n1517\n"
        )
        self.assertEqual("1517", strip_reasoning_markup(text))
        self.assertEqual([], detect_reasoning_leak(strip_reasoning_markup(text)))


class DetectReasoningLeakTest(unittest.TestCase):
    def test_leftover_markup_is_a_hard_hit(self):
        for text in ("<think>未剥离", "残留 </thinking>", 'reasoning_content: "x"'):
            with self.subTest(text=text):
                self.assertTrue(detect_reasoning_leak(text))

    def test_untagged_first_person_meta_commentary_is_detected(self):
        leaked = (
            "The user is asking me to add two numbers. Let me think about how to "
            "approach this. But wait, the prompt says to give a conclusion, so the "
            "answer is 1517."
        )
        self.assertTrue(detect_reasoning_leak(leaked))

    def test_instruction_echo_is_detected(self):
        leaked = (
            "系统要求：请给出明确结论、依据、风险和可验证步骤。"
            "同时说明你当前只产出独立候选答案，最终来源标签由 AgentOS 统一添加。"
        )
        self.assertTrue(detect_reasoning_leak(leaked))

    def test_single_soft_hit_is_not_enough(self):
        self.assertEqual([], detect_reasoning_leak("结论：可行。补充：let me think 这一点无需展开。"))

    def test_legitimate_chinese_review_is_not_flagged(self):
        self.assertEqual([], detect_reasoning_leak(LEGITIMATE_REVIEW))

    def test_empty_output_is_clean(self):
        self.assertEqual([], detect_reasoning_leak(""))


class NonAnswerMarkersTest(unittest.TestCase):
    def test_all_eight_markers_detected_with_case_insensitivity(self):
        expected_markers = (
            "<invoke ",
            "</invoke>",
            "<parameter ",
            "</parameter>",
            "<｜dsml｜",
            "<|dsml|",
            "tool-call serialization",
            "tool call serialization",
        )
        self.assertEqual(expected_markers, NON_ANSWER_MARKERS)
        for marker in expected_markers:
            with self.subTest(marker=marker):
                lower_sample = f"prefix {marker} suffix"
                upper_sample = f"prefix {marker.upper()} suffix"
                self.assertTrue(contains_non_answer_markers(lower_sample))
                self.assertTrue(contains_non_answer_markers(upper_sample))
                self.assertIn(marker, detect_non_answer_markers(lower_sample))
                self.assertIn(marker, detect_non_answer_markers(upper_sample))

    def test_clean_text_and_empty_text_do_not_flag_markers(self):
        self.assertFalse(contains_non_answer_markers(""))
        self.assertEqual([], detect_non_answer_markers(""))
        self.assertFalse(contains_non_answer_markers(LEGITIMATE_REVIEW))
        self.assertEqual([], detect_non_answer_markers(LEGITIMATE_REVIEW))
        clean_prose = "参数传递正确，没有调用工具，正常返回结论。"
        self.assertFalse(contains_non_answer_markers(clean_prose))
        self.assertEqual([], detect_non_answer_markers(clean_prose))


if __name__ == "__main__":
    unittest.main()
