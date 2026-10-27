"""Explicit database migration for v1.6.5 validation extension.

Additive extension version: 1.
This migration is explicit and transaction-safe; ordinary Store initialization
will never implicitly modify an unmigrated database.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

VALIDATION_EXTENSION_VERSION = 1


def _resolve_connection(target: Union[sqlite3.Connection, Path, str]) -> tuple[sqlite3.Connection, bool]:
    if isinstance(target, sqlite3.Connection):
        return target, False
    path = Path(target).resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn, True


def is_validation_extension_installed(conn: sqlite3.Connection) -> bool:
    """Check if the validation extension tables and version metadata are present."""
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    required = {
        "experience_validation_meta",
        "experience_validation_lineages",
        "experience_validation_records",
    }
    if not required.issubset(tables):
        return False
    row = conn.execute(
        "SELECT value FROM experience_validation_meta WHERE key='version'"
    ).fetchone()
    if not row:
        return False
    try:
        return int(row[0]) >= VALIDATION_EXTENSION_VERSION
    except (ValueError, TypeError):
        return False


def apply_validation_schema_migration(
    target: Union[sqlite3.Connection, Path, str],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Explicitly apply validation extension schema migration in an immediate transaction.

    Never called implicitly on ordinary database connections.
    """
    conn, should_close = _resolve_connection(target)
    now = time.time()
    try:
        if is_validation_extension_installed(conn):
            return {
                "ok": True,
                "already_installed": True,
                "version": VALIDATION_EXTENSION_VERSION,
                "dry_run": dry_run,
            }

        if dry_run:
            return {
                "ok": True,
                "already_installed": False,
                "target_version": VALIDATION_EXTENSION_VERSION,
                "dry_run": True,
                "actions": [
                    "CREATE TABLE experience_validation_meta",
                    "CREATE TABLE experience_validation_lineages",
                    "CREATE TABLE experience_validation_records",
                ],
            }

        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS experience_validation_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS experience_validation_lineages (
                candidate_id TEXT PRIMARY KEY REFERENCES memory_candidates(task_id),
                current_round INTEGER NOT NULL DEFAULT 1,
                max_rounds INTEGER NOT NULL DEFAULT 2,
                lineage_status TEXT NOT NULL CHECK(lineage_status IN (
                    'pending_round_1',
                    'round_1_running',
                    'pending_round_2',
                    'round_2_running',
                    'ready_for_final',
                    'retired',
                    'paused',
                    'awaiting_check',
                    'awaiting_diff_materials'
                )),
                rounds_used INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS experience_validation_records (
                id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES memory_candidates(task_id),
                round INTEGER NOT NULL CHECK(round IN (1, 2)),
                candidate_sha256 TEXT NOT NULL,
                delivery_review_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'passed', 'failed', 'paused')),
                task_id TEXT,
                plan_digest TEXT,
                execution_digest TEXT,
                diff_explanation TEXT,
                pause_reason TEXT,
                failure_class TEXT,
                dsh_receipt_id TEXT,
                peer_receipt_id TEXT,
                dsh_verdict TEXT,
                peer_verdict TEXT,
                needs_human_check INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(candidate_id, round)
            );
            CREATE INDEX IF NOT EXISTS idx_exp_val_records_candidate ON experience_validation_records(candidate_id, round);

            INSERT OR REPLACE INTO experience_validation_meta (key, value, updated_at)
            VALUES ('version', '{VALIDATION_EXTENSION_VERSION}', {now});
            INSERT OR REPLACE INTO experience_validation_meta (key, value, updated_at)
            VALUES ('installed_at', '{now}', {now});
            """
        )
        conn.commit()
        return {
            "ok": True,
            "already_installed": False,
            "version": VALIDATION_EXTENSION_VERSION,
            "installed_at": now,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if should_close:
            conn.close()


def rollback_validation_schema_migration(
    target: Union[sqlite3.Connection, Path, str],
) -> Dict[str, Any]:
    """Rollback validation extension schema changes cleanly during an aborted switch."""
    conn, should_close = _resolve_connection(target)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executescript(
            """
            DROP TABLE IF EXISTS experience_validation_records;
            DROP TABLE IF EXISTS experience_validation_lineages;
            DROP TABLE IF EXISTS experience_validation_meta;
            """
        )
        conn.commit()
        return {"ok": True, "rolled_back": True}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if should_close:
            conn.close()
