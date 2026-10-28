"""Sentences the v1.6.5 docs must keep, because no other test reads them.

The round-20 review found three operator-facing errors in OPERATIONS.md that
the full test suite could not notice (a wrong version, a missing fallback, an
ambiguous standby rule). These lines pin the corrected wording of each rule.
"""
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def read(relative):
    return (PROJECT / relative).read_text(encoding="utf-8")


class ReleaseDocsContractTest(unittest.TestCase):
    def test_operations_keeps_the_corrected_rules(self):
        text = read("docs/OPERATIONS.md")
        self.assertIn("策略进化已于 2026-10-11 退役（v1.6.4 起代码移除）", text)
        self.assertNotIn("v1.6.5 起代码移除", text)
        self.assertIn("移不进废纸篓时", text)
        self.assertIn("绝不会悄悄删掉", text)
        self.assertIn("300 秒是“连续无进展”窗口，不是补位许可", text)
        self.assertIn("任务中途的内部故障、输出预算耗尽、思考泄露不补", text)
        self.assertNotIn("连续300秒无实际进展或明确外部故障才允许按条件备用接替", text)

    def test_probe_failure_rule_is_the_same_everywhere(self):
        # User 2026-10-21 (plan 甲): a real failed probe is covered whatever its
        # cause; "internal faults are not covered" only applies mid-task.
        for relative in ("docs/STANDBY_POLICY.md", "docs/OPERATIONS.md",
                         ".agents/skills/xiaoba-agentos/SKILL.md", "docs/RELEASE_V1.6.5.md"):
            with self.subTest(document=relative):
                self.assertIn("不论原因", read(relative))

    def test_entry_skill_does_not_require_both_primaries_to_open_intake(self):
        skill = read(".agents/skills/xiaoba-agentos/SKILL.md")
        self.assertNotIn("两个后台真实探针均通过，才报告可接单", skill)
        self.assertIn("备用后台健康（公开留痕补位），才报告可接单", skill)


if __name__ == "__main__":
    unittest.main()
