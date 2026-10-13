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

from .adapters import AdapterError, AgentResult, build_adapter
from .adjudication import (
    asks_for_restatement,
    constraint_violations,
    detect_output_constraint_state,
    detect_output_constraints,
)
from .capabilities import CapabilityRegistry, capable_agents, web_capable_agents
from .config import AppConfig
from .gateway import ensure_desktop_available, local_cli_readiness
from .routing import adjudicated_label
from .memory import MemoryBridge
from .output_hygiene import detect_reasoning_leak
from .release import release_binding_status
from .receipts import ExecutionReceiptStore
from .recovery import classify_failure, sanitize_diagnostic_text
from .circuit_breaker import CircuitBreaker
from .task_context import render_context
from .store import EventStore
from .workflow import agent_prompt, contribution_records, validate_plan


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
        cls, prompt: str, results: List[AgentResult], weights: Dict[str, float]
    ) -> Dict[str, Dict[str, Any]]:
        """Return every term, weight and violation behind each score."""
        constraints = detect_output_constraints(prompt)
        restatement = asks_for_restatement(prompt)
        effective = cls.effective_weights(restatement)
        prompt_tokens = _tokens(prompt)
        configured_default = min((float(value) for value in weights.values()), default=1.0)
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
            weight = float(weights.get(result.agent, configured_default))
            entry: Dict[str, Any] = dict(terms)
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
    def choose(cls, prompt: str, results: List[AgentResult], weights: Dict[str, float]) -> Tuple[AgentResult, Dict[str, float]]:
        breakdown = cls.explain(prompt, results, weights)
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


class Orchestrator:
    def __init__(self, config: AppConfig, store: EventStore):
        self.config = config
        self.store = store
        self.memory = MemoryBridge(config, store)
        self.circuit_breaker = CircuitBreaker(threshold=2)

    def _run_agent(self, task_id: str, agent_config: Any, prompt: str, context: str) -> AgentResult:
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
        try:
            result = build_adapter(agent_config).run(prompt, context)
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
        return bool(
            current
            and current["status"] == "completed"
            and self.store.get_memory_candidate(task_id)
        )

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
        enabled = [a for a in self.config.agents if a.enabled]
        if not enabled:
            return {"status": "blocked_configuration", "error": "没有启用任何 Agent；任务队列未被消费"}
        if has_work:
            # Only bring the desktop up when there is something to run.
            ensure_desktop_available(self.config, enabled)
        local_readiness = local_cli_readiness(self.config, enabled)
        if not local_readiness["ok"]:
            return {"status": "blocked_configuration", "error": "；".join(local_readiness["problems"]), "local_cli_routes": local_readiness}
        enabled_names = [agent.name for agent in enabled]
        health_rows = {
            row["agent_name"]: row
            for row in self.store.agent_health(
                enabled_names=enabled_names,
                max_age_seconds=float(worker.get("health_max_age_seconds", 86400)),
            )
        }
        if health_rows or any(agent.type == "local_cli" for agent in enabled):
            unhealthy = [
                {
                    "agent": agent.name,
                    "status": health_rows.get(agent.name, {}).get("status", "unprobed"),
                    "failure_class": health_rows.get(agent.name, {}).get("failure_class"),
                }
                for agent in enabled if health_rows.get(agent.name, {}).get("status") != "healthy"
            ]
            if unhealthy:
                return {
                    "status": "degraded_health",
                    "error": "后台 Agent 实际推理健康门禁未通过；任务队列未被消费",
                    "agents": unhealthy,
                }
        task = self.store.lease_next(int(worker["lease_seconds"]))
        if not task:
            return {"status": "idle"}

        try:
            _snap = json.loads(task.get("execution_snapshot_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            _snap = {}
        try:
            validate_plan(self.config, self.store, task, _snap)
        except (ValueError, KeyError, OSError) as exc:
            self.store.fail(task["id"], "invalid collaboration evidence: " + str(exc))
            return {"status": "failed", "task_id": task["id"], "errors": [str(exc)]}
        workflow = _snap.get("workflow", {})
        _req_caps = list(_snap.get("required_capabilities") or [])
        _needs_web = bool(_snap.get("needs_web")) or "native_web_search" in _req_caps
        _gate_agents = list(enabled)
        if _needs_web:
            _gate_agents, _gate_reason = web_capable_agents(_gate_agents, CapabilityRegistry(), needs_web=True)
            if not _gate_agents:
                self.store.fail(task["id"], _gate_reason or "missing native_web_search")
                return {
                    "status": "blocked",
                    "reason": _gate_reason or "no web-capable agent",
                    "capability": "native_web_search",
                    "task_id": task["id"],
                }
        if _req_caps:
            _gate_agents, _missing = capable_agents(_gate_agents, CapabilityRegistry(), _req_caps)
            if not _gate_agents:
                _lacked = sorted({tok for _, toks in _missing for tok in toks})
                self.store.fail(task["id"], "missing capability: " + ", ".join(_lacked))
                return {
                    "status": "blocked",
                    "reason": "missing capability: " + ", ".join(_lacked),
                    "capability": _lacked[0] if _lacked else "unknown",
                    "task_id": task["id"],
                }
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
        results: List[AgentResult] = []
        errors: List[str] = []
        failures: List[Dict[str, Any]] = []
        candidates = []
        for agent in enabled:
            previous = latest.get(agent.name)
            if (
                previous and previous["status"] == "completed" and previous.get("output")
                # Reusing an answer that still carries model reasoning only gets
                # it quarantined again; give the agent a fresh run instead
                # (the invocation budget still bounds this).
                and not detect_reasoning_leak(previous["output"])
            ):
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
                return self._freeze_for_recovery(task, results, failures)
            self.store.fail(task["id"], error)
            return {"status": "failed", "task_id": task["id"], "errors": errors or [error]}

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
            return {"status": "failed", "task_id": task["id"], "errors": [error]}
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
            return {"status": "failed", "task_id": task["id"], "errors": ["invalid frozen context"]}
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
                futures[pool.submit(self._run_agent, task["id"], effective, prompt, context)] = agent.name
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    safe_error = sanitize_diagnostic_text(exc)
                    errors.append(f"{name}: {safe_error}")
                    failures.append(_failure_record(name, safe_error, exc, classify_failure(exc)))

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
                return {
                    "status": "failed",
                    "task_id": task["id"],
                    "errors": [reason],
                    "output_hygiene": quarantined,
                }

        if len(results) < minimum:
            if results and any(
                item.get("failure_class") == "internal_headroom" for item in failures
            ):
                return self._freeze_for_recovery(task, results, failures)
            refreshed = self.store.latest_agent_runs(task["id"])
            retryable = sum(
                1
                for agent in enabled
                if refreshed.get(agent.name, {}).get("status") not in {"completed", "permanent_error"}
            )
            budget_left = invocation_budget - self.store.agent_invocation_count(task["id"])
            if len(results) + retryable < minimum or budget_left < minimum - len(results):
                if results and failures:
                    return self._freeze_for_recovery(task, results, failures)
                error = "; ".join(errors) or "无法达到 Agent 成功门槛"
                self.store.fail(task["id"], error)
                return {"status": "failed", "task_id": task["id"], "errors": errors or [error]}
            base = int(worker["retry_base_seconds"])
            cap = int(worker["retry_max_seconds"])
            delay = min(cap, int(base * math.pow(2, max(0, task["attempts"] - 1))))
            if task["attempts"] >= task["max_attempts"] and results and failures:
                return self._freeze_for_recovery(task, results, failures)
            status = self.store.retry_or_fail(task, "; ".join(errors) or "Agent 全部失败", delay)
            return {"status": status, "task_id": task["id"], "retry_in": delay if status == "retry" else None, "errors": errors}

        if workflow:
            try:
                validate_plan(self.config, self.store, task, _snap)
            except (ValueError, KeyError, OSError) as exc:
                self.store.fail(task["id"], "collaboration evidence changed during execution: " + str(exc))
                return {"status": "failed", "task_id": task["id"], "errors": [str(exc)]}
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
                return {"status": "failed", "task_id": task["id"], "errors": [reason]}
            self.store.complete(task["id"], payload,
                                provenance_kind="agentos_collaboration_work")
            return {**bundle, "status": "completed", "task_id": task["id"], "next_stage": "integration"}

        strategy = self.store.strategy_by_version(int(task.get("strategy_version") or 1))
        try:
            body = json.loads(strategy.get("body_json") or "{}")
        except json.JSONDecodeError:
            body = {}
        winner, scores = DeterministicJudge.choose(task["prompt"], results, body.get("agent_weights", {}))
        breakdown = DeterministicJudge.explain(task["prompt"], results, body.get("agent_weights", {}))
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
                "strategy_version": int(strategy.get("version") or 1),
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
            },
        )
        # Final scan, part 2: nothing reaches tasks.result or memory staging
        # until the assembled answer is confirmed free of reasoning leakage.
        leak = detect_reasoning_leak(final)
        if leak:
            reason = f"output_hygiene: 裁判结果未通过最终扫描，命中特征 {leak[:5]}"
            self.store.fail(task["id"], reason)
            return {
                "status": "failed",
                "task_id": task["id"],
                "errors": [reason],
                "winner": winner.agent,
                "scores": scores,
            }
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
            return {
                "status": "constraint_hold",
                "task_id": task["id"],
                "winner": winner.agent,
                "scores": scores,
                "reason": "constraint_detection_miss",
            }
        runs = self.store.task_runs(task["id"])
        try:
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
                return {
                    "status": status,
                    "task_id": task["id"],
                    "retry_in": delay if status == "retry" else None,
                    "errors": [error],
                }
        return {
            "status": "completed",
            "task_id": task["id"],
            "winner": winner.agent,
            "scores": scores,
            "result": final,
            "delivery_review": {
                "status": "pending_delivery_review",
                "candidate_sha256": evidence_sha256,
                "requires_explicit_user_decision": True,
            },
        }

    def _freeze_for_recovery(
        self,
        task: Dict[str, Any],
        results: List[AgentResult],
        failures: List[Dict[str, Any]],
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
            if not failed_agent or _is_output_budget(item):
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
