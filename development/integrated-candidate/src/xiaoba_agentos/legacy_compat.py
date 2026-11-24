"""Wave 4 Category Q: Read-only historical compatibility and retired-component registry.

Centralizes read-only compatibility paths for historical event-chain replay
(`standby.substituted`, `judge.decided`, `judge.breakdown`, legacy reviewer
names `agy_review` / `dsh_review` / `qoder_review`) while keeping retired
components (`workbuddy`, `headroom` auto-restart, standalone `8797` review
port, `peer_auto_repair`, `strategy_evolution` Schema 7 placeholder tables)
explicitly disabled and non-revivable.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional

from .standby_health import (
    EXTERNAL_FAILURES,
    HEALTH_MARKER,
    coverable_health,
    health_evidence,
)

# Historical named reviewer slots (read-only compatibility; live dispatch uses Agent Pool).
LEGACY_AGENT_NAMES = frozenset({"agy_review", "dsh_review", "qoder_review"})
LEGACY_JUDGE_EVENT_TYPE = "judge.decided"
LEGACY_JUDGE_BREAKDOWN_EVENT_TYPE = "judge.breakdown"
LEGACY_STANDBY_EVENT_TYPE = "standby.substituted"

# Card 45 (v1.6.9): Built-in WorkBuddy engine retired; CodeBuddy CLI remains active.
WORKBUDDY_RETIRED_CODE = "engine_retired"
WORKBUDDY_RETIRED_MESSAGE = (
    "内置 WorkBuddy 引擎已退役，请把这个节点的类型改成 WorkBuddy CLI（CodeBuddy），"
    "入口填 codebuddy 的路径，再测试运行。"
)

# W4-Q2 / W4-Q3: Authoritative registry of retired components & inert placeholders.
RETIRED_COMPONENTS: Dict[str, Dict[str, Any]] = {
    "workbuddy": {
        "status": "retired",
        "since": "v1.6.9",
        "code": WORKBUDDY_RETIRED_CODE,
        "message": WORKBUDDY_RETIRED_MESSAGE,
        "replacement": "codebuddy (WorkBuddy CLI)",
    },
    "headroom": {
        "status": "retired_auto_restart",
        "since": "v1.6.4",
        "policy": "classified as internal_headroom only; never auto-restarted and never triggers standby substitution",
    },
    "legacy_review_port_8797": {
        "status": "merged_into_monitor_8799",
        "since": "v1.6.4",
        "policy": "review-center runs on-demand behind xiaoba-monitor (127.0.0.1:8799), not as a standalone public UI on 8797",
    },
    "peer_auto_repair": {
        "status": "retired",
        "since": "v1.6.4",
        "policy": "probes only record health; frozen tasks unfreeze only via explicit user task-resume with --freeze-hash",
    },
    "strategy_evolution": {
        "status": "retired_inert_schema7_placeholders",
        "since": "2026-10-11",
        "schema_version": 7,
        "placeholder_table_count": 6,
        "policy": "six Schema 7 tables stay untouched as inert placeholders; no risky table-drop migration",
    },
}


def allow_legacy_standby_cover(roster_mode: str) -> bool:
    """W4-Q1: Legacy standby substitution writes are disabled in production Agent Pool mode.

    Only isolated unit-test mock fixtures (`roster_mode == 'mock'`) may exercise
    historical cover guardrails; live pool execution never writes standby cover.
    """
    return roster_mode == "mock"


def read_latest_judge_event(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    """Read-only lookup of the latest historical `judge.decided` event payload."""
    row = conn.execute(
        "SELECT payload_json FROM events WHERE task_id=? AND event_type=? ORDER BY sequence DESC LIMIT 1",
        (task_id, LEGACY_JUDGE_EVENT_TYPE),
    ).fetchone()
    if row is None:
        return None
    raw = row[0] if not isinstance(row, sqlite3.Row) else row["payload_json"]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_prior_standby_substitution(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    """Read-only lookup of any historical `standby.substituted` record on the event chain."""
    row = conn.execute(
        "SELECT payload_json FROM events WHERE task_id=? AND event_type=? ORDER BY sequence LIMIT 1",
        (task_id, LEGACY_STANDBY_EVENT_TYPE),
    ).fetchone()
    if row is None:
        return None
    raw = row[0] if not isinstance(row, sqlite3.Row) else row["payload_json"]
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def verify_historical_replay_readonly(db_path: Path) -> Dict[str, Any]:
    """Verify full event-chain integrity and historical legacy event readability in strict read-only mode."""
    from .store import EventStore

    target = Path(db_path).expanduser().resolve()
    uri = f"file:{target}?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=15)) as conn:
        conn.row_factory = sqlite3.Row
        before_changes = conn.total_changes
        chain = EventStore.verify_event_chain_tx(conn)
        total_events = int(conn.execute("SELECT COUNT(1) FROM events").fetchone()[0])
        legacy_counts: Dict[str, int] = {}
        malformed: list[str] = []
        for ev_type in (
            LEGACY_JUDGE_EVENT_TYPE,
            LEGACY_JUDGE_BREAKDOWN_EVENT_TYPE,
            LEGACY_STANDBY_EVENT_TYPE,
        ):
            rows = conn.execute(
                "SELECT event_id, payload_json FROM events WHERE event_type=? ORDER BY sequence",
                (ev_type,),
            ).fetchall()
            legacy_counts[ev_type] = len(rows)
            for row in rows:
                try:
                    json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError):
                    malformed.append(f"{ev_type}:{row['event_id']}")
        writes_performed = conn.total_changes - before_changes
    return {
        "ok": bool(chain.get("ok")) and not malformed and writes_performed == 0,
        "chain_ok": bool(chain.get("ok")),
        "total_events": total_events,
        "legacy_counts": legacy_counts,
        "malformed": malformed,
        "writes_performed": writes_performed,
    }


__all__ = [
    "EXTERNAL_FAILURES",
    "HEALTH_MARKER",
    "LEGACY_AGENT_NAMES",
    "LEGACY_JUDGE_BREAKDOWN_EVENT_TYPE",
    "LEGACY_JUDGE_EVENT_TYPE",
    "LEGACY_STANDBY_EVENT_TYPE",
    "RETIRED_COMPONENTS",
    "WORKBUDDY_RETIRED_CODE",
    "WORKBUDDY_RETIRED_MESSAGE",
    "allow_legacy_standby_cover",
    "coverable_health",
    "health_evidence",
    "read_latest_judge_event",
    "read_prior_standby_substitution",
    "verify_historical_replay_readonly",
]
