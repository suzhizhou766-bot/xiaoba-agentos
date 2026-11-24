"""W4-P3: Shared constants, state-machine helpers, and PublicationTransaction for EventStore."""
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

# W4-O1: Disambiguate initial memory review ('initial' / legacy 'delivery') from
# user deliverable acceptance ('task-delivery'). SQLite memory_reviews.stage
# retains canonical value 'delivery' so Schema 7 CHECK constraints and historical
# rows remain 100% untouched.
REVIEW_STAGE_ALIASES: Dict[str, str] = {"initial": "delivery"}


def normalize_review_stage(stage: str, *, allow_all: bool = False) -> str:
    """Resolve user-facing review stage alias ('initial' -> 'delivery')."""
    canonical = REVIEW_STAGE_ALIASES.get(stage, stage)
    allowed = {"delivery", "final", "all"} if allow_all else {"delivery", "final"}
    if canonical not in allowed:
        if allow_all:
            raise ValueError("stage must be delivery, final or all (alias: initial)")
        raise ValueError("invalid review stage or decision")
    return canonical


REUSE_METRIC_GLOSSARY: Dict[str, str] = {
    "legacy_reuse_count": "旧版 memory_candidates.reuse_count 历史列，仅作历史兼容展示，不代表正式复用分账本",
    "formal_reuse_score": "事件账本 memory.reuse_scored 投影永久总分（只统计经用户真实交付核验的正式经验复用）",
}

_PROTECTED_PUBLIC_EVENTS = frozenset({
    "memory.candidate_staged", "memory.value_unscored", "memory.value_assessed",
    "memory.value_review_dispatch",
    "open_review.backend_started", "open_review.backend_receipt_ready",
    "open_review.source_attested",
})


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
    ("delivery", "reject"): "retired",
    ("delivery", "revise"): "revision_requested",
    ("final", "approve"): "pending_final_review",
    ("final", "reject"): "final_rejected",
    ("final", "revise"): "revision_requested",
    ("final", "retire"): "retired",
}

LEGACY_DELIVERY_REJECTED = "delivery_rejected"


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



def _view_file_sha256(path):
    """只给台账阅读视图用的原文件哈希。读不到（不存在、符号链接、权限、正被移动）就返回 None，
    不让业务事务因为视图字段失败（卡 64 复查，Qoder 第四节）。"""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
