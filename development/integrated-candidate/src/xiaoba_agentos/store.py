from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
import fcntl
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional
from zoneinfo import ZoneInfo

from .output_hygiene import detect_non_answer_markers, detect_reasoning_leak


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# Snapshot fields derived at submission time from memory, skills, config and
# the installed release.  They legitimately change between a lost response and
# its retry, so they are not what the caller "submitted" and do not count as
# different content when an idempotency key is replayed.
_DERIVED_SNAPSHOT_KEYS = frozenset({
    "knowlp_context", "knowlp_context_hash", "memory_hits", "retrieval_receipt",
    "agent_memory_contexts", "skills", "skill_context", "skill_context_hash",
    "agents", "worker", "provenance", "release_expected_version",
    "source_manifest_sha256", "trial_suggestions",
})


def _submitted_content(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    content = {key: value for key, value in snapshot.items() if key not in _DERIVED_SNAPSHOT_KEYS}
    # freeze_context stamps the capture time on every call, retries included.
    if isinstance(content.get("task_context"), dict):
        content["task_context"] = {k: v for k, v in content["task_context"].items() if k != "captured_at"}
    return content


# A plain settlement may be used by the direct task path before a worker lease
# exists, or by a leased worker.  Retry/fail-after-retry is deliberately kept
# separate: retry_or_fail only accepts a currently leased running row.
TASK_SETTLEMENT_SOURCE_STATES = frozenset({"pending", "running"})


def b19_write_boundary_reason(text: str) -> Optional[str]:
    """Why `text` may not be written into memory, or None when it is clean.

    Memory is the one place contaminated output must never reach, so the same
    two existing detectors guard both write paths: a candidate is refused when
    it is created, and the staged note is checked again right before
    publication even when it bypassed the first layer.  Only the literal
    signatures in output_hygiene count, so normal prose that merely discusses
    reasoning markers or tool calls passes both layers (MB-019).
    """
    leaked = detect_reasoning_leak(text)
    if leaked:
        return "B19 写入边界：疑似泄露思考过程，命中 " + ", ".join(leaked[:5])
    markers = detect_non_answer_markers(text)
    if markers:
        return "B19 写入边界：疑似未执行的工具调用标记，命中 " + ", ".join(markers[:5])
    return None


# --- Memory candidate lifecycle (provisional -> active promotion) -----------
#
# The task database deliberately does NOT store a parallel
# provisional/active pair.  The candidate lifecycle already models it with
# finer, auditable states:
#
#   provisional (unpublished candidate):
#     pending_delivery_review -> delivery_approved -> pending_final_review
#     (plus delivery_rejected / revision_requested / final_rejected /
#      publication_failed side exits)
#   active (served by retrieval):
#     published
#   quarantined after failed reuse:
#     revalidation_required
#   terminal:
#     retired
#
# MEMORY_CANDIDATE_STATES mirrors the memory_candidates status CHECK
# constraint exactly.  Any change here requires a schema migration.
MEMORY_CANDIDATE_STATES = frozenset({
    "pending_delivery_review", "delivery_approved", "delivery_rejected",
    "revision_requested", "pending_final_review", "final_rejected",
    "published", "revalidation_required", "retired", "publication_failed",
})

# Only these database states participate in active retrieval.  Everything
# else is provisional, quarantined or retired.
MEMORY_ACTIVE_RETRIEVAL_STATES = frozenset({"published"})

# A candidate whose lifecycle already ended keeps its evidence hash, its
# staged path and its review history.  Re-registering one (same hash or a
# different hash) is refused before any row, sidecar or event is written.
MEMORY_TERMINAL_STATES = frozenset({"published", "retired"})


def terminal_reregistration_error(task_id: str, status: str) -> ValueError:
    """The one refusal every caller reports for a frozen candidate.

    Shared so the command line can refuse before it stages a file and the store
    reports the identical reason when it refuses inside its transaction.
    """
    return ValueError(
        f"memory candidate for task {task_id!r} is already {status!r} "
        "and cannot be re-registered"
    )

# Explicit transition table: action -> allowed source states.  The table is
# the single auditable definition of the state machine; every status change
# goes through validate_memory_transition().
MEMORY_CANDIDATE_TRANSITIONS: Dict[str, frozenset] = {
    # delivery review is a user decision on the staged candidate; retirement
    # is terminal, so a retired candidate can never be re-reviewed.
    "delivery_review": frozenset(MEMORY_CANDIDATE_STATES - {"retired"}),
    # final review requires an approved delivery review bound to the same
    # candidate hash (enforced separately in review_memory).
    "final_review": frozenset({
        "delivery_approved", "pending_final_review", "final_rejected",
        "revalidation_required", "published", "publication_failed",
    }),
    # publication settles only after final approval; retrying a failed
    # publication keeps the destination state unchanged.  A failed
    # re-publication of an already published memory also lands here: the
    # memory leaves active retrieval until a human re-approves it.
    "publish": frozenset({"pending_final_review", "publication_failed"}),
    "publication_failed": frozenset({"pending_final_review", "publication_failed", "published"}),
    # database settlement of retirement applies to memories that were (or
    # already are) in the Vault; 'retired' allows idempotent re-settlement.
    "retire": frozenset({"published", "revalidation_required", "retired"}),
    # reuse outcomes only flip between published and quarantined.
    "reuse_failed": frozenset({"published", "revalidation_required"}),
    "reuse_validated": frozenset({"published", "revalidation_required"}),
}

# Statuses a publication may settle from: only a candidate whose final review
# approved it.  Kept identical to MEMORY_CANDIDATE_TRANSITIONS["publish"] so the
# gate and the state machine cannot disagree.
MEMORY_PUBLICATION_READY_STATES: frozenset = MEMORY_CANDIDATE_TRANSITIONS["publish"]

# Statuses the pre-publication gate accepts.  `published` is added only so the
# CLI's idempotent re-verification of an already published note passes through
# the same binding check; such a candidate never settles a second time.
MEMORY_PUBLICATION_GATE_STATES: frozenset = MEMORY_PUBLICATION_READY_STATES | {"published"}

# (stage, decision) -> resulting status for user review decisions.
MEMORY_REVIEW_STATUS_MAP: Dict[tuple, str] = {
    ("delivery", "approve"): "delivery_approved",
    ("delivery", "reject"): "delivery_rejected",
    ("delivery", "revise"): "revision_requested",
    ("final", "approve"): "pending_final_review",
    ("final", "reject"): "final_rejected",
    ("final", "revise"): "revision_requested",
    ("final", "retire"): "retired",
}


def validate_memory_transition(action: str, current: str) -> None:
    """Raise ValueError unless `action` may run from status `current`.

    Keeping this table-driven makes every status change explicit, auditable
    and repeatable instead of implicit in scattered conditionals.
    """
    allowed = MEMORY_CANDIDATE_TRANSITIONS.get(action)
    if allowed is None:
        raise ValueError(f"unknown memory transition action: {action!r}")
    if current not in allowed:
        raise ValueError(
            f"memory transition {action!r} is not allowed from status {current!r}"
        )


class CandidatePublication:
    """The locked window one publication runs inside.

    Yielded by `EventStore.publication_transaction`, which already holds the
    store write lock and has verified that the final approval covers this
    evidence.  The caller writes the Vault note and rebuilds the index while
    that lock is held, then calls `settle` to commit the database side on the
    same connection.  Nothing is committed until the caller's block exits
    cleanly.
    """

    def __init__(self, store: "EventStore", conn: sqlite3.Connection, candidate: sqlite3.Row):
        self._store = store
        self._conn = conn
        self._candidate = candidate

    @property
    def status(self) -> str:
        return str(self._candidate["status"])

    @property
    def evidence_sha256(self) -> str:
        return str(self._candidate["evidence_sha256"])

    def settle(self, published_path: str) -> Dict[str, Any]:
        """Settle the database side of this publication on the locked connection.

        An already published candidate is an idempotent re-verification: the
        note exists and is bound to this evidence, so nothing is settled again.
        """
        if self.status == "published":
            return dict(self._candidate)
        return self._store._mark_memory_published_tx(
            self._conn, str(self._candidate["task_id"]), published_path
        )


class EventStore:
    DATABASE_SCHEMA_VERSION = 7

    def __init__(self, path: Path):
        self.path = path

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
    def connect(self) -> Iterator[sqlite3.Connection]:
        self._secure_permissions()
        conn = sqlite3.connect(str(self.path), timeout=30)
        self._secure_permissions()
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            self._secure_permissions()

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

    @staticmethod
    def _append_event_tx(conn: sqlite3.Connection, task_id: Optional[str], event_type: str, payload: Any) -> str:
        # Hold SQLite's writer reservation before reading the chain head.
        # A deferred transaction allows concurrent writers to select the same parent.
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        now = time.time()
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
        return event_id

    def append_event(self, task_id: Optional[str], event_type: str, payload: Any) -> str:
        with self.connect() as conn:
            return self._append_event_tx(conn, task_id, event_type, payload)

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

    def start_session(self, timezone: str = "Asia/Shanghai") -> Dict[str, Any]:
        try:
            zone = ZoneInfo(timezone)
        except Exception as exc:
            raise ValueError(f"invalid timezone: {timezone}") from exc
        now = time.time()
        local_date = datetime.fromtimestamp(now, zone).date().isoformat()
        with self.connect() as conn:
            self._close_resolved_sessions_tx(conn)
            existing = conn.execute(
                "SELECT * FROM work_sessions WHERE status='active' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if existing and existing["local_date"] == local_date:
                return dict(existing)
            if existing:
                unresolved = conn.execute(
                    """SELECT COUNT(*) FROM tasks t LEFT JOIN memory_candidates mc ON mc.task_id=t.id
                       WHERE t.session_id=? AND (
                           t.status IN ('pending','running','retry','frozen_paused','resume_ready') OR
                           mc.status IN ('pending_delivery_review','delivery_approved','revision_requested',
                                         'pending_final_review','publication_failed') OR
                           EXISTS (
                               SELECT 1 FROM constraint_holds ch
                               WHERE ch.task_id=t.id AND ch.status='pending'
                           )
                       )""",
                    (existing["id"],),
                ).fetchone()[0]
                rollover_status = "closing" if unresolved else "closed"
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
                        "unresolved": int(unresolved),
                    },
                )
            session_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO work_sessions(id,local_date,timezone,status,created_at) VALUES(?,?,?,?,?)",
                (session_id, local_date, timezone, "active", now),
            )
            self._append_event_tx(conn, None, "session.started", {"session_id": session_id, "local_date": local_date})
            return dict(conn.execute("SELECT * FROM work_sessions WHERE id=?", (session_id,)).fetchone())

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

    def _close_resolved_sessions_tx(self, conn: sqlite3.Connection) -> List[str]:
        closed: List[str] = []
        now = time.time()
        for row in conn.execute("SELECT id FROM work_sessions WHERE status='closing'").fetchall():
            if conn.execute(self._UNRESOLVED_SESSION_SQL, (row["id"],)).fetchone()[0]:
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
        experience_kind: Optional[str] = None,
        problem_key: str = "",
    ) -> Dict[str, Any]:
        """Register a candidate written by the host, not produced by a task run.

        One transaction creates the completed host_authored task, its staged
        file record and its audit events, so the worker never sees a pending
        task and the candidate cannot exist without its task. It enters the
        normal review queue at pending_delivery_review; nothing is published.
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
        if experience_kind is not None:
            if experience_kind not in {"method", "rule"}:
                raise ValueError("authored candidate kind must be method or rule")
            snapshot["experience_policy"] = {"version": 1, "kind": experience_kind, "problem_key": problem_key}
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
                    if not candidate or task["execution_mode"] != "host_authored":
                        raise ValueError("idempotency key belongs to a different kind of task")
                    changed = []
                    if existing["prompt"] != title:
                        changed.append("title")
                    if existing["result"] != body:
                        changed.append("body")
                    if existing["execution_snapshot_json"] != snapshot_json:
                        changed.append("metadata")
                    if changed:
                        raise ValueError(
                            f"idempotency key {idempotency_key!r} is already used by a candidate "
                            "with different content (" + ", ".join(changed) + "); use a new key"
                        )
                    return {
                        "task_id": str(existing["id"]), "status": candidate["status"],
                        "staged_path": candidate["staged_path"],
                        "evidence_sha256": candidate["evidence_sha256"], "idempotent": True,
                    }
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
                conn.execute(
                    "INSERT INTO memory_candidates(task_id,status,staged_path,evidence_sha256,"
                    "source_task_ids_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (task_id, "pending_delivery_review", str(staged), evidence_sha256, _canonical(sources), now, now),
                )
                self._write_candidate_status_sidecar_values(
                    task_id, str(staged), "pending_delivery_review", evidence_sha256, now
                )
                self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": result_sha256})
                self._append_event_tx(conn, task_id, "memory.candidate_staged", {"evidence_sha256": evidence_sha256})
                self._append_event_tx(conn, task_id, "memory.candidate_authored", {
                    "author": author, "source_task_ids": sources,
                    "external_agents_called": False, "scope": "candidate_only",
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
                "task_id": task_id, "status": "pending_delivery_review",
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
    ) -> Dict[str, Any]:
        allowed = {
            "delivery": {"approve", "reject", "revise"},
            "final": {"approve", "reject", "revise", "retire"},
        }
        if stage not in allowed or decision not in allowed[stage]:
            raise ValueError("invalid review stage or decision")
        now = time.time()
        with self.connect() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
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
            from .experience_workflow import require_final_trial
            trial_task_id = require_final_trial(conn, candidate) if stage == "final" and decision == "approve" else None
            from .experience_loop import final_binding
            loop_sha256 = final_binding(conn, candidate) if stage == "final" and decision == "approve" else None
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
        EventStore._write_candidate_status_sidecar_values(
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
        pending = conn.execute(self._UNRESOLVED_SESSION_SQL, (session_id,)).fetchone()[0]
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
            """SELECT candidate_sha256 FROM memory_reviews
               WHERE task_id=? AND stage='final' AND decision='approve'
               ORDER BY created_at DESC LIMIT 1""",
            (str(candidate["task_id"]),),
        ).fetchone()
        if not approval or approval["candidate_sha256"] != candidate["evidence_sha256"]:
            raise ValueError("memory publication is not bound to the final approved candidate")
        from .experience_workflow import require_final_trial
        trial_task_id = require_final_trial(conn, candidate)
        if trial_task_id:
            event = conn.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.reviewed' ORDER BY sequence DESC LIMIT 1", (candidate["task_id"],)).fetchone()
            payload = json.loads(event["payload_json"]) if event else {}
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
                conn.execute(
                    "UPDATE memory_candidates SET reuse_count=reuse_count+1,updated_at=? WHERE task_id=?",
                    (time.time(), source_task_id),
                )
                self._append_event_tx(
                    conn, task_id, "memory.reused",
                    {"source_task_id": source_task_id, "source_path": source_path},
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

    def decide_reuse_outcomes(self, task_id: str, successful: bool) -> List[str]:
        outcome = "validated" if successful else "failed"
        now = time.time()
        affected: List[str] = []
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_reuse WHERE task_id=? AND outcome='pending'", (task_id,)
            ).fetchall()
            for row in rows:
                source_task_id = str(row["source_task_id"])
                candidate = conn.execute(
                    "SELECT task_id,staged_path,evidence_sha256,status FROM memory_candidates WHERE task_id=?",
                    (source_task_id,),
                ).fetchone()
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
        if stage not in {"delivery", "final", "all"}:
            raise ValueError("stage must be delivery, final or all")
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
            items = [dict(row) for row in conn.execute(query, args)]
            for item in items:
                from .experience_workflow import trial_summary
                item["experience_validation"] = trial_summary(conn, item)
                item["agent_runs"] = [
                    dict(row) for row in conn.execute(
                        "SELECT agent_name,status,output,error,latency_ms,created_at,finished_at FROM agent_runs WHERE task_id=? ORDER BY created_at",
                        (item["task_id"],),
                    )
                ]
                judge = conn.execute(
                    "SELECT payload_json,created_at FROM events WHERE task_id=? AND event_type='judge.decided' ORDER BY sequence DESC LIMIT 1",
                    (item["task_id"],),
                ).fetchone()
                item["judge"] = json.loads(judge["payload_json"]) if judge else None
                item["reviews"] = [
                    dict(row) for row in conn.execute(
                        "SELECT stage,decision,note,candidate_sha256,created_at FROM memory_reviews WHERE task_id=? ORDER BY created_at",
                        (item["task_id"],),
                    )
                ]
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
            for row in rows:
                if row["constraint_hold_details"]:
                    try:
                        row["constraint_hold_details"] = json.loads(row["constraint_hold_details"])
                    except json.JSONDecodeError:
                        row["constraint_hold_details"] = {}
            pending_holds = sum(1 for row in rows if row["constraint_hold_status"] == "pending")
            pending_final = sum(
                1 for row in rows if row["memory_status"] in {"delivery_approved", "publication_failed"}
            )
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
                },
            )
            tasks = [dict(row) for row in rows]
            for item in tasks:
                item["agent_runs"] = [
                    dict(row) for row in conn.execute(
                        "SELECT agent_name,status,output,error,latency_ms FROM agent_runs WHERE task_id=? ORDER BY created_at",
                        (item["id"],),
                    )
                ]
                judge = conn.execute(
                    "SELECT payload_json FROM events WHERE task_id=? AND event_type='judge.decided' ORDER BY sequence DESC LIMIT 1",
                    (item["id"],),
                ).fetchone()
                item["judge"] = json.loads(judge["payload_json"]) if judge else None
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
                "closed_sessions": closed_sessions,
            }

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

    def lease_next(self, lease_seconds: int) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Read the clock only after waiting for the writer lock.  A clock
            # value captured before the wait can produce a lease that is
            # already expired by the time this method returns.
            now = time.time()
            row = conn.execute("SELECT * FROM tasks WHERE status IN ('pending','retry') AND run_after<=? ORDER BY priority DESC, created_at ASC LIMIT 1", (now,)).fetchone()
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
            if metadata:
                payload["execution"] = metadata
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
            self._append_event_tx(
                conn,
                row["task_id"],
                "agent.finished",
                {"agent": row["agent_name"], "run_id": run_id, "status": status, "latency_ms": latency_ms},
            )

    def complete(self, task_id: str, result: str, provenance_kind: str = "agentos_multi_adjudicated") -> None:
        provenance = {
            "kind": provenance_kind,
            "status": "completed",
            "result_sha256": hashlib.sha256(result.encode()).hexdigest(),
        }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if current is None:
                raise KeyError(task_id)
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

    def complete_with_memory_candidate(
        self,
        task_id: str,
        result: str,
        staged_path: str,
        evidence_sha256: str,
        source_task_ids: Optional[List[str]] = None,
        provenance_kind: str = "agentos_multi_adjudicated",
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
            return self._complete_with_memory_candidate_tx(
                task_id, result, staged_path, evidence_sha256, source_task_ids,
                provenance, result_sha256, now,
            )
        except Exception:
            self._restore_sidecar_unless_committed(task_id, staged_path, evidence_sha256, previous_sidecar)
            raise

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
    ) -> bool:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if conn.execute(
                "SELECT 1 FROM constraint_holds WHERE task_id=? AND status='pending'",
                (task_id,),
            ).fetchone():
                # Covers both the normal completed hold and inconsistent
                # legacy states; no candidate can bypass human release.
                return False
            from .experience_workflow import policy
            full_task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
            if policy(full_task)["kind"] == "audit":
                raise ValueError("audit task must use audit-only settlement")
            if row["status"] == "completed":
                if self._existing_candidate_status(conn, task_id) is not None:
                    # A finished candidate is idempotent (no-op) unless it is
                    # terminal, where any second registration is refused.
                    self._reject_terminal_memory_reregistration(conn, task_id)
                    return False
                # Completed without a candidate must not exist; if an older
                # code path left one behind, queue the candidate now instead of
                # reporting an idempotent no-op that would hide the gap.
                conn.execute(
                    """INSERT INTO memory_candidates(
                           task_id,status,staged_path,evidence_sha256,source_task_ids_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (
                        task_id, "pending_delivery_review", staged_path, evidence_sha256,
                        _canonical(sorted(set(source_task_ids or []))), now, now,
                    ),
                )
                self._write_candidate_status_sidecar_values(
                    task_id, staged_path, "pending_delivery_review", evidence_sha256, now
                )
                self._append_event_tx(
                    conn, task_id, "memory.candidate_staged",
                    {"evidence_sha256": evidence_sha256, "repaired_missing_candidate": True},
                )
                return False
            self._reject_terminal_memory_reregistration(conn, task_id)
            conn.execute(
                "UPDATE tasks SET status='completed',result=?,provenance_json=?,last_error=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (result, _canonical(provenance), now, task_id),
            )
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
            self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": result_sha256})
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
            return self._release_constraint_hold_tx(task_id, staged_path, evidence_sha256)
        except Exception:
            self._restore_sidecar_unless_committed(task_id, staged_path, evidence_sha256, previous_sidecar)
            raise

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
            self._append_event_tx(conn, task_id, "task.completed", {"result_sha256": result_hash})
            if staged_path is not None:
                conn.execute(
                    """INSERT INTO memory_candidates(
                           task_id,status,staged_path,evidence_sha256,source_task_ids_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(task_id) DO UPDATE SET
                           staged_path=excluded.staged_path,
                           evidence_sha256=excluded.evidence_sha256,
                           source_task_ids_json=excluded.source_task_ids_json,
                           updated_at=excluded.updated_at""",
                    (task_id, "pending_delivery_review", staged_path, evidence_sha256, _canonical([]), now, now),
                )
                self._write_candidate_status_sidecar_values(
                    task_id, staged_path, "pending_delivery_review", evidence_sha256, now
                )
                self._append_event_tx(
                    conn, task_id, "memory.candidate_staged", {"evidence_sha256": evidence_sha256}
                )
            return {"task_id": task_id, "result": result, "result_sha256": result_hash}

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

    def fail(self, task_id: str, error: str) -> str:
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
            self._append_event_tx(conn, task_id, "task.failed", {"delay_seconds": 0, "error": error[:500]})
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

    def resume_task(
        self,
        task_id: str,
        expected_freeze_hash: Optional[str] = None,
        receipt_root: Optional[Path] = None,
        *,
        enabled_names: Iterable[str],
        health_max_age_seconds: float,
    ) -> Dict[str, Any]:
        """Resume a frozen task on the user's explicit instruction, and only then.

        Accepts a newly frozen task (`frozen_paused`) as well as a legacy
        `resume_ready` row left behind by the retired auto-repair. Neither ever
        resumes itself, and neither resumes without the caller presenting the
        freeze hash. Every recorded hash is re-verified before anything moves, and
        the failed side must hold a real `healthy` probe that is still enabled and
        still inside the configured window: an old healthy row is not capacity
        that exists now. `enabled_names` and `health_max_age_seconds` are required
        so no caller can get the looser check by omitting them.
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
            if failed_agents and any(current.get(name) != "healthy" for name in failed_agents):
                raise ValueError(
                    "failed Agent has no current healthy probe; run session-start first"
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

    def pending_notifications(self, acknowledge: bool = False) -> List[Dict[str, Any]]:
        now = time.time()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE status='pending' ORDER BY created_at"
            ).fetchall()
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
                "pending_notifications": conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE status='pending'"
                ).fetchone()[0],
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
