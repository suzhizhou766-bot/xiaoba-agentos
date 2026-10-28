"""确定性裁判的定向测试。

策略进化（含评估面）已于 2026-10-11 由用户裁决退役，原来的评估服务测试随功能
一起删除；文件名保留 `test_evaluation.py`，因为底线标记清单里的条目按
「文件::函数」定位，改名会让标记失效。这里只剩下裁判本身的不变量。

裁判现在不接任何权重参数：候选之间只有一个中性权重 1.0，分项权重
（`TERM_WEIGHTS`）只由「题面是否要求复述」决定，不由外部配置决定。
"""

import unittest

from xiaoba_agentos.adapters import AgentResult
from xiaoba_agentos.orchestrator import DeterministicJudge


ARITH_PROMPT = "计算 37 乘以 41 等于多少？只输出最终的阿拉伯数字，不要输出任何其他文字。"
VERBOSE_ECHO = (
    "The user wants me to compute 37 乘以 41 and output only the 阿拉伯数字 result. "
    "37 乘以 41 = 1480 + 37 = 1517. So the answer is 1517. "
    "The task says 只输出最终的阿拉伯数字，不要输出任何其他文字. I should just output 1517."
)


class DeterministicJudgeTest(unittest.TestCase):
    def test_explain_decomposition_sums_to_choose_score(self):
        results = [
            AgentResult("a", "1517", 1),
            AgentResult("b", "结论：可行。依据：测试。风险：无。验证：复查。步骤：略。", 1),
            AgentResult("c", "1517", 1),
        ]
        terms = DeterministicJudge.explain("审查方案", results)
        _winner, scores = DeterministicJudge.choose("审查方案", results)
        for agent, entry in terms.items():
            recomposed = entry["weight"] * sum(
                entry["term_weights"][name] * entry[name] for name in entry["term_weights"]
            )
            self.assertAlmostEqual(recomposed, entry["score"], places=12)
            self.assertEqual(entry["score"], scores[agent])
        # No candidate can be reweighted any more: the retired strategy row was
        # the only source of a per-agent multiplier, so every candidate carries
        # the same neutral weight. A hidden factor anywhere in scoring would
        # break the recomposition asserted above.
        self.assertEqual(
            {"a": 1.0, "b": 1.0, "c": 1.0},
            {agent: entry["weight"] for agent, entry in terms.items()},
        )

    def test_echo_terms_are_dropped_unless_the_task_asks_for_restatement(self):
        terse = AgentResult("terse", "1517", 1)
        echo = AgentResult("echo", VERBOSE_ECHO, 1)
        plain = DeterministicJudge.explain(ARITH_PROMPT, [terse, echo])
        for entry in plain.values():
            self.assertEqual(0.0, entry["coverage"])
            self.assertEqual(0.0, entry["relevance_density"])
            self.assertNotIn("coverage", entry["term_weights"])
        self.assertAlmostEqual(1.0, sum(plain["terse"]["term_weights"].values()), places=12)
        # "请逐字复述题面" makes echoing the prompt the deliverable, so the term comes
        # back with its original weight rather than being renormalised away.
        restatement_prompt = "请逐字复述题面：计算 37 乘以 41 等于多少？"
        restated = DeterministicJudge.explain(restatement_prompt, [terse, echo])
        self.assertTrue(restated["echo"]["restatement_task"])
        self.assertGreater(restated["echo"]["coverage"], restated["terse"]["coverage"])
        self.assertEqual(
            DeterministicJudge.TERM_WEIGHTS, restated["echo"]["term_weights"]
        )

    def test_tie_break_is_not_the_agent_name(self):
        slow = AgentResult("aaa_first", "1517", 9000)
        fast = AgentResult("zzz_last", "1517", 1000)
        winner, _scores = DeterministicJudge.choose("审查方案", [slow, fast])
        self.assertEqual("zzz_last", winner.agent)
        breakdown = DeterministicJudge.explain("审查方案", [slow, fast])
        self.assertEqual(
            "latency_after_tie", DeterministicJudge.decision_basis(breakdown, winner)
        )
        # Same score and same latency but different answers: the digest decides, and
        # it must not depend on the order the candidates happened to be listed in.
        differ = [
            AgentResult("aaa_first", "甲方案可行", 1000),
            AgentResult("zzz_last", "乙方案可行", 1000),
        ]
        picked, _ = DeterministicJudge.choose("审查方案", differ)
        repicked, _ = DeterministicJudge.choose("审查方案", list(reversed(differ)))
        self.assertEqual(picked.agent, repicked.agent)
        self.assertEqual(
            "content_digest_after_tie",
            DeterministicJudge.decision_basis(
                DeterministicJudge.explain("审查方案", differ), picked
            ),
        )
        # Byte-identical answers leave nothing to compare, so the recorded winner has
        # to stay stable across runs even though the choice carries no meaning.
        level = [AgentResult("aaa_first", "1517", 1000), AgentResult("zzz_last", "1517", 1000)]
        first, _ = DeterministicJudge.choose("审查方案", level)
        second, _ = DeterministicJudge.choose("审查方案", list(reversed(level)))
        self.assertEqual(first.agent, second.agent)
        self.assertEqual(
            "identical_content",
            DeterministicJudge.decision_basis(
                DeterministicJudge.explain("审查方案", level), first
            ),
        )


if __name__ == "__main__":
    unittest.main()
