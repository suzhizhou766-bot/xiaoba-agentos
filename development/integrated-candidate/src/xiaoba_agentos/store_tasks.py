"""W4-P3: EventStore task submission, lease lifecycle, agent runs, health, and freeze/resume methods."""
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


class TaskStoreMixin:
    def submit(
        self,
        prompt: str,
        max_attempts: int,
        priority: int = 0,
        snapshot: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        session_id: Optional[str] = None,
        route_decision_id: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> str:
        snapshot = snapshot or {"prompt": prompt}
        snapshot_json = _canonical(snapshot)
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            # Serialize the idempotency lookup with the insert.  A deferred
            # transaction lets two submitters both observe an absent key and
            # makes one of them fail on the unique index instead of replaying
            # the first task.
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id,prompt,execution_snapshot_json FROM tasks WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    # MD2-09: a retry of the same submission gets the first task
                    # back.  A key reused for different content is a caller bug,
                    # not a retry, and must never be answered with a task that
                    # froze different input.
                    if (
                        existing["prompt"] != prompt
                        or _submitted_content(json.loads(existing["execution_snapshot_json"] or "{}"))
                        != _submitted_content(snapshot)
                    ):
                        raise ValueError(
                            f"idempotency key {idempotency_key!r} is already used by a task "
                            "with different content; use a new key"
                        )
                    return str(existing["id"])
            now = time.time()
            task_id = str(uuid.uuid4())
            # `strategy_version` is kept on the row as history only. Strategy
            # evolution was retired on 2026-10-11, so every new task records the
            # same value the column default already carries.
            strategy_version = 1
            conn.execute(
                "INSERT INTO tasks(id,prompt,status,priority,max_attempts,run_after,strategy_version,execution_snapshot_json,execution_snapshot_hash,execution_mode,idempotency_key,session_id,route_decision_id,provenance_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id, prompt, "pending", priority, max_attempts, now, strategy_version,
                    snapshot_json, snapshot_hash, "production", idempotency_key, session_id,
                    route_decision_id, _canonical(provenance or {
                        "kind": "agentos_multi_adjudicated",
                        "status": "pending",
                    }), now, now,
                ),
            )
            self._append_event_tx(
                conn,
                task_id,
                "task.submitted",
                {
                    "priority": priority,
                    "strategy_version": strategy_version,
                    "execution_snapshot_hash": snapshot_hash,
                    "session_id": session_id,
                    "route_decision_id": route_decision_id,
                },
            )
            if snapshot.get("experience_trial"):
                from .experience_workflow import link_trial_tx
                link_trial_tx(self, conn, snapshot["experience_trial"]["source_task_id"], task_id)
        return task_id

    def recover_expired(self) -> int:
        """Recover real task leases only.

        Legacy `repair_jobs` rows are deliberately left exactly as they are: peer
        auto-repair is retired, so nothing may rewrite their status, lease or
        events. They stay readable as history.
        """
        now = time.time()
        recovered = 0
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM tasks WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?", (now,)).fetchall()
            for row in rows:
                # The same expiry condition again, in the UPDATE itself: another
                # worker may have recovered this task and leased it anew since
                # the SELECT, and that live lease must not be thrown back to
                # retry (round-29 review; v1.6.4 had the same gap).
                moved = conn.execute(
                    "UPDATE tasks SET status='retry',run_after=?,lease_until=NULL,updated_at=?,last_error=? "
                    "WHERE id=? AND status='running' AND lease_until IS NOT NULL AND lease_until<?",
                    (now, now, "worker lease expired; automatically recovered", row["id"], now),
                ).rowcount
                if not moved:
                    continue
                interrupted = conn.execute("UPDATE agent_runs SET status='interrupted',error=?,finished_at=? WHERE task_id=? AND status='running'", ("worker lease expired; run outcome is unknown", now, row["id"])).rowcount
                self._append_event_tx(conn, row["id"], "task.lease_recovered", {"interrupted_runs": interrupted})
                recovered += 1
            return recovered

    def expire_running_leases(self) -> int:
        """Make every leased task recoverable on the next poll.

        For a single worker that has just lost track of what it was running.
        Only tasks still marked running are touched; recover_expired() then
        moves them to retry and marks their open agent runs interrupted.
        """
        now = time.time()
        with self.connect() as conn:
            return conn.execute(
                "UPDATE tasks SET lease_until=? WHERE status='running' AND (lease_until IS NULL OR lease_until>?)",
                (now - 1, now - 1),
            ).rowcount

    def has_runnable_work(self) -> bool:
        """Cheap read-only check: is there anything a worker poll could act on?

        Covers runnable tasks and expired task leases that need recovery.
        `repair_jobs` is excluded on purpose -- peer auto-repair is retired, so a
        legacy row still marked pending or running is history, not work, and must
        not keep a worker reporting busy forever. Frozen tasks are excluded too:
        only an explicit `task-resume` makes them runnable again.
        """
        now = time.time()
        with self.read_only() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM tasks WHERE (status IN ('pending','retry') AND run_after<=?) "
                "OR (status='running' AND lease_until IS NOT NULL AND lease_until<?) LIMIT 1",
                (now, now),
            ).fetchone())

    def owns_lease(self, task_id: str, attempt: int) -> bool:
        with self.read_only() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM tasks WHERE id=? AND status='running' AND attempts=? AND lease_until>?",
                (task_id, attempt, time.time()),
            ).fetchone())

    def current_lease_until(self, task_id: str, attempt: int) -> Optional[float]:
        """Read-only lease expiry for this attempt, so pacing can ask "how much is left"
        without writing a `task.lease_renewed` event the way renew_lease does."""
        with self.read_only() as conn:
            row = conn.execute(
                "SELECT lease_until FROM tasks WHERE id=? AND status='running' AND attempts=?",
                (task_id, attempt),
            ).fetchone()
        return None if row is None else row["lease_until"]

    def renew_lease(self, task_id: str, attempt: int, lease_seconds: float) -> bool:
        """Only the current, unexpired attempt may extend its own lease."""
        with self.connect() as conn:
            # The clock is read after the write lock is held: a time taken before
            # waiting on another writer could still call an already expired lease
            # unexpired and renew it (round-40 review).
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            changed = conn.execute(
                "UPDATE tasks SET lease_until=?,updated_at=? WHERE id=? AND status='running' "
                "AND attempts=? AND lease_until>?",
                (now + lease_seconds, now, task_id, attempt, now),
            ).rowcount
            if changed:
                self._append_event_tx(conn, task_id, "task.lease_renewed", {"attempt": attempt})
            return bool(changed)

    def record_task_participant(self, task_id: str, actor_id: str, role: str) -> None:
        from .task_participants import record_participant
        record_participant(self, task_id, actor_id, role)

    def mark_dispatch_wait(self, task_id: str, status: str, reason: str, expected_attempt=None) -> None:
        """A visible hold without consuming a retry; release only our own lease.

        Unchanged waiting status/reason is a no-op: avoid write-lock churn while
        Worker polls awaiting_host / ask_user / blocked_review_nodes.
        """
        message = status + ': ' + reason
        with self.read_only() as conn:
            peek = conn.execute(
                'SELECT status,last_error,lease_until FROM tasks WHERE id=?', (task_id,)
            ).fetchone()
            if (peek and peek['status'] == 'pending' and peek['last_error'] == message
                    and peek['lease_until'] is None):
                return
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT status,attempts,last_error,lease_until FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['status'] not in {'pending','retry','running'}:raise ValueError('任务不在可等待状态')
            if row['status']=='running' and (expected_attempt != row['attempts'] or not self._lease_live_tx(conn, task_id, expected_attempt)):
                raise ValueError('执行权已失效')
            if row['status'] == 'pending' and row['last_error'] == message and row['lease_until'] is None:
                return
            conn.execute("UPDATE tasks SET status='pending',lease_until=NULL,last_error=?,updated_at=? WHERE id=?", (message,time.time(),task_id))
            if row['last_error'] != message:
                self._append_event_tx(conn, task_id, 'task.dispatch_wait', {'status': status, 'reason': reason})

    def _lease_live_tx(self, conn, task_id, attempt):
        return bool(conn.execute("SELECT 1 FROM tasks WHERE id=? AND attempts=? AND lease_until>?", (task_id,attempt,time.time())).fetchone())

    def lease_next(self, lease_seconds: int, task_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Read the clock only after waiting for the writer lock.  A clock
            # value captured before the wait can produce a lease that is
            # already expired by the time this method returns.
            now = time.time()
            row = conn.execute("SELECT * FROM tasks WHERE status IN ('pending','retry') AND run_after<=?" + (" AND id=?" if task_id else "") + " ORDER BY priority DESC, created_at ASC LIMIT 1", (now,task_id) if task_id else (now,)).fetchone()
            if not row:
                return None
            attempts = row["attempts"] + 1
            lease_until = now + lease_seconds
            conn.execute("UPDATE tasks SET status='running',attempts=?,lease_until=?,updated_at=? WHERE id=?", (attempts, lease_until, now, row["id"]))
            self._append_event_tx(conn, row["id"], "task.leased", {"attempt": attempts})
            result = dict(row)
            result.update({"attempts": attempts, "status": "running", "lease_until": lease_until, "updated_at": now})
            return result

    def start_agent_run(self, task_id: str, agent_name: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs(id,task_id,agent_name,status,created_at) VALUES(?,?,?,?,?)",
                (run_id, task_id, agent_name, "running", time.time()),
            )
            payload = {"agent": agent_name, "run_id": run_id}
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='ri_sources'").fetchone():
                source = conn.execute('SELECT data FROM ri_sources WHERE id=?',(agent_name,)).fetchone()
                if source:
                    node = json.loads(source[0]).get('node') or {}
                    payload['workday_identity'] = {'agent':node.get('agent') or agent_name,
                                                   'model':(metadata or {}).get('configured_model') or node.get('model') or '未记录'}
            if metadata:
                from .task_scores import capture_pool
                execution = dict(metadata)
                frozen = capture_pool(conn, agent_name, execution.get('purpose'))
                if frozen:
                    if 'configured_model' in execution:
                        frozen['model']=execution['configured_model']
                    if 'pool_source_ref' in execution:
                        # Keep the stable source reference aligned with caller
                        # bindings without replacing the executable identity.
                        frozen['source_ref']=execution['pool_source_ref']
                    execution['pool_score'] = frozen
                payload["execution"] = execution
            self._append_event_tx(conn, task_id, "agent.started", payload)
        return run_id

    def finish_agent_run(
        self,
        run_id: str,
        status: str,
        output: Optional[str],
        error: Optional[str],
        latency_ms: int,
        failure_class: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute("SELECT task_id,agent_name FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            conn.execute(
                "UPDATE agent_runs SET status=?,output=?,error=?,failure_class=?,latency_ms=?,finished_at=? WHERE id=?",
                (status, output, error, failure_class, latency_ms, time.time(), run_id),
            )
            receipt_path = self.path.parent / 'receipts' / row['task_id'] / (run_id+'.json')
            receipt_reference = {}
            receipt_sha256 = _view_file_sha256(receipt_path)
            if receipt_sha256:
                receipt_reference = {'receipt_path':str(receipt_path), 'receipt_file_sha256':receipt_sha256}
            self._append_event_tx(
                conn,
                row["task_id"],
                "agent.finished",
                {"agent": row["agent_name"], "run_id": run_id, "status": status, "latency_ms": latency_ms, **receipt_reference,
                 "receipt_sha256": hashlib.sha256(
                     json.dumps({'output':output,'status':status},ensure_ascii=False,sort_keys=True,
                                separators=(',',':'),allow_nan=False).encode()).hexdigest()},
            )

    def complete(self, task_id: str, result: str, provenance_kind: str = "agentos_multi_adjudicated",
                 expected_attempt: Optional[int] = None) -> None:
        if expected_attempt is None:
            raise TypeError("complete() 必须传入 expected_attempt；手动结算请用 complete_manually()")
        provenance = {
            "kind": provenance_kind,
            "status": "completed",
            "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status,attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
            if int(current["attempts"]) != int(expected_attempt):
                raise ValueError(f"task {task_id!r} 旧尝试不能结算任务")
            if current["status"] not in TASK_SETTLEMENT_SOURCE_STATES:
                raise ValueError(
                    f"task {task_id!r} cannot complete from status {current['status']!r}"
                )
            now = time.time()
            changed = conn.execute(
                "UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=? AND status IN ('pending','running')",
                (result, _canonical(provenance), now, task_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"task {task_id!r} completion update affected {changed} rows")
            self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": hashlib.sha256(result.encode()).hexdigest()})

    def complete_manually(self, task_id: str, result: str, provenance_kind: str = "host_manual") -> None:
        """User or host settlement. Does not check expected_attempt."""
        provenance = {
            "kind": provenance_kind,
            "status": "completed",
            "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
            "manual": True,
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status,attempts,execution_snapshot_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
            if json.loads(current["execution_snapshot_json"] or "{}").get("review_context"):
                raise ValueError("本任务必须经 task-host-complete 提交绑定回执")
            if current["status"] not in TASK_SETTLEMENT_SOURCE_STATES:
                raise ValueError(
                    f"task {task_id!r} cannot complete from status {current['status']!r}"
                )
            now = time.time()
            changed = conn.execute(
                "UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? "
                "WHERE id=? AND status IN ('pending','running')",
                (result, _canonical(provenance), now, task_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"task {task_id!r} completion update affected {changed} rows")
            self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": hashlib.sha256(result.encode()).hexdigest(), "manual": True})

    def retry_or_fail(self, task: Dict[str, Any], error: str, delay_seconds: int) -> str:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status,attempts,max_attempts,lease_until FROM tasks WHERE id=?",
                (task["id"],),
            ).fetchone()
            if current is None:
                raise KeyError(task["id"])
            if current["status"] != "running":
                raise ValueError(
                    f"task {task['id']!r} cannot retry or fail from status {current['status']!r}"
                )
            now = time.time()
            if current["lease_until"] is None or float(current["lease_until"]) <= now:
                raise ValueError(f"task {task['id']!r} lease is no longer valid")
            # The worker's task dictionary is a snapshot.  Exhaustion must use
            # the row locked above so an old attempts/max_attempts pair cannot
            # resurrect or prematurely fail a task.
            exhausted = int(current["attempts"]) >= int(current["max_attempts"])
            status = "failed" if exhausted else "retry"
            changed = conn.execute(
                "UPDATE tasks SET status=?,run_after=?,lease_until=NULL,last_error=?,updated_at=? "
                "WHERE id=? AND status='running' AND lease_until>?",
                (status, now if exhausted else now + delay_seconds, error[:4000], now, task["id"], now),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"task {task['id']!r} retry update affected {changed} rows")
            self._append_event_tx(conn, task["id"], f"task.{status}", {"delay_seconds": 0 if exhausted else delay_seconds, "error": error[:500]})
        return status

    def fail(self, task_id: str, error: str, expected_attempt: Optional[int] = None) -> str:
        if expected_attempt is None:
            raise TypeError("fail() 必须传入 expected_attempt；手动结算请用 fail_manually()")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status,attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
            if int(current["attempts"]) != int(expected_attempt):
                raise ValueError(f"task {task_id!r} 旧尝试不能结算任务")
            if current["status"] not in TASK_SETTLEMENT_SOURCE_STATES:
                raise ValueError(
                    f"task {task_id!r} cannot fail from status {current['status']!r}"
                )
            now = time.time()
            changed = conn.execute(
                "UPDATE tasks SET status='failed',run_after=?,lease_until=NULL,last_error=?,updated_at=? "
                "WHERE id=? AND status IN ('pending','running')",
                (now, error[:4000], now, task_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"task {task_id!r} failure update affected {changed} rows")
            self._append_event_tx(conn, task_id, "task.failed", {"delay_seconds": 0, "error": error[:500]})
        return "failed"

    def fail_manually(self, task_id: str, error: str) -> str:
        """User or host settlement. Does not check expected_attempt."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
            if current["status"] not in TASK_SETTLEMENT_SOURCE_STATES:
                raise ValueError(
                    f"task {task_id!r} cannot fail from status {current['status']!r}"
                )
            now = time.time()
            changed = conn.execute(
                "UPDATE tasks SET status='failed',run_after=?,lease_until=NULL,last_error=?,updated_at=? "
                "WHERE id=? AND status IN ('pending','running')",
                (now, error[:4000], now, task_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"task {task_id!r} failure update affected {changed} rows")
            self._append_event_tx(conn, task_id, "task.failed", {"delay_seconds": 0, "error": error[:500], "manual": True})
        return "failed"

    def record_agent_health(
        self,
        agent_name: str,
        status: str,
        *,
        failure_class: Optional[str] = None,
        error: Optional[str] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if status not in {"unknown", "healthy", "degraded", "repairing", "external_blocked"}:
            raise ValueError("invalid agent health status")
        now = time.time()
        details_json = _canonical(details or {})
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO agent_health(
                       agent_name,status,failure_class,error,provider,model,details_json,last_probe_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(agent_name) DO UPDATE SET
                       status=excluded.status,failure_class=excluded.failure_class,error=excluded.error,
                       provider=excluded.provider,model=excluded.model,details_json=excluded.details_json,
                       last_probe_at=excluded.last_probe_at,updated_at=excluded.updated_at""",
                (
                    agent_name, status, failure_class, (error or "")[:2000] or None,
                    provider, model, details_json, now, now,
                ),
            )
            self._append_event_tx(
                conn,
                None,
                "health.agent_probe",
                {
                    "agent": agent_name,
                    "status": status,
                    "failure_class": failure_class,
                    "provider": provider,
                    "model": model,
                },
            )
            return dict(conn.execute("SELECT * FROM agent_health WHERE agent_name=?", (agent_name,)).fetchone())

    def agent_health(
        self,
        *,
        enabled_names: Optional[Iterable[str]] = None,
        max_age_seconds: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        with self.read_only() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM agent_health ORDER BY agent_name")]
        if enabled_names is not None:
            allowed = set(enabled_names)
            rows = [row for row in rows if row.get("agent_name") in allowed]
        if max_age_seconds is not None:
            cutoff = time.time() - float(max_age_seconds)
            rows = [row for row in rows if float(row.get("last_probe_at") or 0) >= cutoff]
        return rows

    def freeze_task(
        self,
        task_id: str,
        *,
        failure_class: str,
        failures: List[Dict[str, Any]],
        receipts: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["status"] in {"completed", "failed"}:
                raise ValueError("terminal task cannot be frozen")
            runs = conn.execute(
                """SELECT id,agent_name,status,output,latency_ms,failure_class,error,finished_at
                   FROM agent_runs WHERE task_id=? ORDER BY created_at""",
                (task_id,),
            ).fetchall()
            completed = [
                {
                    "run_id": row["id"],
                    "agent": row["agent_name"],
                    "output_sha256": hashlib.sha256(str(row["output"] or "").encode()).hexdigest(),
                    "latency_ms": row["latency_ms"],
                    "finished_at": row["finished_at"],
                }
                for row in runs if row["status"] == "completed" and row["output"]
            ]
            provider_events = [
                {
                    "sequence": int(row["sequence"]),
                    "event_id": row["event_id"],
                    "event_hash": row["event_hash"],
                }
                for row in conn.execute(
                    """SELECT sequence,event_id,event_hash FROM events
                       WHERE task_id=? AND event_type='agent.provider_attempt'
                       ORDER BY sequence""",
                    (task_id,),
                )
            ]
            payload = {
                "version": 2,
                "task_id": task_id,
                "execution_snapshot_hash": task["execution_snapshot_hash"],
                "resume_generation": int(task["resume_generation"] or 0),
                "completed_runs": completed,
                "execution_receipts": receipts or [],
                "provider_attempt_events": provider_events,
                "failures": failures,
                "failure_class": failure_class,
                "frozen_at": now,
            }
            payload_json = _canonical(payload)
            freeze_hash = hashlib.sha256(payload_json.encode()).hexdigest()
            error = "; ".join(
                f"{item.get('agent', 'unknown')}: {item.get('error', 'unknown')}" for item in failures
            )[:4000]
            conn.execute(
                """UPDATE tasks SET status='frozen_paused',freeze_json=?,freeze_hash=?,failure_class=?,
                       last_error=?,lease_until=NULL,updated_at=? WHERE id=?""",
                (payload_json, freeze_hash, failure_class, error, now, task_id),
            )
            self._append_event_tx(
                conn,
                task_id,
                "task.frozen",
                {
                    "freeze_hash": freeze_hash,
                    "execution_snapshot_hash": task["execution_snapshot_hash"],
                    "completed_agents": [item["agent"] for item in completed],
                    "failure_class": failure_class,
                },
            )
            return {"task_id": task_id, "status": "frozen_paused", "freeze_hash": freeze_hash, **payload}

    @staticmethod
    def _verify_frozen_record_integrity(task: sqlite3.Row) -> Dict[str, Any]:
        """Parse and verify the immutable parts shared by resume and abandon."""
        try:
            frozen = json.loads(task["freeze_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("invalid frozen task record") from exc
        if not isinstance(frozen, dict):
            raise ValueError("invalid frozen task record")
        actual_snapshot_hash = hashlib.sha256(
            str(task["execution_snapshot_json"] or "").encode("utf-8")
        ).hexdigest()
        if (
            frozen.get("execution_snapshot_hash") != task["execution_snapshot_hash"]
            or actual_snapshot_hash != task["execution_snapshot_hash"]
        ):
            raise ValueError("execution snapshot changed while task was frozen")
        calculated_freeze_hash = hashlib.sha256(_canonical(frozen).encode()).hexdigest()
        if calculated_freeze_hash != task["freeze_hash"]:
            raise ValueError("frozen task record hash mismatch")
        return frozen

    def resume_task(
        self,
        task_id: str,
        expected_freeze_hash: Optional[str] = None,
        receipt_root: Optional[Path] = None,
        *,
        enabled_names: Iterable[str],
        health_max_age_seconds: float,
        retested_nodes: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """Resume a frozen task on the user's explicit instruction, and only then.

        Accepts a newly frozen task (`frozen_paused`) as well as a legacy
        `resume_ready` row left behind by the retired auto-repair. Neither ever
        resumes itself, and neither resumes without the caller presenting the
        freeze hash. Every recorded hash is re-verified before anything moves, and
        the failed side must hold a real `healthy` probe that is still enabled and
        still inside the configured window: an old healthy row is not capacity
        that exists now. `enabled_names` and `health_max_age_seconds` are required
        so no caller can get the looser check by omitting them. An Agent-pool node
        has no probe row; it counts only when retested after this freeze.
        """
        now = time.time()
        allowed = {str(name) for name in enabled_names}
        cutoff = now - float(health_max_age_seconds)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["status"] not in {"frozen_paused", "resume_ready"}:
                raise ValueError("task is not frozen")
            if not expected_freeze_hash:
                raise ValueError("explicit freeze hash is required")
            if task["freeze_hash"] != expected_freeze_hash:
                raise ValueError("freeze hash mismatch")
            frozen = self._verify_frozen_record_integrity(task)
            try:
                execution_snapshot = json.loads(str(task["execution_snapshot_json"] or "{}"))
            except json.JSONDecodeError as exc:
                raise ValueError("invalid frozen execution snapshot") from exc
            if not isinstance(execution_snapshot, dict):
                raise ValueError("invalid frozen execution snapshot")
            frozen_review_agents = None
            workflow = execution_snapshot.get("workflow")
            if isinstance(workflow, dict) and workflow.get("phase") == "review":
                workflow_agents = workflow.get("agents")
                if isinstance(workflow_agents, list) and workflow_agents:
                    frozen_review_agents = {
                        name for name in workflow_agents if isinstance(name, str) and name
                    }
            completed = {str(item.get("run_id")): item for item in frozen.get("completed_runs", [])}
            for run_id, item in completed.items():
                run = conn.execute(
                    "SELECT status,output FROM agent_runs WHERE id=? AND task_id=?", (run_id, task_id)
                ).fetchone()
                if not run or run["status"] != "completed":
                    raise ValueError("frozen completed Agent run is missing")
                output_hash = hashlib.sha256(str(run["output"] or "").encode()).hexdigest()
                if output_hash != item.get("output_sha256"):
                    raise ValueError("frozen Agent output hash mismatch")
            for item in frozen.get("provider_attempt_events", []):
                event = conn.execute(
                    "SELECT * FROM events WHERE sequence=? AND task_id=?",
                    (item.get("sequence"), task_id),
                ).fetchone()
                if (
                    not event
                    or event["event_id"] != item.get("event_id")
                    or event["event_hash"] != item.get("event_hash")
                ):
                    raise ValueError("frozen Provider attempt event hash mismatch")
                material = (
                    f"{event['prev_hash']}|{event['event_id']}|{event['task_id'] or ''}|"
                    f"{event['event_type']}|{event['payload_json']}|{event['created_at']:.6f}"
                )
                if (
                    event["event_type"] != "agent.provider_attempt"
                    or hashlib.sha256(material.encode("utf-8")).hexdigest() != event["event_hash"]
                ):
                    raise ValueError("frozen Provider attempt event content hash mismatch")
            if frozen.get("execution_receipts") and receipt_root is None:
                raise ValueError("receipt root is required to verify frozen execution receipts")
            if receipt_root is not None:
                for item in frozen.get("execution_receipts", []):
                    name = str(item.get("name") or "")
                    if not name or Path(name).name != name:
                        raise ValueError("invalid frozen receipt name")
                    path = receipt_root / task_id / name
                    if not path.is_file():
                        raise ValueError("frozen execution receipt is missing")
                    if hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
                        raise ValueError("frozen execution receipt hash mismatch")
            frozen_failures = [
                item for item in frozen.get("failures", []) if isinstance(item, dict)
            ]
            failed_agents = {
                str(item.get("agent")) for item in frozen_failures if item.get("agent")
            }
            current = {
                str(row["agent_name"]): str(row["status"])
                for row in conn.execute(
                    "SELECT agent_name,status FROM agent_health "
                    "WHERE last_probe_at>=? AND agent_name IN (%s)" % ",".join("?" * len(allowed)),
                    (cutoff, *sorted(allowed)),
                )
            } if allowed else {}
            frozen_at = float(task["updated_at"])
            retested = {str(name) for name, at in (retested_nodes or {}).items()
                        if float(at) >= max(frozen_at, cutoff)}
            # Allow a healthy third-node substitute to cover a still-unhealthy
            # failed seat; do not require the original failed node itself to heal.
            uncovered = {
                name for name in failed_agents
                if current.get(name) != "healthy" and name not in retested
            }
            completed_agents = {str(item.get("agent")) for item in completed.values()}
            healthy_substitutes = ({
                name for name, status in current.items()
                if status == "healthy" and name not in failed_agents
            } | (retested - failed_agents)) - completed_agents
            if frozen_review_agents is not None:
                healthy_substitutes &= frozen_review_agents
            if uncovered and len(healthy_substitutes) < len(uncovered):
                guidance = (
                    "或启用健康的第3审查节点替补" if frozen_review_agents is None
                    else "；替补必须属于冻结审查名单且该席位尚未完成"
                )
                raise ValueError(
                    "failed Agent has no current healthy probe; 请先在 Agent 池重新测试失败的节点"
                    + guidance
                )
            generation = int(task["resume_generation"] or 0) + 1
            max_attempts = max(int(task["max_attempts"]), int(task["attempts"]) + 1)
            resumed_from = str(task["status"])
            conn.execute(
                """UPDATE tasks SET status='retry',run_after=?,lease_until=NULL,last_error=NULL,
                       resume_generation=?,max_attempts=?,updated_at=? WHERE id=?""",
                (now, generation, max_attempts, now, task_id),
            )
            self._append_event_tx(
                conn,
                task_id,
                "task.resumed",
                {
                    "freeze_hash": task["freeze_hash"],
                    "resume_generation": generation,
                    "resumed_from": resumed_from,
                    "resumed_by": "explicit_user_command",
                },
            )
            result = {
                "task_id": task_id,
                "status": "retry",
                "freeze_hash": task["freeze_hash"],
                "resume_generation": generation,
                "resumed_from": resumed_from,
            }
            budget_agents = sorted({
                str(item.get("agent"))
                for item in frozen_failures
                if item.get("output_limit_exhausted") is True and item.get("agent")
            })
            if budget_agents:
                result["output_budget_exhausted"] = True
                result["warning"] = (
                    "输出预算耗尽说明任务过大，不是后台损坏。原样恢复会把同一个超大任务再发一次，"
                    "很可能再次超限；建议先拆小任务。涉及 Agent：" + "、".join(budget_agents)
                )
            return result

    def abandon_task(self, task_id: str, expected_freeze_hash: Optional[str]) -> Dict[str, Any]:
        """用户放弃一个冻结任务：结束为 failed，冻结记录和已有结论原样保留，不再运行。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["status"] not in {"frozen_paused", "resume_ready"}:
                raise ValueError("task is not frozen")
            if not expected_freeze_hash:
                raise ValueError("explicit freeze hash is required")
            if task["freeze_hash"] != expected_freeze_hash:
                raise ValueError("freeze hash mismatch")
            self._verify_frozen_record_integrity(task)
            now = time.time()
            released = []
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "experience_validation_records" in tables:
                attempts = conn.execute(
                    "SELECT * FROM experience_validation_records WHERE task_id=? AND status='running'",
                    (task_id,),
                ).fetchall()
                for attempt in attempts:
                    link = conn.execute(
                        "SELECT 1 FROM events WHERE task_id=? AND event_type='memory.trial_linked' "
                        "AND json_extract(payload_json,'$.task_id')=? "
                        "AND json_extract(payload_json,'$.attempt_id')=? LIMIT 1",
                        (attempt["candidate_id"], task_id, attempt["id"]),
                    ).fetchone()
                    lineage = conn.execute(
                        "SELECT * FROM experience_validation_lineages WHERE candidate_id=?",
                        (attempt["candidate_id"],),
                    ).fetchone() if "experience_validation_lineages" in tables else None
                    if not link or not lineage or lineage["current_round"] != attempt["round"]:
                        raise ValueError("frozen trial task has no matching validation attempt binding")
                    conn.execute(
                        "UPDATE experience_validation_records SET status='pending',task_id=NULL,plan_digest=NULL,"
                        "execution_digest=NULL,pause_reason=NULL,failure_class=NULL,dsh_receipt_id=NULL,"
                        "peer_receipt_id=NULL,dsh_verdict=NULL,peer_verdict=NULL,updated_at=? WHERE id=?",
                        (now, attempt["id"]),
                    )
                    next_state = f"pending_round_{attempt['round']}"
                    conn.execute(
                        "UPDATE experience_validation_lineages SET lineage_status=?,updated_at=? WHERE candidate_id=?",
                        (next_state, now, attempt["candidate_id"]),
                    )
                    released.append({"candidate_id": attempt["candidate_id"], "attempt_id": attempt["id"],
                                     "round": attempt["round"], "lineage_status": next_state})
            conn.execute(
                "UPDATE tasks SET status='failed',lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
                ("用户放弃：冻结后不再恢复", now, task_id),
            )
            self._append_event_tx(conn, task_id, "task.abandoned", {
                "freeze_hash": task["freeze_hash"], "abandoned_from": str(task["status"]),
                "abandoned_by": "explicit_user_command",
            })
            for item in released:
                self._append_event_tx(conn, item["candidate_id"], "memory.validation_attempt_released", {
                    "attempt_id": item["attempt_id"], "round": item["round"],
                    "abandoned_task_id": task_id, "freeze_hash": task["freeze_hash"],
                    "rounds_used": conn.execute(
                        "SELECT rounds_used FROM experience_validation_lineages WHERE candidate_id=?",
                        (item["candidate_id"],),
                    ).fetchone()[0],
                })
            return {"task_id": task_id, "status": "failed", "abandoned": True,
                    "freeze_hash": task["freeze_hash"], "abandoned_from": str(task["status"]),
                    "validation_attempts_released": released}

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        """用户取消一个还在排队、没有开始的任务；正在跑的不取消。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise KeyError(task_id)
            if task["status"] not in {"pending", "retry"}:
                raise ValueError("only a queued task can be cancelled; this one is " + str(task["status"]))
            now = time.time()
            conn.execute(
                "UPDATE tasks SET status='failed',lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
                ("用户取消：排队中，没有开始", now, task_id),
            )
            self._append_event_tx(conn, task_id, "task.cancelled", {
                "cancelled_from": str(task["status"]), "cancelled_by": "explicit_user_command"})
            return {"task_id": task_id, "status": "failed", "cancelled": True, "cancelled_from": str(task["status"])}

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row else None

    def list_tasks(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.read_only() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))]

    def task_runs(self, task_id: str) -> List[Dict[str, Any]]:
        with self.read_only() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM agent_runs WHERE task_id=? ORDER BY created_at", (task_id,))]

    def latest_agent_runs(self, task_id: str) -> Dict[str, Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT ar.* FROM agent_runs ar
                   JOIN (SELECT agent_name, MAX(created_at) AS latest
                         FROM agent_runs WHERE task_id=? GROUP BY agent_name) picked
                     ON picked.agent_name=ar.agent_name AND picked.latest=ar.created_at
                   WHERE ar.task_id=?""",
                (task_id, task_id),
            ).fetchall()
            return {str(row["agent_name"]): dict(row) for row in rows}

    def agent_invocation_count(self, task_id: str) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM agent_runs WHERE task_id=?", (task_id,)).fetchone()[0])
