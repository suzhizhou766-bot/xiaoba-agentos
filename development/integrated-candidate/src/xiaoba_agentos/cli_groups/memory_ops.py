"""W4-P1: Memory lifecycle, review, trial, value-queue, and delivery CLI command handlers."""
from __future__ import annotations

from typing import Any, Optional


def dispatch_memory_and_review(cli: Any, config: Any, store: Any, args: Any) -> Optional[int]:
    # Bind live attributes from xiaoba_agentos.cli so test monkeypatches apply verbatim.
    EvidenceRestoreError = cli.EvidenceRestoreError
    MEMORY_TERMINAL_STATES = cli.MEMORY_TERMINAL_STATES
    MemoryBridge = cli.MemoryBridge
    MemoryRecoveryError = cli.MemoryRecoveryError
    Path = cli.Path
    PublicationUncertain = cli.PublicationUncertain
    RETAINED_NOTE = cli.RETAINED_NOTE
    _candidate_sha256 = cli._candidate_sha256
    _print = cli._print
    _publish_approved_candidate = cli._publish_approved_candidate
    _read_evidence = cli._read_evidence
    _reconcile_validated_reuses = cli._reconcile_validated_reuses
    _reconciliation_payload = cli._reconciliation_payload
    _restore_evidence = cli._restore_evidence
    _submit_task = cli._submit_task
    dispatch = cli.dispatch
    execute_review = cli.execute_review
    hashlib = cli.hashlib
    json = cli.json
    retained_objects = cli.retained_objects
    sqlite3 = cli.sqlite3
    subprocess = cli.subprocess
    sys = cli.sys
    terminal_reregistration_error = cli.terminal_reregistration_error

    if args.command in {"memory-pending-index", "memory-pending-read", "memory-application-record", "memory-delivery-record", "task-result-register", "task-delivery", "task-delivery-view", "memory-validation-basis-propose", "memory-validation-basis-revoke", "memory-revision-submit", "memory-revision-resubmit", "memory-formal-call"}:
        from ..experience_loop import pending_index, read_pending, record_application, record_delivery
        try:
            if not store.schema_status()["ok"]:
                raise ValueError("database schema must be initialized and verified first")
            if args.command == "task-delivery-view":
                from ..delivery import pending_tasks, detail_tx
                if args.task_id:
                    with store.read_only() as conn:
                        result = detail_tx(conn, args.task_id, getattr(args, "artifact_version", None))
                else:
                    result = pending_tasks(store)
            elif args.command == "memory-pending-index":
                result = pending_index(store, args.problem_key, args.query)
            elif args.command == "memory-pending-read":
                result = read_pending(store, args.task_id, args.candidate_sha256, args.delivery_review_id)
            else:
                path = Path(args.receipt)
                if path.stat().st_size > 65536:
                    raise ValueError("receipt exceeds 64 KiB")
                value = json.loads(path.read_text(encoding="utf-8"))
                if args.command == "task-result-register":
                    from ..delivery import register_direct
                    result = register_direct(store, value)
                elif args.command in {"task-delivery", "memory-delivery-record"}:
                    from ..delivery import submit as submit_delivery, user_decision, legacy_request
                    if not args.user_decision:
                        raise ValueError("--user-decision must explicitly declare this user selection")
                    request = legacy_request(store, value) if args.command == "memory-delivery-record" else value
                    result = submit_delivery(config, store, request, authority=user_decision(request, channel="cli_user_declaration", explicit=True))
                    if not result.get("ok"):
                        _print(result, True)
                        return 1
                elif args.command == "memory-revision-submit":
                    from ..formal_review import submit_draft
                    result = submit_draft(store, value['request_id'], value['draft'])
                elif args.command == "memory-revision-resubmit":
                    from ..formal_review import resubmit
                    result = resubmit(store,value['request_id'],value['application_id'],value['draft'])
                elif args.command == "memory-formal-call":
                    from ..formal_review import record_call
                    result = record_call(store, value['source_task_id'], value['source_sha256'])
                elif args.command == "memory-validation-basis-revoke":
                    from ..validation_basis import revoke_basis
                    result = revoke_basis(store, args.task_id, value)
                elif args.command == "memory-validation-basis-propose":
                    if args.task_id != value.get("candidate_id"):
                        raise ValueError("receipt candidate_id must match task_id")
                    with store.connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        row = conn.execute("SELECT * FROM memory_candidates WHERE task_id=?", (args.task_id,)).fetchone()
                        if not row:
                            raise ValueError("candidate not found")
                        from ..validation_basis import propose_historical_basis_tx
                        result = propose_historical_basis_tx(store, conn, dict(row), value)
                else:
                    result = (record_application if args.command == "memory-application-record" else record_delivery)(store, value)
            _print(result, True)
            return 0
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "memory-retrieve":
        receipt = MemoryBridge(config, store).retrieve_with_receipt(args.query, purpose="diagnostic")
        _print(receipt, True)
        return 0 if receipt["retrieval_status"] != "degraded" else 1
    if args.command == "memory-retained-list":
        # The self-check the user reads before deciding on any cleanup: it
        # enumerates the retained original objects from the journal *and* from a
        # fresh filesystem scan, so it also works from a brand-new process after
        # a restart and never depends on in-memory bookkeeping.
        payload = retained_objects.collect(config)
        if args.write_report:
            report_path, report_error = retained_objects.write_report(config, payload)
            payload["report_written"] = str(report_path) if report_path else None
            if report_error is not None:
                payload["report_error"] = report_error
        if not args.show_resolved:
            # The default view highlights what still needs the user; the objects
            # already handled stay out of it and are shown only when asked for.
            payload["resolved_objects_hidden"] = len(payload.get("resolved_objects") or [])
            payload.pop("resolved_objects", None)
            payload["resolved_objects_note"] = (
                "已处理对象的历史默认隐藏；加 --show-resolved 可显式查看"
            )
        _print(payload, True)
        return 0 if payload["ok"] else 1
    if args.command == "memory-retained-resolve":
        # One object, named on the command line by the user in their own terminal.
        # There is no batch form and no automatic cleanup: this handles exactly the
        # path it was given, and reports the real location on every outcome.
        action = "trash" if args.trash else "confirm_deleted"
        expected_identity = None
        if args.expect_dev is not None or args.expect_ino is not None:
            expected_identity = {
                "dev": args.expect_dev,
                "ino": args.expect_ino,
                "source": "--expect-dev/--expect-ino on the command line",
            }
        outcome = retained_objects.resolve_retained(
            config,
            retained_path=args.retained_path,
            action=action,
            expected_identity=expected_identity,
        )
        _print(outcome, True)
        return 0 if outcome["ok"] else 1
    if args.command in {"memory-trial-run", "memory-trial-status", "memory-trial-match", "memory-trial-resume", "memory-trial-pause", "memory-trial-diff"}:
        from ..experience_workflow import execute_trial, prepare_trial, trial_summary, policy, save_execution_receipt
        from ..experience_workflow import reserve_validation_attempt, pause_validation_attempt, resume_paused_validation
        receipt_path = None
        attempt_id = None
        try:
            if args.command == "memory-trial-pause":
                pause_validation_attempt(store, args.task_id, args.attempt_id, args.reason)
                _print({"status": "paused", "task_id": args.task_id}, True)
            elif args.command == "memory-trial-resume":
                with store.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    value = resume_paused_validation(store, conn, args.task_id)
                _print(value, True)
            elif args.command == "memory-trial-diff":
                value = json.loads(Path(args.materials).read_text())
                _print(store.submit_validation_diff_materials(args.task_id, value), True)
            elif args.command == "memory-trial-match":
                from ..experience_workflow import match_trials
                matches = match_trials(store, args.problem_key, args.query)
                _print({"matches": matches, "action": "suggest_only", "normal_retrieval": False}, True)
            elif args.command == "memory-trial-status":
                candidate = store.get_memory_candidate(args.task_id)
                if not candidate:
                    raise ValueError("candidate not found")
                with store.read_only() as conn:
                    _print(trial_summary(conn, candidate), True)
            else:
                if not store.is_validation_extension_installed():
                    raise RuntimeError("数据库缺少验证扩展表或结构异常，已阻断执行；请先执行架构迁移以启用两轮核验门禁与轮次限制")
                card = json.loads(Path(args.card).read_text())
                # Check approval and source bytes before executing; recheck after it.
                prepare_trial(store, args.task_id, card)
                attempt_id = reserve_validation_attempt(store, args.task_id, card)
                execution = execute_trial(config, card)
                receipt_path = save_execution_receipt(config, args.task_id, card, execution)
                trial, prompt = prepare_trial(store, args.task_id, card, execution, attempt_id=attempt_id)
                session = store.active_session() or store.start_session()
                task_id = _submit_task(config, store, prompt, session_id=str(session["id"]),
                                       experience_kind="audit", experience_trial=trial,
                                       idempotency_key="experience-trial:" + args.task_id + ":" + attempt_id)
                _print({"source_task_id": args.task_id, "validation_task_id": task_id,
                        "execution_passed": execution["passed"], "execution_receipt": str(receipt_path), "status": "pending_independent_review"}, True)
            return 0
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            if attempt_id:
                try:
                    pause_validation_attempt(store, args.task_id, attempt_id, "试验提交中断: " + str(exc))
                except (ValueError, OSError):
                    pass  # Preserve a concurrent human terminal decision.
            _print({"status": "blocked", "error": str(exc), "attempt_id": attempt_id, "execution_receipt": str(receipt_path) if receipt_path else None}, True)
            return 1
    if args.command == "memory-candidate-create":
        try:
            body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
            active = store.active_session()
            created = store.create_authored_candidate(
                args.title, body, config.runtime_dir / "memory-outbox",
                author=args.author, source_task_ids=args.source_task_id,
                experience_kind=args.kind, problem_key=args.problem_key,
                idempotency_key=args.idempotency_key,
                session_id=str(active["id"]) if active else None,
                enforce_value_queue=True,
            )
        except KeyError as exc:
            _print({"status": "blocked", "error": f"source task not found: {exc.args[0]}"}, True)
            return 1
        except (OSError, UnicodeDecodeError, ValueError, sqlite3.Error) as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
        _print(created, True)
        return 0
    if args.command == "constraint-hold-release":
        hold = store.get_constraint_hold(args.task_id, include_result=True)
        task = store.get_task(args.task_id)
        if not hold or hold.get("status") != "pending" or not task:
            _print({"status": "blocked", "error": "pending constraint hold not found"}, True)
            return 1
        # A published or retired candidate keeps frozen evidence, and the store
        # can only refuse inside its own transaction, after the staging below has
        # already rewritten the file.  Refuse here first, before anything is
        # written, and keep the previous bytes so a refusal in the race window
        # (another connection finishing the candidate while we stage) can be
        # undone.  The store guard stays as the concurrency backstop.
        frozen = store.terminal_memory_candidate_status(args.task_id)
        if frozen is not None:
            _print({
                "status": "blocked",
                "task_id": args.task_id,
                "error": str(terminal_reregistration_error(args.task_id, frozen)),
                "hold_status": hold.get("status"),
                "retry_safe": False,
            }, True)
            return 1
        from ..experience_workflow import policy, complete_audit
        if policy(task)["kind"] == "audit":
            complete_audit(store, args.task_id, str(hold["result"]), release_hold=True)
            _print({"task_id": args.task_id, "status": "released", "memory_disposition": "audit_only"}, True)
            return 0
        bridge = MemoryBridge(config)
        target = bridge.staged_path(args.task_id)
        evidence_snapshot = _read_evidence(target)
        if evidence_snapshot[0] and evidence_snapshot[1] is None:
            # The file exists but its bytes could not be captured.  Writing over
            # it would destroy evidence we could never put back, so stop here.
            _print({
                "status": "blocked",
                "task_id": args.task_id,
                "error": f"cannot snapshot the existing evidence file {target}; refusing to overwrite it",
                "hold_status": hold.get("status"),
                "retry_safe": False,
                "manual_action_required": True,
            }, True)
            return 1
        evidence_sha256 = None
        release = None
        try:
            # Stage the file while the hold is still pending, then release the
            # hold and queue the candidate in one transaction.
            runs = store.task_runs(args.task_id)
            staged = bridge.stage_experience(
                args.task_id, task["prompt"], str(hold["result"]), runs
            )
            evidence_sha256 = hashlib.sha256(staged.read_bytes()).hexdigest()
            release = store.release_constraint_hold(args.task_id, str(staged), evidence_sha256)
        except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as exc:
            # An exception does not prove a rollback (the store does
            # maintenance after commit); decide from the database.  Three
            # outcomes stay distinct: committed, provably not committed, and
            # unknown.
            readback_error = None
            try:
                after = store.get_constraint_hold(args.task_id)
                candidate = store.get_memory_candidate(args.task_id)
            except (OSError, RuntimeError, sqlite3.Error) as readback_exc:
                after, candidate = None, None
                readback_error = str(readback_exc)
            value_status = None
            if evidence_sha256:
                try:
                    value_status = store.value_candidate_status(args.task_id, evidence_sha256)
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    value_status = None
            committed = bool(
                after and after.get("status") == "released" and (
                    candidate and candidate.get("evidence_sha256") == evidence_sha256
                    or value_status in {"awaiting_value_review", "value_discarded"}
                )
            )
            if not committed and readback_error is not None:
                # The release may have committed and we cannot tell.  Never put
                # the previous bytes back over evidence we cannot prove we
                # overwrote: that is exactly how the database and the file drift
                # apart.  Keep the current evidence and hand it to a human.
                _print({
                    "status": "reconciliation_required",
                    "task_id": args.task_id,
                    "error": str(exc),
                    "readback_error": readback_error,
                    "hold_status": "unknown",
                    "retry_safe": False,
                    "manual_action_required": True,
                }, True)
                return 1
            if not committed:
                # Provably not committed: put the evidence bytes back as they
                # were, and only if they are still this command's own write.  A
                # restore that cannot finish is reported, never called recovered.
                # Under 受限方案丙 the object this command replaced is kept, so a
                # non-empty retained list is reported as manual action too: the
                # live path is back, but the command does not claim that nothing
                # is left behind.
                try:
                    retained_paths = _restore_evidence(
                        target,
                        evidence_snapshot,
                        evidence_sha256,
                        config=config,
                        task_id=args.task_id,
                        operation="evidence.staged",
                    )
                except EvidenceRestoreError as restore_exc:
                    payload = {
                        "status": "reconciliation_required",
                        "task_id": args.task_id,
                        "error": str(exc),
                        "restore_error": str(restore_exc),
                        "hold_status": after.get("status") if after else "unknown",
                        "retry_safe": False,
                        "manual_action_required": True,
                    }
                    retained_paths = list(getattr(restore_exc, "retained", []) or [])
                    if retained_paths:
                        payload["retained_objects"] = retained_paths
                        payload["retained_note"] = RETAINED_NOTE
                    _print(payload, True)
                    return 1
                still_pending = bool(after and after.get("status") == "pending")
                frozen_now = bool(candidate and candidate.get("status") in MEMORY_TERMINAL_STATES)
                payload = {
                    "status": "blocked" if still_pending else "reconciliation_required",
                    "task_id": args.task_id,
                    "error": str(exc),
                    "hold_status": after.get("status") if after else "unknown",
                    "retry_safe": bool(still_pending and not frozen_now),
                }
                if retained_paths:
                    payload["manual_action_required"] = True
                    payload["retained_objects"] = retained_paths
                    payload["retained_note"] = RETAINED_NOTE
                _print(payload, True)
                return 1
        candidate_row = store.get_memory_candidate(args.task_id)
        candidate_status = (
            release.get("candidate_status") if release is not None else None
        ) or (
            candidate_row.get("status") if candidate_row else None
        ) or (
            store.value_candidate_status(args.task_id, evidence_sha256) if evidence_sha256 else None
        ) or "unknown"
        _print({
            "status": "released",
            "task_id": args.task_id,
            "candidate_status": candidate_status,
            "candidate_sha256": evidence_sha256,
        }, True)
        return 0
    if args.command == "memory-value-list":
        if not store.schema_status()["ok"]:
            print("数据库需要先迁移", file=sys.stderr)
            return 1
        try:
            items = store.value_review_queue()
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
        _print({"status": "ok", "count": len(items), "items": items}, True)
        return 0
    if args.command == "memory-value-dispatch":
        if not store.schema_status()["ok"]:
            print("数据库需要先迁移", file=sys.stderr)
            return 1
        from ..value_queue import dispatch_value_review_item
        try:
            if args.task_id:
                res = dispatch_value_review_item(config, store, args.task_id)
                _print({"status": "ok" if res.get("ok") else res.get("status", "failed"), "results": [res]}, True)
                return 0 if res.get("ok") else 1
            queue = [
                item for item in store.value_review_queue()
                if item.get("queue_status") == "awaiting_value_review"
                and (item.get("dispatch") or {}).get("status") == "not_started"
            ]
            targets = queue if args.dispatch_all else queue[:1]
            results = [
                dispatch_value_review_item(config, store, item["task_id"], item["candidate_sha256"])
                for item in targets
            ]
            _print({"status": "ok", "count": len(results), "results": results}, True)
            return 0
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
    if args.command == "review-list":
        if not store.schema_status()["ok"]:
            print("数据库需要先迁移", file=sys.stderr)
            return 1
        from ..store import normalize_review_stage
        try:
            canonical_stage = normalize_review_stage(args.stage, allow_all=True)
            items = store.review_queue(canonical_stage, args.date)
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
        payload = {"stage": canonical_stage, "date": args.date, "count": len(items), "items": items}
        if args.stage != canonical_stage:
            payload["requested_stage"] = args.stage
        _print(payload, True)
        return 0
    if args.command == "review":
        from ..store import REVIEW_STAGE_ALIASES
        canonical_stage = REVIEW_STAGE_ALIASES.get(args.stage, args.stage)
        code, payload = execute_review(
            config, store, args.task_id, canonical_stage, args.decision, args.note
        )
        _print(payload, True)
        return code
    if args.command == "memory-rename-notes":
        bridge = MemoryBridge(config)
        renamed, skipped = [], []
        for candidate in store.memory_lifecycle_rows():
            if candidate.get("status") not in {"published", "revalidation_required"}:
                continue
            task_id, recorded = str(candidate["task_id"]), str(candidate.get("published_path") or "")
            try:
                result = bridge.rename_note(task_id, dry_run=args.dry_run)
                # 不看这次有没有改名：上次改了名但没记上的，这次补记
                if not args.dry_run and result["to"] != recorded:
                    store.record_memory_note_renamed(task_id, recorded, result["to"])
                (renamed if result["changed"] else skipped).append(result)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                _print({"status": "blocked", "task_id": task_id, "error": str(exc), "renamed": renamed}, True)
                return 1
        _print({"status": "dry_run" if args.dry_run else "renamed", "renamed": renamed, "unchanged": len(skipped)}, True)
        return 0
    if args.command == "memory-retire":
        candidate = store.get_memory_candidate(args.task_id)
        if not candidate:
            _print({"status": "blocked", "error": "memory candidate not found"}, True)
            return 1
        published_path = candidate.get("published_path")
        if not published_path:
            _print({"status": "blocked", "error": "memory is not currently published"}, True)
            return 1
        if candidate.get("status") == "retired":
            archive = Path(str(published_path)).expanduser()
            if archive.is_file():
                _print({"status": "retired", "archive_path": str(archive), "idempotent": True}, True)
                return 0
        elif candidate.get("status") not in {"published", "revalidation_required"}:
            _print({"status": "blocked", "error": "memory is not currently published"}, True)
            return 1
        try:
            retirement = MemoryBridge(config).retire_candidate(
                args.task_id, str(published_path)
            )
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            _print(
                _reconciliation_payload(
                    args.task_id,
                    "explicit_retirement_filesystem",
                    [{"task_id": args.task_id, "error": str(exc)}],
                    f"agentos memory-retire {args.task_id} --reason <reason>",
                ),
                True,
            )
            return 1
        try:
            updated = store.mark_memory_retired(
                args.task_id, retirement["archive_path"], args.reason
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            _print(
                _reconciliation_payload(
                    args.task_id,
                    "explicit_retirement_database_settlement",
                    [{
                        "task_id": args.task_id,
                        "error": str(exc),
                        "archive_path": retirement["archive_path"],
                    }],
                    f"agentos memory-retire {args.task_id} --reason <reason>",
                ),
                True,
            )
            return 1
        _print({"status": updated["status"], "retirement": retirement}, True)
        return 0
    if args.command == "memory-receipt":
        try:
            _print(store.memory_review_receipt(args.task_id), True)
            return 0
        except KeyError:
            _print({"status": "blocked", "error": "memory candidate not found"}, True)
            return 1
    if args.command == "review-packet":
        raw = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
        packet = json.loads(raw)
        result = dispatch({"command": "review_decision", "packet": packet}, store, config)
        _print(result, True)
        return 0 if result.get("ok") else 1
    if args.command == "publish-memory":
        if not args.task_id:
            _print(MemoryBridge(config).publish(), True)
            return 1
        candidate = store.get_memory_candidate(args.task_id)
        if not candidate or candidate["status"] not in {
            "pending_final_review", "publication_failed", "published"
        }:
            _print({"status": "blocked", "error": "task has not passed final review"}, True)
            return 1
        try:
            reviewed_sha256 = _candidate_sha256(candidate)
            if reviewed_sha256 != candidate["evidence_sha256"]:
                raise ValueError("memory candidate changed after final review")
            # The shared publication path checks the final approval binding
            # under the store write lock before any Vault file or index byte is
            # written, then settles on that same locked connection.
            publication, published = _publish_approved_candidate(
                config, store, args.task_id, reviewed_sha256
            )
        except PublicationUncertain as exc:
            # The outcome could not be read back: report reconciliation and leave
            # the candidate alone instead of demoting a publication that may
            # already be committed.
            _print(_reconciliation_payload(
                args.task_id,
                "memory_publication_uncertain",
                [{"task_id": args.task_id, "error": str(exc)}],
            ), True)
            return 1
        except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            errors = [{"task_id": args.task_id, "error": str(exc)}]
            try:
                store.mark_memory_publication_failed(args.task_id, str(exc))
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as mark_exc:
                errors.append({"task_id": args.task_id, "error": f"failure status settlement failed: {mark_exc}"})
            _print(_reconciliation_payload(
                args.task_id,
                "memory_publication",
                errors,
                manual_action=isinstance(exc, MemoryRecoveryError),
            ), True)
            return 1
        reused, validation_updates, validation_errors = _reconcile_validated_reuses(
            config, store, args.task_id
        )
        response = {
            "status": published["status"],
            "candidate_sha256": reviewed_sha256,
            "publication": publication,
            "validated_source_memories": reused,
            "source_memory_index_updates": validation_updates,
        }
        if validation_errors:
            _print({
                **_reconciliation_payload(
                    args.task_id, "source_memory_reactivation", validation_errors
                ),
                **response,
                "status": "reconciliation_required",
            }, True)
            return 1
        _print(response, True)
        return 0
    return None
