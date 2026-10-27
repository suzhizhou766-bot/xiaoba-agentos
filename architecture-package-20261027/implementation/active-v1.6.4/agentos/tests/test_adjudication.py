import unittest

from xiaoba_agentos.adjudication import (
    asks_for_restatement,
    constraint_violations,
    detect_output_constraints,
    detect_output_constraint_state,
)


# Drawn from the 24-case `judge` baseline; the two long prose prompts are abridged.
# Only prompts that state an output rule may produce a constraint -- the prose ones
# must stay empty, because a false positive silently re-enables the prompt-echo bias
# the detector exists to remove and disqualifies an answer the prompt never asked to
# be formatted.
ARITH_PROMPT = "计算 37 乘以 41 等于多少？只输出最终的阿拉伯数字，不要输出任何其他文字。"
SMOKE_PROMPT = (
    "Return exactly two non-empty lines: first line POST_DEPLOY_SMOKE_OK; "
    "second line FINAL_ACCEPTANCE: PASS. Do not use tools."
)
PROSE_PROMPTS = (
    "上班了小八助理",
    "可以",
    "可以，按照你的想法执行",
    "双 Agent现在用的什么api上游",
    "这座桥的净跨径是多少米？",
    "问题已经正在返回修改，等待更新",
    "在不破坏小八AgentOS架构的前提下",
    "你可以把word行文准测及美化概念写进小八AgentOS",
    "检查一下小八导演台 http://127.0.0.1:5173/ 有什么问题没有，有什么可以优化升级的没有",
    "额度耗尽之前制作一份工作接续移交手册给anti-gravity",
    "执行小八 AgentOS v1.3 发布链路短验收：请给出简短结论、风险和验证点，不修改任何文件。",
    "将跨媒介中文文字排版通用准则写入小八AgentOS第二大脑与Playbook",
)


def _kinds(prompt):
    return [(item.kind, item.detail) for item in detect_output_constraints(prompt)]


class OutputConstraintTest(unittest.TestCase):
    def test_numeral_rule_needs_both_the_ask_and_the_exclusion(self):
        # Either half alone is ambiguous: "只输出数字" says nothing about surrounding
        # prose, and "不要输出任何其他文字" says nothing about the answer's shape.
        self.assertEqual([], _kinds("只输出最终的阿拉伯数字"))
        self.assertEqual([], _kinds("不要输出任何其他文字"))
        self.assertEqual([("numeral_only", "[0-9]+")], _kinds(ARITH_PROMPT))

    def test_numeral_violation_reports_shape_not_length(self):
        (rule,) = detect_output_constraints(ARITH_PROMPT)
        self.assertIsNone(rule.violation("1517"))
        self.assertIsNone(rule.violation("  1517\n"))
        self.assertIsNotNone(rule.violation("答案是 1517"))
        self.assertIsNotNone(rule.violation(""))
        self.assertEqual(1, len(constraint_violations("答案是 1517", [rule])))
        self.assertEqual([], constraint_violations("1517", [rule]))

    def test_line_count_rules_across_languages_and_numerals(self):
        self.assertEqual([("line_count", "2")], _kinds(SMOKE_PROMPT))
        self.assertEqual([("line_count", "3")], _kinds("请用恰好三行说明结论。"))
        self.assertEqual([("line_count", "5")], _kinds("Answer in exactly 5 lines."))

    def test_line_count_violation_ignores_blank_lines(self):
        (rule,) = detect_output_constraints(SMOKE_PROMPT)
        self.assertIsNone(rule.violation("POST_DEPLOY_SMOKE_OK\nFINAL_ACCEPTANCE: PASS"))
        self.assertIsNone(rule.violation("POST_DEPLOY_SMOKE_OK\n\nFINAL_ACCEPTANCE: PASS\n"))
        self.assertIsNotNone(rule.violation("POST_DEPLOY_SMOKE_OK"))
        self.assertIn("2", rule.violation("POST_DEPLOY_SMOKE_OK"))

    def test_prose_prompts_produce_no_constraints(self):
        for prompt in PROSE_PROMPTS:
            self.assertEqual([], _kinds(prompt), prompt)

    def test_natural_language_variants_are_detected_without_exact_copy(self):
        prompts = (
            "只回答数字，别的都不要写。",
            "仅输出阿拉伯数字结果，不要附带任何说明。",
            "只输出最终的阿拉伯数字，不要输出任何解释、推理过程或其他文字。",
            "Output only the arabic numeral, no other text.",
        )
        for prompt in prompts:
            self.assertEqual([("numeral_only", "[0-9]+")], _kinds(prompt), prompt)

    def test_partial_contract_is_explicitly_reported(self):
        state = detect_output_constraint_state("只输出最终的阿拉伯数字")
        self.assertEqual("unresolved_partial", state.status)
        self.assertTrue(state.numeral_signal)
        self.assertFalse(state.no_extra_signal)
        self.assertEqual([], state.constraints)
        self.assertEqual("no_constraint", detect_output_constraint_state("请简短说明").status)

    def test_vague_length_asks_are_not_invented_into_rules(self):
        # "简短" has no byte budget, and guessing one would dress an arbitrary
        # number up as a rule the prompt never stated.
        for prompt in ("请给出简短结论。", "Keep it brief.", "简要说明风险"):
            self.assertEqual([], _kinds(prompt))


class RestatementTest(unittest.TestCase):
    def test_only_an_explicit_restatement_ask_re_enables_the_echo_terms(self):
        self.assertTrue(asks_for_restatement("上下文补丁传递验收：请复述当前项目职责"))
        self.assertTrue(asks_for_restatement("请逐字引用原文"))
        self.assertTrue(asks_for_restatement("Repeat the following verbatim."))
        self.assertTrue(asks_for_restatement("Quote exactly what was provided."))

    def test_prose_that_merely_mentions_a_source_is_not_a_restatement_ask(self):
        self.assertFalse(asks_for_restatement("请分析原文的结构"))
        self.assertFalse(asks_for_restatement("向我阐述小八AgentOS v1.4.0 的能力。"))
        self.assertFalse(asks_for_restatement(ARITH_PROMPT))
        self.assertFalse(asks_for_restatement(SMOKE_PROMPT))
        for prompt in PROSE_PROMPTS:
            self.assertFalse(asks_for_restatement(prompt), prompt)

    def test_a_restatement_task_may_also_state_a_shape_rule(self):
        prompt = "请复述题面，并只输出最终的阿拉伯数字，不要输出任何其他文字。"
        self.assertTrue(asks_for_restatement(prompt))
        self.assertEqual([("numeral_only", "[0-9]+")], _kinds(prompt))


if __name__ == "__main__":
    unittest.main()
