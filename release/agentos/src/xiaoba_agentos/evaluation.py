from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .adapters import AgentResult
from .orchestrator import DeterministicJudge
from .store import EventStore


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").split())


def _meets_contract(output: str, contract: Optional[Dict[str, Any]]) -> Optional[bool]:
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


def _answer_correct(output: str, expected: Any) -> Optional[bool]:
    """Answer correctness, kept separate from format compliance."""
    if expected is None:
        return None
    target = _normalise(expected if isinstance(expected, str) else json.dumps(expected, ensure_ascii=False))
    if not target:
        return None
    body = _normalise(output)
    return body == target or target in body


class EvaluationService:
    """Offline evaluation plane; never leases production tasks or writes agent_runs."""

    def __init__(self, store: EventStore):
        self.store = store

    def add_case(self, dataset: str, prompt: str, expected: Any = None, metadata: Optional[Dict[str, Any]] = None) -> str:
        return self.store.add_evaluation_case(dataset, prompt, expected, metadata)

    def replay(self, output: str, prompt: str, strategy_version: int = 1, mode: str = "replay", case_id: Optional[str] = None, dataset: Optional[str] = None) -> Dict[str, Any]:
        result = AgentResult("replay", output, 0)
        winner, scores = DeterministicJudge.choose(prompt, [result], {})
        judge = {"winner": winner.agent, "scores": scores, "deterministic": True}
        evaluation_id = self.store.record_evaluation(mode, prompt, output, judge, strategy_version, dataset, case_id)
        self.store.record_evaluation_run(evaluation_id, "replay", output, "completed", 0)
        return {"evaluation_id": evaluation_id, "mode": mode, "judge": judge}

    def regression(self, dataset: str, strategy_version: int = 1) -> Dict[str, Any]:
        """Evaluate explicit recorded fixtures, never invoke or invent a model result."""
        cases = self.store.list_evaluation_cases(dataset)
        results = []
        passed = bool(cases)
        failed_case_ids = []
        for case in cases:
            metadata = json.loads(case.get("metadata_json") or "{}")
            fixture = metadata.get("fixture_output")
            expected = json.loads(case["expected_json"]) if case.get("expected_json") else None
            expected_text = expected if isinstance(expected, str) else (
                json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                if expected is not None else None
            )
            ok = isinstance(fixture, str) and bool(fixture) and (expected_text is None or fixture == expected_text)
            judge = {"fixture": True, "deterministic": True, "expected_match": ok}
            evaluation_id = self.store.record_evaluation(
                "regression", case["prompt"], fixture if isinstance(fixture, str) else None,
                judge, strategy_version, dataset, case["id"], status="completed" if ok else "failed",
            )
            self.store.record_evaluation_run(
                evaluation_id,
                "fixture",
                fixture if isinstance(fixture, str) else None,
                "completed" if ok else "failed",
                0,
            )
            results.append({"case_id": case["id"], "passed": ok, "evaluation_id": evaluation_id})
            passed = passed and ok
            if not ok:
                failed_case_ids.append(case["id"])
        self.store.record_gate(
            strategy_version,
            "regression",
            "passed" if passed else "failed",
            {
                "dataset": dataset,
                "fixture": True,
                "case_count": len(cases),
                "failed_case_ids": failed_case_ids,
            },
        )
        return {
            "dataset": dataset,
            "strategy_version": strategy_version,
            "passed": passed,
            "cases": results,
            "fixture_semantics": "metadata.fixture_output",
        }

    def adjudication_baseline(self, dataset: str, strategy_version: int = 1) -> Dict[str, Any]:
        """Score recorded head-to-head candidates and persist every judge term.

        `regression` only compares a fixture string to its expectation and never
        calls the judge, so it cannot detect a scoring regression.  This measures
        the judge itself: coverage, relevance density, consensus, structure and
        length quality are stored per candidate alongside instruction-following
        and answer-correctness verdicts, which is what a re-weighting has to be
        checked against.  Candidates come from `metadata.candidates`; no model is
        ever invoked.
        """
        cases = self.store.list_evaluation_cases(dataset)
        rows: List[Dict[str, Any]] = []
        inverted: List[str] = []
        disagreed: List[str] = []
        scored = 0
        for case in cases:
            metadata = json.loads(case.get("metadata_json") or "{}")
            candidates = metadata.get("candidates")
            if not isinstance(candidates, dict) or len(candidates) < 2:
                continue
            contract = metadata.get("format_contract")
            expected = json.loads(case["expected_json"]) if case.get("expected_json") else None
            latencies = metadata.get("latency_ms")
            latencies = latencies if isinstance(latencies, dict) else {}
            results = [
                AgentResult(str(name), str(text), int(latencies.get(str(name), 0) or 0))
                for name, text in sorted(candidates.items())
            ]
            terms = DeterministicJudge.explain(case["prompt"], results, {})
            winner, scores = DeterministicJudge.choose(case["prompt"], results, {})
            per_candidate = {
                item.agent: {
                    "instruction_followed": _meets_contract(item.output, contract),
                    "answer_correct": _answer_correct(item.output, expected),
                    "derived_compliant": terms[item.agent]["compliant"],
                    "violations": terms[item.agent]["violations"],
                    "chars": len(item.output.strip()),
                }
                for item in results
            }
            correctness = [value["answer_correct"] for value in per_candidate.values()]
            compliance = [value["instruction_followed"] for value in per_candidate.values()]
            winner_correct = per_candidate[winner.agent]["answer_correct"]
            winner_followed = per_candidate[winner.agent]["instruction_followed"]
            # Two distinct inversions: picking a factually wrong answer over a
            # right one, and picking a format violator over a compliant answer.
            # Task 302c9408 was the second kind, not the first.
            verdict_inverted = winner_correct is False and any(value is True for value in correctness)
            format_inverted = winner_followed is False and any(value is True for value in compliance)
            defective = verdict_inverted or format_inverted
            # The judge derives its rules from the prompt; the case records the
            # human-verified contract.  Where they disagree the detector, not the
            # weighting, is what needs fixing.
            detector_disagreed = sorted(
                agent
                for agent, value in per_candidate.items()
                if value["instruction_followed"] is not None
                and bool(value["instruction_followed"]) != value["derived_compliant"]
            )
            contributions = {
                agent: {name: round(value, 6) for name, value in entry["contributions"].items()}
                for agent, entry in terms.items()
            }
            judge = {
                "deterministic": True,
                "baseline": True,
                "winner": winner.agent,
                "scores": scores,
                "decision_basis": DeterministicJudge.decision_basis(terms, winner),
                "restatement_task": terms[winner.agent]["restatement_task"],
                "detected_constraints": terms[winner.agent]["constraints"],
                "term_weights": terms[winner.agent]["term_weights"],
                "terms": {
                    agent: {k: round(v, 6) for k, v in entry.items() if isinstance(v, float)}
                    for agent, entry in terms.items()
                },
                "term_contributions": contributions,
                "candidates": per_candidate,
                "expected_answer": expected,
                "format_contract": contract,
                "winner_correct": winner_correct,
                "winner_instruction_followed": winner_followed,
                "verdict_inverted": verdict_inverted,
                "format_inverted": format_inverted,
                "detector_disagreed": detector_disagreed,
            }
            evaluation_id = self.store.record_evaluation(
                "offline",
                case["prompt"],
                winner.output,
                judge,
                strategy_version,
                dataset,
                case["id"],
                source_task_id=str(metadata.get("source_task_id") or "") or None,
                status="failed" if defective else "completed",
            )
            for item in results:
                self.store.record_evaluation_run(
                    evaluation_id, item.agent, item.output, "completed", item.latency_ms
                )
            scored += 1
            if defective:
                inverted.append(case["id"])
            if detector_disagreed:
                disagreed.append(case["id"])
            rows.append(
                {
                    "case_id": case["id"],
                    "source_task_id": metadata.get("source_task_id"),
                    "evaluation_id": evaluation_id,
                    "winner": winner.agent,
                    "decision_basis": judge["decision_basis"],
                    "winner_correct": winner_correct,
                    "winner_instruction_followed": winner_followed,
                    "verdict_inverted": verdict_inverted,
                    "format_inverted": format_inverted,
                    "detector_disagreed": detector_disagreed,
                    "candidates": per_candidate,
                    "terms": judge["terms"],
                    "term_contributions": contributions,
                }
            )
        self.store.record_gate(
            strategy_version,
            "judge_baseline",
            "failed" if inverted else ("passed" if scored else "skipped"),
            {
                "dataset": dataset,
                "case_count": scored,
                "defective_case_ids": inverted,
                "detector_disagreement_case_ids": disagreed,
                "fixture_semantics": "metadata.candidates",
            },
        )
        return {
            "dataset": dataset,
            "strategy_version": strategy_version,
            "cases_scored": scored,
            "defective_case_ids": inverted,
            "detector_disagreement_case_ids": disagreed,
            "passed": not inverted and bool(scored),
            "rows": rows,
        }
