"""The single review flow: decision, publication, reuse reconciliation, retirement.

Every entry point (CLI, bridge, review center) goes through execute_review so
a decision has the same consequences no matter how it arrived.
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config import AppConfig
from .memory import MemoryBridge, MemoryRecoveryError
from .store import EventStore


def candidate_sha256(candidate: Dict[str, Any]) -> str:
    path = Path(str(candidate["staged_path"]))
    if not path.is_file():
        published_marker = path.with_suffix(".published")
        path = published_marker if published_marker.is_file() else path
    if not path.is_file():
        raise RuntimeError("memory candidate file is missing")
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PublicationUncertain(RuntimeError):
    """The database could not be asked whether a publication committed.

    Raised when a failed publication cannot be read back.  An unreadable outcome
    is not proof that nothing committed, so callers report reconciliation
    instead of demoting a memory that may already be published.
    """


def reconciliation_payload(
    task_id: str,
    operation: str,
    errors: list,
    recovery_command: Optional[str] = None,
    manual_action: bool = False,
) -> Dict[str, Any]:
    payload = {
        "status": "reconciliation_required",
        "task_id": task_id,
        "operation": operation,
        "errors": errors,
        "safe_state": "affected memories are kept out of active retrieval whenever possible",
        "recovery_command": recovery_command or f"agentos publish-memory --task-id {task_id}",
    }
    if manual_action:
        # The rollback itself did not finish; a human has to look at the files.
        payload["manual_action_required"] = True
    return payload


def _committed_publication(
    store: EventStore, task_id: str, publication: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Read back whether a failed publication actually committed.

    Returns the published row when this task is settled to this note, and None
    when it is not.  Raises PublicationUncertain when the database cannot be
    read: an unreadable outcome is never treated as "nothing committed".
    """
    try:
        row = store.get_memory_candidate(task_id)
    except (OSError, sqlite3.Error) as exc:
        raise PublicationUncertain(
            f"publication outcome for task {task_id!r} could not be read back: {exc}"
        ) from exc
    if (
        row
        and row.get("status") == "published"
        and str(row.get("published_path") or "") == str(publication.get("path"))
    ):
        return row
    return None


def publish_approved_candidate(
    config: AppConfig, store: EventStore, task_id: str, reviewed_sha256: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """The one publication path: gate, land, settle.

    Opens the store's publication transaction, which takes the write lock and
    verifies that the final approval still covers this exact evidence *before*
    any Vault file, catalog line or index byte is written.  The note and index
    are written inside that lock and the database is settled on the same
    connection, so a concurrent re-registration or review cannot slip between
    the check and the settlement, and a refusal leaves the Vault and index
    byte-identical.

    Failures are read back rather than assumed: a settlement error, and an error
    from the transaction context exit (where the commit runs before the store's
    permission maintenance), both land here, and neither proves a rollback.  A
    memory that is really published is returned as published instead of being
    demoted to a failure, so a retry only has to supply whatever part did not
    land; when the outcome cannot be read at all the call raises
    PublicationUncertain and the caller reconciles without touching the memory.
    """
    bridge = MemoryBridge(config)
    result: Optional[Dict[str, Any]] = None
    try:
        with store.publication_transaction(task_id, reviewed_sha256) as publication:
            result = bridge.publish_candidate(task_id, reviewed_sha256)
            published = publication.settle(result["path"])
    except PublicationUncertain:
        raise
    except Exception:
        if result is None:
            raise
        committed = _committed_publication(store, task_id, result)
        if committed is None:
            raise
        published = committed
    return result, published


def conclusion_cites_source(store: EventStore, task_id: str, reuse: Dict[str, Any]) -> bool:
    """这次任务真的用到这条经验了吗？

    只按现有证据判：任务的结论（tasks.result）里必须出现这条经验的笔记名或任务编号。
    只被检索命中、结论里没引用的，不算一次使用，也就不能因为任务失败把它降级。
    """
    task = store.get_task(task_id) or {}
    conclusion = str(task.get("result") or "")
    if not conclusion.strip():
        return False
    source_task_id = str(reuse.get("source_task_id") or "")
    source = store.get_memory_candidate(source_task_id) if source_task_id else None
    identifiers = {source_task_id}
    for raw in (reuse.get("source_path"), (source or {}).get("published_path")):
        if raw:
            identifiers.add(Path(str(raw)).stem)
    return any(identifier and identifier in conclusion for identifier in identifiers)


def _archive_rejected_note(
    config: AppConfig, store: EventStore, task_id: str, candidate: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """驳回待复验经验后归档：走现有 retire_candidate，和第二轮试验失败同一条路径。

    笔记移出活动目录、索引里不留链接、留一份归档；正文状态改回
    revalidation_required 由 retire_candidate 自己完成。没发布过笔记的候选不需要归档。
    """
    published_path = (candidate or {}).get("published_path")
    if not published_path:
        return None
    try:
        return MemoryBridge(config).retire_candidate(task_id, str(published_path))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}


def cited_source_ids(store: EventStore, task_id: str) -> list:
    """这次任务真正用到的来源（结论里有引用的）。只命中不算。"""
    return [str(item["source_task_id"]) for item in store.memory_reuses(task_id, "pending")
            if conclusion_cites_source(store, task_id, item)]


def quarantine_pending_reuses(
    config: AppConfig, store: EventStore, task_id: str, only: Optional[set] = None
) -> list:
    """Fail closed before committing a negative reuse outcome in SQLite."""
    bridge = MemoryBridge(config)
    errors = []
    for reuse in store.memory_reuses(task_id, "pending"):
        source_task_id = str(reuse["source_task_id"])
        if only is not None and source_task_id not in only:
            continue
        source = store.get_memory_candidate(source_task_id)
        if not source:
            errors.append({"task_id": source_task_id, "error": "source memory database row is missing"})
            continue
        if source.get("status") == "retired":
            continue
        published_path = source.get("published_path")
        if not published_path:
            errors.append({"task_id": source_task_id, "error": "source published path is missing"})
            continue
        try:
            bridge.set_validation_status(source_task_id, str(published_path), "revalidation_required")
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append({"task_id": source_task_id, "error": str(exc)})
    return errors


def reconcile_failed_reuses(
    config: AppConfig, store: EventStore, task_id: str
) -> tuple[list, list, list]:
    """Quarantine, settle, and if necessary retire failed source memories.

    只有“真的被这次任务结论引用”的命中才算一次失败复用。只是检索命中的，
    留一条 memory.reuse_uncited 事件、保持 pending，既不隔离也不降级。
    """
    pending = store.memory_reuses(task_id, "pending")
    cited = cited_source_ids(store, task_id)
    uncited = [str(item["source_task_id"]) for item in pending
               if str(item["source_task_id"]) not in cited]
    if uncited:
        store.record_uncited_reuses(task_id, uncited)
    cited_sources = set(cited)
    errors = quarantine_pending_reuses(config, store, task_id, only=cited_sources)
    if errors:
        return [], [], errors
    try:
        store.decide_reuse_outcomes(task_id, False, sources=cited_sources)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return [], [], [{"task_id": task_id, "error": f"reuse database settlement failed: {exc}"}]

    records = store.memory_reuses(task_id, "failed")
    affected = list(dict.fromkeys(str(item["source_task_id"]) for item in records))
    threshold = max(1, int(config.memory.get("retire_after_failed_reuses", 2)))
    bridge = MemoryBridge(config)
    retirements = []
    for source_task_id in affected:
        source = store.get_memory_candidate(source_task_id)
        if not source:
            errors.append({"task_id": source_task_id, "error": "source memory database row is missing"})
            continue
        if source.get("status") == "retired":
            continue
        published_path = source.get("published_path")
        if not published_path:
            errors.append({"task_id": source_task_id, "error": "source published path is missing"})
            continue
        try:
            if int(source.get("failed_reuse_count") or 0) >= threshold:
                retired = bridge.retire_candidate(source_task_id, str(published_path))
                try:
                    store.mark_memory_retired(
                        source_task_id,
                        retired["archive_path"],
                        f"failed reuse threshold reached: {threshold}",
                    )
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    errors.append({
                        "task_id": source_task_id,
                        "error": f"retirement database settlement failed: {exc}",
                        "archive_path": retired["archive_path"],
                    })
                    continue
                retirements.append(retired)
            else:
                bridge.set_validation_status(
                    source_task_id, str(published_path), "revalidation_required"
                )
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append({"task_id": source_task_id, "error": str(exc)})
    return affected, retirements, errors


def reconcile_validated_reuses(
    config: AppConfig, store: EventStore, task_id: str
) -> tuple[list, list, list]:
    """Settle successful reuse and restore every validated source to the active tree.

    和失败侧一样：只有这次任务结论真的引用到的经验才算一次使用。只被检索
    命中的留一条 memory.reuse_uncited 事件、保持 pending，既不加成功复用分，
    也不会把待复验的经验误判成已复活。
    """
    errors = []
    pending = store.memory_reuses(task_id, "pending")
    cited = set(cited_source_ids(store, task_id))
    uncited = [str(item["source_task_id"]) for item in pending
               if str(item["source_task_id"]) not in cited]
    if uncited:
        store.record_uncited_reuses(task_id, uncited)
    try:
        store.decide_reuse_outcomes(task_id, True, sources=cited)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        return [], [], [{"task_id": task_id, "error": f"reuse database settlement failed: {exc}"}]
    records = store.memory_reuses(task_id, "validated")
    affected = list(dict.fromkeys(str(item["source_task_id"]) for item in records))
    updates = []
    bridge = MemoryBridge(config)
    for source_task_id in affected:
        source = store.get_memory_candidate(source_task_id)
        if not source:
            errors.append({"task_id": source_task_id, "error": "source memory database row is missing"})
            continue
        if source.get("status") == "retired":
            continue
        published_path = source.get("published_path")
        if not published_path:
            errors.append({"task_id": source_task_id, "error": "source published path is missing"})
            continue
        try:
            updates.append(
                bridge.set_validation_status(source_task_id, str(published_path), "active")
            )
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            errors.append({"task_id": source_task_id, "error": str(exc)})
    return affected, updates, errors


def execute_review(
    config: AppConfig,
    store: EventStore,
    task_id: str,
    stage: str,
    decision: str,
    note: str,
    *,
    expected_sha256: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Run one review decision end to end and return (exit_code, payload).

    This is the only review path: the CLI and the bridge both call it, so
    publication, reuse reconciliation, and retirement behave identically no
    matter which entry point delivered the decision.
    """
    retirement = None
    current_candidate = store.get_memory_candidate(task_id)
    if not current_candidate:
        return 1, {"status": "blocked", "error": "memory candidate not found"}
    try:
        reviewed_sha256 = candidate_sha256(current_candidate)
    except RuntimeError as exc:
        return 1, {"status": "blocked", "error": str(exc)}
    if expected_sha256 is not None and expected_sha256 != reviewed_sha256:
        return 1, {"status": "blocked", "error": "evidence hash does not match the staged candidate"}
    if stage == "final" and decision == "approve":
        from .experience_workflow import verify_final_qualification
        with store.read_only() as conn:
            verdict = verify_final_qualification(conn, current_candidate)
            if not verdict.get("qualified"):
                reasons = "; ".join(verdict.get("reasons", ["经验候选不符合终审发布条件"]))
                return 1, {"status": "blocked", "error": f"经验终审门禁拒绝: {reasons}"}
    if stage == "final" and decision == "reject":
        try:
            quarantine_errors = quarantine_pending_reuses(
                config, store, task_id, only=set(cited_source_ids(store, task_id))
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            quarantine_errors = [{"task_id": task_id, "error": str(exc)}]
        if quarantine_errors:
            return 1, (
                reconciliation_payload(
                    task_id,
                    "failed_reuse_quarantine",
                    quarantine_errors,
                    f"agentos review {task_id} --stage final --decision reject",
                )
            )
    if stage == "final" and decision == "retire":
        current = current_candidate
        published_path = current.get("published_path") if current else None
        if current and current.get("status") == "retired" and published_path:
            try:
                Path(str(published_path)).expanduser().resolve().relative_to(
                    Path(config.memory["obsidian_vault"]).expanduser().resolve()
                )
            except ValueError:
                return 0, {"status": "retired", "task_id": task_id, "idempotent": True}
        if current and published_path and current.get("status") in {
            "published", "revalidation_required", "retired"
        }:
            try:
                retirement = MemoryBridge(config).retire_candidate(
                    task_id, str(published_path)
                )
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                return 1, (
                    reconciliation_payload(
                        task_id,
                        "user_retirement",
                        [{"task_id": task_id, "error": str(exc)}],
                        f"agentos memory-retire {task_id} --reason <reason>",
                    )
                )
    try:
        if stage == "final" and decision == "retire" and current_candidate.get("status") == "retired":
            candidate = current_candidate
        else:
            candidate = store.review_memory(
                task_id, stage, decision, note, reviewed_sha256
            )
    except KeyError:
        if retirement:
            return 1, (
                reconciliation_payload(
                    task_id,
                    "user_retirement_database_settlement",
                    [{"task_id": task_id, "error": "memory candidate not found"}],
                    f"agentos memory-retire {task_id} --reason <reason>",
                )
            )
        else:
            return 1, {"status": "blocked", "error": "memory candidate not found"}
    except ValueError as exc:
        if retirement:
            return 1, (
                reconciliation_payload(
                    task_id,
                    "user_retirement_database_settlement",
                    [{"task_id": task_id, "error": str(exc)}],
                    f"agentos memory-retire {task_id} --reason <reason>",
                )
            )
        return 2, {"status": "blocked", "error": str(exc)}
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        return 1, (
            reconciliation_payload(
                task_id,
                "review_database_settlement",
                [{"task_id": task_id, "error": str(exc)}],
                f"agentos review {task_id} --stage {stage} --decision {decision}",
            )
        )
    result: Dict[str, Any] = {
        "task_id": task_id,
        "stage": stage,
        "decision": decision,
        "candidate_sha256": reviewed_sha256,
        "memory_status": candidate["status"],
        "previous_status": candidate.get("previous_status"),
        "review_id": candidate.get("review_id"),
        "reviewed_status": candidate.get("reviewed_status"),
    }
    if retirement:
        try:
            candidate = store.mark_memory_retired(
                task_id, retirement["archive_path"], note or "user final review retirement"
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            return 1, (
                reconciliation_payload(
                    task_id,
                    "user_retirement_database_settlement",
                    [{
                        "task_id": task_id,
                        "error": str(exc),
                        "archive_path": retirement["archive_path"],
                    }],
                    f"agentos memory-retire {task_id} --reason <reason>",
                )
            )
        result["memory_status"] = candidate["status"]
        result["retirement"] = retirement
    if stage == "final" and decision == "approve":
        try:
            publication, candidate = publish_approved_candidate(
                config, store, task_id, reviewed_sha256
            )
        except PublicationUncertain as exc:
            # The outcome could not be read back.  Do not demote a publication
            # that may already be committed; leave the candidate as it is and
            # report reconciliation for a human.
            return 1, reconciliation_payload(
                task_id,
                "memory_publication_uncertain",
                [{"task_id": task_id, "error": str(exc)}],
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            settlement_error = None
            try:
                store.mark_memory_publication_failed(task_id, str(exc))
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as mark_exc:
                settlement_error = str(mark_exc)
            errors = [{"task_id": task_id, "error": str(exc)}]
            if settlement_error:
                errors.append({"task_id": task_id, "error": f"failure status settlement failed: {settlement_error}"})
            return 1, reconciliation_payload(
                task_id,
                "memory_publication",
                errors,
                manual_action=isinstance(exc, MemoryRecoveryError),
            )
        reused, validation_updates, validation_errors = reconcile_validated_reuses(
            config, store, task_id
        )
        result.update({
            "memory_status": candidate["status"],
            "publication": publication,
            "validated_source_memories": reused,
            "source_memory_index_updates": validation_updates,
        })
        if validation_errors:
            return 1, (
                {
                    **reconciliation_payload(
                        task_id, "source_memory_reactivation", validation_errors
                    ),
                    **result,
                    "status": "reconciliation_required",
                }
            )
    elif stage == "final" and decision == "reject":
        archive = _archive_rejected_note(config, store, task_id, candidate)
        if archive is not None:
            result["rejected_note_archive"] = archive
            if archive.get("error"):
                return 1, (
                    {
                        **reconciliation_payload(
                            task_id,
                            "rejected_note_archive",
                            [{"task_id": task_id, "error": archive["error"]}],
                            f"agentos memory-retire {task_id} --reason <reason>",
                        ),
                        **result,
                        "status": "reconciliation_required",
                    }
                )
        try:
            affected, automatic_retirements, retirement_errors = reconcile_failed_reuses(
                config, store, task_id
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            affected, automatic_retirements = [], []
            retirement_errors = [{"task_id": task_id, "error": str(exc)}]
        result["source_memories_requiring_revalidation"] = affected
        result["automatic_retirements"] = automatic_retirements
        if retirement_errors:
            return 1, (
                {
                    **reconciliation_payload(
                        task_id,
                        "failed_reuse_reconciliation",
                        retirement_errors,
                        f"agentos review {task_id} --stage final --decision reject",
                    ),
                    **result,
                    "status": "reconciliation_required",
                }
            )
    return 0, result
