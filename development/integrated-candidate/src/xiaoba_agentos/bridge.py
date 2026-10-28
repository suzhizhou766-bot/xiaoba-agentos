from __future__ import annotations

import json
import hashlib
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

from .health import execution_blockers
from .memory import MemoryBridge
from .review_flow import candidate_sha256 as _candidate_sha256, execute_review
from .routing import AGENTOS_MULTI, ASK_USER, DIRECT, REVIEW, decide_route
from .skills import SkillRegistry, SkillRegistryError
from .task_context import freeze_context, read_context_file
from .store import EventStore

MAX_REQUEST = 1024 * 1024


def _envelope(ok: bool, result: Any = None, error: str = "") -> Dict[str, Any]:
    return {"ok": ok, "result": result if ok else None, "error": error if not ok else None}


def _answer_provenance(route: str) -> str:
    return {
        DIRECT: "session_model_direct",
        AGENTOS_MULTI: "agentos_multi_adjudicated",
        REVIEW: "agentos_review",
        ASK_USER: "agentos_clarification",
    }[route]


def _submit(
    prompt: str,
    store: EventStore,
    config: Any,
    *,
    priority: int = 0,
    session_id: str,
    route_decision_id: str,
    idempotency_key: Optional[str] = None,
    task_context: str = "",
) -> str:
    frozen_context = freeze_context(task_context)
    enabled_names = [agent.name for agent in config.agents if agent.enabled]
    memory = MemoryBridge(config, store)
    assignment = memory.agent_contexts(prompt, enabled_names)
    context = memory.render_hits(assignment["hits"])
    skills = SkillRegistry(config).select_context(prompt)
    snapshot = {
        "prompt": prompt,
        "task_context": frozen_context,
        "knowlp_context": context,
        "knowlp_context_hash": hashlib.sha256(context.encode("utf-8")).hexdigest(),
        "memory_hits": assignment["hits"],
        "retrieval_receipt": assignment.get("receipt"),
        "agent_memory_contexts": assignment["contexts"],
        "skills": skills["selected"],
        "skill_context": skills["context"],
        "skill_context_hash": skills["context_sha256"],
        "agents": [dict(agent.raw) for agent in config.agents],
        "worker": dict(config.worker),
    }
    task_id = store.submit(
        prompt,
        int(config.worker["max_attempts"]),
        priority,
        snapshot=snapshot,
        idempotency_key=idempotency_key,
        session_id=session_id,
        route_decision_id=route_decision_id,
        provenance={"kind": "agentos_multi_adjudicated", "status": "pending"},
    )
    for hit in assignment["hits"]:
        source_task_id = hit.get("source_task_id")
        if source_task_id and source_task_id != task_id:
            store.record_memory_reuse(task_id, str(source_task_id), str(hit.get("path") or ""))
    return task_id


def dispatch(request: Dict[str, Any], store: EventStore, config: Any) -> Dict[str, Any]:
    if not isinstance(request, dict):
        return _envelope(False, error="request must be a JSON object")
    command = request.get("command")
    if command == "status":
        return _envelope(True, store.summary())
    if command == "tasks":
        try:
            limit = max(1, min(100, int(request.get("limit", 20))))
        except (TypeError, ValueError):
            return _envelope(False, error="invalid limit")
        return _envelope(True, store.list_tasks(limit))
    if command == "task_detail":
        task_id = str(request.get("task_id", ""))
        task = store.get_task(task_id)
        return _envelope(bool(task), {"task": task, "runs": store.task_runs(task_id)} if task else None, "task not found")
    if command == "submit":
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt or len(prompt) > 100000:
            return _envelope(False, error="invalid prompt")
        key = request.get("idempotency_key")
        session = store.active_session() or store.start_session()
        try:
            route = store.record_route(
                prompt=prompt,
                route=AGENTOS_MULTI,
                reason_code="explicit_bridge_submit",
                confidence=1.0,
                signals={"explicit_submit": True},
                session_id=str(session["id"]),
                message_id=str(key) if key else None,
            )
        except ValueError as exc:
            return _envelope(False, error=str(exc))
        try:
            task_id = _submit(
                prompt,
                store,
                config,
                task_context=request.get("task_context", ""),
                priority=int(request.get("priority", 0)),
                session_id=str(session["id"]),
                route_decision_id=str(route["id"]),
                idempotency_key=str(key) if key else None,
            )
        except (SkillRegistryError, ValueError) as exc:
            return _envelope(False, error=str(exc))
        return _envelope(True, {"task_id": task_id, "status": "pending", "route_id": route["id"]})
    if command == "intake":
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or not prompt or len(prompt) > 100000:
            return _envelope(False, error="invalid prompt")
        try:
            decision = decide_route(
                prompt,
                active_task_id=str(request["active_task_id"]) if request.get("active_task_id") else None,
                relation=str(request.get("relation", "auto")),
                risk=str(request.get("risk", "auto")),
                requires_action=bool(request.get("requires_action", False)),
                memory_worthy=bool(request.get("memory_worthy", False)),
                explicit_agentos=bool(request.get("explicit_agentos", False)),
                force_route=str(request["force_route"]) if request.get("force_route") else None,
            )
        except ValueError as exc:
            return _envelope(False, error=str(exc))
        session = store.active_session() or store.start_session()
        message_id = str(request["message_id"]) if request.get("message_id") else None
        try:
            route = store.record_route(
                prompt=prompt,
                route=decision.route,
                reason_code=decision.reason_code,
                confidence=decision.confidence,
                signals={
                    "relation": request.get("relation", "auto"),
                    "risk": request.get("risk", "auto"),
                    "requires_action": bool(request.get("requires_action", False)),
                    "memory_worthy": bool(request.get("memory_worthy", False)),
                },
                session_id=str(session["id"]),
                message_id=message_id,
                active_task_id=str(request["active_task_id"]) if request.get("active_task_id") else None,
            )
        except ValueError as exc:
            return _envelope(False, error=str(exc))
        response = {
            "status": "routed", "route_id": route["id"], "route": route["route"],
            "reason_code": route["reason_code"], "confidence": route["confidence"],
            "answer_provenance": _answer_provenance(str(route["route"])),
        }
        if bool(request.get("submit")) and route["route"] == AGENTOS_MULTI:
            blockers = execution_blockers(config, store)
            if blockers and not bool(request.get("queue_when_blocked")):
                return _envelope(False, error="blocked_local_health: 任务未入队；" + "；".join(blockers))
            if blockers:
                response.update({"execution_blocked": True, "blockers": blockers})
            try:
                response["task_id"] = _submit(
                    prompt,
                    store,
                    config,
                    task_context=request.get("task_context", ""),
                    priority=int(request.get("priority", 0)),
                    session_id=str(session["id"]),
                    route_decision_id=str(route["id"]),
                    idempotency_key=f"intake:{route['id']}",
                )
            except (SkillRegistryError, ValueError) as exc:
                return _envelope(False, error=str(exc))
            response["status"] = "pending"
        return _envelope(True, response)
    if command == "review_queue":
        try:
            return _envelope(
                True,
                store.review_queue(str(request.get("stage", "all")), request.get("date")),
            )
        except ValueError as exc:
            return _envelope(False, error=str(exc))
    if command == "review_decision":
        packet = request.get("packet")
        if not isinstance(packet, dict):
            return _envelope(False, error="packet must be a JSON object")
        task_id = str(packet.get("candidate_id") or packet.get("task_id") or "").strip()
        stage = str(packet.get("stage") or "delivery").strip()
        decision = str(packet.get("decision") or "").strip()
        note = str(packet.get("user_note") or packet.get("note") or "").strip()
        requested_mode = str(packet.get("mode") or "review_only").strip()
        if not task_id or not decision:
            return _envelope(False, error="candidate_id and decision are required")
        if requested_mode not in {"review_only", "execute"}:
            return _envelope(False, error="mode must be review_only or execute")
        execution_prompt = str(packet.get("execution_prompt") or "").strip()
        if requested_mode == "execute" and not execution_prompt:
            return _envelope(False, error="execution_prompt is required for execute mode")
        candidate = store.get_memory_candidate(task_id)
        if not candidate:
            return _envelope(False, error="candidate not found")
        actual_hash = _candidate_sha256(candidate)
        supplied_hash = str(packet.get("evidence_sha256") or "")
        if not supplied_hash or supplied_hash != actual_hash:
            return _envelope(False, error="evidence hash does not match the staged candidate")
        # The bridge keeps no review logic of its own: publication, reuse
        # reconciliation, and retirement only happen in execute_review.
        code, outcome = execute_review(
            config, store, task_id, stage, decision, note, expected_sha256=actual_hash
        )
        if outcome.get("status") == "blocked":
            return _envelope(False, error=str(outcome.get("error") or "review blocked"))
        store.append_event(task_id, "review.packet_received", {
            "stage": stage,
            "decision": decision,
            "mode": requested_mode,
            "candidate_sha256": actual_hash,
            "idempotency_key": str(packet.get("idempotency_key") or ""),
            "source": str(packet.get("source") or "review-dashboard"),
            "outcome_status": str(outcome.get("status") or outcome.get("memory_status") or ""),
            "exit_code": code,
        })
        if code != 0:
            return _envelope(False, error=json.dumps({
                "status": outcome.get("status"),
                "operation": outcome.get("operation"),
                "errors": outcome.get("errors"),
                "recovery_command": outcome.get("recovery_command"),
            }, ensure_ascii=False, default=str))
        reviewed = dict(store.get_memory_candidate(task_id) or {})
        for key in ("review_id", "previous_status", "reviewed_status"):
            if key in outcome:
                reviewed[key] = outcome[key]
        result = {"review": reviewed, "outcome": outcome, "mode": requested_mode}
        if requested_mode == "execute":
            session = store.active_session() or store.start_session()
            route = store.record_route(
                prompt=execution_prompt,
                route=AGENTOS_MULTI,
                reason_code="review_dashboard_execution_request",
                confidence=1.0,
                signals={"review_task_id": task_id, "review_decision": decision},
                session_id=str(session["id"]),
                message_id=str(packet.get("idempotency_key") or "") or None,
            )
            task = _submit(
                execution_prompt,
                store,
                config,
                priority=int(packet.get("priority") or 0),
                session_id=str(session["id"]),
                route_decision_id=str(route["id"]),
                idempotency_key=str(packet.get("idempotency_key") or "") or None,
                task_context=(
                    "来源候选：" + task_id + "\n"
                    "用户裁决：" + decision + "\n"
                    "用户补充意见：" + note
                ),
            )
            store.append_event(task_id, "review.execution_submitted", {
                "execution_task_id": task,
                "decision": decision,
                "candidate_sha256": actual_hash,
            })
            result["execution_task_id"] = task
            result["route_id"] = route["id"]
        return _envelope(True, result)
    if command == "events":
        try:
            limit = max(1, min(100, int(request.get("limit", 20))))
        except (TypeError, ValueError):
            return _envelope(False, error="invalid limit")
        with store.connect() as conn:
            rows = conn.execute("SELECT * FROM events ORDER BY sequence DESC LIMIT ?", (limit,)).fetchall()
        return _envelope(True, [dict(row) for row in rows])
    return _envelope(False, error="unsupported command")


class UnixBridge:
    def __init__(self, path: Path, store: EventStore, config: Any):
        self.path, self.store, self.config = path, store, config

    def serve_once(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if self.path.exists():
            if not self.path.is_socket():
                raise RuntimeError("bridge path exists and is not a socket")
            self.path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.settimeout(30.0)
            server.bind(str(self.path))
            os.chmod(self.path, 0o600)
            server.listen(1)
            try:
                conn, _ = server.accept()
                with conn:
                    conn.settimeout(5.0)
                    chunks = []
                    size = 0
                    while size <= MAX_REQUEST:
                        chunk = conn.recv(min(65536, MAX_REQUEST + 1 - size))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    data = b"".join(chunks)
                    if size > MAX_REQUEST:
                        response = _envelope(False, error="request too large")
                    else:
                        try:
                            request = json.loads(data.decode("utf-8"))
                            if not isinstance(request, dict):
                                raise ValueError("request must be a JSON object")
                            response = dispatch(request, self.store, self.config)
                        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
                            response = _envelope(False, error="invalid request: " + str(exc))
                    conn.sendall(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            finally:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
