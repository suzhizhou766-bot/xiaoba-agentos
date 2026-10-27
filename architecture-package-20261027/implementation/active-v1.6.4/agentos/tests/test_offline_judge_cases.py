"""24 条评估用例的离线判定测试。

原 `evaluation_cases` 表里的 24 条用例（dataset=judge）在策略进化退役时转成这份
离线测试：不连数据库、不发模型请求，只把记录下来的候选交给现在的确定性裁判，
断言判定与退役前逐条一致。每次全量测试都会跑。

夹具 `tests/fixtures/offline_judge_cases.json` 由主力 2026-10-19 从线上库只读导出
的 `评估用例24条_导出.json` 整理而来，只留判定需要的字段（id、case_name、prompt、
expected、candidates、format_contract），去掉了 created_at、latency_ms、note、tier
等与判定无关的字段。

`BASELINE_VERDICTS` 是在**未改动的 v1.6.2 代码**上跑出来的结果：那时裁判还接一个
权重参数，调用方传的是生效策略的 `agent_weights`（线上那条策略权重为空，所以每个
候选的权重都是 1.0）。去掉权重参数后再跑一遍，两次逐条相同才算「结果不变」。任何
一条对不上都说明裁判行为变了，必须查清再改这张表，不能反过来改用例去凑。

`_answer_correct` 与 `_meets_contract` 是从已退役的评估面原样搬过来的两个判定
函数。它们定义的是用例里 `expected` 和 `format_contract` 的含义，不依赖任何表，
所以随用例一起留在这里，而不是留在运行代码里。
"""

import io
import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from xiaoba_agentos.adapters import AgentResult
from xiaoba_agentos.cli import main
from xiaoba_agentos.orchestrator import DeterministicJudge


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "offline_judge_cases.json"

# 去掉权重参数前（v1.6.2）的逐条判定结果。字段含义见模块说明。
BASELINE_VERDICTS = {
    "28787cd4-b756-45a6-ac5d-5b1571292c77": {
        "case_name": '302c9408-as-delivered',
        "winner": 'agy_review',
        "decision_basis": 'constraint_compliance',
        "scores": {'agy_review': 0.164062, 'dsh_review': 0.164062},
        "answer_correct": {'agy_review': True, 'dsh_review': True},
        "instruction_followed": {'agy_review': True, 'dsh_review': False},
        "compliant": {'agy_review': True, 'dsh_review': False},
    },
    "c8bcdb6b-d0d0-428f-84bb-3b4ae56b42d4": {
        "case_name": '302c9408-post-hygiene',
        "winner": 'dsh_review',
        "decision_basis": 'identical_content',
        "scores": {'agy_review': 0.6875, 'dsh_review': 0.6875},
        "answer_correct": {'agy_review': True, 'dsh_review': True},
        "instruction_followed": {'agy_review': True, 'dsh_review': True},
        "compliant": {'agy_review': True, 'dsh_review': True},
    },
    "fc13861e-cca7-4607-8fc8-460549d11183": {
        "case_name": '959827b4-exact-two-lines',
        "winner": 'gpt_codex',
        "decision_basis": 'constraint_compliance',
        "scores": {'claude_code': 0.367113, 'gpt_codex': 0.15878},
        "answer_correct": {'claude_code': True, 'gpt_codex': True},
        "instruction_followed": {'claude_code': False, 'gpt_codex': True},
        "compliant": {'claude_code': False, 'gpt_codex': True},
    },
    "0ea90ceb-53f3-4d95-8568-45bedac19371": {
        "case_name": 'historical-c9893e0b',
        "winner": 'gpt_codex',
        "decision_basis": 'content_digest_after_tie',
        "scores": {'claude_code': 0.161458, 'gpt_codex': 0.161458},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "c93a95b9-0d2c-4bdb-9b11-26368e7b78ac": {
        "case_name": 'historical-382598d2',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.538144, 'gpt_codex': 0.467459},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "f1c979d8-575e-4105-a16c-dae80f2c4399": {
        "case_name": 'historical-abe1e8f2',
        "winner": 'gpt_codex',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.41878, 'gpt_codex': 0.488802},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "0fb37009-5002-4412-bfd0-58cc08125fd8": {
        "case_name": 'historical-56e3eb95',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.540284, 'gpt_codex': 0.50209},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "a49be424-df6e-4d93-93c4-f4dc6406904e": {
        "case_name": 'historical-bf420a92',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.373821, 'gpt_codex': 0.15625},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "43081a65-bde9-4bdc-9bc6-bcd1b994c575": {
        "case_name": 'historical-2bc11ed8',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.524685, 'gpt_codex': 0.050213},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "6771725f-a754-4d53-a9b1-35b92f1d08f9": {
        "case_name": 'historical-feca3a4c',
        "winner": 'gpt_codex',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.490951, 'gpt_codex': 0.528326},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "dd943eee-1d16-4afb-946f-48cb79708d37": {
        "case_name": 'historical-80e50eac',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.486557, 'gpt_codex': 0.425665},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "927fa564-34a2-4cba-a84b-b9f4a9d31a48": {
        "case_name": 'historical-9c8f7b92',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.514005, 'gpt_codex': 0.477203},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "0ac2aaad-c6e9-4c30-8616-d86853931d04": {
        "case_name": 'historical-7cd7348b',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.548249, 'gpt_codex': 0.388788},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "c6bdcb06-f1f9-4eeb-b8f7-74db7b6e8c9f": {
        "case_name": 'historical-25459a41',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.460354, 'gpt_codex': 0.174794},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "e609cd45-31de-48e6-bf46-6e9f2dff448e": {
        "case_name": 'historical-2461043c',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.481994, 'gpt_codex': 0.413367},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "ef5d52f6-6082-4a27-a910-a9de4b165b35": {
        "case_name": 'historical-9210b8d3',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.399961, 'gpt_codex': 0.304049},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "5202d556-fe7a-4c97-8c62-cad44cf49dd4": {
        "case_name": 'historical-43281e26',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.54234, 'gpt_codex': 0.476052},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "fdfb1bb6-3c9c-4b26-9621-f2205d1fc3b6": {
        "case_name": 'historical-39f8af51',
        "winner": 'gpt_codex',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.471836, 'gpt_codex': 0.555349},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "04ddefc8-c141-4e8e-88e9-38ef76560de6": {
        "case_name": 'historical-041efb00',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.593598, 'gpt_codex': 0.574159},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "217cc8a5-d06c-4241-8b44-0a4988212d2b": {
        "case_name": 'historical-1f2904b8',
        "winner": 'claude_code',
        "decision_basis": 'score',
        "scores": {'claude_code': 0.537093, 'gpt_codex': 0.4486},
        "answer_correct": {'claude_code': None, 'gpt_codex': None},
        "instruction_followed": {'claude_code': None, 'gpt_codex': None},
        "compliant": {'claude_code': True, 'gpt_codex': True},
    },
    "f1953ddb-5add-4a19-a40c-6d1cdcb408d0": {
        "case_name": 'control-terse-correct-vs-verbose-echo',
        "winner": 'agent_a',
        "decision_basis": 'constraint_compliance',
        "scores": {'agent_a': 0.161562, 'agent_b': 0.161562},
        "answer_correct": {'agent_a': True, 'agent_b': True},
        "instruction_followed": {'agent_a': True, 'agent_b': False},
        "compliant": {'agent_a': True, 'agent_b': False},
    },
    "cd092f10-ebbe-49b1-b0a9-46ac9ebe50ac": {
        "case_name": 'control-identical-compliant-tie',
        "winner": 'agent_b',
        "decision_basis": 'identical_content',
        "scores": {'agent_a': 0.6875, 'agent_b': 0.6875},
        "answer_correct": {'agent_a': True, 'agent_b': True},
        "instruction_followed": {'agent_a': True, 'agent_b': True},
        "compliant": {'agent_a': True, 'agent_b': True},
    },
    "c670ff57-3d23-47d1-85bd-f99305775484": {
        "case_name": 'control-structure-beats-correctness',
        "winner": 'agent_a',
        "decision_basis": 'constraint_compliance',
        "scores": {'agent_a': 0.15625, 'agent_b': 0.46875},
        "answer_correct": {'agent_a': True, 'agent_b': False},
        "instruction_followed": {'agent_a': True, 'agent_b': False},
        "compliant": {'agent_a': True, 'agent_b': False},
    },
    "d6e60300-ba20-4028-9a08-b9a28d76dd78": {
        "case_name": 'control-novel-answer-no-coverage',
        "winner": 'agent_b',
        "decision_basis": 'content_digest_after_tie',
        "scores": {'agent_a': 0.185764, 'agent_b': 0.185764},
        "answer_correct": {'agent_a': True, 'agent_b': True},
        "instruction_followed": {'agent_a': None, 'agent_b': None},
        "compliant": {'agent_a': True, 'agent_b': True},
    },
}


def _normalise(value):
    return " ".join(str(value or "").split())


def _meets_contract(output, contract):
    """Declarative instruction-following check; None when no contract is recorded."""
    if not isinstance(contract, dict):
        return None
    kind = str(contract.get("type") or "")
    text = str(output or "")
    if kind == "exact_lines":
        expected = [str(line) for line in (contract.get("lines") or [])]
        return [line.strip() for line in text.strip().splitlines() if line.strip()] == expected
    if kind == "regex_fullmatch":
        pattern = str(contract.get("pattern") or "")
        try:
            return re.fullmatch(pattern, text.strip()) is not None
        except re.error:
            return None
    if kind == "contains":
        needle = str(contract.get("text") or "")
        return bool(needle) and needle in text
    return None


def _answer_correct(output, expected):
    """Answer correctness, kept separate from format compliance."""
    if expected is None:
        return None
    target = _normalise(expected if isinstance(expected, str)
                        else json.dumps(expected, ensure_ascii=False))
    if not target:
        return None
    body = _normalise(output)
    return body == target or target in body


def _load_cases():
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _candidates(case):
    """候选按名字排序后交给裁判，和退役前评估面读候选的顺序一致。"""
    return [AgentResult(name, text, 0) for name, text in sorted(case["candidates"].items())]


def _judge(case):
    results = _candidates(case)
    terms = DeterministicJudge.explain(case["prompt"], results)
    winner, scores = DeterministicJudge.choose(case["prompt"], results)
    return results, terms, winner, scores


class OfflineJudgeCasesTest(unittest.TestCase):
    def test_the_fixture_still_carries_all_twenty_four_cases(self):
        cases = _load_cases()
        self.assertEqual(24, len(cases), "退役时导出的是 24 条用例")
        ids = [case["id"] for case in cases]
        self.assertEqual(len(set(ids)), len(ids), "用例 id 不得重复")
        self.assertEqual(sorted(ids), sorted(BASELINE_VERDICTS),
                         "夹具与基线表的用例必须一一对应")

    def test_every_case_is_scored_without_a_weight_argument(self):
        for case in _load_cases():
            with self.subTest(case=case["case_name"]):
                results, terms, winner, scores = _judge(case)
                self.assertIn(winner.agent, case["candidates"])
                self.assertEqual(sorted(case["candidates"]), sorted(scores))
                # 每个候选的中性权重都是 1.0：退役后不再有按候选调权的入口。
                self.assertEqual({1.0}, {entry["weight"] for entry in terms.values()})

    def test_verdicts_match_the_pre_retirement_baseline(self):
        for case in _load_cases():
            expected = BASELINE_VERDICTS[case["id"]]
            with self.subTest(case=case["case_name"]):
                results, terms, winner, scores = _judge(case)
                self.assertEqual(expected["winner"], winner.agent, "胜出候选变了")
                self.assertEqual(expected["decision_basis"],
                                 DeterministicJudge.decision_basis(terms, winner),
                                 "判定依据变了")
                self.assertEqual(expected["scores"],
                                 {name: round(value, 6) for name, value in sorted(scores.items())},
                                 "分数变了")
                self.assertEqual(expected["compliant"],
                                 {name: terms[name]["compliant"] for name in sorted(terms)},
                                 "合规判定变了")

    def test_judge_is_order_independent_on_every_case(self):
        """把候选顺序倒过来再判一次，结果必须一样。"""
        for case in _load_cases():
            with self.subTest(case=case["case_name"]):
                _results, _terms, winner, scores = _judge(case)
                reversed_results = list(reversed(_candidates(case)))
                reversed_winner, reversed_scores = DeterministicJudge.choose(
                    case["prompt"], reversed_results)
                self.assertEqual(winner.agent, reversed_winner.agent)
                self.assertEqual(scores, reversed_scores)

    def test_expected_answers_still_mean_what_the_case_recorded(self):
        """`expected` 为空就没有对错判定；有值时判定与退役前逐条相同。"""
        for case in _load_cases():
            baseline = BASELINE_VERDICTS[case["id"]]
            with self.subTest(case=case["case_name"]):
                results = _candidates(case)
                measured = {item.agent: _answer_correct(item.output, case["expected"])
                            for item in results}
                self.assertEqual(baseline["answer_correct"], measured)
                if case["expected"] is None:
                    self.assertEqual({None}, set(measured.values()),
                                     "没有 expected 的用例不得凭空判对错")
                followed = {item.agent: _meets_contract(item.output, case["format_contract"])
                            for item in results}
                self.assertEqual(baseline["instruction_followed"], followed)

    def test_the_cases_need_no_database_and_no_model_call(self):
        def forbidden(*_args, **_kwargs):
            raise AssertionError("离线用例不得连数据库，也不得发起模型请求或起进程")

        with mock.patch.object(sqlite3, "connect", side_effect=forbidden), \
                mock.patch.object(subprocess, "Popen", side_effect=forbidden):
            for case in _load_cases():
                with self.subTest(case=case["case_name"]):
                    _judge(case)


class RetiredStrategyCommandsTest(unittest.TestCase):
    """退役的七个命令必须明确说「已退役」，不得静默成功，也不得先动状态。

    放在本文件里，是因为它们和 24 条用例是同一件事的两半：功能退役，用例留下。
    `tests/test_cli.py` 不在本卡的文件归属里，所以没有加到那边。
    """

    RETIRED = (
        "evolve", "eval", "replay", "regression",
        "judge-baseline", "promote", "rollback",
    )

    def test_each_retired_command_reports_retirement_and_fails(self):
        for name in self.RETIRED:
            with self.subTest(command=name):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = main([name])
                self.assertEqual(2, code, "退役命令不得返回成功")
                self.assertIn("已退役", err.getvalue())
                self.assertIn(name, err.getvalue())
                self.assertEqual("", out.getvalue(), "退役命令不得往标准输出写结果")

    def test_a_retired_command_is_refused_before_the_config_is_read(self):
        """配置根本读不到时也必须先报退役，说明拦截发生在加载配置和开库之前。"""
        missing = Path(tempfile.mkdtemp()) / "does-not-exist.json"
        for name in self.RETIRED:
            with self.subTest(command=name):
                err = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(err):
                    code = main(["--config", str(missing), name])
                self.assertEqual(2, code)
                self.assertIn("已退役", err.getvalue())
                self.assertFalse(missing.exists(), "退役命令不得创建任何文件")

    def test_retired_commands_are_not_reachable_by_another_name(self):
        """命令表里只登记这七个，别的新名字仍然是 argparse 的用法错误。"""
        from xiaoba_agentos.cli import RETIRED_COMMANDS
        self.assertEqual(sorted(self.RETIRED), sorted(RETIRED_COMMANDS))


if __name__ == "__main__":
    unittest.main()
