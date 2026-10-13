import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.adapters import AgentResult
from xiaoba_agentos.evaluation import EvaluationService
from xiaoba_agentos.orchestrator import DeterministicJudge
from xiaoba_agentos.store import EventStore


ARITH_PROMPT = "计算 37 乘以 41 等于多少？只输出最终的阿拉伯数字，不要输出任何其他文字。"
VERBOSE_ECHO = (
    "The user wants me to compute 37 乘以 41 and output only the 阿拉伯数字 result. "
    "37 乘以 41 = 1480 + 37 = 1517. So the answer is 1517. "
    "The task says 只输出最终的阿拉伯数字，不要输出任何其他文字. I should just output 1517."
)


class EvaluationServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.tmp.name) / "evaluation.sqlite3")
        self.store.initialize()
        self.service = EvaluationService(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_records_isolated_evaluation_run(self):
        result = self.service.replay("answer", "prompt")
        with self.store.read_only() as connection:
            evaluation_runs = connection.execute(
                "SELECT agent_name,status,output FROM evaluation_runs WHERE evaluation_id=?",
                (result["evaluation_id"],),
            ).fetchall()
            production_runs = connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        self.assertEqual([("replay", "completed", "answer")], [tuple(row) for row in evaluation_runs])
        self.assertEqual(0, production_runs)

    def test_regression_gate_aggregates_all_cases(self):
        self.service.add_case("mixed", "pass", "expected", {"fixture_output": "expected"})
        self.service.add_case("mixed", "fail", "expected", {"fixture_output": "wrong"})
        result = self.service.regression("mixed", strategy_version=2)
        self.assertFalse(result["passed"])
        with self.store.read_only() as connection:
            gates = connection.execute(
                "SELECT status,details_json FROM promotion_gates WHERE strategy_version=2 AND gate_type='regression'"
            ).fetchall()
            run_count = connection.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0]
        self.assertEqual(1, len(gates))
        self.assertEqual("failed", gates[0]["status"])
        self.assertEqual(2, run_count)

    def test_empty_regression_dataset_fails_closed(self):
        result = self.service.regression("missing", strategy_version=3)
        self.assertFalse(result["passed"])
        with self.store.read_only() as connection:
            gate = connection.execute(
                "SELECT status FROM promotion_gates WHERE strategy_version=3 AND gate_type='regression'"
            ).fetchone()
        self.assertEqual("failed", gate["status"])

    def test_explain_decomposition_sums_to_choose_score(self):
        results = [
            AgentResult("a", "1517", 1),
            AgentResult("b", "结论：可行。依据：测试。风险：无。验证：复查。步骤：略。", 1),
            AgentResult("c", "1517", 1),
        ]
        weights = {"a": 2.0, "b": 0.5}
        terms = DeterministicJudge.explain("审查方案", results, weights)
        _winner, scores = DeterministicJudge.choose("审查方案", results, weights)
        for agent, entry in terms.items():
            recomposed = entry["weight"] * sum(
                entry["term_weights"][name] * entry[name] for name in entry["term_weights"]
            )
            self.assertAlmostEqual(recomposed, entry["score"], places=12)
            self.assertEqual(entry["score"], scores[agent])
        self.assertEqual(2.0, terms["a"]["weight"])
        self.assertEqual(0.5, terms["b"]["weight"])
        # An agent absent from the weight map inherits the *minimum* configured
        # weight, not 1.0 — pinned here so a change to that default is deliberate.
        self.assertEqual(0.5, terms["c"]["weight"])

    def test_echo_terms_are_dropped_unless_the_task_asks_for_restatement(self):
        terse = AgentResult("terse", "1517", 1)
        echo = AgentResult("echo", VERBOSE_ECHO, 1)
        plain = DeterministicJudge.explain(ARITH_PROMPT, [terse, echo], {})
        for entry in plain.values():
            self.assertEqual(0.0, entry["coverage"])
            self.assertEqual(0.0, entry["relevance_density"])
            self.assertNotIn("coverage", entry["term_weights"])
        self.assertAlmostEqual(1.0, sum(plain["terse"]["term_weights"].values()), places=12)
        # "请逐字复述题面" makes echoing the prompt the deliverable, so the term comes
        # back with its original weight rather than being renormalised away.
        restatement_prompt = "请逐字复述题面：计算 37 乘以 41 等于多少？"
        restated = DeterministicJudge.explain(restatement_prompt, [terse, echo], {})
        self.assertTrue(restated["echo"]["restatement_task"])
        self.assertGreater(restated["echo"]["coverage"], restated["terse"]["coverage"])
        self.assertEqual(
            DeterministicJudge.TERM_WEIGHTS, restated["echo"]["term_weights"]
        )

    def test_tie_break_is_not_the_agent_name(self):
        slow = AgentResult("aaa_first", "1517", 9000)
        fast = AgentResult("zzz_last", "1517", 1000)
        winner, _scores = DeterministicJudge.choose("审查方案", [slow, fast], {})
        self.assertEqual("zzz_last", winner.agent)
        breakdown = DeterministicJudge.explain("审查方案", [slow, fast], {})
        self.assertEqual(
            "latency_after_tie", DeterministicJudge.decision_basis(breakdown, winner)
        )
        # Same score and same latency but different answers: the digest decides, and
        # it must not depend on the order the candidates happened to be listed in.
        differ = [
            AgentResult("aaa_first", "甲方案可行", 1000),
            AgentResult("zzz_last", "乙方案可行", 1000),
        ]
        picked, _ = DeterministicJudge.choose("审查方案", differ, {})
        repicked, _ = DeterministicJudge.choose("审查方案", list(reversed(differ)), {})
        self.assertEqual(picked.agent, repicked.agent)
        self.assertEqual(
            "content_digest_after_tie",
            DeterministicJudge.decision_basis(
                DeterministicJudge.explain("审查方案", differ, {}), picked
            ),
        )
        # Byte-identical answers leave nothing to compare, so the recorded winner has
        # to stay stable across runs even though the choice carries no meaning.
        level = [AgentResult("aaa_first", "1517", 1000), AgentResult("zzz_last", "1517", 1000)]
        first, _ = DeterministicJudge.choose("审查方案", level, {})
        second, _ = DeterministicJudge.choose("审查方案", list(reversed(level)), {})
        self.assertEqual(first.agent, second.agent)
        self.assertEqual(
            "identical_content",
            DeterministicJudge.decision_basis(
                DeterministicJudge.explain("审查方案", level, {}), first
            ),
        )

    def _arithmetic_case(self, dataset, verbose=VERBOSE_ECHO):
        return self.service.add_case(
            dataset,
            ARITH_PROMPT,
            "1517",
            {
                "candidates": {"agy_review": "1517", "dsh_review": verbose},
                "format_contract": {"type": "regex_fullmatch", "pattern": "[0-9]+"},
                "source_task_id": "302c9408",
            },
        )

    def test_prompt_echo_no_longer_beats_the_compliant_answer(self):
        self._arithmetic_case("judge")
        baseline = self.service.adjudication_baseline("judge")
        self.assertTrue(baseline["passed"])
        row = baseline["rows"][0]
        self.assertEqual("agy_review", row["winner"])
        self.assertFalse(row["format_inverted"])
        self.assertFalse(row["verdict_inverted"])
        self.assertTrue(row["winner_correct"])
        self.assertTrue(row["winner_instruction_followed"])
        self.assertEqual("constraint_compliance", row["decision_basis"])
        self.assertTrue(row["candidates"]["agy_review"]["derived_compliant"])
        self.assertEqual([], row["candidates"]["agy_review"]["violations"])
        self.assertEqual(1, len(row["candidates"]["dsh_review"]["violations"]))
        # The echo terms are inert here, so coverage can no longer carry the verdict.
        self.assertEqual(0.0, row["terms"]["dsh_review"]["coverage"])
        self.assertEqual(0.0, row["terms"]["agy_review"]["coverage"])
        self.assertEqual([], row["detector_disagreed"])
        with self.store.read_only() as connection:
            status = connection.execute(
                "SELECT status FROM evaluations WHERE case_id=?", (row["case_id"],)
            ).fetchone()["status"]
            gate = connection.execute(
                "SELECT status, details_json FROM promotion_gates WHERE gate_type='judge_baseline'"
            ).fetchone()
        self.assertEqual("completed", status)
        self.assertEqual("passed", gate["status"])

    def test_adjudication_baseline_flags_correctness_inversion(self):
        """A well-organised but factually wrong answer can still win on score.

        The prompt states no output rule, so the compliance partition cannot fire
        and structure alone carries the verdict.  That is the residual weakness of
        a bag-of-words judge; the flag exists so it stays visible instead of being
        read as a quality judgement.
        """
        wrong = (
            "结论：北京有 14 个市辖区。\n"
            "依据：按现行行政区划统计。\n"
            "风险：口径可能与其他来源不同。\n"
            "验证：核对民政局公开名单。\n"
            "步骤：查询官方数据后比对。"
        )
        self.service.add_case(
            "judge",
            "北京有多少个市辖区？",
            "16",
            {
                "candidates": {"a": "16", "b": wrong},
                "latency_ms": {"a": 1000, "b": 1000},
            },
        )
        baseline = self.service.adjudication_baseline("judge")
        row = baseline["rows"][0]
        self.assertEqual("b", row["winner"])
        self.assertEqual("score", row["decision_basis"])
        self.assertTrue(row["verdict_inverted"])
        self.assertFalse(row["winner_correct"])
        self.assertTrue(row["candidates"]["a"]["answer_correct"])
        self.assertEqual([], row["detector_disagreed"])
        self.assertFalse(baseline["passed"])

    def test_baseline_still_flags_an_inversion_it_cannot_derive_from_the_prompt(self):
        """The flag is non-vacuous: an unstated contract still exposes a bad verdict.

        The judge can only enforce rules the prompt itself states.  Here the prompt
        asks a plain question, the recorded contract demands a bare numeral, and the
        better-organised prose answer wins on structure -- so the baseline must still
        report the inversion rather than silently passing it.
        """
        self.service.add_case(
            "judge",
            "这座桥的净跨径是多少米？",
            "460",
            {
                "candidates": {
                    "a": "460",
                    "b": "净跨径：460 米。\n主跨：460 米。\n边跨：118 米。",
                },
                "format_contract": {"type": "regex_fullmatch", "pattern": "[0-9]+"},
            },
        )
        baseline = self.service.adjudication_baseline("judge")
        row = baseline["rows"][0]
        self.assertEqual("b", row["winner"])
        self.assertEqual("score", row["decision_basis"])
        self.assertTrue(row["format_inverted"])
        self.assertFalse(row["verdict_inverted"])
        self.assertFalse(row["winner_instruction_followed"])
        self.assertTrue(row["candidates"]["a"]["instruction_followed"])
        self.assertFalse(baseline["passed"])
        self.assertIn(row["case_id"], baseline["defective_case_ids"])

    def test_baseline_reports_when_the_detector_disagrees_with_the_contract(self):
        self.service.add_case(
            "judge",
            "这座桥的净跨径是多少米？",
            "460",
            {
                "candidates": {"a": "460", "b": "净跨径：460 米。"},
                "latency_ms": {"a": 1000, "b": 2000},
                "format_contract": {"type": "regex_fullmatch", "pattern": "[0-9]+"},
            },
        )
        baseline = self.service.adjudication_baseline("judge")
        row = baseline["rows"][0]
        # The prompt states no rule, so the judge derives none and calls both
        # compliant; the recorded contract disagrees about "b".
        self.assertEqual(["b"], row["detector_disagreed"])
        self.assertIn(row["case_id"], baseline["detector_disagreement_case_ids"])

    def test_adjudication_baseline_passes_when_winner_is_correct_and_compliant(self):
        self.service.add_case(
            "judge",
            ARITH_PROMPT,
            "1517",
            {
                "candidates": {"agy_review": "1517", "dsh_review": "1517"},
                "format_contract": {"type": "regex_fullmatch", "pattern": "[0-9]+"},
            },
        )
        baseline = self.service.adjudication_baseline("judge")
        self.assertTrue(baseline["passed"])
        row = baseline["rows"][0]
        self.assertFalse(row["format_inverted"])
        self.assertFalse(row["verdict_inverted"])
        self.assertTrue(row["winner_correct"])
        self.assertTrue(row["winner_instruction_followed"])
        self.assertEqual(1.0, row["terms"]["agy_review"]["consensus"])
        with self.store.read_only() as connection:
            gate = connection.execute(
                "SELECT status FROM promotion_gates WHERE gate_type='judge_baseline'"
            ).fetchone()
        self.assertEqual("passed", gate["status"])

    def test_adjudication_baseline_skips_single_candidate_cases_and_touches_nothing_production(self):
        self.service.add_case("judge", ARITH_PROMPT, "1517", {"candidates": {"solo": "1517"}})
        self._arithmetic_case("judge")
        baseline = self.service.adjudication_baseline("judge")
        self.assertEqual(1, baseline["cases_scored"])
        with self.store.read_only() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0])
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
            runs = connection.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0]
        self.assertEqual(2, runs)

    def test_exact_lines_contract_checks_instruction_following(self):
        self.service.add_case(
            "smoke",
            "Return exactly two non-empty lines: POST_DEPLOY_SMOKE_OK then FINAL_ACCEPTANCE: PASS.",
            None,
            {
                "candidates": {
                    "good": "POST_DEPLOY_SMOKE_OK\nFINAL_ACCEPTANCE: PASS",
                    "bad": "结论：POST_DEPLOY_SMOKE_OK 与 FINAL_ACCEPTANCE: PASS 均已确认。",
                },
                "format_contract": {
                    "type": "exact_lines",
                    "lines": ["POST_DEPLOY_SMOKE_OK", "FINAL_ACCEPTANCE: PASS"],
                },
            },
        )
        baseline = self.service.adjudication_baseline("smoke")
        row = baseline["rows"][0]
        by_name = {name: value for name, value in row["candidates"].items()}
        self.assertTrue(by_name["good"]["instruction_followed"])
        self.assertFalse(by_name["bad"]["instruction_followed"])
        self.assertIsNone(row["winner_correct"])



if __name__ == "__main__":
    unittest.main()
