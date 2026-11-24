"""W4-P3: EventStore event-chain, schema, connection, session, route, and notification methods."""
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


class EventChainStoreMixin:
    DATABASE_SCHEMA_VERSION = 7

    def __init__(self, path: Path):
        self.path = path
        self.workday_root = None

    def _secure_permissions(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Avoid an unnecessary chmod on every database access. On macOS, a
        # restored directory can already have the required mode while the
        # process is still denied a redundant metadata mutation.
        try:
            if (self.path.parent.stat().st_mode & 0o777) != 0o700:
                os.chmod(self.path.parent, 0o700)
        except OSError:
            if (self.path.parent.stat().st_mode & 0o777) != 0o700:
                raise
        for candidate in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            # SQLite removes -wal/-shm when another thread's last connection
            # closes, so a sidecar can vanish between any two of these calls.
            # A file that is gone needs no hardening; before v1.6.5 the second
            # stat() below raised FileNotFoundError and failed that agent run
            # as an unknown internal fault.
            try:
                if (candidate.stat().st_mode & 0o777) != 0o600:
                    os.chmod(candidate, 0o600)
            except FileNotFoundError:
                continue
            except OSError:
                try:
                    mode = candidate.stat().st_mode & 0o777
                except FileNotFoundError:
                    continue
                if mode != 0o600:
                    raise

    @contextmanager
    def connect(self, *, refresh_views: bool = True, timeout: float = 30) -> Iterator[sqlite3.Connection]:
        self._secure_permissions()
        conn = sqlite3.connect(str(self.path), timeout=timeout)
        self._secure_permissions()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            changed = conn.total_changes > 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            self._secure_permissions()
        if changed and refresh_views:
            try:
                from .workday_ledger import refresh_after_write
                refresh_after_write(self)
            except Exception:
                # A committed business transaction must never look rolled back
                # because a derived-view observer (including its import) failed.
                try:
                    from .workday_ledger import warn
                    warn(self,'marker_failed')
                except Exception:pass

    @contextmanager
    def read_only(self) -> Iterator[sqlite3.Connection]:
        uri = f"file:{self.path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _migration_lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        lock_path = Path(str(self.path) + ".migrate.lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def schema_status(self) -> Dict[str, Any]:
        required_tables = {
            "tasks", "agent_runs", "events", "strategies", "evaluation_cases",
            "evaluations", "evaluation_runs", "promotion_gates", "strategy_audit",
            "work_sessions", "route_decisions", "memory_candidates", "memory_reviews",
            "memory_reuse", "agent_health", "repair_jobs", "notifications",
            "constraint_holds",
        }
        required_task_columns = {
            "execution_snapshot_json", "execution_snapshot_hash", "execution_mode", "idempotency_key",
            "session_id", "route_decision_id", "provenance_json",
            "freeze_json", "freeze_hash", "failure_class", "resume_generation",
        }
        required_run_columns = {"failure_class"}
        required_repair_columns = {"lease_until"}
        required_review_columns = {"candidate_sha256"}
        required_route_columns = {"policy_version"}
        if not self.path.is_file():
            return {
                "ok": False,
                "current_version": 0,
                "expected_version": self.DATABASE_SCHEMA_VERSION,
                "problems": ["database_missing"],
            }
        try:
            with self.read_only() as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")} if "tasks" in tables else set()
                run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_runs)")} if "agent_runs" in tables else set()
                review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_reviews)")} if "memory_reviews" in tables else set()
                route_columns = {row["name"] for row in conn.execute("PRAGMA table_info(route_decisions)")} if "route_decisions" in tables else set()
                repair_columns = {row["name"] for row in conn.execute("PRAGMA table_info(repair_jobs)")} if "repair_jobs" in tables else set()
        except (sqlite3.Error, OSError) as exc:
            return {
                "ok": False,
                "current_version": None,
                "expected_version": self.DATABASE_SCHEMA_VERSION,
                "problems": [str(exc)],
            }
        problems = []
        missing_tables = sorted(required_tables - tables)
        missing_columns = sorted(required_task_columns - task_columns)
        missing_run_columns = sorted(required_run_columns - run_columns)
        missing_review_columns = sorted(required_review_columns - review_columns)
        missing_route_columns = sorted(required_route_columns - route_columns)
        missing_repair_columns = sorted(required_repair_columns - repair_columns)
        if version != self.DATABASE_SCHEMA_VERSION:
            problems.append(f"schema_version={version}, expected={self.DATABASE_SCHEMA_VERSION}")
        if missing_tables:
            problems.append("missing_tables=" + ",".join(missing_tables))
        if missing_columns:
            problems.append("missing_task_columns=" + ",".join(missing_columns))
        if missing_run_columns:
            problems.append("missing_agent_run_columns=" + ",".join(missing_run_columns))
        if missing_review_columns:
            problems.append("missing_review_columns=" + ",".join(missing_review_columns))
        if missing_route_columns:
            problems.append("missing_route_columns=" + ",".join(missing_route_columns))
        if missing_repair_columns:
            problems.append("missing_repair_columns=" + ",".join(missing_repair_columns))
        return {
            "ok": not problems,
            "current_version": version,
            "expected_version": self.DATABASE_SCHEMA_VERSION,
            "problems": problems,
        }

    def initialize(self) -> None:
        # A filesystem lock serializes schema changes across independent CLI and
        # Worker processes. The schema is re-read only after acquiring the lock.
        with self._migration_lock(), self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','running','retry','frozen_paused','resume_ready','completed','failed'
                    )),
                    priority INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    run_after REAL NOT NULL,
                    lease_until REAL,
                    strategy_version INTEGER NOT NULL DEFAULT 1,
                    result TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    execution_snapshot_json TEXT,
                    execution_snapshot_hash TEXT,
                    execution_mode TEXT NOT NULL DEFAULT 'production',
                    idempotency_key TEXT,
                    session_id TEXT,
                    route_decision_id TEXT,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    freeze_json TEXT,
                    freeze_hash TEXT,
                    failure_class TEXT,
                    resume_generation INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_ready ON tasks(status, run_after, priority, created_at);
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    agent_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    output TEXT,
                    error TEXT,
                    failure_class TEXT,
                    latency_ms INTEGER,
                    created_at REAL NOT NULL,
                    finished_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT UNIQUE NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_event_type_seq
                    ON events(event_type, sequence);
                CREATE INDEX IF NOT EXISTS idx_events_task_id_type_seq
                    ON events(task_id, event_type, sequence);
                CREATE TABLE IF NOT EXISTS strategies (
                    version INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL CHECK(status IN ('candidate','active','retained','retired','rejected')),
                    body_json TEXT NOT NULL,
                    score REAL NOT NULL,
                    sample_count INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            # Additive migrations keep existing production databases readable and writable.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            for name, definition in (
                ("execution_snapshot_json", "TEXT"),
                ("execution_snapshot_hash", "TEXT"),
                ("execution_mode", "TEXT NOT NULL DEFAULT 'production'"),
                ("idempotency_key", "TEXT"),
                ("session_id", "TEXT"),
                ("route_decision_id", "TEXT"),
                ("provenance_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("freeze_json", "TEXT"),
                ("freeze_hash", "TEXT"),
                ("failure_class", "TEXT"),
                ("resume_generation", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
            # SQLite cannot ALTER a CHECK constraint. Rebuild only legacy task
            # tables so v1.4 recovery states are real persisted states.
            task_sql = str(
                conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
                ).fetchone()[0]
            )
            if "frozen_paused" not in task_sql or "resume_ready" not in task_sql:
                columns = [row["name"] for row in conn.execute("PRAGMA table_info(tasks)")]
                conn.commit()
                conn.execute("PRAGMA foreign_keys=OFF")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """CREATE TABLE tasks_v6 (
                        id TEXT PRIMARY KEY,
                        prompt TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN (
                            'pending','running','retry','frozen_paused','resume_ready','completed','failed'
                        )),
                        priority INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL,
                        run_after REAL NOT NULL,
                        lease_until REAL,
                        strategy_version INTEGER NOT NULL DEFAULT 1,
                        result TEXT,
                        last_error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        execution_snapshot_json TEXT,
                        execution_snapshot_hash TEXT,
                        execution_mode TEXT NOT NULL DEFAULT 'production',
                        idempotency_key TEXT,
                        session_id TEXT,
                        route_decision_id TEXT,
                        provenance_json TEXT NOT NULL DEFAULT '{}',
                        freeze_json TEXT,
                        freeze_hash TEXT,
                        failure_class TEXT,
                        resume_generation INTEGER NOT NULL DEFAULT 0
                    )"""
                )
                projected = ",".join(columns)
                conn.execute(f"INSERT INTO tasks_v6({projected}) SELECT {projected} FROM tasks")
                conn.execute("DROP TABLE tasks")
                conn.execute("ALTER TABLE tasks_v6 RENAME TO tasks")
                conn.execute(
                    "CREATE INDEX idx_tasks_ready ON tasks(status,run_after,priority,created_at)"
                )
                conn.execute(
                    "CREATE UNIQUE INDEX idx_tasks_idempotency ON tasks(idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
                conn.commit()
                conn.execute("PRAGMA foreign_keys=ON")
                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise RuntimeError("schema v6 migration produced foreign-key violations")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency ON tasks(idempotency_key) WHERE idempotency_key IS NOT NULL")
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agent_runs)")}
            if "failure_class" not in run_columns:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN failure_class TEXT")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS evaluation_cases (
                    id TEXT PRIMARY KEY, dataset TEXT NOT NULL, prompt TEXT NOT NULL,
                    expected_json TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evaluation_cases_dataset ON evaluation_cases(dataset);
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('offline','shadow','replay','regression')),
                    dataset TEXT, case_id TEXT, prompt TEXT NOT NULL, output TEXT, judge_json TEXT,
                    strategy_version INTEGER, status TEXT NOT NULL, source_task_id TEXT,
                    created_at REAL NOT NULL, finished_at REAL
                );
                CREATE TABLE IF NOT EXISTS evaluation_runs (
                    id TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL REFERENCES evaluations(id),
                    agent_name TEXT NOT NULL, output TEXT, status TEXT NOT NULL, latency_ms INTEGER,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promotion_gates (
                    id TEXT PRIMARY KEY, strategy_version INTEGER NOT NULL, gate_type TEXT NOT NULL,
                    status TEXT NOT NULL, details_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_audit (
                    id TEXT PRIMARY KEY, strategy_version INTEGER NOT NULL, action TEXT NOT NULL,
                    reason TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_sessions (
                    id TEXT PRIMARY KEY,
                    local_date TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','closing','closed')),
                    created_at REAL NOT NULL,
                    closing_at REAL,
                    closed_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_work_sessions_one_active
                    ON work_sessions(status) WHERE status='active';
                CREATE INDEX IF NOT EXISTS idx_work_sessions_date ON work_sessions(local_date, created_at);
                CREATE TABLE IF NOT EXISTS route_decisions (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    message_id TEXT,
                    active_task_id TEXT,
                    prompt_sha256 TEXT NOT NULL,
                    prompt_length INTEGER NOT NULL,
                    route TEXT NOT NULL CHECK(route IN ('direct','agentos_multi','review','ask_user')),
                    policy_version TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    signals_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_route_message
                    ON route_decisions(message_id) WHERE message_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_route_session ON route_decisions(session_id, created_at);
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    status TEXT NOT NULL CHECK(status IN (
                        'pending_delivery_review','delivery_approved','delivery_rejected',
                        'revision_requested','pending_final_review','final_rejected',
                        'published','revalidation_required','retired','publication_failed'
                    )),
                    staged_path TEXT NOT NULL,
                    published_path TEXT,
                    evidence_sha256 TEXT NOT NULL,
                    source_task_ids_json TEXT NOT NULL DEFAULT '[]',
                    reuse_count INTEGER NOT NULL DEFAULT 0,
                    successful_reuse_count INTEGER NOT NULL DEFAULT 0,
                    failed_reuse_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    published_at REAL,
                    last_validated_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_candidates_status
                    ON memory_candidates(status, updated_at);
                CREATE TABLE IF NOT EXISTS memory_reviews (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    stage TEXT NOT NULL CHECK(stage IN ('delivery','final')),
                    decision TEXT NOT NULL CHECK(decision IN ('approve','reject','revise','retire')),
                    note TEXT NOT NULL,
                    candidate_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_memory_reviews_task ON memory_reviews(task_id, created_at);
                CREATE TABLE IF NOT EXISTS memory_reuse (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    source_task_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('pending','validated','failed')),
                    created_at REAL NOT NULL,
                    decided_at REAL,
                    UNIQUE(task_id, source_task_id)
                );
                CREATE TABLE IF NOT EXISTS agent_health (
                    agent_name TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK(status IN (
                        'unknown','healthy','degraded','repairing','external_blocked'
                    )),
                    failure_class TEXT,
                    error TEXT,
                    provider TEXT,
                    model TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    last_probe_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS repair_jobs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT REFERENCES tasks(id),
                    failed_agent TEXT NOT NULL,
                    repair_agent TEXT,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','running','completed','external_blocked','candidate_required','failed'
                    )),
                    failure_class TEXT NOT NULL,
                    error TEXT NOT NULL,
                    diagnosis TEXT,
                    repair_action TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    run_after REAL NOT NULL,
                    lease_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_repair_jobs_ready
                    ON repair_jobs(status,run_after,created_at);
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    task_id TEXT,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','acknowledged')),
                    created_at REAL NOT NULL,
                    acknowledged_at REAL
                );
            CREATE INDEX IF NOT EXISTS idx_notifications_pending
            ON notifications(status,created_at);
            CREATE TABLE IF NOT EXISTS constraint_holds (
                task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                result TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','released')),
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                released_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_constraint_holds_status
            ON constraint_holds(status,created_at);
            """
        )
            review_columns = {row["name"] for row in conn.execute("PRAGMA table_info(memory_reviews)")}
            if "candidate_sha256" not in review_columns:
                conn.execute("ALTER TABLE memory_reviews ADD COLUMN candidate_sha256 TEXT")
            route_columns = {row["name"] for row in conn.execute("PRAGMA table_info(route_decisions)")}
            if "policy_version" not in route_columns:
                conn.execute("ALTER TABLE route_decisions ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'v1.3'")
            repair_columns = {row["name"] for row in conn.execute("PRAGMA table_info(repair_jobs)")}
            if "lease_until" not in repair_columns:
                conn.execute("ALTER TABLE repair_jobs ADD COLUMN lease_until REAL")
            # Strategy evolution was retired by the user on 2026-10-11. Its six
            # tables stay in the schema as read-only history, the same treatment
            # the retired repair history got: nothing in this build reads or
            # writes them, but keeping them means a database this build has
            # touched still passes the v1.6.2 self-check at schema version 7, so
            # a release can be rolled back without losing rows. The only
            # behaviour a brand new database sees differently is that no initial
            # strategy row and no `strategy.initialized` event are written any
            # more; no code reads either.
            conn.execute(f"PRAGMA user_version={self.DATABASE_SCHEMA_VERSION}")

        self.recover_discarded_candidate_cleanup()

    @staticmethod
    def _append_event_tx(conn: sqlite3.Connection, task_id: Optional[str], event_type: str, payload: Any) -> str:
        # Hold SQLite's writer reservation before reading the chain head.
        # A deferred transaction allows concurrent writers to select the same parent.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        now = time.time()
        if task_id and event_type == 'memory.candidate_staged':
            candidate = conn.execute('SELECT staged_path FROM memory_candidates WHERE task_id=?',(task_id,)).fetchone()
            if candidate:payload = dict(payload, staged_path=candidate[0])
        row = conn.execute("SELECT event_hash FROM events ORDER BY sequence DESC LIMIT 1").fetchone()
        prev_hash = row[0] if row else "0" * 64
        event_id = str(uuid.uuid4())
        payload_json = _canonical(payload)
        material = f"{prev_hash}|{event_id}|{task_id or ''}|{event_type}|{payload_json}|{now:.6f}"
        event_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO events(event_id,task_id,event_type,payload_json,created_at,prev_hash,event_hash) VALUES(?,?,?,?,?,?,?)",
            (event_id, task_id, event_type, payload_json, now, prev_hash, event_hash),
        )
        # All host/Worker/audit completion and terminal abandonment paths share
        # this transaction boundary. Score events do not re-enter settlement.
        if task_id and event_type in {'task.completed', 'task.failed', 'task.abandoned', 'task.cancelled'}:
            from .task_scores import settlement_tx
            settlement_tx(conn, task_id, event_type == 'task.completed', now)
        return event_id

    def append_event(self, task_id: Optional[str], event_type: str, payload: Any) -> str:
        if event_type in _PROTECTED_PUBLIC_EVENTS:
            raise ValueError(f"{event_type} 必须由对应的事务业务入口写入")
        with self.connect() as conn:
            return self._append_event_tx(conn, task_id, event_type, payload)

    def start_session(self, timezone: str = "Asia/Shanghai") -> Dict[str, Any]:
        try:
            zone = ZoneInfo(timezone)
        except Exception as exc:
            raise ValueError(f"invalid timezone: {timezone}") from exc
        now = time.time()
        local_date = datetime.fromtimestamp(now, zone).date().isoformat()
        with self.connect() as conn:
            self._close_resolved_sessions_tx(conn)
            value_queue_status = self._session_value_queue_status_tx(conn)
            existing = conn.execute(
                "SELECT * FROM work_sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if existing and existing["local_date"] == local_date:
                return {**dict(existing), "value_queue_status": value_queue_status}
            if existing:
                unresolved = self._unresolved_session_count_tx(conn, existing["id"])
                rollover_status = "closing" if unresolved is None or unresolved else "closed"
                conn.execute(
                    "UPDATE work_sessions SET status=?,closing_at=?,closed_at=? WHERE id=?",
                    (rollover_status, now, now if rollover_status == "closed" else None, existing["id"]),
                )
                self._append_event_tx(
                    conn,
                    None,
                    "session.rolled_over",
                    {
                        "session_id": existing["id"],
                        "from_local_date": existing["local_date"],
                        "to_local_date": local_date,
                        "status": rollover_status,
                        "unresolved": unresolved,
                        "value_queue_status": value_queue_status,
                    },
                )
            session_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO work_sessions(id,local_date,timezone,status,created_at) VALUES(?,?,?,?,?)",
                (session_id, local_date, timezone, "active", now),
            )
            self._append_event_tx(conn, None, "session.started", {"session_id": session_id, "local_date": local_date})
            return {**dict(conn.execute("SELECT * FROM work_sessions WHERE id=?", (session_id,)).fetchone()),
                    "value_queue_status": value_queue_status}

    _UNRESOLVED_SESSION_SQL = """SELECT COUNT(*) FROM tasks t LEFT JOIN memory_candidates mc ON mc.task_id=t.id
               WHERE t.session_id=? AND (
                   t.status IN ('pending','running','retry','frozen_paused','resume_ready') OR
                   mc.status IN ('pending_delivery_review','delivery_approved','revision_requested',
                                 'pending_final_review','publication_failed') OR
                   EXISTS (
                       SELECT 1 FROM constraint_holds ch
                       WHERE ch.task_id=t.id AND ch.status='pending'
                   )
               )"""

    @staticmethod
    def _session_value_queue_status_tx(conn: sqlite3.Connection) -> Dict[str, Any]:
        # Session bookkeeping must remain available when the event-derived
        # queue cannot be trusted. This does not relax dispatch or approval.
        if not EventChainStoreMixin.verify_event_chain_tx(conn)["ok"]:
            return {"ok": False, "status": "unverifiable",
                    "warning": "价值队列无法验证：审计事件链损坏，待评分状态未知；会话保留为待处理"}
        return {"ok": True, "status": "available"}

    def session_value_queue_status(self) -> Dict[str, Any]:
        with self.read_only() as conn:
            return self._session_value_queue_status_tx(conn)

    def _unresolved_session_count_tx(self, conn: sqlite3.Connection, session_id: str) -> Optional[int]:
        count = conn.execute(self._UNRESOLVED_SESSION_SQL, (session_id,)).fetchone()[0]
        if not self._session_value_queue_status_tx(conn)["ok"]:
            return None  # Unknown is not an empty queue; never auto-close it.
        from .value_queue import pending_items
        session_tasks = {row["id"] for row in conn.execute(
            "SELECT id FROM tasks WHERE session_id=?", (session_id,))}
        queued = {item["task_id"] for item in pending_items(conn)
                  if item["task_id"] in session_tasks}
        return int(count) + len(queued)

    def _close_resolved_sessions_tx(self, conn: sqlite3.Connection) -> List[str]:
        closed: List[str] = []
        now = time.time()
        for row in conn.execute("SELECT id FROM work_sessions WHERE status='closing'").fetchall():
            unresolved = self._unresolved_session_count_tx(conn, row["id"])
            if unresolved is None or unresolved:
                continue
            if conn.execute(
                "UPDATE work_sessions SET status='closed',closed_at=? WHERE id=? AND status='closing'",
                (now, row["id"]),
            ).rowcount:
                self._append_event_tx(conn, None, "session.closed", {"session_id": row["id"], "reason": "resolved"})
                closed.append(str(row["id"]))
        return closed

    def close_resolved_sessions(self) -> List[str]:
        """Close every closing session that has nothing left to review or run.

        A session used to leave "closing" only when one of its candidates was
        reviewed, so a session that entered closing with nothing pending, or
        whose last item resolved some other way, stayed there forever.
        """
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._close_resolved_sessions_tx(conn)

    def active_session(self) -> Optional[Dict[str, Any]]:
        with self.read_only() as conn:
            row = conn.execute(
                "SELECT * FROM work_sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            return dict(row) if row else None

    def record_route(
        self,
        *,
        prompt: str,
        route: str,
        reason_code: str,
        confidence: float,
        signals: Dict[str, Any],
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
        active_task_id: Optional[str] = None,
        policy_version: str = "v1.3",
    ) -> Dict[str, Any]:
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self.connect() as conn:
            if message_id:
                existing = conn.execute(
                    "SELECT * FROM route_decisions WHERE message_id=?", (message_id,)
                ).fetchone()
                if existing:
                    if (
                        existing["prompt_sha256"] != prompt_hash
                        or existing["route"] != route
                        or existing["policy_version"] != policy_version
                        or existing["reason_code"] != reason_code
                        or existing["signals_json"] != _canonical(signals)
                        or existing["session_id"] != session_id
                        or existing["active_task_id"] != active_task_id
                    ):
                        raise ValueError("message_id conflicts with an existing route decision")
                    return dict(existing)
            route_id = str(uuid.uuid4())
            now = time.time()
            try:
                conn.execute(
                "INSERT INTO route_decisions(id,session_id,message_id,active_task_id,prompt_sha256,prompt_length,route,policy_version,reason_code,confidence,signals_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        route_id, session_id, message_id, active_task_id, prompt_hash, len(prompt),
                        route, policy_version, reason_code, float(confidence), _canonical(signals), now,
                    ),
                )
            except sqlite3.IntegrityError:
                if not message_id:
                    raise
                existing = conn.execute("SELECT * FROM route_decisions WHERE message_id=?", (message_id,)).fetchone()
                if not existing or (
                    existing["prompt_sha256"] != prompt_hash
                    or existing["route"] != route
                    or existing["policy_version"] != policy_version
                    or existing["reason_code"] != reason_code
                    or existing["signals_json"] != _canonical(signals)
                    or existing["session_id"] != session_id
                    or existing["active_task_id"] != active_task_id
                ):
                    raise ValueError("message_id conflicts with an existing route decision")
                return dict(existing)
            self._append_event_tx(
                conn,
                active_task_id,
                "route.decided",
                {
                    "route_id": route_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "route": route,
                    "policy_version": policy_version,
                    "reason_code": reason_code,
                    "confidence": float(confidence),
                    "prompt_sha256": prompt_hash,
                },
            )
            return dict(conn.execute("SELECT * FROM route_decisions WHERE id=?", (route_id,)).fetchone())

    def get_route(self, route_id: str) -> Optional[Dict[str, Any]]:
        with self.read_only() as conn:
            row = conn.execute("SELECT * FROM route_decisions WHERE id=?", (route_id,)).fetchone()
            return dict(row) if row else None

    def session_review_packet(self, session_id: str) -> Dict[str, Any]:
        with self.connect() as conn:
            session = conn.execute("SELECT * FROM work_sessions WHERE id=?", (session_id,)).fetchone()
            if not session:
                raise KeyError(session_id)
            rows = conn.execute(
            """SELECT t.id,t.prompt,t.status,t.result,t.provenance_json,
                      mc.status AS memory_status, ch.status AS constraint_hold_status,
                      ch.details_json AS constraint_hold_details,
                      mc.reuse_count,mc.successful_reuse_count,mc.failed_reuse_count
               FROM tasks t LEFT JOIN memory_candidates mc ON mc.task_id=t.id
               LEFT JOIN constraint_holds ch ON ch.task_id=t.id
                   WHERE t.session_id=? ORDER BY t.created_at""",
                (session_id,),
            ).fetchall()
            routes = [
                dict(row) for row in conn.execute(
                    """SELECT id,message_id,active_task_id,prompt_sha256,prompt_length,
                              route,policy_version,reason_code,confidence,created_at
                       FROM route_decisions WHERE session_id=? ORDER BY created_at""",
                    (session_id,),
                )
            ]
            from .reuse_scores import projection_tx
            formal_scores = projection_tx(conn)
            rows = [dict(row) for row in rows]
            for row in rows:
                row["legacy_reuse_count"] = int(row.get("reuse_count") or 0)
                row["formal_reuse_score"] = int((formal_scores.get(row["id"]) or {}).get("score", 0))
                if row["constraint_hold_details"]:
                    try:
                        row["constraint_hold_details"] = json.loads(row["constraint_hold_details"])
                    except json.JSONDecodeError:
                        row["constraint_hold_details"] = {}
            pending_holds = sum(1 for row in rows if row["constraint_hold_status"] == "pending")
            pending_final = sum(
                1 for row in rows if row["memory_status"] in {"delivery_approved", "publication_failed"}
            )
            value_queue_status = self._session_value_queue_status_tx(conn)
            from .value_queue import pending_items
            value_items = pending_items(conn) if value_queue_status["ok"] else None
            value_by_task = {
                item["task_id"]: item for item in value_items or []
                if item["task_id"] and item.get("queue_status") == "awaiting_value_review"
            }
            pending_value_reviews = (sum(1 for row in rows if row["id"] in value_by_task)
                                     if value_items is not None else None)
            known_unresolved_tasks = conn.execute(self._UNRESOLVED_SESSION_SQL, (session_id,)).fetchone()[0]
            now = time.time()
            conn.execute(
                "UPDATE work_sessions SET status='closing',closing_at=? WHERE id=? AND status='active'",
                (now, session_id),
            )
            self._append_event_tx(
                conn, None, "session.closing",
                {
                    "session_id": session_id,
                    "tasks": len(rows),
                    "routes": len(routes),
                    "pending_final_reviews": pending_final,
                    "pending_constraint_holds": pending_holds,
                    "pending_value_reviews": pending_value_reviews,
                    "value_queue_status": value_queue_status,
                },
            )
            tasks = [dict(row) for row in rows]
            for item in tasks:
                value_item = value_by_task.get(item["id"])
                item["memory_value_status"] = ("unverifiable" if value_items is None
                                               else "awaiting_value_review" if value_item else None)
                item["memory_value_reason"] = (value_queue_status["warning"] if value_items is None
                                               else value_item["reason"] if value_item else None)
                item["agent_runs"] = [
                    dict(row) for row in conn.execute(
                        "SELECT agent_name,status,output,error,latency_ms FROM agent_runs WHERE task_id=? ORDER BY created_at",
                        (item["id"],),
                    )
                ]
                from .legacy_compat import read_latest_judge_event
                item["judge"] = read_latest_judge_event(conn, item["id"])
                item["reviews"] = [
                    dict(row) for row in conn.execute(
                        "SELECT stage,decision,note,candidate_sha256,created_at FROM memory_reviews WHERE task_id=? ORDER BY created_at",
                        (item["id"],),
                    )
                ]
            closed_sessions = self._close_resolved_sessions_tx(conn)
            current = conn.execute("SELECT * FROM work_sessions WHERE id=?", (session_id,)).fetchone()
            return {
                "session": dict(current or session),
                "routes": routes,
                "tasks": tasks,
                "pending_final_reviews": pending_final,
                "pending_value_reviews": pending_value_reviews,
                "value_queue_status": value_queue_status,
                "known_unresolved_tasks": int(known_unresolved_tasks),
                "closed_sessions": closed_sessions,
                "reuse_metric_glossary": dict(REUSE_METRIC_GLOSSARY),
            }

    def repair_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Historical peer-repair rows, read-only.

        The write path is gone with peer auto-repair, so this only ever reports
        what earlier versions recorded. Rows, statuses and audit events are left
        exactly as they are; nothing requeues, rewrites or terminates them.
        """
        with self.read_only() as conn:
            return [
                dict(row) for row in conn.execute(
                    "SELECT * FROM repair_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                )
            ]

    @staticmethod
    def _pending_notifications_tx(conn):
        return conn.execute(
                """SELECT n.* FROM notifications n WHERE n.status='pending'
                   AND NOT (n.kind='task.recovery_blocked' AND EXISTS (
                     SELECT 1 FROM events e WHERE e.task_id=n.task_id
                     AND e.event_type IN ('task.abandoned','task.cancelled')))
                   ORDER BY n.created_at"""
        ).fetchall()

    def pending_notifications(self, acknowledge: bool = False) -> List[Dict[str, Any]]:
        now = time.time()
        with self.connect() as conn:
            rows = self._pending_notifications_tx(conn)
            result = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(item.pop("payload_json"))
                except json.JSONDecodeError:
                    item["payload"] = {}
                result.append(item)
            if acknowledge and rows:
                conn.executemany(
                    "UPDATE notifications SET status='acknowledged',acknowledged_at=? WHERE id=?",
                    [(now, row["id"]) for row in rows],
                )
            return result

    def create_notification(
        self, kind: str, payload: Dict[str, Any], task_id: Optional[str] = None
    ) -> str:
        with self.connect() as conn:
            payload_json = _canonical(payload)
            existing = conn.execute(
                """SELECT id FROM notifications
                   WHERE kind=? AND COALESCE(task_id,'')=COALESCE(?, '')
                     AND payload_json=? AND status='pending' LIMIT 1""",
                (kind, task_id, payload_json),
            ).fetchone()
            if existing:
                return str(existing["id"])
            notification_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO notifications(id,kind,task_id,payload_json,status,created_at) VALUES(?,?,?,?,?,?)",
                (notification_id, kind, task_id, payload_json, "pending", time.time()),
            )
            self._append_event_tx(
                conn, task_id, "notification.created", {"notification_id": notification_id, "kind": kind}
            )
        return notification_id

    def latest_route_resolved(self, agent: str) -> Optional[Dict[str, Any]]:
        with self.read_only() as conn:
            rows = conn.execute(
                "SELECT task_id,payload_json,created_at FROM events "
                "WHERE event_type='agent.route_resolved' ORDER BY sequence DESC"
            ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                continue
            if payload.get("agent") == agent:
                return {"task_id": row["task_id"], "created_at": row["created_at"], **payload}
        return None

    def summary(self) -> Dict[str, Any]:
        with self.read_only() as conn:
            counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM tasks GROUP BY status")}
            route_counts = {r["route"]: r["n"] for r in conn.execute("SELECT route,COUNT(*) n FROM route_decisions GROUP BY route")}
            memory_counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM memory_candidates GROUP BY status")}
            repair_counts = {r["status"]: r["n"] for r in conn.execute("SELECT status,COUNT(*) n FROM repair_jobs GROUP BY status")}
            session = conn.execute("SELECT * FROM work_sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1").fetchone()
            return {
                "tasks": counts,
                "routes": route_counts,
                "memory_candidates": memory_counts,
                "repairs": repair_counts,
                "pending_notifications": len(self._pending_notifications_tx(conn)),
                "pending_constraint_holds": conn.execute(
                    "SELECT COUNT(*) FROM constraint_holds WHERE status='pending'"
                ).fetchone()[0],
                "active_session": dict(session) if session else None,
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                "database_schema_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
            }

    def verify_event_chain(self) -> Dict[str, Any]:
        with self.read_only() as conn:
            return self.verify_event_chain_tx(conn)

    @staticmethod
    def verify_event_chain_tx(conn) -> Dict[str, Any]:
        rows = conn.execute("SELECT * FROM events ORDER BY sequence").fetchall()
        expected_prev = "0" * 64
        for row in rows:
            if row["prev_hash"] != expected_prev:
                return {"ok": False, "sequence": row["sequence"], "reason": "prev_hash mismatch"}
            material = f"{row['prev_hash']}|{row['event_id']}|{row['task_id'] or ''}|{row['event_type']}|{row['payload_json']}|{row['created_at']:.6f}"
            actual = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if actual != row["event_hash"]:
                return {"ok": False, "sequence": row["sequence"], "reason": "event_hash mismatch"}
            expected_prev = actual
        return {"ok": True, "events": len(rows), "head": expected_prev}
