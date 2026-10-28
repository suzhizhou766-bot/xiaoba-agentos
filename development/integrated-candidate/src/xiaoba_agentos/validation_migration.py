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
    """Fail closed on unsupported versions and incomplete extension schemas."""
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
    columns = {
        "experience_validation_meta": {"key", "value", "updated_at"},
        "experience_validation_lineages": {
            "candidate_id", "current_round", "max_rounds", "lineage_status",
            "rounds_used", "created_at", "updated_at"},
        "experience_validation_records": {
            "id", "candidate_id", "round", "candidate_sha256", "delivery_review_id",
            "status", "task_id", "plan_digest", "execution_digest", "diff_explanation",
            "pause_reason", "failure_class", "dsh_receipt_id", "peer_receipt_id",
            "dsh_verdict", "peer_verdict", "needs_human_check", "created_at", "updated_at"},
    }
    for table, required_columns in columns.items():
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not required_columns.issubset({r[1] for r in info}):
            return False
        primary = "key" if table.endswith("_meta") else "candidate_id" if table.endswith("_lineages") else "id"
        if not any(r[1] == primary and r[5] == 1 for r in info):
            return False
    non_null = {
        "experience_validation_meta": {"value", "updated_at"},
        "experience_validation_lineages": {"current_round", "max_rounds", "lineage_status", "rounds_used", "created_at", "updated_at"},
        "experience_validation_records": {"candidate_id", "round", "candidate_sha256", "delivery_review_id", "status", "needs_human_check", "created_at", "updated_at"},
    }
    for table, names in non_null.items():
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not names.issubset({r[1] for r in info if r[3]}):
            return False
    for table in ("experience_validation_lineages", "experience_validation_records"):
        keys = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        if not any(r[2:5] == ("memory_candidates", "candidate_id", "task_id") for r in keys):
            return False
    row = conn.execute(
        "SELECT value FROM experience_validation_meta WHERE key='version'"
    ).fetchone()
    if not row:
        return False
    try:
        if int(row[0]) != VALIDATION_EXTENSION_VERSION:
            return False
    except (ValueError, TypeError):
        return False
    unique_round = False
    for index in conn.execute("PRAGMA index_list(experience_validation_records)").fetchall():
        if index[2] and not index[4]:
            name = str(index[1]).replace('"', '""')
            names = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')]
            unique_round |= names == ["candidate_id", "round"]
    if not unique_round:
        return False
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='experience_validation_records'").fetchone()[0]
    compact = "".join(str(sql).lower().split())
    lineage_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='experience_validation_lineages'").fetchone()[0]
    lineage_sql = "".join(str(lineage_sql).lower().split())
    return ("check(roundin(1,2))" in compact
            and "check(statusin('pending','running','passed','failed','paused'))" in compact
            and "check(lineage_statusin('pending_round_1','round_1_running','pending_round_2','round_2_running','ready_for_final','retired','paused','awaiting_check','awaiting_diff_materials'))" in lineage_sql)


def apply_validation_schema_migration(
    target: Union[sqlite3.Connection, Path, str],
    *,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Explicitly apply validation extension schema migration in an immediate transaction.

    Never called implicitly on ordinary database connections.
    """
    if dry_run and isinstance(target, (str, Path)):
        path = Path(target).resolve()
        if not path.is_file():
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

    if dry_run and isinstance(target, (str, Path)):
        conn = sqlite3.connect(Path(target).resolve().as_uri() + "?mode=ro", uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        should_close = True
    else:
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

        existing = conn.execute("SELECT name FROM sqlite_master WHERE name IN ('experience_validation_meta','experience_validation_lineages','experience_validation_records')").fetchall()
        if existing:
            raise ValueError("验证扩展已存在但版本或结构不受支持；禁止隐式修补，请人工核对迁移")

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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS experience_validation_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS experience_validation_lineages (
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
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS experience_validation_records (
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
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_exp_val_records_candidate
               ON experience_validation_records(candidate_id, round)"""
        )
        conn.execute(
            "INSERT OR REPLACE INTO experience_validation_meta (key, value, updated_at) VALUES ('version', ?, ?)",
            (str(VALIDATION_EXTENSION_VERSION), now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO experience_validation_meta (key, value, updated_at) VALUES ('installed_at', ?, ?)",
            (str(now), now),
        )
        if not is_validation_extension_installed(conn):
            raise ValueError("验证扩展迁移后结构复核失败")
        conn.execute("COMMIT")
        return {
            "ok": True,
            "already_installed": False,
            "version": VALIDATION_EXTENSION_VERSION,
            "installed_at": now,
        }
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
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
        conn.execute("DROP TABLE IF EXISTS experience_validation_records")
        conn.execute("DROP TABLE IF EXISTS experience_validation_lineages")
        conn.execute("DROP TABLE IF EXISTS experience_validation_meta")
        conn.execute("COMMIT")
        return {"ok": True, "rolled_back": True}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        if should_close:
            conn.close()
