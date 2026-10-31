"""Task-local use of initially approved methods; never a review/approval writer.

The candidate database and its existing event chain remain authoritative. No
pending text enters KnowLP, memory_reuse, or an ordinary task's prompt.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from .experience_workflow import digest, latest_delivery, policy


def _events(conn, kind, candidate_id=None):
    sql, args = "SELECT * FROM events WHERE event_type=?", [kind]
    if candidate_id is not None:
        sql += " AND task_id=?"
        args.append(candidate_id)
    return [dict(json.loads(r["payload_json"]), event_id=r["event_id"], sequence=r["sequence"])
            for r in conn.execute(sql + " ORDER BY sequence", args)]


def _chain(conn):
    from .store import EventStore
    result = EventStore.verify_event_chain_tx(conn)
    if not result["ok"]:
        raise ValueError("experience loop requires a verifiable event chain")
    return result


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError("nonempty text required: " + name)
    return value


def _safe(value):
    from .memory import MemoryBridge
    text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(text.encode()) > 65536 or MemoryBridge._sensitive_reason(text):
        raise ValueError("oversized or sensitive experience-loop input")


def _candidate(conn, candidate_id):
    row = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (candidate_id,)).fetchone()
    if not row or row["published_path"] or row["status"] not in {"delivery_approved", "revalidation_required", "pending_final_review"}:
        raise ValueError("candidate is not an initially approved pending method")
    candidate = dict(row)
    source = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (candidate_id,)).fetchone())
    value = policy(source)
    if value["kind"] != "method":
        raise ValueError("only classified method candidates enter the pending index")
    from .validation_evidence import protect_final_decision
    protect_final_decision(conn, candidate)
    approved = latest_delivery(conn, candidate)
    if not any(e.get("review_id") == approved["id"] and e.get("stage") == "delivery"
               and e.get("decision") == "approve" and e.get("candidate_sha256") == candidate["evidence_sha256"]
               for e in _events(conn, "memory.reviewed", candidate_id)):
        raise ValueError("initial approval receipt is not verifiable in the event chain")
    path = Path(candidate["staged_path"])
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
        raise ValueError("candidate must be a regular file of at most 128 KiB")
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != candidate["evidence_sha256"]:
        raise ValueError("candidate bytes changed after initial approval")
    return candidate, approved, source, value, body.decode("utf-8")


def pending_index(store, problem_key=None, query=None):
    """A read-only, hashed projection, always rebuilt from current candidates."""
    from .experience_workflow import _verify_trial_qualification
    with store.read_only() as conn:
        chain = _chain(conn)
        entries = []
        for row in conn.execute("SELECT task_id FROM memory_candidates WHERE status IN ('delivery_approved','revalidation_required') ORDER BY task_id"):
            try:
                c, approved, source, p, _ = _candidate(conn, row[0])
                status = _verify_trial_qualification(conn, c)
                if status.get("qualified") or status.get("state") in {"invalid_schema", "awaiting_check", "retired", "final_decision"}:
                    continue
                exact = bool(problem_key and p.get("problem_key") == problem_key)
                words = bool(query and query.strip().casefold() in source["prompt"].casefold())
                if (problem_key or query) and not (exact or words):
                    continue
                entries.append({"task_id": c["task_id"], "title": source["prompt"],
                    "candidate_sha256": c["evidence_sha256"], "delivery_review_id": approved["id"],
                    "problem_key": p.get("problem_key", ""), "task_type": p.get("task_type"),
                    "applicability_conditions": p.get("applicability_conditions"),
                    "exclusions": p.get("exclusions"), "environment": p.get("environment"),
                    "conditions_require_task_check": True, "validation_state": status["state"],
                    "source_path": c["staged_path"], "evidence_command": "memory-trial-status " + c["task_id"],
                    "match_basis": "problem_key" if exact else "title" if words else "inventory",
                    "normal_retrieval": False})
            except (ValueError, OSError, UnicodeError):
                continue  # Invalid objects never leak into the pending view.
        entries.sort(key=lambda e: (e["match_basis"] != "problem_key", e["task_id"]))
        value = {"version": 1, "source": "candidate_database", "normal_retrieval": False,
                 "event_chain_head": chain["head"], "entries": entries}
        return dict(value, index_sha256=digest(value))


def read_pending(store, candidate_id, candidate_sha256, delivery_review_id):
    with store.read_only() as conn:
        _chain(conn)
        c, approved, _, _, body = _candidate(conn, candidate_id)
        if (candidate_sha256, delivery_review_id) != (c["evidence_sha256"], approved["id"]):
            raise ValueError("pending source binding changed; retrieve again")
        _safe(body)
        return {"task_id": candidate_id, "candidate_sha256": candidate_sha256,
                "delivery_review_id": delivery_review_id, "body": body,
                "classification": "initially_approved_pending_validation", "applied": False}


def _file(ref, workspace):
    if not isinstance(ref, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(ref.get("sha256", ""))):
        raise ValueError("file reference requires SHA-256")
    path = Path(_text(ref.get("path"), "path"))
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_relative_to(workspace) or not path.is_file():
        raise ValueError("evidence must be a regular file inside the authorized task workspace")
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1048576), b""):
            sha.update(chunk)
    if sha.hexdigest() != ref["sha256"]:
        raise ValueError("task artifact or execution evidence changed")


def _application_files(value):
    workspace = Path(_text(value["real_task"].get("workspace"), "workspace"))
    if not workspace.is_absolute() or workspace.resolve() != workspace or not workspace.is_dir():
        raise ValueError("real task needs an explicit absolute workspace")
    _file(value["artifact"], workspace)
    refs = value.get("execution_evidence")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
        raise ValueError("actual application requires 1..8 execution evidence files")
    for ref in refs:
        _file(ref, workspace)


def trial_binding(application):
    return {"application_id": application["application_id"],
            "real_task_id": application["real_task"]["id"], "artifact_sha256": application["artifact"]["sha256"]}


def record_application(store, value):
    """Explicit host report of actual use; retrieval itself never calls this."""
    if not isinstance(value, dict):
        raise ValueError("application must be a JSON object")
    _safe(value)
    if any(not isinstance(value.get(k), dict) for k in ("real_task", "artifact", "conditions_checked")):
        raise ValueError("real_task/artifact/conditions_checked must be JSON objects")
    for key in ("candidate_id", "candidate_sha256", "delivery_review_id", "application_location", "steps_applied", "deviations"):
        _text(value.get(key), key)
    for key in ("id", "problem_key", "description", "task_type", "authorization_ref"):
        _text(value.get("real_task", {}).get(key), "real_task." + key)
    for key in ("applicability", "exclusions", "environment"):
        _text(value.get("conditions_checked", {}).get(key), "conditions_checked." + key)
    _text(value.get("artifact", {}).get("version"), "artifact.version")
    _application_files(value)
    application = dict(value, application_id=digest(value))
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _chain(conn)
        c, approved, _, p, _ = _candidate(conn, value["candidate_id"])
        if value["real_task"]["id"] == c["task_id"]:
            raise ValueError("a later real task must differ from the candidate-authoring task")
        if (value["candidate_sha256"], value["delivery_review_id"]) != (c["evidence_sha256"], approved["id"]):
            raise ValueError("application must bind current candidate and initial approval")
        if p.get("problem_key") and p["problem_key"] != value["real_task"]["problem_key"]:
            raise ValueError("application problem_key does not match the candidate")
        old = _events(conn, "memory.application_recorded", c["task_id"])
        if not any(a["application_id"] == application["application_id"] for a in old):
            from .experience_workflow import _verify_trial_qualification
            if _verify_trial_qualification(conn, c).get("qualified"):
                raise ValueError("record actual use before isolated validation, not retrospectively")
            store._append_event_tx(conn, c["task_id"], "memory.application_recorded", application)
            conn.execute("UPDATE memory_candidates SET updated_at=? WHERE task_id=?", (time.time(), c["task_id"]))
    return dict(application, trial_binding=trial_binding(application), normal_retrieval=False)


def validate_trial_application(conn, candidate, card):
    binding = card.get("experience_application")
    applications = _events(conn, "memory.application_recorded", candidate["task_id"])
    if binding is None:
        if applications:
            raise ValueError("actual-task use requires an explicit trial application binding")
        return  # Old trial cards keep their existing behavior.
    app = next((a for a in applications if trial_binding(a) == binding), None)
    if not app or app["candidate_sha256"] != candidate["evidence_sha256"] or app["delivery_review_id"] != latest_delivery(conn, candidate)["id"]:
        raise ValueError("trial application binding is missing or stale")
    _application_files(app)


def application_packet(conn, candidate):
    applications = _events(conn, "memory.application_recorded", candidate["task_id"])
    deliveries = _events(conn, "task.delivery_recorded") if applications else []
    attached = _events(conn, "memory.delivery_attached", candidate["task_id"]) if applications else []
    return {"applications": applications, "deliveries": [d for d in deliveries if any(
                a["real_task"]["id"] == d["real_task_id"] for a in applications)], "attachments": attached,
            "external_reviews": external_reviews(conn, candidate["task_id"]) if applications else [],
            "approval": "human_final_review_required", "causal_effect": "not_inferred_from_product_acceptance"}


def external_reviews(conn, candidate_id):
    """Expose original optional ingress receipts, including FAIL and stale sources.

    This is a display projection, never a replacement qualification evaluator.
    Read on every view so later receipts cannot be hidden by a delivery snapshot.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"ri_requests", "ri_assignments", "ri_receipts", "experience_validation_records"} <= tables:
        return []
    record = conn.execute("SELECT * FROM experience_validation_records WHERE candidate_id=? ORDER BY round DESC LIMIT 1", (candidate_id,)).fetchone()
    if not record:
        return []
    from .validation_evidence import binding
    expected = binding(dict(record))
    result = []
    for request in conn.execute("SELECT * FROM ri_requests WHERE candidate_id=? ORDER BY rowid", (candidate_id,)):
        if json.loads(request["common"]).get("validation_binding") != expected:
            continue
        for row in conn.execute("SELECT r.*,a.role,a.source_id,a.active FROM ri_receipts r JOIN ri_assignments a ON a.id=r.assignment_id WHERE r.request_id=? ORDER BY r.rowid", (request["id"],)):
            result.append({"request_id": request["id"], "plan_revision": request["revision"],
                "request_state": request["state"], "receipt_id": row["id"], "reviewer_id": row["source_id"],
                "role": row["role"], "active": bool(row["active"]), "status": row["status"],
                "attestation": row["attestation"], "receipt_sha256": row["digest"],
                "original_receipt": json.loads(row["data"])})
    return result


def attach_delivery_tx(store, conn, candidate_id, delivery_id, applications):
    """Refresh evidence when either delivery or asynchronous trial completion lands."""
    from .experience_workflow import _verify_trial_qualification, extract_trial_verdict
    candidate = dict(conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (candidate_id,)).fetchone())
    technical = _verify_trial_qualification(conn, candidate)
    qualification = require_application_delivery(conn, candidate, technical) if technical.get("qualified") else technical
    trial_id = technical.get("task_id")
    if not trial_id:
        links = _events(conn, "memory.trial_linked", candidate_id)
        trial_id = links[-1]["task_id"] if links else None
    from .workflow import verified_snapshot
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (trial_id,)).fetchone() if trial_id else None
    trial = verified_snapshot(dict(task)).get("experience_trial", {}) if task else {}
    reviews = [dict(r) for r in conn.execute("SELECT id,agent_name,status,output,created_at,finished_at FROM agent_runs WHERE task_id=? ORDER BY created_at,rowid", (trial_id,))] if trial_id else []
    for r in reviews:
        r["validation_rating"] = extract_trial_verdict(r.get("output") or "") if r["status"] == "completed" else None
        r["numeric_score"] = None
        r["score_status"] = "not_supplied; no_invented_rubric_or_threshold"
    for app in applications:
        attachment = {"delivery_event_id": delivery_id, "application_id": app["application_id"],
            "candidate_sha256": app["candidate_sha256"], "delivery_review_id": app["delivery_review_id"],
            "validation": qualification, "validation_task_id": trial_id,
            "execution": trial.get("execution"), "plan_sha256": trial.get("card_sha256"),
            "execution_sha256": trial.get("execution_sha256"), "independent_reviews": reviews,
            "external_reviews": external_reviews(conn, candidate_id),
            "human_final_review_required": True}
        sha = digest(attachment)
        if not any(a.get("attachment_sha256") == sha for a in _events(conn, "memory.delivery_attached", candidate_id)):
            store._append_event_tx(conn, candidate_id, "memory.delivery_attached", dict(attachment, attachment_sha256=sha))
            # The existing atomic browser guard already binds updated_at.
            conn.execute("UPDATE memory_candidates SET updated_at=? WHERE task_id=?", (time.time(), candidate_id))


def refresh_delivery_tx(store, conn, candidate_id):
    packet = application_packet(conn, {"task_id": candidate_id})
    if packet["deliveries"]:
        delivery = packet["deliveries"][-1]
        apps = [a for a in packet["applications"] if a["application_id"] in delivery["application_ids"]]
        if apps:
            attach_delivery_tx(store, conn, candidate_id, delivery["event_id"], apps)


def _application_for_trial(conn, applications, task_id):
    """本轮绑定的那次应用：按发起审查的试验卡解析，不按“最新一条”。

    同一条经验在同一轮里被多个任务应用时，本轮只绑定最先发起审查的那次应用；
    后来登记的应用保留记录，不影响本轮。试验卡没写绑定或绑定已失效时返回 None，
    由调用方决定是拒绝还是退回旧行为。
    """
    from .workflow import verified_snapshot
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone() if task_id else None
    trial = verified_snapshot(dict(task)).get("experience_trial") if task else None
    card = trial.get("card") if isinstance(trial, dict) else None
    binding_value = card.get("experience_application") if isinstance(card, dict) else None
    if binding_value is None:
        return None
    return next((a for a in applications if trial_binding(a) == binding_value), None)


def require_application_delivery(conn, candidate, verdict):
    """Extra conditions only for candidates opting into actual-task use."""
    packet = application_packet(conn, candidate)
    applications = packet["applications"]
    if not applications:
        return verdict  # Published, legacy, and existing trial behavior stay intact.
    try:
        # 终审按本轮发起审查的那次应用检查，不按最新一条。
        app = _application_for_trial(conn, applications, verdict.get("task_id"))
        if app is None:
            raise ValueError("current isolated validation is not bound to this real-task application")
        if (app["candidate_sha256"], app["delivery_review_id"]) != (candidate["evidence_sha256"], latest_delivery(conn, candidate)["id"]):
            raise ValueError("application refers to a superseded candidate or initial review")
        _application_files(app)
        deliveries = [d for d in packet["deliveries"] if d["real_task_id"] == app["real_task"]["id"]]
        latest = deliveries[-1] if deliveries else {}
        if latest.get("decision") != "accept" or latest.get("artifact") != app["artifact"] or app["application_id"] not in latest.get("application_ids", []):
            raise ValueError("current product version needs explicit user delivery acceptance")
        _file(latest["delivery_record"], Path(app["real_task"]["workspace"]))
    except (ValueError, OSError, KeyError, TypeError) as exc:
        return dict(verdict, qualified=False, ready=False, state="application_delivery_required", reasons=[str(exc)])
    return verdict


def final_binding(conn, candidate):
    """Bind the human decision to the actual delivery event, not just the trial."""
    packet = application_packet(conn, candidate)
    if not packet["applications"]:
        return None
    links = _events(conn, "memory.trial_linked", candidate["task_id"])
    task_id = links[-1]["task_id"] if links else None
    # 与终审同一口径：优先用本轮绑定的那次应用；解析不到时退回最新一条，不阻断发布。
    # 任务快照漂移等原因解析失败也不能抛：终审与发布两条路径都直接调它。
    try:
        app = _application_for_trial(conn, packet["applications"], task_id)
    except (ValueError, OSError, KeyError, TypeError):
        app = None
    if app is None:
        app = packet["applications"][-1]
    deliveries = [d for d in packet["deliveries"] if d["real_task_id"] == app["real_task"]["id"]]
    delivery = deliveries[-1] if deliveries else {}
    reviews = [dict(r) for r in conn.execute("SELECT id,agent_name,status,output FROM agent_runs WHERE task_id=? ORDER BY created_at,rowid", (task_id,))] if task_id else []
    return digest({"application_id": app["application_id"], "delivery_event_id": delivery.get("event_id"),
                   "candidate_sha256": app["candidate_sha256"], "delivery_review_id": app["delivery_review_id"],
                   "artifact": app["artifact"], "delivery_record": delivery.get("delivery_record"),
                   "reviews_sha256": digest({"native": reviews, "external": packet["external_reviews"]})})


def record_delivery(store, value):
    """Attach a genuine user event. This function cannot create approval rows."""
    if not isinstance(value, dict):
        raise ValueError("delivery must be a JSON object")
    _safe(value)
    decision = value.get("decision", "accept")
    allowed = r"交付[。！!]?" if decision == "accept" else r"撤回验收[。！!]?" if decision == "withdraw" else r"(?!)"
    if value.get("origin") != "user" or not re.fullmatch(allowed, str(value.get("user_message", "")).strip()):
        raise ValueError("only a direct user delivery/withdrawal message can record acceptance")
    for key in ("message_id", "source_ref", "real_task_id"):
        _text(value.get(key), key)
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _chain(conn)
        history = _events(conn, "task.delivery_recorded")
        same_message = next((d for d in history if d["message_id"] == value["message_id"]), None)
        if same_message:
            if same_message["input_sha256"] != digest(value):
                raise ValueError("delivery message identity was already used for different input")
            return same_message
        apps = [a for a in _events(conn, "memory.application_recorded") if a["real_task"]["id"] == value["real_task_id"] and a["artifact"] == value.get("artifact")]
        if not apps:
            raise ValueError("no actual applications bind this task and artifact version")
        for app in apps:
            if decision == "accept":
                _application_files(app)
            _file(value["delivery_record"], Path(app["real_task"]["workspace"]))
        app_ids = sorted(a["application_id"] for a in apps)
        latest = next((d for d in reversed(history) if d["real_task_id"] == value["real_task_id"]), None)
        if latest and (latest["decision"], latest["artifact"], latest["application_ids"]) == (decision, value["artifact"], app_ids):
            return latest
        delivery = dict(value, decision=decision, input_sha256=digest(value), application_ids=app_ids)
        delivery_id = store._append_event_tx(conn, None, "task.delivery_recorded", delivery)
        for candidate_id in sorted({a["candidate_id"] for a in apps}):
            attach_delivery_tx(store, conn, candidate_id, delivery_id, [a for a in apps if a["candidate_id"] == candidate_id])
        return dict(delivery, event_id=delivery_id)
