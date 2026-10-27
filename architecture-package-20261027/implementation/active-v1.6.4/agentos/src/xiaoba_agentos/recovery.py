"""Live Agent health probes.

Peer auto-repair is retired. A probe records health and stops there: it never
asks one Agent to diagnose another, never restarts or re-probes a backend, never
queues a repair job, and never advances a frozen task. Unfreezing stays an
explicit user `task-resume` carrying the freeze hash, so a recovered environment
can be reported but cannot act on the user's decision.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

from .adapters import AdapterError, AgentResult, build_adapter


HEALTH_MARKER = "XIAOBA_AGENT_HEALTH_OK"


_DSH_PROCESS_UNAVAILABLE_MARKERS = (
    "no such file or directory",
    "not found",
    "cannot spawn",
    "failed to spawn",
    "connection refused",
    "econnrefused",
    "dsh timeout",
    "command timed out",
)


def _is_dsh_agent(agent: Any) -> bool:
    return str(getattr(agent, "driver", "") or "") == "dsh"


def _dsh_probe_failure(exc: BaseException) -> Optional[tuple[str, str, str]]:
    """Return a user-readable DSH health state for process-level failures."""
    text = _safe_error(exc).lower()
    if any(marker in text for marker in _DSH_PROCESS_UNAVAILABLE_MARKERS):
        return (
            "dsh_process_unavailable",
            "process_unavailable",
            "DSH 进程或端点未启动、已退出或无响应",
        )
    if "未返回有效文本响应" in text or "empty output" in text:
        return (
            "dsh_health_marker_missing",
            "response_without_health_marker",
            "DSH 命令已返回，但没有返回健康确认信号",
        )
    return None


@dataclass(frozen=True)
class FailureClassification:
    name: str
    # Descriptive only since peer auto-repair was retired: no control path may
    # start an action because this is True. Kept so historical failure records
    # and the frozen payload stay comparable across versions.
    repairable: bool
    external: bool
    user_action_required: bool = False


def _safe_error(value: object) -> str:
    text = str(value or "unknown failure")
    text = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|auth[_-]?token|authorization|cookie|password)\s*[:=]\s*)[^\s,;]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\b(?:sk|key)-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    return text[:2000]


def sanitize_diagnostic_text(value: object) -> str:
    return _safe_error(value)


def classify_failure(error: object) -> FailureClassification:
    declared = str(getattr(error, "failure_class", "") or "")
    if declared in {"dsh_process_unavailable", "dsh_health_marker_missing"}:
        return FailureClassification(declared, False, False, True)
    if declared == "internal_headroom":
        return FailureClassification("internal_headroom", True, False)
    if declared == "internal_adapter_candidate":
        return FailureClassification("internal_adapter_candidate", False, False, True)
    if declared in {"external_upstream", "external_auth"}:
        return FailureClassification(declared, False, True, declared == "external_auth")
    if bool(getattr(error, "external_upstream", False)):
        return FailureClassification("external_upstream", False, True)
    text = _safe_error(error).lower()
    # Provider capacity and availability responses are external conditions.
    # Check these before generic transient handling so AGY 503/unavailable/no
    # capacity responses cannot become internal repair candidates.
    if any(token in text for token in ("503", "service unavailable", "no capacity", "capacity unavailable")) or (
        any(token in text for token in ("agy", "antigravity")) and "unavailable" in text
    ):
        return FailureClassification("external_upstream", False, True)
    if "headroom" in text and any(token in text for token in ("unavailable", "readiness", "startup")):
        return FailureClassification("internal_headroom", True, False)
    if any(token in text for token in ("401", "403", "unauthorized", "forbidden", "authentication", "bad auth", "invalid auth", "missing_credential", "no api key", "credentials service")):
        return FailureClassification("external_auth", False, True, True)
    # Transport failures are upstream conditions even when the provider does
    # not return a structured status.  In particular AGY has historically
    # surfaced EOF/reset as bare process errors; classifying those as an
    # internal candidate creates a repair job for a problem code cannot fix.
    # Resolution wording is resolver-specific: the glibc strings alone never
    # match on macOS, where Go emits "no such host" and libc emits "nodename
    # nor servname provided".
    if any(token in text for token in (
        "429", "rate limit", "quota", "upstream", "timed out", "timeout",
        "connection reset", "connection refused", "connection aborted",
        "broken pipe", "unexpected eof", " eof", "eof:", "tls handshake",
        "temporary failure in name resolution", "name or service not known",
        "network is unreachable", "no such host", "nodename nor servname provided",
        "server misbehaving",
    )):
        return FailureClassification("external_upstream", False, True)
    if any(token in text for token in ("输出契约", "tool-call", "工具调用标记", "no last agent message")):
        return FailureClassification("internal_adapter_candidate", False, False, True)
    if isinstance(error, AdapterError) and error.transient:
        return FailureClassification("transient_unknown", True, False)
    return FailureClassification("internal_unknown_candidate", False, False, True)


def _probe_one(agent: Any) -> tuple[AgentResult, list[dict[str, Any]]]:
    prompt = f"Reply with exactly {HEALTH_MARKER} and no other text."
    try:
        result = build_adapter(agent).run(prompt, "")
    except Exception as exc:
        if _is_dsh_agent(agent):
            classified = _dsh_probe_failure(exc)
            if classified:
                failure_class, probe_state, message = classified
                error = RuntimeError(f"{message}；原始错误：{_safe_error(exc)}")
                error.failure_class = failure_class
                error.health_probe_state = probe_state
                error.health_marker_expected = HEALTH_MARKER
                error.route_attempts = list(getattr(exc, "route_attempts", []))
                raise error from exc
        raise
    if HEALTH_MARKER not in result.output:
        if _is_dsh_agent(agent):
            error = RuntimeError("DSH 命令已返回，但没有返回健康确认信号")
            error.failure_class = "dsh_health_marker_missing"
            error.health_probe_state = "response_without_health_marker"
            error.health_marker_expected = HEALTH_MARKER
            error.response_length = len(result.output or "")
        else:
            error = RuntimeError("health probe returned no health marker")
            error.failure_class = "internal_adapter_candidate"
        raise error
    return result, list(getattr(result, "route_attempts", []))


def live_agent_probe(config: Any, store: Any, agents: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """Run real, side-effect-free inference without creating a user task or memory candidate."""
    selected = [agent for agent in (agents or config.agents) if agent.enabled]
    outcomes: Dict[str, Dict[str, Any]] = {}
    parallelism = max(1, min(len(selected), int(config.worker.get("parallelism", 2))))
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(_probe_one, agent): agent for agent in selected}
        for future in as_completed(futures):
            agent = futures[future]
            try:
                result, attempts = future.result()
                for attempt in attempts:
                    store.append_event(
                        None, "health.provider_attempt", {"agent": agent.name, **attempt}
                    )
                record = store.record_agent_health(
                    agent.name,
                    "healthy",
                    provider=result.provider,
                    model=result.model,
                    details={
                        "marker_received": True,
                        "health_probe_state": "marker_received",
                        "health_marker": HEALTH_MARKER,
                    },
                )
                outcomes[agent.name] = {
                    "status": "healthy",
                    "provider": result.provider,
                    "model": result.model,
                    "latency_ms": result.latency_ms,
                    "health_probe_state": "marker_received",
                    "recorded_at": record["last_probe_at"],
                }
            except Exception as exc:
                for attempt in getattr(exc, "route_attempts", []):
                    store.append_event(
                        None, "health.provider_attempt", {"agent": agent.name, **attempt}
                    )
                classification = classify_failure(exc)
                status = "external_blocked" if classification.external else "degraded"
                probe_state = str(
                    getattr(exc, "health_probe_state", "")
                    or (
                        "response_without_health_marker"
                        if classification.name == "dsh_health_marker_missing"
                        else "process_unavailable"
                        if classification.name == "dsh_process_unavailable"
                        else "unknown"
                    )
                )
                store.record_agent_health(
                    agent.name,
                    status,
                    failure_class=classification.name,
                    error=_safe_error(exc),
                    details={
                        "repairable": classification.repairable,
                        "external": classification.external,
                        "user_action_required": classification.user_action_required,
                        "health_probe_state": probe_state,
                        "health_marker": HEALTH_MARKER,
                    },
                )
                outcomes[agent.name] = {
                    "status": status,
                    "failure_class": classification.name,
                    "error": _safe_error(exc),
                    "repairable": classification.repairable,
                    "external": classification.external,
                    "health_probe_state": probe_state,
                }
    healthy = sorted(name for name, item in outcomes.items() if item["status"] == "healthy")
    failed = sorted(name for name, item in outcomes.items() if item["status"] != "healthy")
    required = min(len(selected), int(config.worker["minimum_successful_agents"]))
    return {
        "ok": bool(selected) and len(healthy) >= required,
        "healthy_agents": healthy,
        "failed_agents": failed,
        "agents": outcomes,
        "remote_inference_tested": True,
        "memory_candidate_created": False,
    }


def start_session_health(config: Any, store: Any) -> Dict[str, Any]:
    """Probe every enabled Agent and report health; take no recovery action.

    Failed Agents keep the health status their own probe produced. Nothing is
    marked `repairing`, no repair job is queued and no frozen task is advanced:
    a session start may discover that the environment recovered, but only the
    user's `task-resume` may act on it.
    """
    result = live_agent_probe(config, store)
    result["peer_auto_repair"] = "retired"
    result["repair_jobs"] = []
    result["session_mode"] = (
        ("dual_agent" if len(result["healthy_agents"]) == 2 else "multi_agent")
        if result["ok"]
        else "degraded_blocked"
    )
    return result
