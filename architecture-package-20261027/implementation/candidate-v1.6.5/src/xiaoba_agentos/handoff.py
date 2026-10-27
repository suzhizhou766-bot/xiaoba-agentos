from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

HANDOFF_FIELDS = (
    "task_id",
    "candidate_sha256",
    "source_agent",
    "next_agent",
    "completed",
    "pending",
    "blocked",
    "failed",
    "constraints",
    "updated_at",
)


class HandoffError(RuntimeError):
    """Raised when HANDOFF.json cannot be read or is missing required fields."""


def empty_handoff(task_id: str, source_agent: str, next_agent: str) -> Dict[str, Any]:
    """Create a new structured cross-agent handoff state."""
    return {
        "task_id": task_id,
        "candidate_sha256": "",
        "source_agent": source_agent,
        "next_agent": next_agent,
        "completed": [],
        "pending": [],
        "blocked": [],
        "failed": [],
        "constraints": [],
        "updated_at": time.time(),
    }


def read_handoff(path: Path) -> Dict[str, Any]:
    """Read and validate a HANDOFF.json document.

    Every required field must be present; the verifier must not trust the
    completed list without re-verification.
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HandoffError(f"HANDOFF.json not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise HandoffError(f"HANDOFF.json is not valid JSON: {exc}") from exc
    missing = [f for f in HANDOFF_FIELDS if f not in data]
    if missing:
        raise HandoffError("HANDOFF.json missing required fields: " + ",".join(missing))
    return data


def write_handoff(path: Path, data: Dict[str, Any]) -> Path:
    """Persist a HANDOFF.json document."""
    p = Path(path)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def verify_completed(
    handoff: Dict[str, Any], verifier: Callable[[Any], bool]
) -> Tuple[List[Any], List[Any]]:
    """Re-verify every completed item before trust.

    Items whose verification fails are reverted to pending graveside so the next
    agent re-runs them instead of trusting a stale completion.

    Returns (still_completed, reverted_to_pending).
    """
    still: List[Any] = []
    reverted: List[Any] = []
    for item in handoff.get("completed", []):
        if verifier(item):
            still.append(item)
        else:
            reverted.append(item)
    if reverted:
        handoff["completed"] = still
        pending = list(handoff.get("pending", []))
        pending.extend(reverted)
        handoff["pending"] = pending
        handoff["updated_at"] = time.time()
    return still, reverted


def block_with_reason(handoff: Dict[str, Any], item: Any, reason: str) -> None:
    """Move a pending item to blocked with a concrete capability/runtime reason."""
    pending = list(handoff.get("pending", []))
    if item in pending:
        pending.remove(item)
        handoff["pending"] = pending
    blocked = handoff.setdefault("blocked", [])
    for entry in blocked:
        if isinstance(entry, dict) and entry.get("item") == item:
            entry["reason"] = reason
            return
    blocked.append({"item": item, "reason": reason})
    handoff["updated_at"] = time.time()


def require_real_evidence(handoff: Dict[str, Any], item: Any, has_evidence: bool, reason: str) -> bool:
    """Fail-closed guard: a completed claim without real evidence is not trusted."""
    if has_evidence:
        return True
    block_with_reason(handoff, item, reason)
    return False
