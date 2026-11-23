"""Event-backed preparation queue for experience candidates awaiting value review."""

from __future__ import annotations

import json
import hashlib
import math
import re
import sqlite3
from typing import Any, Dict, List, Optional


_HASH = re.compile(r"[0-9a-f]{64}\Z")
_DEFER_FIELDS = {
    "version", "staged_path", "candidate_sha256", "source_task_ids", "reason",
}
_ASSESSMENT_FIELDS_V1 = {
    "version", "candidate_sha256", "scores", "samples", "average", "spread",
    "duplicate_of", "reason", "decision", "human_approval",
}
_ASSESSMENT_FIELDS_V2 = _ASSESSMENT_FIELDS_V1 | {"review_request_id"}
_ASSESSMENT_FIELDS_V3 = _ASSESSMENT_FIELDS_V1 | {"review_mode"}


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value:
        raise ValueError("待评分事件必须绑定有效 task_id")
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError("candidate_sha256 必须是小写 SHA-256")
    return value


def _text(value: Any, field: str, limit: int = 1024) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or "\x00" in value or len(value) > limit:
        raise ValueError(f"{field} 必须是有效的非空文本")
    return value


def _require_task(conn: sqlite3.Connection, task_id: str) -> None:
    if conn.execute("SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is None:
        raise ValueError("待评分事件引用了不存在的任务")


def _defer_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _DEFER_FIELDS:
        raise ValueError("memory.value_unscored v1 字段不匹配")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("不支持的 memory.value_unscored 版本")
    staged_path = _text(payload["staged_path"], "staged_path", 4096)
    candidate_sha256 = _sha256(payload["candidate_sha256"])
    source_task_ids = payload["source_task_ids"]
    if not isinstance(source_task_ids, list):
        raise ValueError("source_task_ids 必须是字符串列表")
    sources = [_text(source, "source_task_ids 项", 512) for source in source_task_ids]
    if len(sources) != len(set(sources)):
        raise ValueError("source_task_ids 不能重复")
    reason = _text(payload["reason"], "reason", 512)
    return {
        "version": 1,
        "staged_path": staged_path,
        "candidate_sha256": candidate_sha256,
        "source_task_ids": sorted(sources),
        "reason": reason,
    }


def _assessment_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict) or type(payload.get("version")) is not int:
        raise ValueError("memory.value_assessed v1 字段不匹配")
    version = payload["version"]
    fields = ({1: _ASSESSMENT_FIELDS_V1, 2: _ASSESSMENT_FIELDS_V2,
               3: _ASSESSMENT_FIELDS_V3}.get(version))
    if fields is None:
        raise ValueError("不支持的 memory.value_assessed 版本")
    if set(payload) != fields:
        raise ValueError(f"memory.value_assessed v{version} 字段不匹配")
    candidate_sha256 = _sha256(payload["candidate_sha256"])
    review_request_id = _text(payload["review_request_id"], "review_request_id", 128) if version == 2 else None
    scores = payload["scores"]
    if not isinstance(scores, dict) or not scores:
        raise ValueError("价值评分必须包含至少一名审查员")
    normalized_scores: Dict[str, float] = {}
    for reviewer, score in scores.items():
        reviewer = _text(reviewer, "reviewer_id", 512)
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 10:
            raise ValueError("价值评分必须是 0–10 的有限数")
        normalized_scores[reviewer] = score
    samples = payload["samples"]
    if type(samples) is not int or samples != len(normalized_scores):
        raise ValueError("价值评分 samples 与审查员数量不一致")
    average, spread = payload["average"], payload["spread"]
    if type(average) not in (int, float) or not math.isfinite(average):
        raise ValueError("价值评分 average 必须是有限数")
    if type(spread) not in (int, float) or not math.isfinite(spread):
        raise ValueError("价值评分 spread 必须是有限数")
    expected_average = sum(normalized_scores.values()) / samples
    expected_spread = max(normalized_scores.values()) - min(normalized_scores.values())
    if not math.isclose(average, expected_average, rel_tol=0, abs_tol=1e-9):
        raise ValueError("价值评分 average 与评分明细不一致")
    if not math.isclose(spread, expected_spread, rel_tol=0, abs_tol=1e-9):
        raise ValueError("价值评分 spread 与评分明细不一致")
    duplicates = payload["duplicate_of"]
    if not isinstance(duplicates, list):
        raise ValueError("duplicate_of 必须是字符串列表")
    normalized_duplicates = [_text(value, "duplicate_of 项", 512) for value in duplicates]
    if normalized_duplicates != sorted(set(normalized_duplicates)):
        raise ValueError("duplicate_of 必须排序且无重复")
    reason = payload["reason"]
    if type(reason) is not str:
        raise ValueError("价值评分 reason 无效")
    expected_reason = (
        "duplicate" if normalized_duplicates else
        "disagreement" if expected_spread >= 4 else
        "average" if expected_average >= 6 else
        "below_threshold"
    )
    expected_decision = "discard" if expected_reason == "below_threshold" else "initial_review"
    if reason != expected_reason or payload["decision"] != expected_decision:
        raise ValueError("价值评分决定与 6/4 规则不一致")
    if payload["human_approval"] is not False:
        raise ValueError("价值评分事件不能表示用户批准")
    normalized = {
        "version": version,
        "candidate_sha256": candidate_sha256,
        "scores": normalized_scores,
        "samples": samples,
        "average": average,
        "spread": spread,
        "duplicate_of": normalized_duplicates,
        "reason": reason,
        "decision": expected_decision,
        "human_approval": False,
    }
    if version == 2:
        normalized["review_request_id"] = review_request_id
    if version == 3:
        review_mode = payload["review_mode"]
        if review_mode not in {"two_pool", "pool_plus_host", "host_fallback_no_independent_review"}:
            raise ValueError("价值评分 review_mode 无效")
        if ((review_mode == "host_fallback_no_independent_review" and samples != 1)
                or (review_mode != "host_fallback_no_independent_review" and samples != 2)):
            raise ValueError("宿主兜底价值评分必须明确标为无独立审查")
        normalized["review_mode"] = review_mode
    return normalized


def _canonical(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _event_rows(conn: sqlite3.Connection, event_type: str):
    return conn.execute(
        "SELECT sequence,event_id,task_id,payload_json FROM events WHERE event_type=? ORDER BY sequence",
        (event_type,),
    )


def _versioned_payload(raw: str, event_type: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{event_type} payload 不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{event_type} payload 必须是对象")
    if "version" not in payload:
        return None
    allowed = {1, 2, 3} if event_type == "memory.value_assessed" else {1}
    if type(payload["version"]) is not int or payload["version"] not in allowed:
        raise ValueError(f"不支持的 {event_type} 版本")
    return payload


def _dispatch_payload(payload: Any) -> Dict[str, Any]:
    fields = {"version", "candidate_sha256", "request_id", "status", "reason"}
    if not isinstance(payload, dict) or set(payload) != fields:
        raise ValueError("memory.value_review_dispatch v1 字段不匹配")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("不支持的 memory.value_review_dispatch 版本")
    candidate_sha256 = _sha256(payload["candidate_sha256"])
    request_id = payload["request_id"]
    if request_id is not None:
        request_id = _text(request_id, "review_request_id", 128)
    status = payload["status"]
    if status not in {"blocked", "started", "failed", "waiting_receipts"}:
        raise ValueError("价值评分派工状态无效")
    reason = payload["reason"]
    if not isinstance(reason, str) or len(reason) > 512 or "\x00" in reason:
        raise ValueError("价值评分派工原因无效")
    return {
        "version": 1,
        "candidate_sha256": candidate_sha256,
        "request_id": request_id,
        "status": status,
        "reason": reason,
    }


def defer_tx(conn: sqlite3.Connection, task_id: str, payload: Dict[str, Any]) -> str:
    """Append a versioned, body-free preparation event; return its event id."""
    task_id = _task_id(task_id)
    normalized = _defer_payload(payload)
    _require_task(conn, task_id)
    encoded = _canonical(normalized)
    for _, event_id, event_task_id, raw in _event_rows(conn, "memory.value_unscored"):
        if event_task_id != task_id:
            continue
        existing = _versioned_payload(raw, "memory.value_unscored")
        if existing is None:
            continue
        prior = _defer_payload(existing)
        if prior["candidate_sha256"] == normalized["candidate_sha256"]:
            if _canonical(prior) != encoded:
                raise ValueError("同一任务与候选哈希不能重复登记不同待评分材料")
            return event_id
    from .store import EventStore

    return EventStore._append_event_tx(conn, task_id, "memory.value_unscored", normalized)


def assessment_tx(conn: sqlite3.Connection, task_id: str, payload: Dict[str, Any]) -> str:
    """Append an immutable versioned value decision bound to candidate bytes."""
    task_id = _task_id(task_id)
    normalized = _assessment_payload(payload)
    _require_task(conn, task_id)
    encoded = _canonical(normalized)
    for _, event_id, event_task_id, raw in _event_rows(conn, "memory.value_assessed"):
        if event_task_id != task_id:
            continue
        existing = _versioned_payload(raw, "memory.value_assessed")
        if existing is None:
            continue
        prior = _assessment_payload(existing)
        if prior["candidate_sha256"] == normalized["candidate_sha256"]:
            if _canonical(prior) != encoded:
                raise ValueError("同一任务与候选哈希不能登记冲突的价值决定")
            return event_id
    from .store import EventStore

    return EventStore._append_event_tx(conn, task_id, "memory.value_assessed", normalized)


def dispatch_status_tx(
    conn: sqlite3.Connection,
    task_id: str,
    payload: Dict[str, Any],
) -> str:
    """Append a visible dispatch state while the candidate remains queued."""
    task_id = _task_id(task_id)
    normalized = _dispatch_payload(payload)
    _require_task(conn, task_id)
    if not any(item["task_id"] == task_id and item["candidate_sha256"] == normalized["candidate_sha256"]
               for item in pending_items(conn)):
        raise ValueError("候选已不在待价值评分队列，拒绝更新派工状态")
    from .store import EventStore

    return EventStore._append_event_tx(conn, task_id, "memory.value_review_dispatch", normalized)


def assessment_from_review_request_tx(
    conn: sqlite3.Connection,
    task_id: str,
    request_id: str,
    candidate_sha256: str,
) -> Dict[str, Any]:
    """Derive a value decision from the request's two current, attested pool receipts."""
    task_id = _task_id(task_id)
    request_id = _text(request_id, "review_request_id", 128)
    candidate_sha256 = _sha256(candidate_sha256)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ri_%'"
    )}
    required = {"ri_requests", "ri_assignments", "ri_sources", "ri_receipts"}
    if not required.issubset(tables):
        raise ValueError("Agent 池审查记录不可用，候选保持待价值评分")

    request = conn.execute("SELECT * FROM ri_requests WHERE id=?", (request_id,)).fetchone()
    if request is None or request["candidate_id"] != task_id:
        raise ValueError("价值审查请求未绑定当前候选")
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise ValueError("价值审查来源任务不存在")
    from .task_participants import execution_context
    excluded_ids = set(execution_context(None, dict(task), conn).get("excluded_ids", []))
    try:
        common = json.loads(request["common"])
    except (TypeError, ValueError) as exc:
        raise ValueError("价值审查任务卡损坏") from exc
    if (not isinstance(common, dict) or common.get("value_review") != {"version": 1}
            or common.get("review_type") != "new_experience"
            or common.get("contract_version") != 2
            or common.get("candidate_sha256") != candidate_sha256
            or common.get("delivery_review") is not None
            or not isinstance(common.get("body"), str)
            or hashlib.sha256(common["body"].encode("utf-8")).hexdigest() != candidate_sha256):
        raise ValueError("价值审查没有绑定当前待评分候选版本")

    assignments = conn.execute(
        "SELECT * FROM ri_assignments WHERE request_id=? AND active=1 ORDER BY role", (request_id,)
    ).fetchall()
    if len(assignments) != 2 or {row["role"] for row in assignments} != {"dsh", "peer"}:
        raise ValueError("价值审查必须有两名独立审查员")
    from xiaoba_review.agent_pool import independence

    reviews = []
    identities = set()
    independent_sources = set()
    for assignment in assignments:
        try:
            card = json.loads(assignment["card"])
        except (TypeError, ValueError) as exc:
            raise ValueError("价值审查任务卡损坏") from exc
        if (not isinstance(card, dict) or card.get("review_request_id") != request_id
                or card.get("assignment_id") != assignment["id"]
                or type(card.get("plan_revision")) is not int
                or not (1 <= card["plan_revision"] <= request["revision"])
                or card.get("reviewer_role") != assignment["role"]
                or card.get("reviewer_id") != assignment["source_id"]
                or card.get("candidate_sha256") != candidate_sha256
                or card.get("input_digest") != hashlib.sha256(_canonical(common).encode("utf-8")).hexdigest()
                or card.get("review_type") != "new_experience"
                or card.get("value_review") != {"version": 1}
                or card.get("channel") not in {"backend_adapter", "external_handoff"}):
            raise ValueError("价值审查任务卡与候选或审查席位不匹配")
        card_material = dict(card)
        card_digest = card_material.pop("card_digest", None)
        if card_digest != hashlib.sha256(_canonical(card_material).encode("utf-8")).hexdigest():
            raise ValueError("价值审查任务卡摘要无效")
        source_row = conn.execute("SELECT data FROM ri_sources WHERE id=?", (assignment["source_id"],)).fetchone()
        if source_row is None:
            raise ValueError("价值审查节点已不存在")
        try:
            source = json.loads(source_row["data"])
            from xiaoba_agentos.pool_dispatch import mapped_role
            user_role = mapped_role(source.get("kind"))
            identity = independence(source)
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("价值审查节点身份无效") from exc
        if user_role not in {"review", "both"}:
            raise ValueError("价值评分只能使用 Agent 池审查或两者角色节点")
        node = source.get("node")
        if not isinstance(node, dict) or node.get("interface") == "tmux":
            raise ValueError("价值评分不能使用未绑定角色或非独立在场节点")
        node_ids = {assignment["source_id"]}
        for key in ("id", "node_id", "agent_id"):
            value = node.get(key)
            if isinstance(value, str) and value:
                node_ids.add(value)
        if node_ids & excluded_ids:
            raise ValueError("候选作者或原任务执行者不能为自己的经验打分")
        if identity in independent_sources or assignment["source_id"] in identities:
            raise ValueError("价值审查节点必须彼此独立")
        independent_sources.add(identity)
        identities.add(assignment["source_id"])

        receipt = conn.execute(
            "SELECT * FROM ri_receipts WHERE assignment_id=? ORDER BY rowid DESC LIMIT 1",
            (assignment["id"],),
        ).fetchone()
        if (receipt is None or receipt["request_id"] != request_id or receipt["status"] != "valid"
                or not isinstance(receipt["attestation"], str) or not receipt["attestation"].strip()):
            raise ValueError("两份有效且已核对来源的价值评分回执尚未齐备")
        try:
            data = json.loads(receipt["data"])
        except (TypeError, ValueError) as exc:
            raise ValueError("价值审查回执损坏") from exc
        if hashlib.sha256(_canonical(data).encode("utf-8")).hexdigest() != receipt["digest"]:
            raise ValueError("价值审查回执摘要无效")
        for key in (
            "review_request_id", "assignment_id", "reviewer_role", "plan_revision", "reviewer_id",
            "source_ref", "input_digest", "candidate_sha256", "card_digest",
        ):
            if data.get(key) != card.get(key):
                raise ValueError("价值审查回执与冻结任务卡不匹配：" + key)
        score = data.get("score")
        if (data.get("verdict") not in {'PASS', 'FAIL', 'NEEDS_INFO'}
                or data.get("review_type") != "new_experience" or isinstance(score, bool)
                or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 10
                or not isinstance(data.get("findings"), str) or not data["findings"].strip()
                or not isinstance(data.get("applicability"), str) or not data["applicability"].strip()
                or not isinstance(data.get("evidence_refs"), list) or not data["evidence_refs"]):
            raise ValueError("价值审查回执缺少有效结论、评分、适用条件或证据")
        from .output_hygiene import contains_non_answer_markers, detect_reasoning_leak
        if any(contains_non_answer_markers(text) or detect_reasoning_leak(text)
               for text in (data['findings'], data['applicability'])):
            raise ValueError("价值审查回执违反 B19")
        duplicate = data.get("duplicate_of")
        if duplicate is not None and (not isinstance(duplicate, str) or len(duplicate) > 512):
            raise ValueError("价值审查重复项标识无效")
        if not all(any(
                (isinstance(item, str) and item == ref.get("ref"))
                or (isinstance(item, dict) and item.get("ref") == ref.get("ref")
                    and item.get("sha256") == ref.get("sha256"))
                for ref in common.get("evidence_refs", []))
            for item in data["evidence_refs"]):
            raise ValueError("价值审查回执未引用冻结的候选证据")

        attestations = []
        backend_receipts = []
        started = []
        for event in conn.execute(
            "SELECT event_type,payload_json FROM events WHERE event_type IN "
            "('open_review.source_attested','open_review.backend_started','open_review.backend_receipt_ready') ORDER BY sequence"
        ):
            try:
                event_payload = json.loads(event["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(event_payload, dict) or event_payload.get("request_id") != request_id:
                continue
            if event["event_type"] == "open_review.source_attested" and event_payload.get("receipt_id") == receipt["id"]:
                attestations.append(event_payload)
            elif event["event_type"] == "open_review.backend_started" and event_payload.get("assignment_id") == assignment["id"]:
                started.append(event_payload)
            elif event["event_type"] == "open_review.backend_receipt_ready" and event_payload.get("assignment_id") == assignment["id"]:
                backend_receipts.append(event_payload)
        if card.get("channel") == "backend_adapter":
            valid_run = any(
                ready.get("receipt_id") == receipt["id"] and ready.get("receipt_digest") == receipt["digest"]
                and any(run.get("attempt") == ready.get("attempt") for run in started)
                and any(attest.get("basis") == "后台适配器实际执行；attempt=" + str(ready.get("attempt"))
                        for attest in attestations)
                for ready in backend_receipts
            )
        else:
            valid_run = bool(attestations)
        if not valid_run:
            raise ValueError("价值评分缺少与本次回执绑定的实际执行或来源核对记录")
        reviews.append({"reviewer_id": data["reviewer_id"], "score": score,
                        "duplicate_of": duplicate})

    from .value_scoring import decide
    return {
        "version": 2,
        "review_request_id": request_id,
        "candidate_sha256": candidate_sha256,
        **decide(reviews),
    }


def _discarded(conn: sqlite3.Connection, task_id: str, candidate_sha256: str) -> bool:
    row = conn.execute("SELECT result,provenance_json FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        return False
    try:
        provenance = json.loads(row["provenance_json"] or "{}")
    except (TypeError, ValueError):
        return False
    if (provenance.get("memory_disposition") != "value_discarded"
            or provenance.get("result_sha256") != hashlib.sha256((row["result"] or "").encode("utf-8")).hexdigest()):
        return False
    for _, _, event_task_id, raw in _event_rows(conn, "memory.value_assessed"):
        if event_task_id != task_id:
            continue
        try:
            payload = _versioned_payload(raw, "memory.value_assessed")
            if payload is not None:
                normalized = _assessment_payload(payload)
                if normalized["candidate_sha256"] == candidate_sha256 and normalized["decision"] == "discard":
                    return True
        except ValueError:
            continue
    return False


def validated_assessment_tx(conn: sqlite3.Connection, task_id: str, candidate_sha256: str) -> bool:
    """Whether a matching initial_review assessment is reproducible from its actual review source."""
    candidate_sha256 = _sha256(candidate_sha256)
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        return False
    from .experience_workflow import policy
    if policy(dict(task))["kind"] not in {"method", "rule"}:
        return False
    if _discarded(conn, task_id, candidate_sha256):
        return False
    for row in conn.execute(
        "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed' ORDER BY sequence",
        (task_id,),
    ):
        try:
            raw = json.loads(row["payload_json"])
            normalized = _assessment_payload(raw)
        except (TypeError, ValueError):
            continue
        if normalized["candidate_sha256"] != candidate_sha256:
            continue
        if normalized["decision"] != "initial_review":
            return False
        if normalized["version"] == 2:
            try:
                derived = assessment_from_review_request_tx(
                    conn, task_id, normalized["review_request_id"], candidate_sha256
                )
            except (ValueError, KeyError, TypeError, IndexError):
                continue
            if derived.get("decision") == "initial_review" and _canonical(derived) == _canonical(normalized):
                return True
            continue
        from .value_scoring import native_value
        reasons = []
        try:
            derived = native_value(conn, task_id, reason_out=reasons)
        except (ValueError, KeyError, TypeError, IndexError):
            continue
        derived_version = 3 if normalized["version"] == 3 else 1
        if derived is not None and derived_version == 1:
            derived = {key: value for key, value in derived.items() if key != "review_mode"}
        if (derived is not None
                and derived.get("decision") == "initial_review"
                and (derived_version == 3 or derived.get("samples") == 2)
                and _canonical({"version": derived_version, "candidate_sha256": candidate_sha256, **derived})
                == _canonical(normalized)):
            return True
    return False


def pending_items(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Rebuild pending value reviews from the event log without writing state."""
    from .store import EventStore
    chain = EventStore.verify_event_chain_tx(conn)
    if not chain["ok"]:
        raise ValueError("审计事件链损坏，待评分队列不可用")

    def blocked_item(event_type, sequence, event_id, task_id, message, candidate_sha256=None):
        safe_task_id = task_id if isinstance(task_id, str) and task_id.strip() else None
        safe_hash = candidate_sha256 if isinstance(candidate_sha256, str) and _HASH.fullmatch(candidate_sha256) else None
        return {
            "task_id": safe_task_id,
            "candidate_sha256": safe_hash,
            "staged_path": None,
            "source_task_ids": [],
            "reason": "待评分事件需要人工核对",
            "sequence": sequence,
            "event_id": event_id,
            "version": 1,
            "queue_status": "blocked_event",
            "queue_error": message,
            "source_event": {"event_type": event_type, "sequence": sequence, "event_id": event_id},
            "dispatch": {"status": "blocked", "request_id": None, "reason": message},
        }

    deferrals: Dict[tuple[str, str], Dict[str, Any]] = {}
    blocked: List[Dict[str, Any]] = []
    conflicts = set()
    for sequence, event_id, task_id, raw in _event_rows(conn, "memory.value_unscored"):
        try:
            payload = _versioned_payload(raw, "memory.value_unscored")
            if payload is None:
                continue
            normalized = _defer_payload(payload)
            task_id = _task_id(task_id)
        except (TypeError, ValueError) as exc:
            blocked.append(blocked_item("memory.value_unscored", sequence, event_id, task_id, str(exc)))
            continue
        key = (task_id, normalized["candidate_sha256"])
        item = {
            **normalized,
            "task_id": task_id,
            "sequence": sequence,
            "event_id": event_id,
            "queue_status": "awaiting_value_review",
        }
        previous = deferrals.get(key)
        if previous is not None:
            prior_payload = {name: previous[name] for name in _DEFER_FIELDS}
            current_payload = {name: item[name] for name in _DEFER_FIELDS}
            if _canonical(prior_payload) != _canonical(current_payload):
                conflicts.add(key)
                blocked.append(blocked_item(
                    "memory.value_unscored", sequence, event_id, task_id,
                    "同一任务与候选哈希存在冲突的待评分事件", normalized["candidate_sha256"],
                ))
            continue
        deferrals[key] = item

    current_candidates = {
        (task_id, evidence_sha256)
        for task_id, evidence_sha256 in conn.execute(
            "SELECT task_id,evidence_sha256 FROM memory_candidates"
        )
    }

    pending = []
    dispatches: Dict[tuple[str, str], Dict[str, Any]] = {}
    dispatch_errors = {}
    for sequence, event_id, task_id, raw in _event_rows(conn, "memory.value_review_dispatch"):
        try:
            payload = _versioned_payload(raw, "memory.value_review_dispatch")
            if payload is None:
                continue
            normalized = _dispatch_payload(payload)
            task_id = _task_id(task_id)
        except (TypeError, ValueError) as exc:
            try:
                partial = json.loads(raw)
            except (TypeError, ValueError):
                partial = None
            candidate = partial.get("candidate_sha256") if isinstance(partial, dict) else None
            if isinstance(task_id, str) and isinstance(candidate, str) and _HASH.fullmatch(candidate):
                key = (task_id, candidate)
                dispatches[key] = {
                    "status": "blocked", "request_id": None, "reason": str(exc),
                    "event_id": event_id, "sequence": sequence,
                }
                dispatch_errors[key] = True
            else:
                blocked.append(blocked_item(
                    "memory.value_review_dispatch", sequence, event_id, task_id, str(exc), candidate,
                ))
            continue
        dispatches[(task_id, normalized["candidate_sha256"])] = {
            "status": normalized["status"],
            "request_id": normalized["request_id"],
            "reason": normalized["reason"],
            "event_id": event_id,
            "sequence": sequence,
        }

    assessment_errors = {}
    for sequence, event_id, task_id, raw in _event_rows(conn, "memory.value_assessed"):
        try:
            payload = _versioned_payload(raw, "memory.value_assessed")
            if payload is None:
                continue
            _assessment_payload(payload)
        except (TypeError, ValueError) as exc:
            try:
                partial = json.loads(raw)
            except (TypeError, ValueError):
                partial = None
            candidate = partial.get("candidate_sha256") if isinstance(partial, dict) else None
            if isinstance(task_id, str) and isinstance(candidate, str) and _HASH.fullmatch(candidate):
                assessment_errors[(task_id, candidate)] = blocked_item(
                    "memory.value_assessed", sequence, event_id, task_id, str(exc), candidate,
                )
            else:
                blocked.append(blocked_item(
                    "memory.value_assessed", sequence, event_id, task_id, str(exc), candidate,
                ))
    for (task_id, candidate_sha256), item in sorted(
        deferrals.items(), key=lambda pair: pair[1]["sequence"]
    ):
        consumed = (task_id, candidate_sha256) in current_candidates
        if not consumed:
            consumed = _discarded(conn, task_id, candidate_sha256)
        if not consumed:
            key = (task_id, candidate_sha256)
            item["dispatch"] = dispatches.get(key, {
                "status": "not_started", "request_id": None, "reason": "",
            })
            if key in conflicts:
                item["queue_status"] = "blocked_event"
                item["queue_error"] = "同一任务与候选哈希存在冲突的待评分事件"
            elif key in dispatch_errors:
                item["queue_status"] = "blocked_event"
                item["queue_error"] = item["dispatch"]["reason"]
            elif key in assessment_errors:
                item["queue_status"] = "blocked_event"
                item["queue_error"] = assessment_errors[key]["queue_error"]
            pending.append(item)
    return sorted(pending + blocked, key=lambda item: item["sequence"])


def value_dispatch_request_id(candidate_id: str, candidate_sha256: str) -> str:
    """Deterministic request_id matching xiaoba_review/monitor.py value-review dispatch."""
    import uuid
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "xiaoba:value-review:" + str(candidate_id) + ":" + str(candidate_sha256),
        )
    )


def record_value_dispatch_event(
    store,
    candidate_id: str,
    candidate_sha256: str,
    request_id: str,
    status: str,
    reason: str = "",
) -> Dict[str, Any]:
    """Append a versioned memory.value_review_dispatch event through EventStore."""
    payload = _dispatch_payload({
        "version": 1,
        "candidate_sha256": candidate_sha256,
        "request_id": request_id,
        "status": status,
        "reason": str(reason or "")[:512],
    })
    with store.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event_id = store._append_event_tx(conn, candidate_id, "memory.value_review_dispatch", payload)
    return {"event_id": event_id, **payload}


def _default_value_review_runner(config, store, item: Dict[str, Any], request_id: str) -> Dict[str, Any]:
    """Invoke live xiaoba_review pool backend for a single pending value review item."""
    from xiaoba_review.agent_pool import AgentPool
    from xiaoba_review.backends import BackendRunner
    from xiaoba_review.store import ReviewStore

    candidate_id = item["task_id"]
    candidate_sha256 = item["candidate_sha256"]
    review_store = ReviewStore(config.runtime_dir / "agentos.sqlite3")
    pool = AgentPool(review_store)
    backends = BackendRunner(review_store, pool=pool)

    with store.read_only() as conn:
        existing = conn.execute("SELECT 1 FROM ri_requests WHERE id=?", (request_id,)).fetchone()
    if existing:
        request = review_store.get(request_id)
    else:
        prepared = pool.runtime.prepare(["review", "review_2"], candidate_id=candidate_id)
        if prepared.get("route") != "run":
            questions = prepared.get("questions") or []
            reason = prepared.get("reason") or (questions[0].get("question") if questions else "")
            if not reason:
                reason = "Agent 池当前不能派工，已转人工待办"
            return {"status": "blocked", "reason": str(reason)[:512]}
        pair = prepared["selections"]
        request = review_store.create(
            candidate_id,
            pair["review"],
            pair["review_2"],
            "新经验价值评分：两名独立审查节点分别给出 0–10 分、重复经验标识、判断依据，并引用候选材料；核心按 6/4 规则自动结算，不代表用户批准。",
            [{"ref": item["staged_path"], "sha256": candidate_sha256}],
            request_id=request_id,
            dsh_channel="backend_adapter",
            peer_channel="backend_adapter",
            review_type="new_experience",
            value_review=True,
        )
    receipts = request["receipts"]
    for assignment in request["assignments"]:
        if not assignment["active"]:
            continue
        latest = next((r for r in reversed(receipts) if r["assignment_id"] == assignment["id"]), None)
        if latest and latest["status"] == "valid":
            continue
        backends.execute(assignment["id"], require_test=True)
    still_pending = any(
        row["task_id"] == candidate_id and row["candidate_sha256"] == candidate_sha256
        for row in store.value_review_queue()
    )
    if still_pending:
        return {
            "status": "waiting_receipts",
            "reason": "两份有效价值评分回执尚未齐备；候选仍留在待价值评分队列。",
        }
    return {"status": "completed", "reason": ""}


def dispatch_value_review_item(
    config,
    store,
    candidate_id: str,
    candidate_sha256: Optional[str] = None,
    *,
    runner=None,
) -> Dict[str, Any]:
    """Controlled single-item value-review dispatcher (CLI & Worker shared)."""
    items = store.value_review_queue()
    item = next(
        (
            row for row in items
            if row.get("task_id") == candidate_id
            and (candidate_sha256 is None or row.get("candidate_sha256") == candidate_sha256)
            and row.get("queue_status") == "awaiting_value_review"
        ),
        None,
    )
    if item is None:
        raise ValueError("候选已变化或不在待价值评分队列")
    sha = item["candidate_sha256"]
    request_id = value_dispatch_request_id(candidate_id, sha)
    record_value_dispatch_event(store, candidate_id, sha, request_id, "started", "")
    try:
        outcome = (
            runner(item, request_id)
            if runner is not None
            else _default_value_review_runner(config, store, item, request_id)
        )
        status = str((outcome or {}).get("status") or "failed")
        reason = str((outcome or {}).get("reason") or "")[:512]
        if status == "completed":
            return {"ok": True, "task_id": candidate_id, "candidate_sha256": sha,
                    "request_id": request_id, "status": "completed", "reason": reason}
        if status not in {"blocked", "started", "failed", "waiting_receipts"}:
            status = "failed"
        record_value_dispatch_event(store, candidate_id, sha, request_id, status, reason)
        return {"ok": False, "task_id": candidate_id, "candidate_sha256": sha,
                "request_id": request_id, "status": status, "reason": reason}
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"[:512]
        try:
            record_value_dispatch_event(store, candidate_id, sha, request_id, "failed", reason)
        except Exception:
            pass
        return {"ok": False, "task_id": candidate_id, "candidate_sha256": sha,
                "request_id": request_id, "status": "failed", "reason": reason}


def auto_dispatch_pending_value_reviews(
    config,
    store,
    *,
    runner=None,
    limit: int = 1,
) -> Dict[str, Any]:
    """Switch-controlled, once-per-SHA256 value-review auto-dispatch; never fails caller."""
    worker = getattr(config, "worker", None) or {}
    if not bool(worker.get("auto_dispatch_value_review", False)):
        return {"enabled": False, "dispatched": [], "skipped": 0}
    try:
        queue = store.value_review_queue()
    except Exception as exc:
        return {"enabled": True, "dispatched": [], "skipped": 0, "error": str(exc)[:240]}
    eligible = [
        item for item in queue
        if item.get("queue_status") == "awaiting_value_review"
        and (item.get("dispatch") or {}).get("status") == "not_started"
    ]
    skipped = len(queue) - len(eligible)
    dispatched = []
    for item in eligible[: max(1, int(limit))]:
        try:
            dispatched.append(
                dispatch_value_review_item(
                    config, store, item["task_id"], item["candidate_sha256"], runner=runner,
                )
            )
        except Exception as exc:
            dispatched.append({
                "ok": False,
                "task_id": item.get("task_id"),
                "candidate_sha256": item.get("candidate_sha256"),
                "status": "failed",
                "reason": str(exc)[:240],
            })
    return {"enabled": True, "dispatched": dispatched, "skipped": skipped}
