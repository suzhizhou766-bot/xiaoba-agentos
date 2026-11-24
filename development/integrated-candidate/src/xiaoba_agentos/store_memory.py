"""W4-P3: EventStore memory candidate, value queue, review, publication, reuse, and hold methods."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional
from zoneinfo import ZoneInfo

from .output_hygiene import detect_non_answer_markers, detect_reasoning_leak
from .store_common import (
    _canonical,
    _DERIVED_SNAPSHOT_KEYS,
    _submitted_content,
    TASK_SETTLEMENT_SOURCE_STATES,
    REVIEW_STAGE_ALIASES,
    normalize_review_stage,
    REUSE_METRIC_GLOSSARY,
    _PROTECTED_PUBLIC_EVENTS,
    b19_write_boundary_reason,
    MEMORY_CANDIDATE_STATES,
    MEMORY_ACTIVE_RETRIEVAL_STATES,
    MEMORY_TERMINAL_STATES,
    terminal_reregistration_error,
    MEMORY_CANDIDATE_TRANSITIONS,
    MEMORY_PUBLICATION_READY_STATES,
    MEMORY_PUBLICATION_GATE_STATES,
    MEMORY_REVIEW_STATUS_MAP,
    LEGACY_DELIVERY_REJECTED,
    validate_memory_transition,
    CandidatePublication,
    _view_file_sha256,
)


class MemoryStoreMixin:
    def _existing_candidate_status(self, conn: sqlite3.Connection, task_id: str) -> Optional[str]:
        row = conn.execute(
            "SELECT status FROM memory_candidates WHERE task_id=?", (task_id,)
        ).fetchone()
        return None if row is None else str(row["status"])

    def _reject_terminal_memory_reregistration(
        self, conn: sqlite3.Connection, task_id: str
    ) -> None:
        """Refuse to register a candidate whose lifecycle has already ended.

        Published and retired candidates are final: their evidence hash, their
        staged path, their source tasks and their review history are frozen.
        Re-registering one is refused whether the new evidence hash is the same
        or different.  Every registration path calls this before its first
        write, so a refused call leaves no database row, no sidecar and no
        event behind.
        """
        status = self._existing_candidate_status(conn, task_id)
        if status is not None and status in MEMORY_TERMINAL_STATES:
            raise terminal_reregistration_error(task_id, status)

    def terminal_memory_candidate_status(self, task_id: str) -> Optional[str]:
        """The frozen status of this task's candidate, or None when it is not frozen.

        Read-only, so a caller that has to write a file before it can call the
        store (the constraint release stages evidence first) can refuse without
        touching anything.  It is a precondition, not a lock: the store repeats
        the check inside the transaction that writes.
        """
        candidate = self.get_memory_candidate(task_id)
        if candidate and candidate.get("status") in MEMORY_TERMINAL_STATES:
            return str(candidate["status"])
        return None

    def stage_memory_candidate(
        self,
        task_id: str,
        staged_path: str,
        evidence_sha256: str,
        source_task_ids: Optional[List[str]] = None,
    ) -> None:
        now = time.time()
        with self.connect() as conn:
            # A plain SELECT starts no transaction, so another connection could
            # publish or retire this candidate between the terminal check and the
            # upsert below. Take the write lock first: the check and the write
            # then share one boundary no other writer can slip into.
            conn.execute("BEGIN IMMEDIATE")
            self._reject_terminal_memory_reregistration(conn, task_id)
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            from .experience_workflow import policy
            from .value_queue import _discarded, validated_assessment_tx
            if _discarded(conn, task_id, evidence_sha256):
                raise ValueError("value-discarded candidate cannot enter initial review")
            if policy(dict(task))["kind"] in {"method", "rule"}:
                existing = conn.execute(
                    "SELECT staged_path,evidence_sha256 FROM memory_candidates WHERE task_id=?", (task_id,)
                ).fetchone()
                same_existing = bool(existing and existing["staged_path"] == staged_path
                                     and existing["evidence_sha256"] == evidence_sha256)
                if not same_existing and not validated_assessment_tx(conn, task_id, evidence_sha256):
                    raise ValueError("method/rule candidates require a matching independent value assessment before initial review")
            self._require_staged_evidence(staged_path, evidence_sha256)
            conn.execute(
                """INSERT INTO memory_candidates(
                       task_id,status,staged_path,evidence_sha256,source_task_ids_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(task_id) DO UPDATE SET
                       staged_path=excluded.staged_path,
                       evidence_sha256=excluded.evidence_sha256,
                       source_task_ids_json=excluded.source_task_ids_json,
                       updated_at=excluded.updated_at""",
                (
                    task_id, "pending_delivery_review", staged_path, evidence_sha256,
                    _canonical(sorted(set(source_task_ids or []))), now, now,
                ),
            )
            self._write_candidate_status_sidecar_values(
                task_id, staged_path, "pending_delivery_review", evidence_sha256, now
            )
            self._append_event_tx(conn, task_id, "memory.candidate_staged", {"evidence_sha256": evidence_sha256})

    def value_review_queue(self) -> List[Dict[str, Any]]:
        from .value_queue import pending_items
        with self.read_only() as conn:
            if not self.verify_event_chain_tx(conn)["ok"]:
                raise ValueError("审计事件链损坏，待评分队列不可用")
            return pending_items(conn)

    def record_value_review_dispatch(
        self,
        task_id: str,
        candidate_sha256: str,
        request_id: Optional[str],
        status: str,
        reason: str = "",
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> str:
        from .value_queue import dispatch_status_tx
        payload = {
            "version": 1,
            "candidate_sha256": candidate_sha256,
            "request_id": request_id,
            "status": status,
            "reason": reason,
        }
        if conn is not None:
            if not conn.in_transaction:
                raise ValueError("外部派工状态连接必须持有事务")
            if not self.verify_event_chain_tx(conn)["ok"]:
                raise ValueError("审计事件链损坏，拒绝更新价值评分派工状态")
            return dispatch_status_tx(conn, task_id, payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.verify_event_chain_tx(db)["ok"]:
                raise ValueError("审计事件链损坏，拒绝更新价值评分派工状态")
            return dispatch_status_tx(db, task_id, payload)

    def value_candidate_status(self, task_id: str, candidate_sha256: str) -> Optional[str]:
        from .value_queue import _discarded, pending_items
        with self.read_only() as conn:
            if not self.verify_event_chain_tx(conn)["ok"]:
                return None
            candidate = conn.execute(
                "SELECT status,evidence_sha256 FROM memory_candidates WHERE task_id=?", (task_id,)
            ).fetchone()
            if candidate and candidate["evidence_sha256"] == candidate_sha256:
                return str(candidate["status"])
            if any(item["task_id"] == task_id and item["candidate_sha256"] == candidate_sha256
                   and item.get("queue_status") == "awaiting_value_review"
                   for item in pending_items(conn)):
                return "awaiting_value_review"
            if _discarded(conn, task_id, candidate_sha256):
                return "value_discarded"
        return None

    def cleanup_discarded_candidate(self, task_id: str, staged_path: Optional[str], candidate_sha256: Optional[str]) -> bool:
        """Remove only the exact, hash-bound body of a committed discarded candidate."""
        if not isinstance(staged_path, str) or not isinstance(candidate_sha256, str):
            return False
        path = Path(staged_path)
        resolved_target = str(path.resolve(strict=False))
        with self.read_only() as conn:
            from .value_queue import _discarded
            if not self.verify_event_chain_tx(conn)["ok"] or not _discarded(conn, task_id, candidate_sha256):
                return False
            referenced = conn.execute(
                "SELECT 1 FROM memory_candidates WHERE "
                "(task_id=? AND status!='retired') OR staged_path IN (?,?) OR evidence_sha256=? LIMIT 1",
                (task_id, staged_path, resolved_target, candidate_sha256),
            ).fetchone()
            if referenced is not None:
                return False
        outbox = Path(self.path).parent / "memory-outbox"
        try:
            if (outbox.is_symlink() or path.is_symlink() or path.name != f"{task_id}.md"
                    or path.resolve(strict=False).parent != outbox.resolve(strict=True)):
                return False
        except OSError:
            return False
        sidecar = path.with_suffix(path.suffix + ".status.json")
        if path.exists():
            try:
                if hashlib.sha256(path.read_bytes()).hexdigest() != candidate_sha256:
                    return False
            except OSError:
                return False
        if sidecar.exists():
            try:
                metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return False
            if metadata.get("task_id") != task_id or metadata.get("evidence_sha256") != candidate_sha256:
                return False
        try:
            path.unlink(missing_ok=True)
            sidecar.unlink(missing_ok=True)
            return True
        except OSError:
            return False

    def recover_discarded_candidate_cleanup(self) -> Dict[str, Any]:
        """Retry cleanup from durable discard assessments after a process restart."""
        outbox = Path(self.path).parent / "memory-outbox"
        if outbox.is_symlink() or not outbox.is_dir():
            return {"ok": True, "checked": 0, "cleaned": 0, "retained": 0}
        with self.read_only() as conn:
            if not self.verify_event_chain_tx(conn)["ok"]:
                return {"ok": False, "reason": "event_chain_invalid", "checked": 0,
                        "cleaned": 0, "retained": 0}
            rows = conn.execute(
                "SELECT task_id,payload_json FROM events "
                "WHERE event_type='memory.value_assessed' AND task_id IS NOT NULL ORDER BY sequence"
            ).fetchall()

        from .value_queue import _assessment_payload

        discarded = {}
        for row in rows:
            try:
                assessment = _assessment_payload(json.loads(row["payload_json"]))
            except (TypeError, ValueError):
                continue
            if assessment["decision"] == "discard":
                discarded[(str(row["task_id"]), assessment["candidate_sha256"])] = None

        cleaned = retained = 0
        for task_id, candidate_sha256 in discarded:
            staged_path = outbox / f"{task_id}.md"
            if self.cleanup_discarded_candidate(task_id, str(staged_path), candidate_sha256):
                cleaned += 1
            else:
                retained += 1
        return {"ok": True, "checked": len(discarded), "cleaned": cleaned, "retained": retained}

    def assess_value_candidate(
        self, task_id: str, review_request_id: str, *, conn: Optional[sqlite3.Connection] = None
    ) -> Dict[str, Any]:
        """Derive a value decision from a bound Agent Pool request and settle it atomically."""
        if conn is not None:
            if not conn.in_transaction:
                raise ValueError("external value assessment connection must hold a transaction")
            return self._assess_value_candidate_tx(conn, task_id, review_request_id)
        cleanup = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            result = self._assess_value_candidate_tx(db, task_id, review_request_id)
            if result.get("decision") == "discard":
                cleanup = (result.get("staged_path"), result.get("candidate_sha256"))
        if cleanup:
            self.cleanup_discarded_candidate(task_id, *cleanup)
        return result

    def _assess_value_candidate_tx(
        self, conn: sqlite3.Connection, task_id: str, review_request_id: str
    ) -> Dict[str, Any]:
        from .value_queue import (
            _assessment_payload, assessment_from_review_request_tx, assessment_tx, pending_items,
        )
        if not self.verify_event_chain_tx(conn)["ok"]:
            raise ValueError("审计事件链损坏，拒绝价值评分结算")
        row = conn.execute("SELECT status,provenance_json FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["status"] != "completed":
            raise ValueError("待评分候选的来源任务尚未完成")

        existing = conn.execute(
            "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed' ORDER BY sequence DESC",
            (task_id,),
        ).fetchall()
        for event in existing:
            try:
                prior = json.loads(event["payload_json"])
            except (TypeError, ValueError):
                continue
            if prior.get("version") != 2 or prior.get("candidate_sha256") is None:
                continue
            normalized = _assessment_payload(prior)
            if normalized["candidate_sha256"] != prior["candidate_sha256"]:
                continue
            if normalized["review_request_id"] != review_request_id:
                raise ValueError("同一候选哈希不能由不同价值审查请求重复结算")
            derived = assessment_from_review_request_tx(
                conn, task_id, review_request_id, normalized["candidate_sha256"]
            )
            if _canonical(derived) != _canonical(normalized):
                raise ValueError("已记录价值评分与当前双审回执不一致")
            candidate = conn.execute(
                "SELECT status FROM memory_candidates WHERE task_id=?", (task_id,)
            ).fetchone()
            return {"status": "already_assessed", "decision": prior["decision"],
                    "candidate_status": candidate["status"] if candidate else "value_discarded",
                    "candidate_sha256": prior["candidate_sha256"], "assessment": normalized}

        pending = [item for item in pending_items(conn)
                   if item["task_id"] == task_id and item.get("queue_status") == "awaiting_value_review"]
        if len(pending) != 1:
            raise ValueError("候选没有唯一的待价值评分材料")
        item = pending[0]
        payload = assessment_from_review_request_tx(conn, task_id, review_request_id, item["candidate_sha256"])

        path = Path(item["staged_path"])
        outbox = Path(self.path).parent / "memory-outbox"
        if (outbox.is_symlink() or path.is_symlink() or path.name != f"{task_id}.md"
                or not outbox.is_dir() or path.resolve(strict=True).parent != outbox.resolve(strict=True)):
            raise ValueError("待评分候选路径不在受保护的 memory-outbox 中")
        self._require_staged_evidence(str(path), item["candidate_sha256"])
        if conn.execute("SELECT 1 FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone():
            raise ValueError("待评分候选已经进入审核队列")

        assessment_tx(conn, task_id, payload)
        if payload["decision"] == "discard":
            provenance = json.loads(row["provenance_json"] or "{}")
            provenance.update(memory_disposition="value_discarded",
                              result_sha256=hashlib.sha256(
                                  (conn.execute("SELECT result FROM tasks WHERE id=?", (task_id,)).fetchone()[0] or "").encode("utf-8")
                              ).hexdigest())
            conn.execute("UPDATE tasks SET provenance_json=?,updated_at=? WHERE id=?",
                         (_canonical(provenance), time.time(), task_id))
            return {"status": "value_discarded", "decision": "discard",
                    "candidate_status": "value_discarded", "candidate_sha256": item["candidate_sha256"],
                    "staged_path": item["staged_path"], "assessment": payload}

        self._reject_terminal_memory_reregistration(conn, task_id)
        now = time.time()
        conn.execute(
            "INSERT INTO memory_candidates(task_id,status,staged_path,evidence_sha256,source_task_ids_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, "pending_delivery_review", item["staged_path"], item["candidate_sha256"],
             _canonical(item["source_task_ids"]), now, now),
        )
        self._write_candidate_status_sidecar_values(
            task_id, item["staged_path"], "pending_delivery_review", item["candidate_sha256"], now
        )
        self._append_event_tx(conn, task_id, "memory.candidate_staged", {
            "evidence_sha256": item["candidate_sha256"], "value_assessment": True,
            "review_request_id": review_request_id,
        })
        return {"status": "pending_delivery_review", "decision": "initial_review",
                "candidate_status": "pending_delivery_review", "candidate_sha256": item["candidate_sha256"],
                "assessment": payload}

    def create_authored_candidate(
        self,
        title: str,
        body: str,
        outbox: Path,
        *,
        author: str,
        source_task_ids: Optional[List[str]] = None,
        idempotency_key: Optional[str] = None,
        session_id: Optional[str] = None,
        experience_kind: str = "method",
        problem_key: str = "",
        enforce_value_queue: bool = True,
    ) -> Dict[str, Any]:
        """Register a candidate written by the host, not produced by a task run.

        One transaction creates the completed host_authored task, its staged
        staged evidence and its audit events, so the candidate cannot exist
        without its completed task. New method/rule drafts wait for independent
        value assessment before a user review row is created.
        """
        title, body, author = title.strip(), body.strip(), author.strip()
        if not title or not body or not author:
            raise ValueError("title, body and author are required")
        # B19 (write boundary): neither the title nor the body may carry leaked
        # reasoning or unexecuted tool markup.  The refusal happens before the
        # transaction opens, so it leaves no task, no staged file, no sidecar
        # and no event behind.
        for field, value in (("title", title), ("body", body)):
            reason = b19_write_boundary_reason(value)
            if reason:
                raise ValueError(f"candidate {field} refused: {reason}")
        sources = sorted(set(source_task_ids or []))
        # The snapshot is built up front so the idempotency replay below can
        # compare exactly what a first submission would have stored.
        snapshot = {
            "kind": "host_authored_candidate", "author": author,
            "source_task_ids": sources, "external_agents_called": False,
        }
        if experience_kind not in {"method", "rule"}:
            raise ValueError("authored candidate kind must be method or rule")
        if enforce_value_queue is not True:
            raise ValueError("new authored candidates must pass independent value review")
        snapshot["experience_policy"] = {"version": 1, "kind": experience_kind, "problem_key": problem_key}
        snapshot["value_queue"] = {"version": 1, "required": True}
        snapshot_json = _canonical(snapshot)
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id,prompt,result,execution_snapshot_json FROM tasks WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    # A retry after a lost response gets the same record back,
                    # not an error it cannot tell apart from bad input.  The
                    # same key with different content (MD2-09) is never
                    # silently answered with the first candidate.
                    candidate = conn.execute(
                        "SELECT status,staged_path,evidence_sha256 FROM memory_candidates WHERE task_id=?",
                        (existing["id"],),
                    ).fetchone()
                    task = conn.execute(
                        "SELECT execution_mode FROM tasks WHERE id=?", (existing["id"],)
                    ).fetchone()
                    if task["execution_mode"] != "host_authored":
                        raise ValueError("idempotency key belongs to a different kind of task")
                    changed = []
                    if existing["prompt"] != title:
                        changed.append("title")
                    authored = conn.execute(
                        "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.candidate_authored' ORDER BY sequence LIMIT 1",
                        (existing["id"],),
                    ).fetchone()
                    authored_payload = json.loads(authored["payload_json"]) if authored else {}
                    stored_body_sha = authored_payload.get("body_sha256")
                    if (stored_body_sha and stored_body_sha != hashlib.sha256(body.encode("utf-8")).hexdigest()
                            or not stored_body_sha and existing["result"] != body):
                        changed.append("body")
                    if existing["execution_snapshot_json"] != snapshot_json:
                        changed.append("metadata")
                    if changed:
                        raise ValueError(
                            f"idempotency key {idempotency_key!r} is already used by a candidate "
                            "with different content (" + ", ".join(changed) + "); use a new key"
                        )
                    if candidate:
                        return {
                            "task_id": str(existing["id"]), "status": candidate["status"],
                            "staged_path": candidate["staged_path"],
                            "evidence_sha256": candidate["evidence_sha256"], "idempotent": True,
                        }
                    from .value_queue import pending_items
                    prepared = [item for item in pending_items(conn)
                                if item["task_id"] == str(existing["id"])
                                and item.get("queue_status") == "awaiting_value_review"]
                    if len(prepared) == 1:
                        return {
                            "task_id": str(existing["id"]), "status": "awaiting_value_review",
                            "staged_path": prepared[0]["staged_path"],
                            "evidence_sha256": prepared[0]["candidate_sha256"], "idempotent": True,
                        }
                    discarded = conn.execute(
                        "SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed' ORDER BY sequence DESC LIMIT 1",
                        (existing["id"],),
                    ).fetchone()
                    if discarded and json.loads(discarded["payload_json"]).get("decision") == "discard":
                        return {"task_id": str(existing["id"]), "status": "value_discarded", "idempotent": True}
                    raise ValueError("idempotency key candidate no longer has a matching value-review state")
            for source in sources:
                if not conn.execute("SELECT 1 FROM tasks WHERE id=?", (source,)).fetchone():
                    raise KeyError(source)
            task_id = str(uuid.uuid4())
            staged = Path(outbox) / f"{task_id}.md"
            staged.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
              staged.write_text(
                "---\n"
                f"task_id: {task_id}\n"
                f"created_at: {time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now))}\n"
                "status: pending_review\n"
                'type: "experience-candidate"\n'
                "authority: false\n"
                f"author: {json.dumps(author, ensure_ascii=False)}\n"
                f"title: {json.dumps(title, ensure_ascii=False)}\n"
                "---\n\n" + body + "\n",
                encoding="utf-8",
              )
              os.chmod(staged, 0o600)
            except Exception:
                try:
                    staged.unlink()
                except OSError:
                    pass
                raise
            try:
                evidence_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
                result_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
                conn.execute(
                    "INSERT INTO tasks(id,prompt,status,priority,max_attempts,run_after,strategy_version,"
                    "execution_snapshot_json,execution_snapshot_hash,execution_mode,idempotency_key,session_id,"
                    "route_decision_id,provenance_json,result,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        task_id, title, "completed", 0, 1, now, 1, snapshot_json,
                        hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(), "host_authored",
                        idempotency_key, session_id, None,
                        _canonical({"kind": "session_model_direct", "status": "completed",
                                    "result_sha256": result_sha256}),
                        body, now, now,
                    ),
                )
                self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": result_sha256})
                from .value_queue import defer_tx
                defer_tx(conn, task_id, {
                    "version": 1,
                    "staged_path": str(staged),
                    "candidate_sha256": evidence_sha256,
                    "source_task_ids": sources,
                    "reason": "host_draft",
                })
                self._append_event_tx(conn, task_id, "memory.candidate_authored", {
                    "author": author, "source_task_ids": sources,
                    "external_agents_called": False, "scope": "candidate_only",
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                })
            except Exception:
                # The transaction rolls back; do not leave an orphan file behind.
                for leftover in (staged, staged.with_suffix(staged.suffix + ".status.json")):
                    try:
                        leftover.unlink()
                    except OSError:
                        pass
                raise
            return {
                "task_id": task_id, "status": "awaiting_value_review",
                "staged_path": str(staged), "evidence_sha256": evidence_sha256,
            }

    def get_memory_candidate(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.read_only() as conn:
            row = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def get_validation_lineage(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        with self.read_only() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "experience_validation_lineages" not in tables:
                return None
            row = conn.execute("SELECT * FROM experience_validation_lineages WHERE candidate_id=?", (candidate_id,)).fetchone()
            return dict(row) if row else None

    def get_validation_records(self, candidate_id: str) -> list[Dict[str, Any]]:
        with self.read_only() as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "experience_validation_records" not in tables:
                return []
            rows = conn.execute("SELECT * FROM experience_validation_records WHERE candidate_id=? ORDER BY round ASC", (candidate_id,)).fetchall()
            return [dict(r) for r in rows]

    def apply_validation_extension(self, dry_run: bool = False) -> Dict[str, Any]:
        from .validation_migration import apply_validation_schema_migration
        return apply_validation_schema_migration(self.path, dry_run=dry_run)

    def rollback_validation_extension(self) -> Dict[str, Any]:
        from .validation_migration import rollback_validation_schema_migration
        return rollback_validation_schema_migration(self.path)

    def is_validation_extension_installed(self) -> bool:
        from .validation_migration import is_validation_extension_installed
        with self.read_only() as conn:
            return is_validation_extension_installed(conn)

    def submit_validation_diff_materials(self, candidate_id: str, diff_materials: Any) -> Dict[str, Any]:
        from .experience_workflow import submit_diff_materials
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            res = submit_diff_materials(self, conn, candidate_id, diff_materials)
            conn.commit()
            return res

    def review_memory(
        self,
        task_id: str,
        stage: str,
        decision: str,
        note: str,
        candidate_sha256: str,
        validation_basis_confirmation: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        stage = REVIEW_STAGE_ALIASES.get(stage, stage)
        allowed = {
            "delivery": {"approve", "reject", "revise"},
            "final": {"approve", "reject", "revise", "retire"},
        }
        if stage not in allowed or decision not in allowed[stage]:
            raise ValueError("invalid review stage or decision")
        if validation_basis_confirmation is not None and (stage != "final" or decision != "approve"):
            raise ValueError("validation-basis confirmation is only accepted with final approval")
        with self.connect() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            # Timestamp the decision after acquiring the write lock so review
            # order follows committed events even when an earlier caller waits.
            now = time.time()
            candidate = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone()
            if not candidate:
                raise KeyError(task_id)
            if not candidate_sha256 or candidate_sha256 != candidate["evidence_sha256"]:
                raise ValueError("review candidate hash does not match the staged evidence")
            current = str(candidate["status"])
            # Explicit state-machine gate: a retired memory can never be
            # reviewed back to life, and final review needs prior delivery
            # approval (hash binding is checked separately below).
            try:
                validate_memory_transition(f"{stage}_review", current)
            except ValueError:
                if stage == "final":
                    raise ValueError("final review requires an approved delivery review") from None
                raise ValueError(f"delivery review is not allowed from status {current!r}") from None
            if stage == "final":
                delivery = conn.execute(
                    """SELECT candidate_sha256 FROM memory_reviews
                       WHERE task_id=? AND stage='delivery' AND decision='approve'
                       ORDER BY created_at DESC LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if not delivery or delivery["candidate_sha256"] != candidate_sha256:
                    raise ValueError("final review is not bound to the approved delivery candidate")
            validation_basis_binding = None
            if stage == "final" and decision == "approve":
                from .validation_basis import confirm_basis_tx
                validation_basis_binding = confirm_basis_tx(self, conn, dict(candidate), validation_basis_confirmation)
            from .experience_workflow import require_final_trial
            trial_task_id = require_final_trial(conn, candidate) if stage == "final" and decision == "approve" else None
            from .experience_loop import final_binding
            loop_sha256 = final_binding(conn, candidate) if stage == "final" and decision == "approve" else None
            # 已经退回的条目，作者还没改正文时再点「需要修改」：不新增裁决、
            # 也不新增审计记录，只回报上次退回的时间和意见。哈希绑定、
            # 状态机和终审绑定校验都在上面完成；批准、驳回不受影响。
            if decision == "revise" and current == "revision_requested":
                previous_revision = conn.execute(
                    """SELECT id,note,candidate_sha256,created_at FROM memory_reviews
                       WHERE task_id=? AND decision='revise'
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (task_id,),
                ).fetchone()
                if previous_revision is not None and previous_revision["candidate_sha256"] == candidate_sha256:
                    return {
                        "task_id": task_id,
                        "status": "already_revision_requested",
                        "stage": stage,
                        "decision": decision,
                        "candidate_sha256": candidate_sha256,
                        "previous_status": current,
                        "reviewed_status": current,
                        "review_id": None,
                        "revision_requested_review_id": previous_revision["id"],
                        "revision_requested_at": previous_revision["created_at"],
                        "revision_note": previous_revision["note"],
                    }
            status = MEMORY_REVIEW_STATUS_MAP[(stage, decision)]
            review_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO memory_reviews(id,task_id,stage,decision,note,candidate_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                (review_id, task_id, stage, decision, note[:4000], candidate_sha256, now),
            )
            conn.execute(
                "UPDATE memory_candidates SET status=?,updated_at=? WHERE task_id=?",
                (status, now, task_id),
            )
            self._write_candidate_status_sidecar(candidate, status, candidate_sha256, now)
            self._append_event_tx(
                conn, task_id, "memory.reviewed",
                {
                    "review_id": review_id,
                    "stage": stage,
                    "decision": decision,
                    "previous_status": current,
                    "trial_task_id": trial_task_id,
                    "experience_loop_sha256": loop_sha256,
                    "validation_basis": validation_basis_binding,
                    **({"method_classification_confirmed": True} if validation_basis_binding is not None else {}),
                    "status": status,
                    "candidate_sha256": candidate_sha256,
                },
            )
            # Any decision can be the one that resolves a closing session
            # (a delivery rejection ends the candidate's review just as a
            # final one does). The check is a no-op while anything is pending.
            self._close_session_if_resolved_tx(conn, task_id)
            receipt = dict(
                conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone()
            )
            # Review receipt: every decision is auditable from the returned
            # payload alone, without querying the event log.
            receipt.update({
                "review_id": review_id,
                "previous_status": current,
                "reviewed_status": status,
            })
            return receipt

    @staticmethod
    def _write_candidate_status_sidecar(
        candidate: sqlite3.Row, status: str, evidence_sha256: str, updated_at: float
    ) -> None:
        """Publish mutable review status without mutating immutable evidence bytes."""
        MemoryStoreMixin._write_candidate_status_sidecar_values(
            str(candidate["task_id"]), str(candidate["staged_path"]),
            status, evidence_sha256, updated_at,
        )

    @staticmethod
    def _sidecar_path(staged_path: str) -> Path:
        path = Path(staged_path)
        return path.with_suffix(path.suffix + ".status.json")

    def _sidecar_snapshot(self, staged_path: str) -> Optional[bytes]:
        try:
            return self._sidecar_path(staged_path).read_bytes()
        except OSError:
            return None

    def _restore_sidecar_unless_committed(
        self, task_id: str, staged_path: str, evidence_sha256: str, previous: Optional[bytes]
    ) -> None:
        """Undo the status sidecar written inside a transaction that did not commit.

        The sidecar is a file, so SQLite cannot roll it back. An exception does
        not prove a rollback either (connect() does maintenance after commit),
        so the database decides: if the candidate row carries this evidence
        hash the write stands; otherwise the previous sidecar is put back. If
        the database cannot be read the sidecar is left alone.
        """
        try:
            with self.read_only() as conn:
                row = conn.execute(
                    "SELECT evidence_sha256 FROM memory_candidates WHERE task_id=?", (task_id,)
                ).fetchone()
        except (OSError, sqlite3.Error):
            return
        if row is not None and row["evidence_sha256"] == evidence_sha256:
            return
        sidecar = self._sidecar_path(staged_path)
        try:
            if previous is None:
                sidecar.unlink()
            else:
                sidecar.write_bytes(previous)
                os.chmod(sidecar, 0o600)
        except OSError:
            pass

    @staticmethod
    def _require_staged_evidence(staged_path: str, evidence_sha256: str) -> None:
        """Last check before a candidate is queued: the file is there and is the reviewed content."""
        path = Path(staged_path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("staged candidate file is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != evidence_sha256:
            raise ValueError("staged candidate file does not match its evidence hash")

    @staticmethod
    def _publication_evidence_bytes(staged_path: str, evidence_sha256: str) -> bytes:
        """The real bytes a publication would land, or a refusal.

        Reads the file the bridge publishes: the staged candidate, or the
        `.published` marker a previous publication left in its place.  The bytes
        must hash to the evidence the final review approved, so the content that
        lands is the content that was approved, not merely the content the
        database currently points at.
        """
        path = Path(staged_path)
        if not path.is_file():
            marker = path.with_suffix(".published")
            if marker.is_file():
                path = marker
        if path.is_symlink() or not path.is_file():
            raise ValueError("staged candidate file is missing")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != evidence_sha256:
            raise ValueError("staged candidate file does not match its evidence hash")
        return data

    @staticmethod
    def _write_candidate_status_sidecar_values(
        task_id: str, staged_path: str, status: str, evidence_sha256: str, updated_at: float
    ) -> None:
        path = Path(staged_path)
        if not path.is_file() or path.is_symlink():
            return
        sidecar = path.with_suffix(path.suffix + ".status.json")
        payload = _canonical({
            "task_id": task_id,
            "status": status,
            "evidence_sha256": evidence_sha256,
            "updated_at": updated_at,
            "source": "agentos.memory_review",
        }) + "\n"
        temporary = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, sidecar)

    def _close_session_if_resolved_tx(self, conn: sqlite3.Connection, task_id: str) -> None:
        task = conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        session_id = task["session_id"] if task else None
        if not session_id:
            return
        # Same definition of "unresolved" as session rollover and the sweep.
        pending = self._unresolved_session_count_tx(conn, session_id)
        if pending == 0:
            updated = conn.execute(
                "UPDATE work_sessions SET status='closed',closed_at=? WHERE id=? AND status='closing'",
                (time.time(), session_id),
            ).rowcount
            if updated:
                self._append_event_tx(conn, None, "session.closed", {"session_id": session_id})

    def record_memory_note_renamed(self, task_id: str, old_path: str, new_path: str) -> Dict[str, Any]:
        """经验笔记在 Vault 里改了文件名：只更新记录的路径并留一条事件，状态不变。"""
        with self.connect() as conn:
            candidate = conn.execute(
                "SELECT task_id,status,published_path FROM memory_candidates WHERE task_id=?", (task_id,)
            ).fetchone()
            if not candidate or candidate["status"] not in {"published", "revalidation_required"}:
                raise ValueError("only a currently published memory note can be renamed")
            if str(candidate["published_path"] or "") != old_path:
                raise ValueError("recorded path does not match the note being renamed")
            conn.execute(
                "UPDATE memory_candidates SET published_path=?,updated_at=? WHERE task_id=?",
                (new_path, time.time(), task_id),
            )
            self._append_event_tx(conn, task_id, "memory.note_renamed", {"from": old_path, "to": new_path})
            return dict(conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone())

    @staticmethod
    def _final_approval_binds_evidence(conn: sqlite3.Connection, candidate: sqlite3.Row) -> None:
        """The one definition of "this evidence was finally approved".

        Both the pre-publication gate and the settlement call it, so the two
        entry points cannot drift into different binding checks.
        """
        approval = conn.execute(
            """SELECT id,candidate_sha256 FROM memory_reviews
               WHERE task_id=? AND stage='final' AND decision='approve'
               ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (str(candidate["task_id"]),),
        ).fetchone()
        if not approval or approval["candidate_sha256"] != candidate["evidence_sha256"]:
            raise ValueError("memory publication is not bound to the final approved candidate")
        event = conn.execute("SELECT event_id,payload_json FROM events WHERE task_id=? AND event_type='memory.reviewed' ORDER BY sequence DESC LIMIT 1",
                             (candidate["task_id"],)).fetchone()
        payload = json.loads(event["payload_json"]) if event else {}
        if (payload.get("review_id") != approval["id"] or payload.get("stage") != "final"
                or payload.get("decision") != "approve" or payload.get("candidate_sha256") != candidate["evidence_sha256"]):
            raise ValueError("final approval event is missing or no longer matches the review row")
        from .validation_basis import validate_basis, basis_binding
        basis = validate_basis(conn, dict(candidate))
        if basis.get("status") == "revoked":
            raise ValueError("memory publication is blocked because the validation basis was revoked")
        if basis.get("status") != "missing" and "real_task_acceptance" in basis.get("required_checks", []):
            basis = validate_basis(conn, dict(candidate), require_confirmed=True)
        current_basis_binding = basis_binding(basis) if basis.get("status") != "missing" else None
        if payload.get("validation_basis") != current_basis_binding:
            raise ValueError("final approval is not bound to the current validation basis")
        if "method_classification_confirmed" in payload and (
                payload["method_classification_confirmed"] is not True or current_basis_binding is None):
            raise ValueError("final approval has an invalid method classification confirmation")
        from .experience_workflow import require_final_trial
        trial_task_id = require_final_trial(conn, candidate)
        if trial_task_id:
            if payload.get("trial_task_id") != trial_task_id or payload.get("stage") != "final" or payload.get("decision") != "approve":
                raise ValueError("final approval is not bound to the latest trial evidence")
        from .experience_loop import final_binding
        loop_sha256 = final_binding(conn, candidate)
        if loop_sha256 is not None and payload.get("experience_loop_sha256") != loop_sha256:
            raise ValueError("final approval is not bound to the current application and delivery acceptance")

    def _publication_binding_candidate(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        """The candidate a settlement may publish, or a refusal.

        Only a publish-ready status passes and its latest final approval must be
        bound to its current evidence hash.  This is the same check the gate
        runs, restated at settlement so the two share one definition.
        """
        candidate = conn.execute(
            "SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)
        ).fetchone()
        if not candidate or str(candidate["status"]) not in MEMORY_PUBLICATION_READY_STATES:
            raise ValueError("memory publication requires final approval")
        validate_memory_transition("publish", str(candidate["status"]))
        self._final_approval_binds_evidence(conn, candidate)
        return candidate

    def _publication_gate_candidate(
        self, conn: sqlite3.Connection, task_id: str, expected_sha256: Optional[str]
    ) -> sqlite3.Row:
        """Verify, before any Vault write, that this evidence may be published.

        Checks the candidate status, the final approval binding, the hash the
        caller reviewed and the real staged bytes, all under the caller's write
        lock so none of them can change between this check and the settlement.
        """
        candidate = conn.execute(
            "SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)
        ).fetchone()
        if not candidate or str(candidate["status"]) not in MEMORY_PUBLICATION_GATE_STATES:
            raise ValueError("memory publication requires final approval")
        self._final_approval_binds_evidence(conn, candidate)
        if expected_sha256 is not None and expected_sha256 != candidate["evidence_sha256"]:
            raise ValueError("memory candidate changed after final review")
        self._publication_evidence_bytes(
            str(candidate["staged_path"]), str(candidate["evidence_sha256"])
        )
        return candidate

    @contextmanager
    def publication_transaction(
        self, task_id: str, expected_sha256: Optional[str] = None
    ) -> Iterator[CandidatePublication]:
        """Publish inside one lock: check the binding, then land, then settle.

        Takes the write lock (BEGIN IMMEDIATE) and, under it, verifies *before*
        the caller writes anything that the candidate is publishable, that its
        latest final approval is bound to its current evidence, that the hash
        the caller reviewed matches, and that the real staged bytes hash to that
        same evidence.

        The lock is held across the caller's Vault write and index rebuild, and
        the settlement runs on this same connection, so a concurrent
        re-registration or review on another connection cannot slip between the
        check and the settlement.  Nothing commits until the block exits
        cleanly, so a failed Vault write or settlement leaves the database
        untouched, and a refusal leaves the Vault and index byte-identical.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidate = self._publication_gate_candidate(conn, task_id, expected_sha256)
            yield CandidatePublication(self, conn, candidate)

    def _mark_memory_published_tx(
        self, conn: sqlite3.Connection, task_id: str, published_path: str
    ) -> Dict[str, Any]:
        now = time.time()
        candidate = self._publication_binding_candidate(conn, task_id)
        conn.execute(
            "UPDATE memory_candidates SET status='published',published_path=?,published_at=?,last_validated_at=?,updated_at=? WHERE task_id=?",
            (published_path, now, now, now, task_id),
        )
        self._write_candidate_status_sidecar(candidate, "published", candidate["evidence_sha256"], now)
        self._append_event_tx(
            conn,
            task_id,
            "memory.published",
            {"path": published_path, "candidate_sha256": candidate["evidence_sha256"]},
        )
        self._close_session_if_resolved_tx(conn, task_id)
        return dict(conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone())

    def mark_memory_published(self, task_id: str, published_path: str) -> Dict[str, Any]:
        with self.connect() as conn:
            # Same boundary as the publication gate: lock first so a concurrent
            # re-registration cannot slip between the binding check and the
            # settlement and leave published evidence carrying an unapproved hash.
            conn.execute("BEGIN IMMEDIATE")
            return self._mark_memory_published_tx(conn, task_id, published_path)

    def mark_memory_publication_failed(self, task_id: str, error: str) -> None:
        with self.connect() as conn:
            candidate = conn.execute(
                "SELECT task_id,status,staged_path,evidence_sha256 FROM memory_candidates WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if candidate:
                validate_memory_transition("publication_failed", str(candidate["status"]))
            conn.execute(
                "UPDATE memory_candidates SET status='publication_failed',updated_at=? WHERE task_id=?",
                (time.time(), task_id),
            )
            if candidate:
                self._write_candidate_status_sidecar(candidate, "publication_failed", candidate["evidence_sha256"], time.time())
            self._append_event_tx(conn, task_id, "memory.publication_failed", {"error": error[:500]})

    def record_memory_reuse(self, task_id: str, source_task_id: str, source_path: str) -> None:
        with self.connect() as conn:
            created = conn.execute(
                "INSERT OR IGNORE INTO memory_reuse(id,task_id,source_task_id,source_path,outcome,created_at) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), task_id, source_task_id, source_path, "pending", time.time()),
            ).rowcount
            if created:
                from .reuse_scores import is_formal
                source = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (source_task_id,)).fetchone()
                if not is_formal(dict(source) if source else None):
                    conn.execute(
                        "UPDATE memory_candidates SET reuse_count=reuse_count+1,updated_at=? WHERE task_id=?",
                        (time.time(), source_task_id),
                    )
                source_sha256=_view_file_sha256(Path(source_path))
                self._append_event_tx(
                    conn, task_id, "memory.reused",
                    {"source_task_id": source_task_id, "source_path": source_path, "source_sha256":source_sha256},
                )

    def memory_reuses(self, task_id: str, outcome: Optional[str] = None) -> List[Dict[str, Any]]:
        if outcome not in {None, "pending", "validated", "failed"}:
            raise ValueError("invalid memory reuse outcome")
        query = """SELECT mr.*,mc.status AS source_status,mc.published_path,
                          mc.failed_reuse_count,mc.successful_reuse_count
                   FROM memory_reuse mr
                   LEFT JOIN memory_candidates mc ON mc.task_id=mr.source_task_id
                   WHERE mr.task_id=?"""
        parameters: List[Any] = [task_id]
        if outcome is not None:
            query += " AND mr.outcome=?"
            parameters.append(outcome)
        query += " ORDER BY mr.created_at,mr.id"
        with self.read_only() as conn:
            return [dict(row) for row in conn.execute(query, parameters)]

    def record_uncited_reuses(self, task_id: str, source_task_ids: List[str]) -> None:
        """只是检索命中、没被这次任务结论引用的经验：留痕，但不当作一次使用。

        不建新的 outcome 值（表上有 CHECK 约束），只记一条事件：
        命中本身不是使用，所以不改来源记忆的状态，也不累加失败次数。
        """
        ids = [str(item) for item in source_task_ids if str(item)]
        if not ids:
            return
        with self.connect() as conn:
            self._append_event_tx(
                conn, task_id, "memory.reuse_uncited", {"source_task_ids": ids},
            )

    def decide_reuse_outcomes(
        self, task_id: str, successful: bool, sources: Optional[Iterable[str]] = None
    ) -> List[str]:
        """Settle pending reuses for this task.

        `sources` narrows the settlement to the given source task ids: a hit the
        task's conclusion never cited stays pending instead of being counted as
        a failed (or successful) use.
        """
        outcome = "validated" if successful else "failed"
        now = time.time()
        affected: List[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_reuse WHERE task_id=? AND outcome='pending'", (task_id,)
            ).fetchall()
            for row in rows:
                source_task_id = str(row["source_task_id"])
                if sources is not None and source_task_id not in sources:
                    continue
                candidate = conn.execute(
                    "SELECT * FROM memory_candidates WHERE task_id=?",
                    (source_task_id,),
                ).fetchone()
                from .reuse_scores import is_formal
                if is_formal(dict(candidate) if candidate else None):
                    continue  # Formal reuse is settled only after explicit user delivery.
                conn.execute(
                    "UPDATE memory_reuse SET outcome=?,decided_at=? WHERE id=?",
                    (outcome, now, row["id"]),
                )
                if successful:
                    conn.execute(
                        """UPDATE memory_candidates
                           SET successful_reuse_count=successful_reuse_count+1,
                               failed_reuse_count=0,
                               status=CASE WHEN status='revalidation_required' THEN 'published' ELSE status END,
                               last_validated_at=?,updated_at=? WHERE task_id=?""",
                        (now, now, source_task_id),
                    )
                    if candidate:
                        new_status = "published" if candidate["status"] == "revalidation_required" else candidate["status"]
                        if new_status != candidate["status"]:
                            validate_memory_transition("reuse_validated", str(candidate["status"]))
                        self._write_candidate_status_sidecar(candidate, new_status, candidate["evidence_sha256"], now)
                else:
                    conn.execute(
                        "UPDATE memory_candidates SET failed_reuse_count=failed_reuse_count+1,status=CASE WHEN status='published' THEN 'revalidation_required' ELSE status END,updated_at=? WHERE task_id=?",
                        (now, source_task_id),
                    )
                    if candidate:
                        new_status = "revalidation_required" if candidate["status"] == "published" else candidate["status"]
                        if new_status != candidate["status"]:
                            validate_memory_transition("reuse_failed", str(candidate["status"]))
                        self._write_candidate_status_sidecar(candidate, new_status, candidate["evidence_sha256"], now)
                affected.append(source_task_id)
            if affected:
                self._append_event_tx(
                    conn, task_id, "memory.reuse_outcomes_decided",
                    {"outcome": outcome, "source_task_ids": affected},
                )
        return affected

    def mark_memory_retired(self, task_id: str, archive_path: str, reason: str) -> Dict[str, Any]:
        with self.connect() as conn:
            candidate = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone()
            if not candidate:
                raise KeyError(task_id)
            validate_memory_transition("retire", str(candidate["status"]))
            conn.execute(
                "UPDATE memory_candidates SET status='retired',published_path=?,updated_at=? WHERE task_id=?",
                (archive_path, time.time(), task_id),
            )
            self._write_candidate_status_sidecar(
                candidate, "retired", candidate["evidence_sha256"], time.time()
            )
            self._append_event_tx(
                conn, task_id, "memory.retired", {"archive_path": archive_path, "reason": reason[:500]},
            )
            return dict(conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone())

    def memory_review_receipt(self, task_id: str) -> Dict[str, Any]:
        """Return an auditable promotion receipt for one memory candidate.

        The receipt is derived only from committed rows, so it can be used to
        re-check after a process interruption what was actually approved, when,
        and against which candidate hash.
        """
        with self.read_only() as conn:
            candidate = conn.execute(
                "SELECT * FROM memory_candidates WHERE task_id=?", (task_id,)
            ).fetchone()
            if not candidate:
                raise KeyError(task_id)
            reviews = [
                dict(row) for row in conn.execute(
                    """SELECT id,stage,decision,note,candidate_sha256,created_at
                       FROM memory_reviews WHERE task_id=? ORDER BY created_at,id""",
                    (task_id,),
                )
            ]
            events = [
                dict(row) for row in conn.execute(
                    """SELECT event_type,payload_json,created_at FROM events
                       WHERE task_id=? AND event_type LIKE 'memory.%'
                       ORDER BY sequence""",
                    (task_id,),
                )
            ]
        status = str(candidate["status"])
        current_sha = str(candidate["evidence_sha256"])
        deliveries = [
            item for item in reviews
            if item["stage"] == "delivery" and item["decision"] == "approve"
        ]
        finals = [
            item for item in reviews
            if item["stage"] == "final" and item["decision"] == "approve"
        ]
        delivery_bound = bool(deliveries) and deliveries[-1]["candidate_sha256"] == current_sha
        final_bound = bool(finals) and finals[-1]["candidate_sha256"] == current_sha
        publications = [
            item for item in events
            if item["event_type"] in {"memory.published", "memory.publication_failed"}
        ]
        return {
            "task_id": task_id,
            "status": status,
            "active_retrieval": status in MEMORY_ACTIVE_RETRIEVAL_STATES,
            "fully_approved": delivery_bound and final_bound,
            "publish_ready": (
                status in MEMORY_CANDIDATE_TRANSITIONS["publish"]
                and delivery_bound and final_bound
            ),
            "candidate_sha256": current_sha,
            "delivery_approved_count": len(deliveries),
            "final_approved_count": len(finals),
            "reviews": reviews,
            "publications": publications,
            "stopped_after_delivery_review": delivery_bound and not final_bound,
        }

    def review_queue(self, stage: str, local_date: Optional[str] = None) -> List[Dict[str, Any]]:
        stage = normalize_review_stage(stage, allow_all=True)
        statuses = {
            "delivery": ("pending_delivery_review", "revision_requested"),
            "final": ("delivery_approved", "pending_final_review", "publication_failed"),
            "all": (
                "pending_delivery_review", "delivery_approved", "revision_requested",
                "pending_final_review", "publication_failed",
                "revalidation_required",
            ),
        }[stage]
        placeholders = ",".join("?" for _ in statuses)
        query = f"""SELECT mc.*,t.prompt,t.result,t.created_at AS task_created_at,t.session_id,
                           t.provenance_json
                    FROM memory_candidates mc JOIN tasks t ON t.id=mc.task_id
                    WHERE mc.status IN ({placeholders})"""
        args: List[Any] = list(statuses)
        if local_date:
            query += " AND t.session_id IN (SELECT id FROM work_sessions WHERE local_date=?)"
            args.append(local_date)
        query += " ORDER BY t.created_at"
        with self.read_only() as conn:
            from .reuse_scores import projection_tx
            formal_scores = projection_tx(conn)
            items = [dict(row) for row in conn.execute(query, args)]
            for item in items:
                from .experience_workflow import trial_summary
                item["legacy_reuse_count"] = int(item.get("reuse_count") or 0)
                item["formal_reuse_score"] = int((formal_scores.get(item["task_id"]) or {}).get("score", 0))
                item["experience_validation"] = trial_summary(conn, item)
                item["agent_runs"] = [
                    dict(row) for row in conn.execute(
                        "SELECT agent_name,status,output,error,latency_ms,created_at,finished_at FROM agent_runs WHERE task_id=? ORDER BY created_at",
                        (item["task_id"],),
                    )
                ]
                from .legacy_compat import read_latest_judge_event
                item["judge"] = read_latest_judge_event(conn, item["task_id"])
                value = conn.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.value_assessed' ORDER BY sequence DESC LIMIT 1", (item['task_id'],)).fetchone()
                item['value_assessment'] = json.loads(value[0]) if value else None
                item["reviews"] = [
                    dict(row) for row in conn.execute(
                        "SELECT stage,decision,note,candidate_sha256,created_at FROM memory_reviews WHERE task_id=? ORDER BY created_at",
                        (item["task_id"],),
                    )
                ]
            if stage in {"final", "all"}:
                from .formal_review import projection, _view
                items.extend(_view(v) for v in projection(conn).values() if v['status'] not in {'approved','rejected','withdrawn'})
            return items

    def memory_lifecycle_rows(self) -> List[Dict[str, Any]]:
        """Return only fields needed by the read-only Vault lifecycle audit."""
        with self.read_only() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT task_id,status,published_path
                       FROM memory_candidates
                       WHERE status IN ('published','revalidation_required','retired')
                       ORDER BY task_id"""
                )
            ]

    def complete_with_memory_candidate(
        self,
        task_id: str,
        result: str,
        staged_path: str,
        evidence_sha256: str,
        source_task_ids: Optional[List[str]] = None,
        provenance_kind: str = "agentos_multi_adjudicated",
        expected_attempt: Optional[int] = None,
    ) -> bool:
        """Complete a task and stage its review candidate in one transaction.

        Either both land or neither does, except a pending constraint hold
        deliberately keeps its completed task out of the review queue.
        Completion is idempotent: a task that is
        already completed is left untouched and no second task.completed or
        memory.candidate_staged event is written. Returns True when this call
        performed the completion.

        An exception does not prove the transaction rolled back: connect()
        runs permission maintenance after commit. Callers must re-read the
        task before treating a failure as "not completed".
        """
        if expected_attempt is None:
            raise TypeError("complete_with_memory_candidate() 必须传入 expected_attempt")
        self._require_staged_evidence(staged_path, evidence_sha256)
        result_sha256 = hashlib.sha256(result.encode()).hexdigest()
        provenance = {
            "kind": provenance_kind,
            "status": "completed",
            "result_sha256": result_sha256,
        }
        now = time.time()
        previous_sidecar = self._sidecar_snapshot(staged_path)
        try:
            completed = self._complete_with_memory_candidate_tx(
                task_id, result, staged_path, evidence_sha256, source_task_ids,
                provenance, result_sha256, now, expected_attempt=expected_attempt,
            )
        except Exception:
            self._restore_sidecar_unless_committed(task_id, staged_path, evidence_sha256, previous_sidecar)
            raise
        if self.value_candidate_status(task_id, evidence_sha256) == "value_discarded":
            self.cleanup_discarded_candidate(task_id, staged_path, evidence_sha256)
        return completed

    def complete_worker_with_memory_candidate(
        self,
        task_id: str,
        result: str,
        staged_path: str,
        stage_candidate,
        source_task_ids: Optional[List[str]],
        *,
        expected_attempt: int,
        provenance_kind: str = "agentos_multi_adjudicated",
    ) -> bool:
        """Stage and settle a Worker result under the same live-lease write lock."""
        target = Path(staged_path)
        sidecar = self._sidecar_path(staged_path)
        staged_sha256 = None
        previous_body = previous_sidecar = None
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status,attempts,lease_until FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                now = time.time()
                if (row["status"] != "running" or int(row["attempts"]) != int(expected_attempt)
                        or row["lease_until"] is None or float(row["lease_until"]) <= now):
                    raise ValueError(f"task {task_id!r} Worker attempt or live lease was lost before staging")
                if target.is_symlink() or sidecar.is_symlink():
                    raise ValueError("Worker candidate staging paths cannot be symlinks")
                previous_body = target.read_bytes() if target.exists() else None
                previous_sidecar = sidecar.read_bytes() if sidecar.exists() else None
                try:
                    staged = Path(stage_candidate())
                    if staged != target:
                        raise ValueError("Worker candidate staging returned an unexpected path")
                    staged_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
                    self._require_staged_evidence(str(target), staged_sha256)
                    result_sha256 = hashlib.sha256(result.encode()).hexdigest()
                    provenance = {"kind": provenance_kind, "status": "completed",
                                  "result_sha256": result_sha256}
                    completed = self._complete_with_memory_candidate_in_tx(
                        conn, task_id, result, str(target), staged_sha256,
                        source_task_ids, provenance, result_sha256, time.time(),
                        expected_attempt=expected_attempt, require_live_lease=True,
                    )
                except BaseException:
                    self._restore_worker_stage(target, previous_body, sidecar, previous_sidecar)
                    raise
        except BaseException:
            if staged_sha256 is not None:
                self._restore_worker_stage_unless_committed(
                    task_id, target, staged_sha256, previous_body, sidecar, previous_sidecar,
                )
            raise
        if self.value_candidate_status(task_id, staged_sha256) == "value_discarded":
            self.cleanup_discarded_candidate(task_id, str(target), staged_sha256)
        return completed

    @staticmethod
    def _restore_worker_stage(target: Path, body: Optional[bytes], sidecar: Path,
                              sidecar_body: Optional[bytes]) -> None:
        for path, data in ((target, body), (sidecar, sidecar_body)):
            if path.is_symlink():
                raise OSError("Worker staging path changed to a symlink during rollback")
            if data is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(data)
                os.chmod(path, 0o600)

    def _restore_worker_stage_unless_committed(
        self, task_id: str, target: Path, staged_sha256: str,
        body: Optional[bytes], sidecar: Path, sidecar_body: Optional[bytes],
    ) -> None:
        """Handle post-commit maintenance errors without racing a later Worker."""
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                candidate = conn.execute(
                    "SELECT staged_path,evidence_sha256 FROM memory_candidates WHERE task_id=?", (task_id,)
                ).fetchone()
                committed_candidate = bool(candidate and candidate["staged_path"] == str(target)
                                          and candidate["evidence_sha256"] == staged_sha256)
                from .value_queue import pending_items
                committed_queue = any(item["task_id"] == task_id
                    and item.get("queue_status") == "awaiting_value_review"
                    and item.get("staged_path") == str(target)
                    and item.get("candidate_sha256") == staged_sha256 for item in pending_items(conn))
                discarded = bool(task and task["status"] == "completed" and conn.execute(
                    "SELECT 1 FROM events WHERE task_id=? AND event_type='memory.value_assessed' "
                    "AND json_extract(payload_json,'$.decision')='discard' "
                    "AND json_extract(payload_json,'$.candidate_sha256')=? LIMIT 1",
                    (task_id, staged_sha256),
                ).fetchone())
                if committed_candidate or committed_queue or discarded:
                    return
                if target.is_symlink() or (target.exists() and
                        hashlib.sha256(target.read_bytes()).hexdigest() != staged_sha256):
                    return
                self._restore_worker_stage(target, body, sidecar, sidecar_body)
        except (OSError, sqlite3.Error, ValueError):
            return

    def _candidate_gates_tx(self, conn: sqlite3.Connection, task_id: str) -> bool:
        """The gates every settlement path passes before a candidate is queued.

        Which entry completed a task must not decide whether its candidate
        reaches initial review, so both the worker and the host receipt ask
        here. Returns False when a pending constraint hold keeps the candidate
        out of delivery; neither path may bypass human release.
        """
        if conn.execute(
            "SELECT 1 FROM constraint_holds WHERE task_id=? AND status='pending'",
            (task_id,),
        ).fetchone():
            # Covers both the normal completed hold and inconsistent
            # legacy states; no candidate can bypass human release.
            return False
        from .experience_workflow import policy
        full_task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        disposition = policy(full_task)
        from .workflow import verified_snapshot
        task_purpose = verified_snapshot(full_task).get("task_purpose")
        if task_purpose in {"release_smoke", "release_acceptance", "experience_trial"} and disposition["kind"] != "audit":
            raise ValueError("controlled smoke, acceptance and trial tasks must use audit settlement")
        if disposition["kind"] == "audit":
            raise ValueError("audit task must use audit-only settlement")
        self._reject_terminal_memory_reregistration(conn, task_id)
        return True

    def _insert_candidate_tx(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        staged_path: str,
        evidence_sha256: str,
        source_task_ids: Optional[List[str]],
        now: float,
        *,
        repaired: bool = False,
    ) -> bool:
        """Admit only scored method/rule candidates; defer unscored entries without a review row."""
        from .value_scoring import native_value, record_tx
        from .experience_workflow import policy
        task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        kind = policy(task)["kind"]
        reasons = []
        try:
            assessment = native_value(conn, task_id, reason_out=reasons)
            score_error = False
        except (ValueError, KeyError, TypeError, IndexError):
            assessment = None
            score_error = True
        if kind in {"method", "rule"}:
            if assessment is None:
                from .value_queue import defer_tx
                reason = "invalid_score_receipt" if score_error else (reasons[0] if reasons else "no_scoring_source")
                defer_tx(conn, task_id, {
                    "version": 1,
                    "staged_path": str(staged_path),
                    "candidate_sha256": evidence_sha256,
                    "source_task_ids": sorted(set(source_task_ids or [])),
                    "reason": reason,
                })
                return False
            from .value_queue import assessment_tx
            assessment_tx(conn, task_id, {
                "version": 3,
                "candidate_sha256": evidence_sha256,
                **assessment,
            })
        else:
            record_tx(conn, task_id, assessment)
            if assessment is None:
                self._append_event_tx(conn, task_id, 'memory.value_unscored', {
                    'reason': reasons[0] if reasons else 'kind_not_method_rule',
                })
        if assessment and assessment['decision'] == 'discard':
            task = conn.execute('SELECT result,provenance_json FROM tasks WHERE id=?', (task_id,)).fetchone()
            provenance = json.loads(task['provenance_json'] or '{}')
            provenance.update(
                memory_disposition='value_discarded',
                result_sha256=hashlib.sha256((task['result'] or '').encode('utf-8')).hexdigest(),
            )
            conn.execute('UPDATE tasks SET provenance_json=?,updated_at=? WHERE id=?',
                         (_canonical(provenance), now, task_id))
            return False
        conflict = "" if repaired else """
                   ON CONFLICT(task_id) DO UPDATE SET
                       staged_path=excluded.staged_path,
                       evidence_sha256=excluded.evidence_sha256,
                       source_task_ids_json=excluded.source_task_ids_json,
                       updated_at=excluded.updated_at"""
        conn.execute(
            """INSERT INTO memory_candidates(
                   task_id,status,staged_path,evidence_sha256,source_task_ids_json,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""" + conflict,
            (
                task_id, "pending_delivery_review", staged_path, evidence_sha256,
                _canonical(sorted(set(source_task_ids or []))), now, now,
            ),
        )
        self._write_candidate_status_sidecar_values(
            task_id, staged_path, "pending_delivery_review", evidence_sha256, now
        )
        return True

    def _complete_with_memory_candidate_tx(
        self,
        task_id: str,
        result: str,
        staged_path: str,
        evidence_sha256: str,
        source_task_ids: Optional[List[str]],
        provenance: Dict[str, Any],
        result_sha256: str,
        now: float,
        expected_attempt: Optional[int] = None,
    ) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._complete_with_memory_candidate_in_tx(
                conn, task_id, result, staged_path, evidence_sha256, source_task_ids,
                provenance, result_sha256, now, expected_attempt=expected_attempt,
            )

    def _complete_with_memory_candidate_in_tx(
        self, conn: sqlite3.Connection, task_id: str, result: str, staged_path: str,
        evidence_sha256: str, source_task_ids: Optional[List[str]],
        provenance: Dict[str, Any], result_sha256: str, now: float,
        *, expected_attempt: Optional[int], require_live_lease: bool = False,
    ) -> bool:
        row = conn.execute("SELECT status,attempts,lease_until FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        if expected_attempt is None:
            raise TypeError("complete_with_memory_candidate() 必须传入 expected_attempt")
        if require_live_lease:
            if (row["status"] != "running" or int(row["attempts"]) != int(expected_attempt)
                    or row["lease_until"] is None or float(row["lease_until"]) <= time.time()):
                raise ValueError(f"task {task_id!r} Worker attempt or live lease was lost before settlement")
        elif row["status"] not in {"completed"} and int(row["attempts"]) != int(expected_attempt):
            raise ValueError(f"task {task_id!r} 旧尝试不能结算任务")
        if not self._candidate_gates_tx(conn, task_id):
            if require_live_lease:
                raise ValueError("Worker task is held and cannot stage a candidate")
            return False
        if row["status"] == "completed":
            if conn.execute("SELECT 1 FROM events WHERE task_id=? AND event_type='memory.value_assessed' AND json_extract(payload_json,'$.decision')='discard'", (task_id,)).fetchone():
                return False
            if self._existing_candidate_status(conn, task_id) is not None:
                # A finished candidate is idempotent (no-op) unless it is
                # terminal, where any second registration is refused.
                self._reject_terminal_memory_reregistration(conn, task_id)
                return False
            from .value_queue import pending_items
            prior_prepared = [item for item in pending_items(conn)
                              if item["task_id"] == task_id
                              and item.get("queue_status") == "awaiting_value_review"]
            if prior_prepared:
                if (len(prior_prepared) != 1 or prior_prepared[0]["candidate_sha256"] != evidence_sha256
                        or prior_prepared[0]["staged_path"] != staged_path):
                    raise ValueError("completed task already has different prepared candidate evidence")
                return False
            # Completed without a candidate must not exist; if an older
            # code path left one behind, queue the candidate now instead of
            # reporting an idempotent no-op that would hide the gap.
            if self._insert_candidate_tx(
                conn, task_id, staged_path, evidence_sha256, source_task_ids, now,
                repaired=True,
            ):
                self._append_event_tx(
                    conn, task_id, "memory.candidate_staged",
                    {"evidence_sha256": evidence_sha256, "repaired_missing_candidate": True},
                )
            return False
        if require_live_lease:
            changed = conn.execute(
                "UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=? AND status='running' AND attempts=? AND lease_until>?",
                (result, _canonical(provenance), now, task_id, expected_attempt, time.time()),
            ).rowcount
        else:
            changed = conn.execute(
                "UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=? AND status IN ('pending','running')",
                (result, _canonical(provenance), now, task_id),
            ).rowcount
        if changed != 1:
            raise ValueError(f"task {task_id!r} Worker attempt or live lease was lost before settlement")
        admitted = self._insert_candidate_tx(conn, task_id, staged_path, evidence_sha256, source_task_ids, now)
        final = conn.execute('SELECT result FROM tasks WHERE id=?', (task_id,)).fetchone()[0]
        result_sha256 = hashlib.sha256(final.encode()).hexdigest()
        self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": result_sha256})
        if admitted:
            self._append_event_tx(conn, task_id, "memory.candidate_staged", {"evidence_sha256": evidence_sha256})
        return True

    def hold_constraint_task(self, task_id: str, result: str, details: Dict[str, Any]) -> None:
        """Complete execution while keeping the result out of delivery fields."""
        result_hash = hashlib.sha256(result.encode("utf-8")).hexdigest()
        now = time.time()
        with self.connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='completed',result=NULL,provenance_json=?,"
                "last_error=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (_canonical({
                    "kind": "agentos_multi_adjudicated",
                    "status": "constraint_hold",
                    "result_sha256": result_hash,
                }), now, task_id),
            )
            conn.execute(
                "INSERT INTO constraint_holds(task_id,result,status,details_json,created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET result=excluded.result,"
                "status='pending',details_json=excluded.details_json,released_at=NULL",
                (task_id, result, "pending", _canonical(details), now),
            )
            self._append_event_tx(
                conn, task_id, "constraint_detection_miss",
                {**details, "result_sha256": result_hash},
            )

    def get_constraint_hold(self, task_id: str, *, include_result: bool = False) -> Optional[Dict[str, Any]]:
        with self.read_only() as conn:
            row = conn.execute(
                "SELECT task_id,status,details_json,created_at,released_at"
                + (",result" if include_result else "")
                + " FROM constraint_holds WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            try:
                item["details"] = json.loads(item.pop("details_json") or "{}")
            except json.JSONDecodeError:
                item["details"] = {}
            return item

    def release_constraint_hold(
        self,
        task_id: str,
        staged_path: Optional[str] = None,
        evidence_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Release a held result into the normal task result field.

        When staged_path and evidence_sha256 are given, the review candidate is
        queued in the same transaction, so a released task can never be
        missing from the review queue.
        """
        if (staged_path is None) != (evidence_sha256 is None):
            raise ValueError("staged_path and evidence_sha256 must be given together")
        if staged_path is None:
            return self._release_constraint_hold_tx(task_id, None, None)
        self._require_staged_evidence(staged_path, evidence_sha256)
        previous_sidecar = self._sidecar_snapshot(staged_path)
        try:
            result = self._release_constraint_hold_tx(task_id, staged_path, evidence_sha256)
        except Exception:
            self._restore_sidecar_unless_committed(task_id, staged_path, evidence_sha256, previous_sidecar)
            raise
        if result.get("candidate_status") == "value_discarded":
            self.cleanup_discarded_candidate(task_id, staged_path, evidence_sha256)
        return result

    def _release_constraint_hold_tx(
        self, task_id: str, staged_path: Optional[str], evidence_sha256: Optional[str]
    ) -> Dict[str, Any]:
        now = time.time()
        with self.connect() as conn:
            # Same boundary as stage_memory_candidate: lock first so the terminal
            # check below cannot be overtaken by a concurrent publish or retire
            # that would leave published evidence carrying an unapproved hash.
            conn.execute("BEGIN IMMEDIATE")
            if staged_path is not None:
                self._reject_terminal_memory_reregistration(conn, task_id)
            row = conn.execute(
                "SELECT result,status FROM constraint_holds WHERE task_id=?", (task_id,)
            ).fetchone()
            if not row:
                raise KeyError(task_id)
            if row["status"] != "pending":
                raise ValueError("constraint hold is not pending")
            result = str(row["result"])
            result_hash = hashlib.sha256(result.encode("utf-8")).hexdigest()
            conn.execute(
                "UPDATE constraint_holds SET status='released',released_at=? WHERE task_id=?",
                (now, task_id),
            )
            conn.execute(
                "UPDATE tasks SET status='completed',result=?,provenance_json=?,"
                "last_error=NULL,updated_at=? WHERE id=?",
                (result, _canonical({
                    "kind": "agentos_multi_adjudicated",
                    "status": "completed",
                    "result_sha256": result_hash,
                    "constraint_hold_released": True,
                }), now, task_id),
            )
            self._append_event_tx(
                conn, task_id, "constraint_hold_released", {"result_sha256": result_hash}
            )
            if staged_path is not None:
                if self._insert_candidate_tx(conn,task_id,staged_path,evidence_sha256,[],now):
                    self._append_event_tx(conn,task_id,'memory.candidate_staged',{'evidence_sha256':evidence_sha256})
                else:
                    result=conn.execute('SELECT result FROM tasks WHERE id=?',(task_id,)).fetchone()[0]
                    result_hash=hashlib.sha256(result.encode()).hexdigest()
            self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": result_hash})
            candidate = conn.execute("SELECT status FROM memory_candidates WHERE task_id=?", (task_id,)).fetchone()
            if candidate:
                candidate_status = candidate["status"]
            elif staged_path is None:
                candidate_status = "none"
            else:
                from .value_queue import pending_items
                pending = any(item["task_id"] == task_id
                              and item.get("queue_status") == "awaiting_value_review"
                              for item in pending_items(conn))
                candidate_status = "awaiting_value_review" if pending else "value_discarded"
            return {"task_id": task_id, "result": result, "result_sha256": result_hash,
                    "candidate_status": candidate_status}
