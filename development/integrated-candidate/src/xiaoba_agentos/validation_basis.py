"""Versioned evidence bindings for method candidates that need real-task proof."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
import math
from pathlib import Path
from typing import Any, Dict, Optional


TRUST_BOUNDARY = "same_machine_business_boundary"
_CHECKS = {"real_task_acceptance", "sandbox"}
_CLAIM_CLASSES = {"artifact", "code"}
_SHA = set("0123456789abcdef")
_BASIS_KEYS = {
    "version", "policy_version", "status", "basis_id", "candidate_id", "candidate_sha256", "initial_review_id",
    "route", "required_checks", "method_scope", "method_claims", "application_scope", "task_origin", "real_task",
    "application_id", "delivery_event_id", "artifact", "execution_evidence", "acceptance_ref",
    "acceptance_sha256", "accepted_at", "evidence_items", "trust_boundary", "basis_sha256",
}
_HISTORICAL_BASIS_KEYS = _BASIS_KEYS | {"history_pack"}
_CONFIRMED_KEYS = {
    "version", "policy_version", "confirmation_id", "basis_id", "basis_sha256", "candidate_sha256",
    "initial_review_id", "confirmed_at", "trust_boundary",
}
_REVOKED_KEYS = {
    "version", "policy_version", "basis_id", "basis_sha256", "candidate_sha256", "initial_review_id",
    "reason", "revoked_at", "trust_boundary",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\x00" in value or len(value) > limit:
        raise ValueError(f"validation basis requires valid {name}")
    return value


def _timestamp(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"validation basis requires valid {name}")
    return float(value)


def _sha256(value: Any, name: str = "sha256") -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _SHA:
        raise ValueError(f"validation basis requires lowercase {name}")
    return value


def _read_stable(ref: Dict[str, Any], workspace: Path) -> bytes:
    if not isinstance(ref, dict):
        raise ValueError("validation evidence file reference must be an object")
    raw_path = _text(ref.get("path"), "file path")
    expected = _sha256(ref.get("sha256"))
    path = Path(raw_path)
    if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink() or not path.is_relative_to(workspace):
        raise ValueError("validation evidence must be a regular file inside its recorded workspace")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("validation evidence must be a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("validation evidence changed while opening")
        chunks = []
        while True:
            part = os.read(fd, 1024 * 1024)
            if not part:
                break
            chunks.append(part)
        after = os.fstat(fd)
        current = path.lstat()
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if identity(opened) != identity(after) or identity(after) != identity(current):
            raise ValueError("validation evidence changed while reading")
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("validation evidence hash changed")
    return data


def _workspace(value: Any) -> Path:
    raw = _text(value, "workspace")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or path.resolve(strict=True) != path or not path.is_dir():
        raise ValueError("validation evidence requires a stable absolute workspace")
    return path


def _reviewed_candidate(conn, candidate: Dict[str, Any]):
    row = conn.execute(
        "SELECT * FROM memory_reviews WHERE task_id=? AND stage='delivery' ORDER BY created_at DESC,rowid DESC LIMIT 1",
        (str(candidate["task_id"]),),
    ).fetchone()
    if not row or row["decision"] != "approve" or row["candidate_sha256"] != candidate["evidence_sha256"]:
        raise ValueError("validation basis requires the current approved initial review")
    body = candidate_evidence_bytes(candidate)
    return dict(row), body


def candidate_evidence_bytes(candidate: Dict[str, Any]) -> bytes:
    """Read the approved staged bytes before or after publication moves them."""
    path = Path(str(candidate["staged_path"]))
    if not path.is_file():
        published_marker = path.with_suffix(".published")
        if published_marker.is_file():
            path = published_marker
    if path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError("candidate evidence path changed")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != candidate["evidence_sha256"]:
        raise ValueError("candidate body hash changed")
    return body


def _events(conn, candidate_id: str):
    rows = conn.execute(
        "SELECT sequence,event_id,event_type,payload_json,created_at FROM events WHERE task_id=? AND event_type IN "
        "('memory.validation_basis_proposed','memory.validation_basis_confirmed','memory.validation_basis_revoked') ORDER BY sequence",
        (candidate_id,),
    )
    result = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ValueError("validation basis event is not valid JSON") from exc
        if not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1:
            raise ValueError("unsupported or malformed validation basis event version")
        expected = {
            "memory.validation_basis_proposed": _HISTORICAL_BASIS_KEYS if payload.get("task_origin") == "historical_direct" else _BASIS_KEYS,
            "memory.validation_basis_confirmed": _CONFIRMED_KEYS,
            "memory.validation_basis_revoked": _REVOKED_KEYS,
        }[row["event_type"]]
        if set(payload) != expected:
            raise ValueError("malformed validation basis event fields")
        result.append({**dict(row), "payload": payload})
    return result


def _reference(label: str, ref: str, sha: str) -> Dict[str, str]:
    return {"label": label, "ref": ref, "sha256": _sha256(sha)}


def _method_claims(value: Any, fallback: Any) -> list[Dict[str, str]]:
    if value is None:
        raise ValueError("awaiting_evidence: method claims must be explicitly classified as artifact or code")
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("validation basis requires 1..16 structured method claims")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"claim", "class"}:
            raise ValueError("each method claim requires exactly claim and class")
        claim = _text(item.get("claim"), "method claim", 1024)
        kind = item.get("class")
        if kind not in _CLAIM_CLASSES:
            raise ValueError("awaiting_evidence: method claim class must be artifact or code")
        result.append({"claim": claim, "class": kind})
    return result


def normalize_method_claims(value: Any, fallback: Any) -> list[Dict[str, str]]:
    return _method_claims(value, fallback)


def _checks_for_claims(claims: list[Dict[str, str]]) -> list[str]:
    classes = {item["class"] for item in claims}
    checks = []
    if "artifact" in classes:
        checks.append("real_task_acceptance")
    if "code" in classes:
        checks.append("sandbox")
    return sorted(checks)


def _route_for_checks(checks: list[str]) -> str:
    if checks == ["sandbox"]:
        return "sandbox"
    if checks == ["real_task_acceptance"]:
        return "real_task_acceptance"
    if checks == ["real_task_acceptance", "sandbox"]:
        return "combined"
    raise ValueError("method claims do not select a supported validation route")


def _validate_required_checks(value: Any, claims: list[Dict[str, str]]) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in _CHECKS for item in value):
        raise ValueError("required_checks must name real_task_acceptance and/or sandbox")
    checks = sorted(set(value))
    if checks != sorted(_checks_for_claims(claims)):
        raise ValueError("required checks do not match the structured method claims")
    return checks


def _application_scope(application: Dict[str, Any]) -> Dict[str, Any]:
    conditions = application.get("conditions_checked")
    if not isinstance(conditions, dict):
        raise ValueError("application scope requires checked conditions")
    return {
        "description": _text(application.get("real_task", {}).get("description"), "real task description"),
        "task_type": _text(application.get("real_task", {}).get("task_type"), "real task type"),
        "authorization_ref": _text(application.get("real_task", {}).get("authorization_ref"), "authorization reference"),
        "application_location": _text(application.get("application_location"), "application location"),
        "steps_applied": _text(application.get("steps_applied"), "applied steps"),
        "deviations": _text(application.get("deviations"), "application deviations"),
        "conditions_checked": {
            "applicability": _text(conditions.get("applicability"), "applicability"),
            "exclusions": _text(conditions.get("exclusions"), "exclusions"),
            "environment": _text(conditions.get("environment"), "environment"),
        },
    }


def _history_pack(value: Any) -> Dict[str, Any]:
    fields = {"version", "workspace", "task_id", "original_task_ref", "accepted_at",
              "work_record", "original_acceptance", "artifact", "execution_evidence"}
    if not isinstance(value, dict) or set(value) != fields or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("historical validation basis requires a complete evidence pack v1")
    if not isinstance(value.get("execution_evidence"), list) or not 1 <= len(value["execution_evidence"]) <= 8:
        raise ValueError("historical evidence pack requires 1..8 execution evidence files")
    _text(value.get("task_id"), "historical task id")
    _text(value.get("original_task_ref"), "historical task record")
    _text(value.get("accepted_at"), "historical acceptance time", 128)
    return dict(value)


def _task_origin(conn, application: Dict[str, Any], workspace: Path):
    real_task = application.get("real_task")
    task_id = _text(real_task.get("id") if isinstance(real_task, dict) else None, "real task id")
    if task_id == application.get("candidate_id"):
        raise ValueError("candidate authoring task cannot be real-task evidence")
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row:
        task = dict(row)
        if task["execution_mode"] == "host_authored":
            raise ValueError("candidate registration shell is not a real task")
        from .workflow import is_ordinary_user_task, verified_snapshot
        snapshot = verified_snapshot(task)
        if not is_ordinary_user_task(task, snapshot, conn):
            raise ValueError("only a completed ordinary user task can support real-task evidence")
        if task["status"] != "completed" or not task.get("result"):
            raise ValueError("real task is not completed")
        completed = conn.execute(
            "SELECT payload_json FROM events WHERE task_id=? AND event_type='task.completed' ORDER BY sequence DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        result_sha = hashlib.sha256(task["result"].encode("utf-8")).hexdigest()
        provenance = json.loads(task.get("provenance_json") or "{}")
        if (not completed or json.loads(completed[0]).get("result_sha256") != result_sha
                or provenance.get("result_sha256") != result_sha
                or not is_ordinary_user_task(task, snapshot, conn)):
            raise ValueError("real task completion evidence does not match its frozen task")
        prompt = _text(task.get("prompt"), "real task prompt")
        return "native_task", {
            "id": task_id,
            "purpose": "user_task",
            "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "snapshot_sha256": _sha256(task.get("execution_snapshot_hash"), "snapshot hash"),
            "completion_sha256": result_sha,
            "workspace": str(workspace),
        }, [_reference("真实任务冻结记录", "task:" + task_id + ":snapshot", task["execution_snapshot_hash"]),
            _reference("真实任务提示词", "task:" + task_id + ":prompt", hashlib.sha256(prompt.encode("utf-8")).hexdigest()),
            _reference("真实任务完成结果", "task:" + task_id + ":result", result_sha)]

    history = _history_pack(application.get("history_pack"))
    if _workspace(history.get("workspace")) != workspace:
        raise ValueError("historical evidence workspace does not match the application")
    original_ref = _text(history.get("original_task_ref"), "historical task record")
    if original_ref != _text(history.get("work_record", {}).get("path"), "historical work record path"):
        raise ValueError("historical task reference does not match the hashed work record")
    original_acceptance = history.get("original_acceptance")
    if not isinstance(original_acceptance, dict):
        raise ValueError("historical task requires its original acceptance record")
    _text(history.get("accepted_at"), "historical acceptance time", 128)
    refs = [history.get("work_record"), original_acceptance]
    for ref in refs:
        _read_stable(ref, workspace)
    items = [
        _reference("历史原任务记录", original_ref, _sha256(history["work_record"]["sha256"])),
        _reference("历史原采用记录", _text(original_acceptance.get("path"), "historical acceptance path"),
                   _sha256(original_acceptance.get("sha256"))),
    ]
    details = {
        "id": task_id,
        "purpose": "historical_direct",
        "original_task_ref": original_ref,
        "accepted_at": history["accepted_at"],
        "workspace": str(workspace),
    }
    return "historical_direct", details, items


def _effective_claims(conn, candidate: Dict[str, Any], review: Dict[str, Any], claims: list[Dict[str, str]],
                      *, before_sequence: Optional[int] = None):
    """A later application cannot remove a check required by an earlier claim."""
    merged = {(item["claim"], item["class"]) for item in claims}
    history = [event for event in _events(conn, str(candidate["task_id"]))
               if before_sequence is None or event["sequence"] < before_sequence]
    revoked = _revoked_basis_ids(history)
    for event in history:
        if event["event_type"] != "memory.validation_basis_proposed":
            continue
        previous = event["payload"]
        if previous.get("basis_id") in revoked:
            continue
        if (previous.get("candidate_sha256"), previous.get("initial_review_id")) != (
                candidate["evidence_sha256"], str(review["id"])):
            continue
        old = _method_claims(previous.get("method_claims"), previous.get("method_scope"))
        merged.update((item["claim"], item["class"]) for item in old)
    return [{"claim": claim, "class": kind} for claim, kind in sorted(merged)]


def _basis_uuid(identity: Any) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "xiaoba-validation-basis-v1:" + digest(identity)))


def _proposal_payload(conn, candidate: Dict[str, Any], application: Dict[str, Any], delivery: Dict[str, Any],
                      delivery_event_id: str, accepted_at: float, *,
                      before_sequence: Optional[int] = None) -> Dict[str, Any]:
    review, _ = _reviewed_candidate(conn, candidate)
    if application.get("formal_reuse"):
        raise ValueError("formal reuse is not a new-candidate validation basis")
    if (application.get("candidate_id"), application.get("candidate_sha256"), application.get("delivery_review_id")) != (
            candidate["task_id"], candidate["evidence_sha256"], review["id"]):
        raise ValueError("application must bind the current candidate and initial review")
    workspace = _workspace(application.get("real_task", {}).get("workspace"))
    _read_stable(application.get("artifact"), workspace)
    execution = application.get("execution_evidence")
    if not isinstance(execution, list) or not 1 <= len(execution) <= 8:
        raise ValueError("application must contain 1..8 execution evidence references")
    for ref in execution:
        _read_stable(ref, workspace)
    task_origin, real_task, task_items = _task_origin(conn, application, workspace)
    method_scope = _text(application.get("method_scope") or application.get("steps_applied"), "method scope")
    claims = _effective_claims(conn, candidate, review, _method_claims(application.get("method_claims"), method_scope),
                               before_sequence=before_sequence)
    required_checks = _checks_for_claims(claims)
    if application.get("required_checks") is not None:
        _validate_required_checks(application["required_checks"], claims)
    delivery_id = _text(delivery_event_id, "acceptance event id")
    if (delivery.get("decision") != "accept" or delivery.get("real_task_id") != real_task["id"]
            or delivery.get("artifact") != application.get("artifact")
            or application.get("application_id") not in delivery.get("application_ids", [])
            or delivery.get("origin") != "user"):
        raise ValueError("acceptance record does not bind this task, artifact and application")
    delivery_record = delivery.get("delivery_record")
    _read_stable(delivery_record, workspace)
    acceptance_sha = _sha256(delivery.get("input_sha256"), "acceptance input hash")
    evidence_items = list(task_items)
    evidence_items.append(_reference("采用成品", application["artifact"]["path"], application["artifact"]["sha256"]))
    for number, ref in enumerate(execution, 1):
        evidence_items.append(_reference(f"执行证据 {number}", ref["path"], ref["sha256"]))
    evidence_items.append(_reference("交付认可原件", delivery_record["path"], delivery_record["sha256"]))
    evidence_items.append(_reference("当前交付认可", "event:" + delivery_id, acceptance_sha))
    basis = {
        "version": 1,
        "policy_version": 1,
        "status": "proposed",
        "basis_id": _basis_uuid({
            "candidate_id": candidate["task_id"], "candidate_sha256": candidate["evidence_sha256"],
            "initial_review_id": review["id"], "application_id": application.get("application_id"),
            "delivery_event_id": delivery_id, "method_claims": claims,
        }),
        "candidate_id": str(candidate["task_id"]),
        "candidate_sha256": _sha256(candidate["evidence_sha256"], "candidate hash"),
        "initial_review_id": str(review["id"]),
        "route": _route_for_checks(required_checks),
        "required_checks": required_checks,
        "method_scope": method_scope,
        "method_claims": claims,
        "application_scope": _application_scope(application),
        "task_origin": task_origin,
        "real_task": real_task,
        "application_id": _text(application.get("application_id"), "application id"),
        "delivery_event_id": delivery_id,
        "artifact": dict(application["artifact"]),
        "execution_evidence": [dict(ref) for ref in execution],
        "acceptance_ref": "event:" + delivery_id,
        "acceptance_sha256": acceptance_sha,
        "accepted_at": accepted_at,
        "evidence_items": evidence_items,
        "trust_boundary": TRUST_BOUNDARY,
    }
    basis["basis_sha256"] = digest({key: value for key, value in basis.items() if key != "basis_sha256"})
    return basis


def propose_from_delivery_tx(store, conn, candidate_id: str, application: Dict[str, Any],
                             delivery: Dict[str, Any], delivery_event_id: str, accepted_at: float):
    candidate_row = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (candidate_id,)).fetchone()
    if not candidate_row:
        return None
    candidate = dict(candidate_row)
    from .experience_workflow import policy
    source = conn.execute("SELECT * FROM tasks WHERE id=?", (candidate_id,)).fetchone()
    if not source or policy(dict(source))["kind"] != "method":
        return None
    real_task_id = application.get("real_task", {}).get("id")
    recorded_task = conn.execute("SELECT * FROM tasks WHERE id=?", (real_task_id,)).fetchone()
    if not recorded_task and not application.get("history_pack"):
        return None
    if not application.get("method_claims"):
        if application.get("validation_basis_policy_version") == 1:
            return {"status": "awaiting_evidence", "reason": "缺少结构化方法主张及核验类别"}
        return None
    if recorded_task:
        from .workflow import is_ordinary_user_task, verified_snapshot
        task_dict = dict(recorded_task)
        snapshot = verified_snapshot(task_dict)
        if not is_ordinary_user_task(task_dict, snapshot, conn) or task_dict["status"] != "completed":
            return None
    basis = _proposal_payload(conn, candidate, application, delivery, delivery_event_id, accepted_at)
    for event in _events(conn, candidate_id):
        if event["event_type"] == "memory.validation_basis_proposed" and event["payload"].get("basis_sha256") == basis["basis_sha256"]:
            return event["event_id"]
    store._append_event_tx(conn, candidate_id, "memory.validation_basis_proposed", basis)
    return basis["basis_id"]


def _historical_basis_payload(conn, candidate: Dict[str, Any], proposal: Dict[str, Any], basis_id: str,
                              *, before_sequence: Optional[int] = None):
    review, _ = _reviewed_candidate(conn, candidate)
    if type(proposal.get("version")) is not int or proposal.get("version") != 1:
        raise ValueError("historical validation basis requires payload version 1")
    history = proposal.get("history_pack")
    if not isinstance(history, dict) or type(history.get("version")) is not int or history.get("version") != 1:
        raise ValueError("historical validation basis requires evidence pack version 1")
    workspace = _workspace(history.get("workspace"))
    task_origin, real_task, task_items = _task_origin(conn, {
        "candidate_id": candidate["task_id"], "real_task": {
            "id": _text(history.get("task_id"), "historical task id"),
            "workspace": str(workspace),
        }, "history_pack": history,
    }, workspace)
    if task_origin != "historical_direct":
        raise ValueError("historical command only accepts an unrecorded historical direct task")
    artifact = history.get("artifact")
    execution = history.get("execution_evidence")
    if not isinstance(artifact, dict) or not isinstance(execution, list) or not 1 <= len(execution) <= 8:
        raise ValueError("historical packet needs an artifact and 1..8 execution evidence files")
    _read_stable(artifact, workspace)
    for ref in execution:
        _read_stable(ref, workspace)
    acceptance = history["original_acceptance"]
    method_scope = _text(proposal.get("method_scope"), "method scope")
    claims = _effective_claims(conn, candidate, review, _method_claims(proposal.get("method_claims"), method_scope),
                               before_sequence=before_sequence)
    required_checks = _checks_for_claims(claims)
    if proposal.get("required_checks") is not None:
        _validate_required_checks(proposal["required_checks"], claims)
    app_scope = proposal.get("application_scope")
    if not isinstance(app_scope, dict) or set(app_scope) != {
            "description", "task_type", "authorization_ref", "application_location", "steps_applied",
            "deviations", "conditions_checked"}:
        raise ValueError("historical packet requires a complete application scope")
    app_scope = {
        "description": _text(app_scope.get("description"), "historical application description"),
        "task_type": _text(app_scope.get("task_type"), "historical application type"),
        "authorization_ref": _text(app_scope.get("authorization_ref"), "historical authorization reference"),
        "application_location": _text(app_scope.get("application_location"), "historical application location"),
        "steps_applied": _text(app_scope.get("steps_applied"), "historical steps applied"),
        "deviations": _text(app_scope.get("deviations"), "historical deviations"),
        "conditions_checked": {
            "applicability": _text(app_scope.get("conditions_checked", {}).get("applicability"), "historical applicability"),
            "exclusions": _text(app_scope.get("conditions_checked", {}).get("exclusions"), "historical exclusions"),
            "environment": _text(app_scope.get("conditions_checked", {}).get("environment"), "historical environment"),
        },
    }
    application_id = "historical:" + digest({"task": real_task, "artifact": artifact, "claims": claims})
    delivery_id = "historical:" + digest(history)
    basis = {
        "version": 1, "policy_version": 1, "status": "proposed", "basis_id": basis_id,
        "candidate_id": str(candidate["task_id"]),
        "candidate_sha256": _sha256(candidate["evidence_sha256"], "candidate hash"),
        "initial_review_id": str(review["id"]), "route": _route_for_checks(required_checks),
        "required_checks": required_checks, "method_scope": method_scope, "method_claims": claims,
        "application_scope": app_scope,
        "task_origin": task_origin, "real_task": real_task, "application_id": application_id,
        "delivery_event_id": delivery_id, "artifact": dict(artifact),
        "execution_evidence": [dict(ref) for ref in execution],
        "acceptance_ref": _text(acceptance.get("path"), "historical acceptance path"),
        "acceptance_sha256": _sha256(acceptance.get("sha256"), "historical acceptance hash"),
        "accepted_at": history["accepted_at"],
        "evidence_items": task_items + [_reference("采用成品", artifact["path"], artifact["sha256"])]
        + [_reference(f"执行证据 {i}", ref["path"], ref["sha256"]) for i, ref in enumerate(execution, 1)]
        + [_reference("历史原认可", acceptance["path"], acceptance["sha256"])],
        "trust_boundary": TRUST_BOUNDARY, "history_pack": history,
    }
    return basis


def propose_historical_basis_tx(store, conn, candidate: Dict[str, Any], proposal: Dict[str, Any]):
    """Stage a frozen historical evidence packet for the final review."""
    from .experience_workflow import policy
    from .store import EventStore
    if not EventStore.verify_event_chain_tx(conn)["ok"]:
        raise ValueError("审计事件链损坏，禁止登记历史验证依据")
    row = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (candidate.get("task_id"),)).fetchone()
    if not row or row["status"] not in {"delivery_approved", "pending_final_review", "revalidation_required"} or row["published_path"]:
        raise ValueError("historical basis requires an unpublished candidate awaiting final validation")
    source = conn.execute("SELECT * FROM tasks WHERE id=?", (candidate.get("task_id"),)).fetchone()
    if not source or policy(dict(source))["kind"] != "method":
        raise ValueError("historical basis applies only to method candidates")
    candidate = dict(row)
    review, _ = _reviewed_candidate(conn, candidate)
    basis_id = _basis_uuid({
        "candidate_id": candidate["task_id"], "candidate_sha256": candidate["evidence_sha256"],
        "initial_review_id": review["id"], "history_pack": proposal.get("history_pack"),
        "method_scope": proposal.get("method_scope"), "method_claims": proposal.get("method_claims"),
    })
    basis = _historical_basis_payload(conn, candidate, proposal, basis_id)
    basis["basis_sha256"] = digest({key: value for key, value in basis.items() if key != "basis_sha256"})
    for event in _events(conn, str(candidate["task_id"])):
        if (event["event_type"] == "memory.validation_basis_proposed"
                and event["payload"].get("basis_sha256") == basis["basis_sha256"]):
            return event["payload"]
    store._append_event_tx(conn, candidate["task_id"], "memory.validation_basis_proposed", basis)
    conn.execute("UPDATE memory_candidates SET updated_at=? WHERE task_id=?", (time.time(), candidate["task_id"]))
    return basis


def _source_application(conn, candidate_id: str, application_id: str):
    from .experience_loop import _events
    return next((item for item in _events(conn, "memory.application_recorded", candidate_id)
                 if item.get("application_id") == application_id and not item.get("formal_reuse")), None)


def _source_delivery(conn, event_id: str):
    row = conn.execute("SELECT payload_json,created_at FROM events WHERE event_id=? AND event_type='task.delivery_recorded'", (event_id,)).fetchone()
    if not row:
        raise ValueError("basis acceptance event is missing")
    return json.loads(row["payload_json"]), float(row["created_at"])


def validate_basis(conn, candidate: Dict[str, Any], *, require_confirmed: bool = False,
                   basis_id: Optional[str] = None) -> Dict[str, Any]:
    """Rebuild evidence from source events/files and reject stale or mismatched bindings."""
    history = _events(conn, str(candidate["task_id"]))
    proposals = [event for event in history if event["event_type"] == "memory.validation_basis_proposed"]
    if not proposals:
        from .experience_loop import _events as loop_events
        applications = [event for event in loop_events(conn, "memory.application_recorded", str(candidate["task_id"]))
                        if not event.get("formal_reuse")]
        if any(event.get("validation_basis_policy_version") == 1 and not event.get("method_claims")
               for event in applications):
            return {
                "status": "awaiting_evidence",
                "candidate_id": str(candidate["task_id"]),
                "method_scope": next((event.get("steps_applied") for event in reversed(applications)
                                      if event.get("validation_basis_policy_version") == 1), None),
                "method_claims": [],
                "required_checks": [],
                "reason": "缺少结构化方法主张及核验类别；补齐前不能选择免隔离路线",
            }
        return {"status": "missing"}
    review, _ = _reviewed_candidate(conn, candidate)
    current = [event for event in proposals if
               (event["payload"].get("candidate_sha256"), event["payload"].get("initial_review_id")) ==
               (candidate["evidence_sha256"], str(review["id"]))]
    if not current:
        raise ValueError("validation basis candidate or initial review changed")
    revoked = _revoked_basis_ids(history)
    active = [event for event in current if event["payload"]["basis_id"] not in revoked]
    # Verify every active proposal. A forged newest packet cannot be hidden by
    # selecting an older confirmation, while a valid new proposal is not itself
    # a revocation of an already confirmed, independently bound task.
    checked = [(event, *_validate_proposal(conn, candidate, history, event, allow_superseded=True))
               for event in active]
    current_checked = [(event, value) for event, value, superseded in checked if not superseded]
    required = set().union(*(set(value['required_checks']) for _, value in current_checked))
    if not current_checked:
        if checked:
            raise ValueError("current validation basis was invalidated by a newer delivery or withdrawal")
        selected = dict(current[0]["payload"], status="revoked")
    else:
        eligible = [(event, value) for event, value in current_checked
                    if required.issubset(set(value["required_checks"]))]
        confirmed = [(event, value) for event, value in eligible if value["status"] == "confirmed"]
        # Keep the first still-valid confirmation when it covers every claim.
        # A later proposal can add checks, but cannot replace the chosen task
        # merely by being newer or reduce an already applicable route.
        selected = (confirmed or eligible)[0][1]
    if basis_id is not None and (selected['status'] == 'revoked' or selected['basis_id'] != basis_id):
        raise ValueError("validation-basis confirmation does not match the current evidence")
    if require_confirmed and "real_task_acceptance" in selected.get("required_checks", []) and selected["status"] != "confirmed":
        raise ValueError("real-task validation basis is not currently confirmed")
    return selected


def matches_basis_confirmation(basis: Dict[str, Any], confirmation: Optional[Dict[str, Any]]) -> bool:
    return (
        isinstance(confirmation, dict)
        and set(confirmation) == {"basis_id", "basis_sha256", "confirmed", "classification_confirmed"}
        and confirmation["confirmed"] is True
        and confirmation["classification_confirmed"] is True
        and confirmation["basis_id"] == basis["basis_id"]
        and confirmation["basis_sha256"] == basis["basis_sha256"]
    )


def confirm_basis_tx(store, conn, candidate: Dict[str, Any], confirmation: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    requested = confirmation.get("basis_id") if isinstance(confirmation, dict) else None
    basis = validate_basis(conn, candidate, basis_id=requested)
    if basis["status"] == "missing":
        if confirmation is not None:
            raise ValueError("stale validation-basis confirmation: no current basis")
        return None
    if basis["status"] == "awaiting_evidence":
        raise ValueError(basis.get("reason") or "awaiting_evidence: method claims are incomplete")
    if basis["status"] == "revoked":
        raise ValueError("validation basis was revoked")
    if confirmation is None:
        raise ValueError("终审批准前须在裁决中心确认方法主张分类与核验路线")
    if not matches_basis_confirmation(basis, confirmation):
        raise ValueError("validation-basis confirmation does not match the current evidence or lacks explicit method classification confirmation")
    if basis["status"] == "proposed":
        event = {
            "version": 1,
            "policy_version": basis["policy_version"],
            "confirmation_id": str(uuid.uuid4()),
            "basis_id": basis["basis_id"],
            "basis_sha256": basis["basis_sha256"],
            "candidate_sha256": candidate["evidence_sha256"],
            "initial_review_id": basis["initial_review_id"],
            "confirmed_at": time.time(),
            "trust_boundary": TRUST_BOUNDARY,
        }
        store._append_event_tx(conn, candidate["task_id"], "memory.validation_basis_confirmed", event)
        basis = validate_basis(conn, candidate, require_confirmed=True, basis_id=basis["basis_id"])
    return basis_binding(basis)


def basis_binding(basis: Dict[str, Any]) -> Dict[str, Any]:
    keys = (
        "policy_version", "basis_id", "basis_sha256", "candidate_sha256", "initial_review_id",
        "route", "required_checks",
    )
    missing = [key for key in keys if key not in basis]
    if missing:
        raise ValueError("validation basis is incomplete; missing binding fields: " + ", ".join(missing))
    return {key: basis[key] for key in keys}


def revoke_basis_tx(store, conn, candidate_id: str, basis_id: str, reason: str, *,
                    expected_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Revoke the explicitly named proposal; never silently retarget a later one."""
    row = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (candidate_id,)).fetchone()
    if row is None:
        raise ValueError("candidate not found")
    candidate = dict(row)
    review, _ = _reviewed_candidate(conn, candidate)
    history = _events(conn, candidate_id)
    proposal = next((event for event in history if event["event_type"] == "memory.validation_basis_proposed"
                     and event["payload"].get("basis_id") == basis_id), None)
    if proposal is None:
        raise ValueError("validation basis id is not current")
    basis = proposal["payload"]
    if (basis.get("candidate_sha256"), basis.get("initial_review_id")) != (candidate["evidence_sha256"], str(review["id"])):
        raise ValueError("validation basis candidate or initial review changed")
    if basis.get("basis_sha256") != digest({key: value for key, value in basis.items() if key != "basis_sha256"}):
        raise ValueError("validation basis payload hash mismatch")
    if expected_sha256 is not None and _sha256(expected_sha256) != basis["basis_sha256"]:
        raise ValueError("stale validation-basis revocation")
    reason = _text(reason, "revocation reason", 1024)
    revoked = _revoked_basis_ids(history)
    if basis_id in revoked:
        prior = next(event["payload"] for event in history if event["event_type"] == "memory.validation_basis_revoked"
                     and event["payload"]["basis_id"] == basis_id)
        if prior["reason"] != reason:
            raise ValueError("validation basis was already revoked for a different reason")
        return prior
    payload = {"version": 1, "policy_version": basis["policy_version"],
               "basis_id": basis_id, "basis_sha256": basis["basis_sha256"],
               "candidate_sha256": basis["candidate_sha256"], "initial_review_id": basis["initial_review_id"],
               "reason": reason, "revoked_at": time.time(), "trust_boundary": TRUST_BOUNDARY}
    store._append_event_tx(conn, candidate_id, "memory.validation_basis_revoked", payload)
    return payload


def _revoked_basis_ids(history):
    proposals = {event["payload"]["basis_id"]: event for event in history
                 if event["event_type"] == "memory.validation_basis_proposed"}
    revoked = set()
    for event in history:
        if event["event_type"] != "memory.validation_basis_revoked":
            continue
        payload = event["payload"]
        proposal = proposals.get(payload.get("basis_id"))
        if proposal is None or event["sequence"] <= proposal["sequence"]:
            raise ValueError("validation-basis revocation has no preceding proposal")
        basis = proposal["payload"]
        binding = {key: basis[key] for key in
                   ("basis_id", "basis_sha256", "candidate_sha256", "initial_review_id", "policy_version", "trust_boundary")}
        if any(payload.get(key) != value for key, value in binding.items()):
            raise ValueError("validation-basis revocation binding changed")
        _text(payload.get("reason"), "revocation reason", 1024)
        _timestamp(payload.get("revoked_at"), "revocation time")
        if basis["basis_id"] in revoked:
            raise ValueError("duplicate validation-basis state events")
        revoked.add(basis["basis_id"])
    return revoked


def _validate_proposal(conn, candidate, history, proposal, *, allow_superseded=False):
    basis = proposal["payload"]
    superseded = False
    computed = digest({key: value for key, value in basis.items() if key != "basis_sha256"})
    if basis.get("basis_sha256") != computed:
        raise ValueError("validation basis payload hash mismatch")
    review, _ = _reviewed_candidate(conn, candidate)
    if (basis.get("candidate_sha256"), basis.get("initial_review_id")) != (
            candidate["evidence_sha256"], str(review["id"])):
        raise ValueError("validation basis candidate or initial review changed")
    if (basis.get("candidate_id") != str(candidate["task_id"])
            or basis.get("trust_boundary") != TRUST_BOUNDARY or basis.get("policy_version") != 1):
        raise ValueError("validation basis route or trust boundary is invalid")
    claims = _method_claims(basis.get("method_claims"), basis.get("method_scope"))
    required_checks = _validate_required_checks(basis.get("required_checks"), claims)
    if basis.get("route") != _route_for_checks(required_checks):
        raise ValueError("validation basis route does not match the structured claims")
    application = _source_application(conn, str(candidate["task_id"]), basis.get("application_id", ""))
    if application:
        delivery, accepted_at = _source_delivery(conn, basis.get("delivery_event_id", ""))
        rebuilt = _proposal_payload(conn, candidate, application, delivery, basis["delivery_event_id"], accepted_at,
                                    before_sequence=proposal["sequence"])
        # The nonce is assigned when proposed, but every evidence-bearing field is reproducible.
        rebuilt["basis_id"] = basis.get("basis_id")
        rebuilt["basis_sha256"] = digest({key: value for key, value in rebuilt.items() if key != "basis_sha256"})
        if rebuilt != basis:
            raise ValueError("validation basis no longer matches its application, task, artifact or acceptance")
        current_delivery = conn.execute(
            "SELECT event_id,payload_json FROM events WHERE event_type='task.delivery_recorded' "
            "AND json_extract(payload_json,'$.real_task_id')=? ORDER BY sequence DESC LIMIT 1",
            (basis["real_task"]["id"],),
        ).fetchone()
        if not current_delivery or current_delivery["event_id"] != basis["delivery_event_id"]:
            if not allow_superseded:
                raise ValueError("a newer delivery or withdrawal invalidated this basis")
            superseded = True
        payload = json.loads(current_delivery["payload_json"])
        if not superseded and payload.get("decision") != "accept":
            raise ValueError("delivery acceptance was withdrawn")
    else:
        history_pack = basis.get("history_pack")
        if not isinstance(history_pack, dict):
            raise ValueError("historical basis has no frozen source evidence pack")
        rebuilt = _historical_basis_payload(conn, candidate, {
            "version": 1, "history_pack": history_pack, "method_scope": basis.get("method_scope"),
            "method_claims": claims, "required_checks": required_checks,
            "application_scope": basis.get("application_scope"),
        }, basis.get("basis_id"), before_sequence=proposal["sequence"])
        rebuilt["basis_sha256"] = digest({key: value for key, value in rebuilt.items() if key != "basis_sha256"})
        if rebuilt != basis:
            raise ValueError("historical validation basis no longer matches its frozen evidence pack")
    confirmations = []
    revocations = []
    for event in history:
        if event["sequence"] <= proposal["sequence"] or event["payload"].get("basis_id") != basis.get("basis_id"):
            continue
        payload = event["payload"]
        binding = {
            "basis_id": basis["basis_id"], "basis_sha256": basis["basis_sha256"],
            "candidate_sha256": basis["candidate_sha256"],
            "initial_review_id": basis["initial_review_id"], "trust_boundary": TRUST_BOUNDARY,
            "policy_version": basis["policy_version"],
        }
        if event["event_type"] == "memory.validation_basis_confirmed":
            if any(payload.get(key) != value for key, value in binding.items()):
                raise ValueError("validation-basis confirmation binding changed")
            _text(payload.get("confirmation_id"), "confirmation id")
            _timestamp(payload.get("confirmed_at"), "confirmation time")
            confirmations.append(event)
        elif event["event_type"] == "memory.validation_basis_revoked":
            if any(payload.get(key) != value for key, value in binding.items()):
                raise ValueError("validation-basis revocation binding changed")
            _text(payload.get("reason"), "revocation reason", 1024)
            _timestamp(payload.get("revoked_at"), "revocation time")
            revocations.append(event)
    if len(confirmations) > 1 or len(revocations) > 1:
        raise ValueError("duplicate validation-basis state events")
    revoked = bool(revocations)
    needs_confirmation = "real_task_acceptance" in required_checks
    confirmed = bool(confirmations) and not revoked
    if revoked:
        status = "revoked"
    elif confirmed:
        status = "confirmed"
    elif not needs_confirmation:
        status = "not_required"
    else:
        status = "proposed"
    result = dict(basis, status=status)
    return result, superseded


def revoke_basis(store, candidate_id: str, request: Dict[str, Any]):
    """Public manual mutation, requiring a bound user decision and retry identity.

    Like the existing human-review CLI, only a direct user decision authorizes
    invoking this command; an Agent implementation approval is not that decision.
    """
    fields = {"version", "candidate_id", "candidate_sha256", "initial_review_id", "basis_id", "basis_sha256",
              "decision", "reason", "user_decision_ref", "idempotency_key"}
    if not isinstance(request, dict) or set(request) != fields or type(request.get("version")) is not int or request["version"] != 1:
        raise ValueError("revocation requires a complete version 1 user-decision receipt")
    if request.get("candidate_id") != candidate_id or request.get("decision") != "revoke":
        raise ValueError("revocation user decision does not match this candidate")
    for key in ("basis_id", "initial_review_id", "user_decision_ref", "idempotency_key"):
        _text(request.get(key), key, 1024)
    _sha256(request.get("candidate_sha256"), "candidate hash")
    _sha256(request.get("basis_sha256"), "basis hash")
    _text(request.get("reason"), "revocation reason", 1024)
    from .memory import MemoryBridge
    if MemoryBridge._sensitive_reason(_canonical(request)):
        raise ValueError("revocation receipt contains sensitive material")
    checksum = digest(request)
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not store.verify_event_chain_tx(conn)["ok"]:
            raise ValueError("event chain is invalid")
        for row in conn.execute("SELECT payload_json FROM events WHERE event_type='memory.validation_basis_revocation_requested'"):
            prior = json.loads(row[0])
            if prior.get("idempotency_key") == request["idempotency_key"]:
                if prior.get("request_sha256") != checksum:
                    raise ValueError("revocation retry identity was already used for different input")
                return dict(prior["revocation"], replayed=True)
        candidate = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (candidate_id,)).fetchone()
        if not candidate or candidate["evidence_sha256"] != request["candidate_sha256"]:
            raise ValueError("revocation candidate version changed")
        review, _ = _reviewed_candidate(conn, dict(candidate))
        if str(review["id"]) != request["initial_review_id"]:
            raise ValueError("revocation initial review changed")
        result = revoke_basis_tx(store, conn, candidate_id, request["basis_id"], request["reason"],
                                 expected_sha256=request["basis_sha256"])
        store._append_event_tx(conn, candidate_id, "memory.validation_basis_revocation_requested", {
            "version": 1, "idempotency_key": request["idempotency_key"], "request_sha256": checksum,
            "user_decision_ref": request["user_decision_ref"], "decision": "revoke", "revocation": result,
        })
        return result
