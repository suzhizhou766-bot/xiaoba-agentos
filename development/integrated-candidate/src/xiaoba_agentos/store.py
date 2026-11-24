"""W4-P3: Authoritative EventStore facade composed from event-chain, task, and memory mixins."""
from __future__ import annotations

import os
import time
import uuid

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
from .store_events import EventChainStoreMixin
from .store_memory import MemoryStoreMixin
from .store_tasks import TaskStoreMixin


class EventStore(EventChainStoreMixin, TaskStoreMixin, MemoryStoreMixin):
    """Single authoritative SQLite store for AgentOS events, tasks, and memory lifecycle."""

    # Literal integer kept on store.py so static AST release scanners (build_release.py) read it directly.
    DATABASE_SCHEMA_VERSION = 7
