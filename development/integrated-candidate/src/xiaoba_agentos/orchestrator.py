from __future__ import annotations
from dataclasses import replace

import hashlib
import json
import math
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

from .adapters import AdapterError, AgentResult, PermanentAdapterError, build_adapter
from .adjudication import (
    asks_for_restatement,
    constraint_violations,
    detect_output_constraint_state,
    detect_output_constraints,
)
from .capabilities import CapabilityRegistry, capable_agents, web_capable_agents
from .config import AppConfig
from .gateway import local_cli_readiness
from .routing import adjudicated_label
from .memory import MemoryBridge
from .output_hygiene import detect_reasoning_leak
from .release import release_binding_status
from .receipts import ExecutionReceiptStore
from .recovery import classify_failure, sanitize_diagnostic_text
from .circuit_breaker import CircuitBreaker
from .task_context import render_context
from .store import EventStore
from .standby_health import EXTERNAL_FAILURES, coverable_health, health_evidence
from .workflow import agent_prompt, contribution_records, validate_plan


# A running CLI calls progress_update every few seconds. Without pacing, one
# review writes a `task.lease_renewed` plus an `agent.progress` per call, so the
# event chain grows by hundreds of rows per task. These three thresholds trade
# resolution for size; none of them moves the no-progress clock, which lives in
# `adapters.run_cli`.
# Renew only once this fraction of the lease is left: 1800s renews ~every 15min.
LEASE_RENEW_REMAINING_RATIO = 0.5
# Shortest gap between two `agent.progress` events written for the same run.
PROGRESS_EVENT_MIN_INTERVAL_SECONDS = 30.0
# A flat run still gets one low-frequency "still here" event after this much quiet.
PROGRESS_EVENT_MAX_SILENCE_SECONDS = 60.0


def _tokens(text: str) -> set:
    lowered = text.lower()
    latin = set(re.findall(r"[a-z0-9_]+", lowered))
    chinese = re.findall(r"[\u4e00-\u9fff]", lowered)
    return latin | set(chinese) | {"".join(chinese[index:index + 2]) for index in range(len(chinese) - 1)}


_STRUCTURE_LINE = re.compile(
    r"^[ \t]*(?:"
    r"#{1,6}[ \t]+\S"
    r"|\d+[.、)）][ \t]*\S"
    r"|[（(]\d+[)）][ \t]*\S"
    r"|[-*•][ \t]+\S"
    r"|[一二三四五六七八九十]+[、.．][ \t]*\S"
    r"|\w{1,12}(?:：[ \t]*\S|:[ \t]+\S)"
    r")"
)


def _structure_density(text: str) -> float:
    """Fraction of lines that carry their own structure.

    The judge term this replaces counts five section words that `_full_prompt`
    appends to every task, so echoing the instruction back scores full marks; it
    saturated in 29 of the 48 baseline candidates and, being bound to that
    vocabulary, said nothing about the answer.  Pure density is vocabulary-free,
    length-neutral and measurably discriminating -- it saturates in 2 of 48 and
    halves the number of cases the judge cannot separate on score at all.  An
    earlier draft scaled this by two and capped it, which saturated at 28/48 and
    was no better than the term it replaced.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    return sum(1 for line in lines if _STRUCTURE_LINE.match(line)) / len(lines)


def _length_fit(length: int) -> float:
    """Flat across the normal range; only empty or runaway output is penalised.

    The `length_quality` term this is written for ramps to its cap at 300 characters,
    so a correct four-character answer scores 0.0007 against 0.05 for anything longer
    -- roughly a 70x penalty for being concise. Length is not evidence of quality, so
    it should be bounded at the two pathological ends rather than rewarded.
    """
    if length <= 0:
        return 0.0
    if length <= 4000:
        return 1.0
    return math.exp(-(length - 4000) / 4000.0)


class DeterministicJudge:
    """A reproducible fallback judge; it never pretends to be semantic consensus.

    Two measurements over the 24-case `judge` baseline shaped this.  Prompt overlap
    is evidence of quality only when the task asks for the prompt to be restated;
    otherwise it rewards paraphrasing the question, and the winner had higher
    coverage in 20 of 24 cases and lower in none.  And no weighting of soft terms can
    guarantee that an answer violating a rule stated in the prompt loses to one that
    obeys it, so compliance is a partition rather than a score.

    With exactly two candidates `consensus` is symmetric and therefore constant, so a
    two-agent task carrying no stated output rule leaves the judge with almost no
    correctness signal -- 6 of the 24 baseline cases tie on score outright.  That is a
    property of a deterministic bag-of-words judge, not something the weighting can
    hide, so `decision_basis` names the rule that actually decided instead of letting
    every verdict be read as a quality judgement.
    """

    TERM_WEIGHTS = {
        "coverage": 0.50,
        "relevance_density": 0.18,
        "consensus": 0.17,
        "structure": 0.10,
        "length_fit": 0.05,
    }
    ECHO_TERMS = ("coverage", "relevance_density")

    @classmethod
    def effective_weights(cls, restatement: bool) -> Dict[str, float]:
        """Renormalise the soft weights; both echo terms drop out together.

        Zeroing coverage alone leaves `relevance_density` deciding, and that is the
        same overlap measurement normalised by answer length -- the verbose echo still
        won the leak-free baseline control on it.  The freed weight is spread over the
        surviving terms in their original proportions rather than reassigned by hand.
        """
        if restatement:
            return dict(cls.TERM_WEIGHTS)
        kept = {
            name: value for name, value in cls.TERM_WEIGHTS.items() if name not in cls.ECHO_TERMS
        }
        total = sum(kept.values())
        return {name: value / total for name, value in kept.items()}

    @classmethod
    def explain(
        cls, prompt: str, results: List[AgentResult]
    ) -> Dict[str, Dict[str, Any]]:
        """Return every term, weight and violation behind each score."""
        constraints = detect_output_constraints(prompt)
        restatement = asks_for_restatement(prompt)
        effective = cls.effective_weights(restatement)
        prompt_tokens = _tokens(prompt)
        breakdown: Dict[str, Dict[str, Any]] = {}
        for result in results:
            output_tokens = _tokens(result.output)
            shared = len(prompt_tokens & output_tokens)
            peers = [_tokens(item.output) for item in results if item.agent != result.agent]
            length = len(result.output.strip())
            violations = constraint_violations(result.output, constraints)
            terms = {
                "coverage": shared / max(1, len(prompt_tokens)) if restatement else 0.0,
                "relevance_density": shared / max(1, len(output_tokens)) if restatement else 0.0,
                "consensus": sum(
                    len(output_tokens & peer) / max(1, len(output_tokens | peer)) for peer in peers
                ) / max(1, len(peers)),
                "structure": _structure_density(result.output),
                "length_fit": _length_fit(length),
            }
            entry: Dict[str, Any] = dict(terms)
            # A per-agent weight used to come from the retired strategy row.
            # Strategy evolution was retired on 2026-10-11, so every candidate
            # now carries the neutral weight the empty strategy produced; the
            # key stays in the payload so recorded `judge.breakdown` events keep
            # their shape.
            weight = 1.0
            entry["weight"] = weight
            entry["score"] = weight * sum(effective[name] * terms[name] for name in effective)
            entry["term_weights"] = dict(effective)
            entry["contributions"] = {
                name: weight * effective[name] * terms[name] for name in effective
            }
            entry["violations"] = violations
            entry["compliant"] = not violations
            entry["restatement_task"] = restatement
            entry["constraints"] = [
                {"kind": item.kind, "detail": item.detail} for item in constraints
            ]
            entry["shared_prompt_tokens"] = float(shared)
            entry["prompt_tokens"] = float(len(prompt_tokens))
            entry["output_tokens"] = float(len(output_tokens))
            entry["output_chars"] = float(length)
            entry["latency_ms"] = float(result.latency_ms or 0)
            entry["output_digest"] = hashlib.sha256(
                (result.output or "").encode("utf-8")
            ).hexdigest()
            breakdown[result.agent] = entry
        return breakdown

    @staticmethod
    def _rank(entry: Dict[str, Any], result: AgentResult) -> Tuple[int, float, int, str, str]:
        """Compliance first, then score, then speed, then content digest, then name.

        The tie-break this replaces was the agent's own name *alone*, so an exact
        score tie always went to the alphabetically later candidate: for the current
        pair that meant `dsh_review` could never lose a tie and `agy_review` could
        never win one.  Latency is a recorded operational fact and the digest favours
        no identity.  The name is reached only once the two answers are byte-identical
        -- there is then no difference left for it to favour, and without it the
        verdict would depend on the order the results were listed in.
        """
        return (
            1 if entry["compliant"] else 0,
            entry["score"],
            -int(result.latency_ms or 0),
            entry["output_digest"],
            result.agent,
        )

    @classmethod
    def choose(cls, prompt: str, results: List[AgentResult]) -> Tuple[AgentResult, Dict[str, float]]:
        breakdown = cls.explain(prompt, results)
        scores = {agent: entry["score"] for agent, entry in breakdown.items()}
        winner = max(results, key=lambda item: cls._rank(breakdown[item.agent], item))
        return winner, scores

    @classmethod
    def decision_basis(cls, breakdown: Dict[str, Dict[str, Any]], winner: AgentResult) -> str:
        """Name the first ranking component that actually separated the candidates."""
        if len(breakdown) < 2:
            return "sole_candidate"
        entries = list(breakdown.values())
        for name, key in (
            ("constraint_compliance", lambda entry: 1 if entry["compliant"] else 0),
            ("score", lambda entry: entry["score"]),
            ("latency_after_tie", lambda entry: -int(entry["latency_ms"])),
            ("content_digest_after_tie", lambda entry: entry["output_digest"]),
        ):
            best = max(key(entry) for entry in entries)
            if sum(1 for entry in entries if key(entry) == best) < len(entries):
                return name
        return "identical_content"


_DECISION_BASIS_LABEL = {
    "constraint_compliance": "题面明确的输出约束：违反约束的候选不得胜出",
    "score": "分项评分",
    "latency_after_tie": "评分完全相同，按耗时决胜，不代表质量判断",
    "content_digest_after_tie": "评分与耗时均相同，按内容摘要决胜，不代表质量判断",
    "identical_content": "各候选答案字节完全相同，按 Agent 名称固定选取，不代表质量判断",
    "sole_candidate": "仅有一个候选，无需比较",
}


_OUTPUT_BUDGET_SCOPE = "task_output_budget"


def _failure_record(
    agent: str, error: str, source: Any, failure: Any, failure_class: Optional[str] = None
) -> Dict[str, Any]:
    """Build one `failures` entry for the frozen payload.

    The task-level output-budget reason is taken only from the structured
    attribute the adapter sets on the exception. The error text is never parsed
    for it, so a message that merely mentions a limit cannot downgrade an
    environment fault and let the queue through.
    """
    record: Dict[str, Any] = {
        "agent": agent,
        "error": error,
        "failure_class": failure_class or failure.name,
        "repairable": failure.repairable,
        "external": failure.external,
    }
    if getattr(source, "output_limit_exhausted", False) is True:
        record["output_limit_exhausted"] = True
        record["failure_scope"] = _OUTPUT_BUDGET_SCOPE
    return record


def _is_output_budget(item: Dict[str, Any]) -> bool:
    """True only for a failure that carries the structured task-budget marker."""
    return item.get("output_limit_exhausted") is True


def _freeze_priority(failures: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pick the failure that names the frozen task's `failure_class`.

    An environment fault outranks a task-level output budget, so a mixed failure
    is never reported as though the backend were fine.
    """
    environment = [item for item in failures if not _is_output_budget(item)]
    pool = environment or failures
    return next((item for item in pool if not item.get("external")), pool[0])


# Only a fault outside this codebase may be covered by the standby. An exhausted
# output budget carries `external_upstream` too, so it is ruled out separately by
# `_is_output_budget` rather than by this set.
_STANDBY_SUBSTITUTABLE_CLASSES = EXTERNAL_FAILURES


def _substitution_payload(
    replaced: str, standby_name: str, reason: str, failure_class: str, error: str
) -> Dict[str, Any]:
    """One `standby.substituted` payload; `model` is filled in once it is known."""
    return {
        "replaced": replaced,
        "standby": standby_name,
        "reason": reason,
        "failure_class": failure_class,
        "error": error,
        "model": "",
    }


def _reusable_run(run: Optional[Dict[str, Any]]) -> bool:
    """A finished run whose answer may stand as this side's conclusion as-is.

    Reusing an answer that still carries model reasoning only gets it
    quarantined again; give the agent a fresh run instead (the invocation budget
    still bounds this). A side that already answered this way is a real
    conclusion, so it is also a side the standby must never be swapped in for.
    """
    return bool(
        run
        and run["status"] == "completed"
        and run.get("output")
        and not detect_reasoning_leak(run["output"])
    )


class Orchestrator:
    def __init__(self, config: AppConfig, store: EventStore):
        self.config = config
        self.store = store
        self.memory = MemoryBridge(config, store)
        self.circuit_breaker = CircuitBreaker(threshold=2)

    def _run_agent(self, task_id: str, agent_config: Any, prompt: str, context: str,
                   lease_attempt: Optional[int] = None) -> AgentResult:
        run_id = self.store.start_agent_run(
            task_id,
            agent_config.name,
            {
                "driver": str(agent_config.raw.get("driver", agent_config.type)),
                "gateway_route": agent_config.raw.get("gateway_route"),
                "cli_route": agent_config.raw.get("cli_route"),
            },
        )
        receipt = ExecutionReceiptStore(self.config.runtime_dir / "receipts" / task_id / (str(run_id) + ".json"))
        receipt.record_call({"task_id": task_id, "step_id": "agent_run:" + agent_config.name,
                             "agent": agent_config.name, "tool": "agent_adapter",
                             "tool_call_id": str(run_id), "command_hash": receipt.command_hash(prompt + "\n" + context)})
        started = time.monotonic()
        lease_seconds = float(self.config.worker["lease_seconds"])
        # What the last `agent.progress` event said, so the next one only goes out
        # when something is actually different or the quiet got long enough. The
        # sentinel makes the first callback always qualify.
        pace = {"stage": None, "activity_events": 0, "written_at": float("-inf")}

        def progress_update(progress):
            if lease_attempt is not None:
                # Every callback still has to earn the right to keep running; a lost
                # lease stops the CLI and never triggers substitution.
                if not self.store.owns_lease(task_id, lease_attempt):
                    raise PermanentAdapterError("task lease lost; stop this CLI without substitution")
                lease_left = self.store.current_lease_until(task_id, lease_attempt)
                if lease_left is not None and lease_left - time.time() < lease_seconds * LEASE_RENEW_REMAINING_RATIO:
                    if not self.store.renew_lease(task_id, lease_attempt, lease_seconds):
                        raise PermanentAdapterError("task lease lost; stop this CLI without substitution")
            # The receipt is a file, not an event: it keeps updating on every callback.
            receipt.record_progress(str(run_id), progress)
            # Lease state is wall-clock (that is what the queue stores); pacing two
            # events is an interval, so it uses the monotonic clock.
            # `written_at` moves only when an event is written: a throttled
            # callback that moved it would keep the silence backstop from ever
            # firing. So the longest gap between events is the 60-second
            # backstop plus one callback interval (at most 5 seconds).
            now = time.monotonic()
            quiet_for = now - pace["written_at"]
            if not (
                progress["stage"] != pace["stage"]
                or (progress["activity_events"] > pace["activity_events"]
                    and quiet_for >= PROGRESS_EVENT_MIN_INTERVAL_SECONDS)
                or quiet_for >= PROGRESS_EVENT_MAX_SILENCE_SECONDS
            ):
                return
            pace["stage"] = progress["stage"]
            pace["activity_events"] = progress["activity_events"]
            pace["written_at"] = now
            self.store.append_event(task_id, "agent.progress", {
                "agent": agent_config.name, "run_id": run_id, **receipt.get(str(run_id))["progress"],
            })

        try:
            adapter = build_adapter(agent_config)
            if hasattr(adapter, "progress_callback"):
                adapter.progress_callback = progress_update
                adapter.progress_interval = min(5.0, max(0.01, float(self.config.worker["lease_seconds"]) / 3))
            result = adapter.run(prompt, context)
        except Exception as exc:
            for attempt in getattr(exc, "route_attempts", []):
                self.store.append_event(task_id, "agent.provider_attempt", {"agent":agent_config.name, "run_id":run_id, **attempt})
            latency = int((time.monotonic() - started) * 1000)
            prior = [run for run in self.store.task_runs(task_id) if run["agent_name"] == agent_config.name and run["id"] != run_id]
            external_upstream = bool(
                isinstance(exc, AdapterError)
                and exc.transient
                and getattr(exc, "external_upstream", False)
            )
            step_id = "agent_run:" + agent_config.name
            breaker_state = self.circuit_breaker.record_step(
                task_id, step_id, tool_calls=1, tool_results=1,
                exit_code=1, error_signature=str(exc),
            )
            # The in-process breaker handles repeated failures during one
            # worker lifetime; the prior-run check preserves the same
            # fail-closed behavior across worker restarts.
            consecutive_failure = (
                (breaker_state.tripped or (bool(prior) and prior[-1].get("status") != "completed"))
                and not external_upstream
            )
            status = "permanent_error" if consecutive_failure or (isinstance(exc, AdapterError) and not exc.transient) else "transient_error"
            if consecutive_failure:
                self.store.append_event(task_id, "execution.circuit_breaker", {"agent": agent_config.name, "step_id": step_id, "reason": breaker_state.reason})
            elif external_upstream and prior:
                self.store.append_event(task_id, "execution.upstream_retry", {
                    "agent": agent_config.name,
                    "step_id": "agent_run:" + agent_config.name,
                    "reason": "third_party_transport_or_slow_response",
                    "prior_failures": len(prior),
                })
            receipt.associate_result(str(run_id), "adapter-result:" + str(run_id), exit_code=1)
            failure = classify_failure(exc)
            self.store.finish_agent_run(
                run_id, status, None, str(exc), latency, failure_class=failure.name
            )
            raise
        for attempt in getattr(result, "route_attempts", []):
            self.store.append_event(task_id, "agent.provider_attempt", {"agent":agent_config.name, "run_id":run_id, **attempt})
        receipt.associate_result(str(run_id), "adapter-result:" + str(run_id), exit_code=0)
        self.circuit_breaker.record_step(
            task_id, "agent_run:" + agent_config.name,
            tool_calls=1, tool_results=1, exit_code=0,
        )
        self.store.finish_agent_run(run_id, "completed", result.output, None, result.latency_ms)
        if result.provider or result.model:
            previous_route = self.store.latest_route_resolved(result.agent)
            route_payload = {
                "agent": result.agent,
                "provider": result.provider,
                "model": result.model,
                "source": result.route_source or "static",
            }
            self.store.append_event(
                task_id,
                "agent.route_resolved",
                route_payload,
            )
            if previous_route and (
                previous_route.get("provider") != result.provider
                or previous_route.get("model") != result.model
            ):
                change = {
                    "agent": result.agent,
                    "from": {
                        "provider": previous_route.get("provider"),
                        "model": previous_route.get("model"),
                    },
                    "to": {
                        "provider": result.provider,
                        "model": result.model,
                    },
                }
                self.store.append_event(task_id, "agent.route_changed", change)
                self.store.create_notification("agent.route_changed", change, task_id=task_id)
        return result

    def _completion_committed(self, task_id: str) -> bool:
        current = self.store.get_task(task_id)
        from .experience_workflow import audit_committed
        return bool(
            current
            and current["status"] == "completed"
            and (self.store.get_memory_candidate(task_id) or
                 audit_committed(self.store, current))
        )

    def _prior_substitution(self, task_id: str) -> Optional[Dict[str, Any]]:
        """The substitution this task already made, read back from the chain.

        One substitution per task is the cap, and a frozen task resumes in a
        fresh process where the round that decided it is gone, so the event
        chain is the only record that survives. Reading it back is also what
        keeps a retried or resumed round from announcing the same cover twice.
        """
        with self.store.read_only() as conn:
            row = conn.execute(
                "SELECT payload_json FROM events WHERE task_id=? "
                "AND event_type='standby.substituted' ORDER BY sequence LIMIT 1",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _standby_substitution(
        self,
        *,
        standby: Any,
        standby_usable: bool,
        already_substituted: bool,
        workflow: Dict[str, Any],
        required_capabilities: List[str],
        needs_web: bool,
        primaries: List[Any],
        results: List[AgentResult],
        failures: List[Dict[str, Any]],
        leaked: List[str],
        budget_left: int,
    ) -> Optional[Tuple[Any, Dict[str, Any]]]:
        """The one primary the standby may cover this round, and its record.

        None for every combination where covering is forbidden. A primary that
        answered is never re-reviewed by somebody else, so only a side that
        produced no conclusion is eligible, and only when its fault came from
        outside: an internal fault, an exhausted output budget or a quarantined
        reasoning leak all keep the existing flow. Two failed primaries are not
        covered because one standby cannot pair with itself, and one
        substitution per task is the cap -- a retried or resumed round inherits
        it. A disabled or unhealthy standby has nothing to offer.

        Collaboration tasks are excluded because their roster and per-agent
        assignments are frozen by name and do not contain the standby. Tasks with
        a capability requirement are excluded because the standby runs with every
        tool switched off and cannot satisfy one.
        """
        if standby is None or not standby_usable or already_substituted:
            return None
        if workflow or required_capabilities or needs_web or leaked:
            return None
        primary_names = {agent.name for agent in primaries}
        # The standby only ever completes a pair of two distinct primaries; with
        # fewer than two enabled there is nobody for it to pair with.
        if len(primary_names) < 2:
            return None
        # Config keeps names unique and the standby out of the primaries; this
        # holds on its own so a standby can never stand in for, or pair with, a
        # primary of its own name (round-40 review).
        if str(standby.name) in primary_names:
            return None
        failed = [item for item in failures if str(item.get("agent")) in primary_names]
        if len(failed) != 1:
            return None
        failure = failed[0]
        if _is_output_budget(failure):
            return None
        if str(failure.get("failure_class")) not in _STANDBY_SUBSTITUTABLE_CLASSES:
            return None
        failed_name = str(failure.get("agent"))
        answered = [
            item
            for item in results
            if item.agent in primary_names and str(item.output or "").strip()
        ]
        # Callers put each agent in either results or failures, but this
        # function does not rely on that. If the failed side also left text it
        # counts as having answered, and an answered primary is never covered;
        # the one surviving conclusion must come from the *other* primary.
        if any(item.agent == failed_name for item in answered):
            return None
        if len(answered) != 1 or budget_left < 1:
            return None
        replaced = next(agent for agent in primaries if agent.name == str(failure.get("agent")))
        return replaced, _substitution_payload(
            replaced.name,
            standby.name,
            "external_failure",
            str(failure.get("failure_class")),
            str(failure.get("error") or ""),
        )

    def _record_substitution(
        self,
        task_id: str,
        payload: Dict[str, Any],
        *,
        model: str,
        delivered: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Write the substitution event and the user's notification, once.

        Both land before the task settles, so a settled result can never exist
        without its substitution record (B20). `delivered` stays None when the
        substitute has not been called yet, which is the case for a substitution
        decided from the health probe before dispatch; its `model` is then the
        one that probe actually used. A substitute that returned nothing reports
        no model, so the probed one is recorded and the gap stays visible in the
        failure record beside it.
        """
        record = dict(payload, model=str(model or ""))
        self.store.append_event(task_id, "standby.substituted", record)
        if record.get("reason") == "external_failure" and record.get("replaced"):
            self.store.record_agent_health(
                str(record["replaced"]),
                "external_blocked",
                failure_class=str(record.get("failure_class") or "unknown"),
                error=str(record.get("error") or "unknown"),
            )
        cause = (
            f"主后台 {record['replaced']} 本次不可用"
            f"（{record['reason']}，故障分类 {record['failure_class']}）"
        )
        if delivered is None:
            outcome = "这一侧在分派前改由备用后台补位"
        elif delivered:
            outcome = "这一侧已由备用后台补上，两份结论一起进入裁判"
        else:
            outcome = (
                "备用后台补位后也没有给出合格结论；只剩一份结论不够，"
                "任务照现有规则处理，不静默"
            )
        message = (
            f"{cause}，{outcome}"
            f"（备用后台 {record['standby']}，模型 {record['model'] or '未知'}）"
        )
        self.store.create_notification(
            "standby.substituted", {**record, "message": message}, task_id=task_id
        )
        return record

    def run_once(self, preflight_when_idle: bool = True) -> Dict[str, Any]:
        """Run one worker poll.

        With preflight_when_idle=False an empty queue returns "idle" before
        the release hash, audit-chain and route checks run. Every gate still
        runs, unchanged and in the same order, before any task is leased. The
        long-running worker passes False and re-runs the full preflight on its
        heartbeat so a blocked or degraded state is still reported while idle.
        """
        has_work = self.store.has_runnable_work()
        if not preflight_when_idle and not has_work:
            return {"status": "idle"}
        release_binding = release_binding_status(self.config)
        if not release_binding.get("ok", False):
            return {
                "status": "blocked_configuration",
                "error": "活动源码未绑定到已验证不可变发布；任务队列未被消费",
                "release_binding": release_binding,
            }
        worker = self.config.worker
        event_chain = self.store.verify_event_chain()
        if not event_chain["ok"]:
            return {"status": "blocked_configuration", "error": "审计链校验失败；任务队列未被消费", "event_chain": event_chain}
        self.store.recover_expired()
        # Only primaries are ever dispatched. The standby is resolved here so the
        # health gate can tell "one primary down but coverable" from "the pair
        # cannot be formed", and so a standby that is itself unhealthy never
        # blocks a queue two healthy primaries could serve. Readiness is
        # checked for the primaries alone: a standby with broken
        # wiring is reported by doctor, not allowed to hold up intake.
        primaries = self.config.primary_agents
        standbys = self.config.standby_agents
        standby = standbys[0] if standbys else None
        enabled = primaries
        if not enabled:
            # A standby only ever stands in for one of two primaries, so with no
            # primary enabled no pair can form; refuse before leasing, as v1.6.4
            # did, instead of leasing a task only to fail it.
            return {"status": "blocked_configuration", "error": "没有启用任何主后台 Agent；任务队列未被消费"}
        required = int(worker.get("minimum_successful_agents", 2))
        if len(enabled) < required:
            # Same rule as health.execution_blockers: the standby covers one
            # side of a pair, it never makes up a missing primary; and without
            # enough primaries no task could ever settle (round 31).
            return {
                "status": "blocked_configuration",
                "error": f"启用的主后台只有 {len(enabled)} 个，少于 {required} 个；"
                         + ("备用不能补足主后台；" if standby is not None else "")
                         + "任务队列未被消费",
            }
        local_readiness = local_cli_readiness(self.config, enabled)
        if not local_readiness["ok"]:
            return {"status": "blocked_configuration", "error": "；".join(local_readiness["problems"]), "local_cli_routes": local_readiness}
        probed = list(enabled) + ([standby] if standby is not None else [])
        enabled_names = [agent.name for agent in probed]
        health_rows = {
            row["agent_name"]: row
            for row in self.store.agent_health(
                enabled_names=enabled_names,
                max_age_seconds=float(worker.get("health_max_age_seconds", 86400)),
            )
        }
        unhealthy = [
            {
                "agent": agent.name,
                "status": health_rows.get(agent.name, {}).get("status", "unprobed"),
                "failure_class": health_rows.get(agent.name, {}).get("failure_class"),
            }
            for agent in probed if health_rows.get(agent.name, {}).get("status") != "healthy"
        ]
        unhealthy_primaries = [
            item for item in unhealthy if standby is None or item["agent"] != standby.name
        ]
        standby_usable = standby is not None and len(unhealthy) == len(unhealthy_primaries)
        if health_rows or any(agent.type == "local_cli" for agent in probed):
            # One unhealthy primary that a healthy standby can cover still yields
            # two independent conclusions; two never do, because a standby may not
            # pair with itself and the queue stays blocked instead.
            can_cover = (standby_usable and len(unhealthy_primaries) == 1
                         and coverable_health(health_rows.get(unhealthy_primaries[0]['agent'])))
            if len(unhealthy_primaries) > (1 if can_cover else 0):
                return {
                    "status": "degraded_health",
                    "error": "后台 Agent 实际推理健康门禁未通过；任务队列未被消费",
                    "agents": unhealthy,
                }
        task = self.store.lease_next(int(worker["lease_seconds"]))
        if not task:
            return {"status": "idle"}

        # Every result for a leased task retains its current or earlier cover,
        # including early failures and lease_lost (round 34). This is reporting
        # only: it must never settle, substitute or write after lease loss.
        substitution: Optional[Dict[str, Any]] = None
        # A historical cover remains reportable even if the standby has since
        # been disabled. Reading history does not enable or dispatch a standby.
        prior_substitution = self._prior_substitution(task["id"])

        def task_result(payload: Dict[str, Any]) -> Dict[str, Any]:
            """Every result this round returns goes through here, so every one
            names the task's cover (this attempt's, else an earlier one's)."""
            return {**payload, "standby_substitution": substitution or prior_substitution}

        try:
            _snap = json.loads(task.get("execution_snapshot_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            _snap = {}
        try:
            validate_plan(self.config, self.store, task, _snap)
        except (ValueError, KeyError, OSError) as exc:
            self.store.fail(task["id"], "invalid collaboration evidence: " + str(exc))
            return task_result({"status": "failed", "task_id": task["id"], "errors": [str(exc)]})
        workflow = _snap.get("workflow", {})
        _req_caps = list(_snap.get("required_capabilities") or [])
        _needs_web = bool(_snap.get("needs_web")) or "native_web_search" in _req_caps
        _gate_agents = list(enabled)
        if _needs_web:
            _gate_agents, _gate_reason = web_capable_agents(_gate_agents, CapabilityRegistry(), needs_web=True)
            if not _gate_agents:
                self.store.fail(task["id"], _gate_reason or "missing native_web_search")
                return task_result({
                    "status": "blocked",
                    "reason": _gate_reason or "no web-capable agent",
                    "capability": "native_web_search",
                    "task_id": task["id"],
                })
        if _req_caps:
            _gate_agents, _missing = capable_agents(_gate_agents, CapabilityRegistry(), _req_caps)
            if not _gate_agents:
                _lacked = sorted({tok for _, toks in _missing for tok in toks})
                self.store.fail(task["id"], "missing capability: " + ", ".join(_lacked))
                return task_result({
                    "status": "blocked",
                    "reason": "missing capability: " + ", ".join(_lacked),
                    "capability": _lacked[0] if _lacked else "unknown",
                    "task_id": task["id"],
                })
        enabled = _gate_agents

        latest = self.store.latest_agent_runs(task["id"])
        try:
            frozen_record = json.loads(task.get("freeze_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            frozen_record = {}
        resume_failed_agents = {
            str(item.get("agent"))
            for item in frozen_record.get("failures", [])
            if item.get("agent")
        }
        resumed_after_freeze = int(task.get("resume_generation") or 0) > int(
            frozen_record.get("resume_generation") or 0
        )
        if (
            standby_usable
            and len(unhealthy_primaries) == 1
            and not workflow
            and not _req_caps
            and not _needs_web
        ):
            replaced_name = str(unhealthy_primaries[0]["agent"])
            # A side that already answered this task holds a real conclusion, so
            # it is never swapped for the standby: covering a failed probe is not
            # a licence to have somebody else re-review an answer that exists.
            if (
                prior_substitution is not None
                and str(prior_substitution.get("replaced") or "") != replaced_name
            ):
                # One substitution per task: the standby already covered the
                # other primary here, and its record names that side. Covering
                # this one too would reuse a record about the wrong agent, so
                # the task freezes instead (STANDBY_POLICY: "已用过一次备用").
                return self._freeze_for_recovery(
                    task,
                    [
                        AgentResult(agent.name, latest[agent.name]["output"],
                                    int(latest[agent.name].get("latency_ms") or 0))
                        for agent in enabled
                        if _reusable_run(latest.get(agent.name))
                    ],
                    [{
                        "agent": replaced_name,
                        "error": "本任务已用过一次备用（替换 "
                        + str(prior_substitution.get("replaced") or "")
                        + "），这次不健康的是 " + replaced_name + "，不再补位",
                        "failure_class": str(unhealthy_primaries[0].get("failure_class") or "unprobed"),
                        "external": unhealthy_primaries[0].get("status") == "external_blocked",
                        # The probe already recorded this side's health; the
                        # freeze must not overwrite its error with this note.
                        "health_already_recorded": True,
                    }],
                    prior_substitution,
                )
            if not _reusable_run(latest.get(replaced_name)):
                position = next(
                    index for index, agent in enumerate(enabled) if agent.name == replaced_name
                )
                enabled = enabled[:position] + [standby] + enabled[position + 1:]
                # The swap still happens on a later round -- an unhealthy primary
                # must not be dispatched -- but the cover was already announced
                # once for this task, so the earlier record is carried instead of
                # a duplicate event and notification.
                substitution = prior_substitution if prior_substitution is not None else (
                    self._record_substitution(
                        task["id"],
                        {**_substitution_payload(
                            replaced_name,
                            standby.name,
                            "health_probe_failed",
                            str(unhealthy_primaries[0].get("failure_class") or "unprobed"),
                            sanitize_diagnostic_text(
                                health_rows.get(replaced_name, {}).get("error") or "健康探针未通过"
                            ),
                        ), 'health_evidence': health_evidence(health_rows.get(replaced_name))},
                        model=str(health_rows.get(standby.name, {}).get("model") or ""),
                    )
                )
        results: List[AgentResult] = []
        errors: List[str] = []
        failures: List[Dict[str, Any]] = []
        candidates = []
        for agent in enabled:
            previous = latest.get(agent.name)
            if _reusable_run(previous):
                results.append(AgentResult(agent.name, previous["output"], int(previous.get("latency_ms") or 0)))
            elif previous and previous["status"] == "permanent_error" and not (
                resumed_after_freeze and agent.name in resume_failed_agents
            ):
                safe_previous_error = sanitize_diagnostic_text(previous.get("error") or "unknown")
                errors.append(f"{agent.name}: 永久错误，不再重试：{safe_previous_error}")
                failure = classify_failure(previous.get("error") or "unknown")
                # The original exception is gone by now and only its text
                # survives, which is not evidence of a task-level output budget:
                # the entry stays unmarked so an environment gate is never
                # relaxed on a guess.
                failures.append(
                    _failure_record(
                        agent.name,
                        safe_previous_error,
                        None,
                        failure,
                        previous.get("failure_class"),
                    )
                )
            else:
                candidates.append(agent)

        minimum = len(workflow["agents"]) if workflow else int(worker["minimum_successful_agents"])
        if len(results) + len(candidates) < minimum:
            error = "; ".join(errors) or "可用 Agent 数不足，无法达到成功门槛"
            if results and failures:
                return self._freeze_for_recovery(task, results, failures, substitution or prior_substitution)
            self.store.fail(task["id"], error)
            return task_result({"status": "failed", "task_id": task["id"], "errors": errors or [error]})

        default_budget = int(worker["max_attempts"]) * len(enabled)
        base_invocation_budget = int(worker.get("max_agent_invocations_per_task", default_budget))
        recovery_allowance = int(task.get("resume_generation") or 0) * max(
            1, len(resume_failed_agents)
        )
        invocation_budget = base_invocation_budget + recovery_allowance
        remaining_budget = max(0, invocation_budget - self.store.agent_invocation_count(task["id"]))
        needed = max(0, minimum - len(results))
        if remaining_budget < needed:
            error = f"Agent 调用预算不足：剩余 {remaining_budget}，至少需要 {needed}"
            self.store.append_event(
                task["id"],
                "execution.budget_exhausted",
                {
                    "base_budget": base_invocation_budget,
                    "recovery_allowance": recovery_allowance,
                    "invocations": self.store.agent_invocation_count(task["id"]),
                    "needed": needed,
                },
            )
            self.store.fail(task["id"], error)
            return task_result({"status": "failed", "task_id": task["id"], "errors": [error]})
        candidates = candidates[:remaining_budget]

        task_context = ""
        snapshot_context = ""
        snapshot_skill_context = ""
        snapshot_agent_contexts: Dict[str, str] = {}
        memory_source_task_ids: List[str] = []
        try:
            snapshot = json.loads(task.get("execution_snapshot_json") or "{}")
            if not isinstance(snapshot, dict):
                raise ValueError("snapshot must be an object")
            task_context = render_context(snapshot.get("task_context"))
            snapshot_context = snapshot.get("knowlp_context", "")
            snapshot_skill_context = snapshot.get("skill_context", "")
            snapshot_agent_contexts = snapshot.get("agent_memory_contexts", {})
            memory_source_task_ids = [
                str(item.get("source_task_id"))
                for item in snapshot.get("memory_hits", [])
                if item.get("source_task_id")
            ]
        except (TypeError, ValueError) as exc:
            self.store.fail(task["id"], "invalid frozen context: " + str(exc))
            return task_result({"status": "failed", "task_id": task["id"], "errors": ["invalid frozen context"]})
        if "execution_snapshot_json" in task and task.get("execution_snapshot_json"):
            fallback_context = "\n\n".join(value for value in (task_context, snapshot_skill_context, snapshot_context) if value)
        else:
            fallback_context = self.memory.retrieve(task["prompt"]) if candidates else ""
        parallelism = min(int(worker["parallelism"]), len(candidates))
        with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
            futures = {}
            for agent in candidates:
                memory_context = snapshot_agent_contexts.get(agent.name, snapshot_context)
                context = "\n\n".join(
                    value for value in (task_context, snapshot_skill_context, memory_context) if value
                ) or fallback_context
                effective = replace(agent, raw={**agent.raw, "task_required_capabilities": list(set(_req_caps + (["native_web_search"] if _needs_web else [])))})
                prompt = agent_prompt(task["prompt"], workflow, agent.name)
                futures[pool.submit(self._run_agent, task["id"], effective, prompt, context,
                                    task["attempts"])] = agent.name
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    safe_error = sanitize_diagnostic_text(exc)
                    errors.append(f"{name}: {safe_error}")
                    failures.append(_failure_record(name, safe_error, exc, classify_failure(exc)))

        if not self.store.owns_lease(task["id"], task["attempts"]):
            return task_result({"status": "lease_lost", "task_id": task["id"],
                    "error": "Execution ownership lost; no substitution or settlement performed"})

        # Mid-round cover. The leak scan runs over this round's answers first
        # because a quarantined answer is not a fault the standby may cover; the
        # substitute's own answer is appended below and meets the same scan.
        leaked = [item.agent for item in results if detect_reasoning_leak(item.output)]
        if len(results) < minimum:
            decision = self._standby_substitution(
                standby=standby,
                standby_usable=standby_usable,
                already_substituted=substitution is not None or prior_substitution is not None,
                workflow=workflow,
                required_capabilities=_req_caps,
                needs_web=_needs_web,
                primaries=primaries,
                results=results,
                failures=failures,
                leaked=leaked,
                budget_left=invocation_budget - self.store.agent_invocation_count(task["id"]),
            )
            if decision is not None:
                memory_context = snapshot_agent_contexts.get(standby.name, snapshot_context)
                context = "\n\n".join(
                    value for value in (task_context, snapshot_skill_context, memory_context) if value
                ) or fallback_context
                effective = replace(standby, raw={**standby.raw, "task_required_capabilities": list(set(_req_caps + (["native_web_search"] if _needs_web else [])))})
                standby_result: Optional[AgentResult] = None
                try:
                    standby_result = self._run_agent(
                        task["id"],
                        effective,
                        agent_prompt(task["prompt"], workflow, standby.name),
                        context,
                        task["attempts"],
                    )
                except Exception as exc:
                    safe_error = sanitize_diagnostic_text(exc)
                    errors.append(f"{standby.name}: {safe_error}")
                    failures.append(
                        _failure_record(standby.name, safe_error, exc, classify_failure(exc))
                    )
                else:
                    results.append(standby_result)
                if not self.store.owns_lease(task["id"], task["attempts"]):
                    return task_result({"status": "lease_lost", "task_id": task["id"],
                            "error": "Execution ownership lost; no substitution or settlement performed"})
                substitution = self._record_substitution(
                    task["id"],
                    decision[1],
                    model=str(
                        (standby_result.model if standby_result is not None else "")
                        or health_rows.get(standby.name, {}).get("model")
                        or ""
                    ),
                    delivered=bool(
                        standby_result is not None and str(standby_result.output or "").strip()
                    ),
                )

        # Final scan, part 1: drop candidates whose answer still carries model
        # reasoning.  They are quarantined rather than retried because DSH picks
        # its model from a hash of the prompt, so a retry reproduces the
        # identical leak until the invocation budget burns out into a freeze.
        quarantined: List[Dict[str, Any]] = []
        clean_results: List[AgentResult] = []
        for result in results:
            signatures = detect_reasoning_leak(result.output)
            if signatures:
                quarantined.append(
                    {"agent": result.agent, "model": result.model, "signatures": signatures[:8]}
                )
            else:
                clean_results.append(result)
        if quarantined:
            results = clean_results
            self.store.append_event(
                task["id"], "output_hygiene.quarantined", {"candidates": quarantined}
            )
            if len(results) < minimum:
                reason = (
                    "output_hygiene: 候选答案含未剥离的模型推理内容，已隔离且干净候选不足："
                    + ", ".join(item["agent"] for item in quarantined)
                )
                self.store.fail(task["id"], reason)
                return task_result({
                    "status": "failed",
                    "task_id": task["id"],
                    "errors": [reason],
                    "output_hygiene": quarantined,
                    "standby_substitution": substitution or prior_substitution,
                })

        if len(results) < minimum:
            if results and any(
                item.get("failure_class") == "internal_headroom" for item in failures
            ):
                return self._freeze_for_recovery(task, results, failures, substitution or prior_substitution)
            refreshed = self.store.latest_agent_runs(task["id"])
            retryable = sum(
                1
                for agent in enabled
                if refreshed.get(agent.name, {}).get("status") not in {"completed", "permanent_error"}
            )
            budget_left = invocation_budget - self.store.agent_invocation_count(task["id"])
            if len(results) + retryable < minimum or budget_left < minimum - len(results):
                if results and failures:
                    return self._freeze_for_recovery(task, results, failures, substitution or prior_substitution)
                error = "; ".join(errors) or "无法达到 Agent 成功门槛"
                self.store.fail(task["id"], error)
                return task_result({
                    "status": "failed",
                    "task_id": task["id"],
                    "errors": errors or [error],
                    "standby_substitution": substitution or prior_substitution,
                })
            base = int(worker["retry_base_seconds"])
            cap = int(worker["retry_max_seconds"])
            delay = min(cap, int(base * math.pow(2, max(0, task["attempts"] - 1))))
            if task["attempts"] >= task["max_attempts"] and results and failures:
                return self._freeze_for_recovery(task, results, failures, substitution or prior_substitution)
            status = self.store.retry_or_fail(task, "; ".join(errors) or "Agent 全部失败", delay)
            return task_result({"status": status, "task_id": task["id"], "retry_in": delay if status == "retry" else None, "errors": errors,
                    "standby_substitution": substitution or prior_substitution})

        if workflow:
            try:
                validate_plan(self.config, self.store, task, _snap)
            except (ValueError, KeyError, OSError) as exc:
                self.store.fail(task["id"], "collaboration evidence changed during execution: " + str(exc))
                return task_result({"status": "failed", "task_id": task["id"], "errors": [str(exc)]})
        if workflow.get("phase") == "work":
            contributions = contribution_records(self.config, self.store, task["id"], workflow["agents"])
            bundle = {"phase": "work", "status": "awaiting_integration", "contributions": contributions,
                      "outputs": {item.agent: item.output for item in results}}
            self.store.append_event(task["id"], "collaboration.work_completed", {"contributions": contributions})
            payload = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
            leak = detect_reasoning_leak(payload)
            if leak:
                reason = f"output_hygiene: 协作交付内容未通过最终扫描，命中特征 {leak[:5]}"
                self.store.fail(task["id"], reason)
                return task_result({"status": "failed", "task_id": task["id"], "errors": [reason]})
            self.store.complete(task["id"], payload,
                                provenance_kind="agentos_collaboration_work")
            return task_result({**bundle, "status": "completed", "task_id": task["id"], "next_stage": "integration"})

        winner, scores = DeterministicJudge.choose(task["prompt"], results)
        breakdown = DeterministicJudge.explain(task["prompt"], results)
        detection = detect_output_constraint_state(task["prompt"])
        basis = DeterministicJudge.decision_basis(breakdown, winner)
        evidence = "\n".join(f"- {r.agent}: {scores[r.agent]:.3f}" for r in sorted(results, key=lambda item: item.agent))
        breaches = [
            f"  - {agent}: {text}"
            for agent, entry in sorted(breakdown.items())
            for text in entry["violations"]
        ]
        final = (
            f"{adjudicated_label(len(results))}\n\n"
            f"{winner.output}\n\n---\nAgentOS 裁判记录（确定性回退）\n{evidence}"
            f"\n- 判定依据：{basis}（{_DECISION_BASIS_LABEL[basis]}）"
            + ("\n- 违反题面输出约束：\n" + "\n".join(breaches) if breaches else "")
        )
        self.store.append_event(
            task["id"],
            "judge.breakdown",
            {
                "breakdown": breakdown,
                "constraint_detection": {
                    "status": detection.status,
                    "numeral_signal": detection.numeral_signal,
                    "no_extra_signal": detection.no_extra_signal,
                },
            },
        )
        self.store.append_event(
            task["id"],
            "judge.decided",
            {
                "winner": winner.agent,
                "scores": scores,
                "successful_agents": len(results),
                "strategy_version": 1,
                "decision_basis": basis,
                "restatement_task": breakdown[winner.agent]["restatement_task"],
                "detected_constraints": breakdown[winner.agent]["constraints"],
                "detected_constraints_by_agent": {
                    agent: entry["constraints"]
                    for agent, entry in sorted(breakdown.items())
                },
                "violations": {
                    agent: entry["violations"]
                    for agent, entry in sorted(breakdown.items())
                },
                "constraint_detection_status": detection.status,
                "standby_substitution": substitution or prior_substitution,
            },
        )
        # Final scan, part 2: nothing reaches tasks.result or memory staging
        # until the assembled answer is confirmed free of reasoning leakage.
        leak = detect_reasoning_leak(final)
        if leak:
            reason = f"output_hygiene: 裁判结果未通过最终扫描，命中特征 {leak[:5]}"
            self.store.fail(task["id"], reason)
            return task_result({
                "status": "failed",
                "task_id": task["id"],
                "errors": [reason],
                "winner": winner.agent,
                "scores": scores,
            })
        if detection.status == "unresolved_partial":
            self.store.hold_constraint_task(
                task["id"],
                final,
                {
                    "status": detection.status,
                    "numeral_signal": detection.numeral_signal,
                    "no_extra_signal": detection.no_extra_signal,
                    "constraints": [
                        {"kind": item.kind, "detail": item.detail}
                        for item in detection.constraints
                    ],
                    "prompt_sha256": __import__("hashlib").sha256(
                        task["prompt"].encode("utf-8")
                    ).hexdigest(),
                },
            )
            return task_result({
                "status": "constraint_hold",
                "task_id": task["id"],
                "winner": winner.agent,
                "scores": scores,
                "reason": "constraint_detection_miss",
                "standby_substitution": substitution or prior_substitution,
            })
        runs = self.store.task_runs(task["id"])
        evidence_sha256 = None
        audit_only = False
        try:
            from .experience_workflow import policy, complete_audit
            audit_only = policy(task)["kind"] == "audit"
            if audit_only:
                complete_audit(self.store, task["id"], final)
            else:
                staged = self.memory.stage_experience(task["id"], task["prompt"], final, runs)
                evidence_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
                self.store.complete_with_memory_candidate(
                    task["id"], final, str(staged), evidence_sha256, memory_source_task_ids
                )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            # An exception here does not say whether the transaction committed
            # (the store does maintenance after commit). Decide from the
            # database, never from the exception: a task that is completed with
            # its candidate queued must not be sent back to retry. If the state
            # cannot be read, this raises and the task stays leased.
            if not self._completion_committed(task["id"]):
                # Nothing was queued: do not leave the staged file or its
                # status sidecar behind for a task that may end up failed.
                orphan = self.memory.outbox / f"{task['id']}.md"
                for leftover in (orphan, orphan.with_suffix(orphan.suffix + ".status.json")):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
                worker = self.config.worker
                delay = min(
                    int(worker["retry_max_seconds"]),
                    int(int(worker["retry_base_seconds"]) * math.pow(2, max(0, task["attempts"] - 1))),
                )
                error = "memory candidate staging failed: " + sanitize_diagnostic_text(str(exc))
                status = self.store.retry_or_fail(task, error, delay)
                # task_result (defined at the top of this method) adds
                # standby_substitution to every result, this one included.
                return task_result({
                    "status": status,
                    "task_id": task["id"],
                    "retry_in": delay if status == "retry" else None,
                    "errors": [error],
                })
        return task_result({
            "status": "completed",
            "task_id": task["id"],
            "winner": winner.agent,
            "scores": scores,
            "result": final,
            "standby_substitution": substitution or prior_substitution,
            "delivery_review": {
                "status": "audit_only" if audit_only else "pending_delivery_review",
                "candidate_sha256": evidence_sha256 if evidence_sha256 is not None else (
                    None if audit_only else self.store.get_memory_candidate(task["id"])["evidence_sha256"]),
                "requires_explicit_user_decision": not audit_only,
            },
        })

    def _freeze_for_recovery(
        self,
        task: Dict[str, Any],
        results: List[AgentResult],
        failures: List[Dict[str, Any]],
        substitution: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Freeze the task, keep the successful side, and wait for the user.

        No peer repair job is queued and nothing is restarted. The task stays
        `frozen_paused` until an explicit `task-resume` carries this freeze hash.
        Health is recorded per failure reason, so a round that only ran out of
        output budget does not mark its whole backend unavailable -- while a real
        capacity, auth or network fault beside it still does.
        """
        priority = _freeze_priority(failures)
        frozen = self.store.freeze_task(
            task["id"],
            failure_class=str(priority.get("failure_class") or "unknown"),
            failures=failures,
            receipts=self._completed_receipt_hashes(task["id"], results),
        )
        for item in failures:
            failed_agent = str(item.get("agent") or "")
            if not failed_agent or _is_output_budget(item) or item.get("health_already_recorded") is True:
                # Exhausting one round's output tokens says the task was too
                # large, not that the backend is broken. A global health row here
                # would keep every unrelated task out of the queue until the next
                # probe, which is exactly the misjudgement this separates out.
                continue
            self.store.record_agent_health(
                failed_agent,
                "external_blocked" if item.get("external") else "degraded",
                failure_class=str(item.get("failure_class") or "unknown"),
                error=str(item.get("error") or "unknown"),
            )
        budget_only = all(_is_output_budget(item) for item in failures)
        payload = {
            "task_id": task["id"],
            "freeze_hash": frozen["freeze_hash"],
            "failed_agent": str(priority.get("agent") or ""),
            "failure_class": priority.get("failure_class"),
            "peer_auto_repair": "retired",
        }
        if budget_only:
            payload["failure_scope"] = _OUTPUT_BUDGET_SCOPE
        self.store.create_notification("task.recovery_blocked", payload, task_id=task["id"])
        return {
            "status": "frozen_paused",
            "task_id": task["id"],
            "freeze_hash": frozen["freeze_hash"],
            "completed_agents": [result.agent for result in results],
            "failed_agents": [item.get("agent") for item in failures],
            "output_budget_exhausted": budget_only,
            "peer_auto_repair": "retired",
            "requires_explicit_resume": True,
            "standby_substitution": substitution,
        }

    def _completed_receipt_hashes(
        self, task_id: str, results: List[AgentResult]
    ) -> List[Dict[str, Any]]:
        completed_agents = {result.agent for result in results}
        latest = self.store.latest_agent_runs(task_id)
        receipts: List[Dict[str, Any]] = []
        root = self.config.runtime_dir / "receipts" / task_id
        for agent in sorted(completed_agents):
            run = latest.get(agent)
            if not run or run.get("status") != "completed":
                continue
            path = root / f"{run['id']}.json"
            if path.is_file():
                receipts.append(
                    {
                        "agent": agent,
                        "run_id": run["id"],
                        "name": path.name,
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                )
        return receipts
