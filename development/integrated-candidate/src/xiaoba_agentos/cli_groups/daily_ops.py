"""W4-P1: Daily, session, task, and collaboration CLI command handlers."""
from __future__ import annotations

from typing import Any, Optional


def dispatch_daily_and_tasks(cli: Any, config: Any, store: Any, args: Any) -> Optional[int]:
    # Bind live attributes from xiaoba_agentos.cli so test monkeypatches apply verbatim.
    AGENTOS_MULTI = cli.AGENTOS_MULTI
    DIRECT = cli.DIRECT
    Path = cli.Path
    REVIEW = cli.REVIEW
    SkillRegistryError = cli.SkillRegistryError
    _answer_provenance = cli._answer_provenance
    _health_report = cli._health_report
    _primary_agents = cli._primary_agents
    _print = cli._print
    _submission_result = cli._submission_result
    _submit_task = cli._submit_task
    _worker_runtime_status = cli._worker_runtime_status
    adjudicated_label = cli.adjudicated_label
    decide_route = cli.decide_route
    doctor = cli.doctor
    execution_blockers = cli.execution_blockers
    json = cli.json
    read_context_file = cli.read_context_file
    sqlite3 = cli.sqlite3
    start_session_health = cli.start_session_health
    sys = cli.sys

    if args.command == "doctor":
        result = doctor(config, store, entity_root=args.entity_root)
        _print(result, True)
        return 0 if result["ok"] else 1
    if args.command == "session-start":
        local_health = doctor(config, store)
        if not local_health.get("ok"):
            _print(
                {
                    "status": "blocked_local_health",
                    "ready_for_external_tasks": False,
                    "doctor": local_health,
                },
                True,
            )
            return 1
        try:
            session = store.start_session(args.timezone)
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
        probe = start_session_health(config, store)
        from ..workday_ledger import rebuild_view,local_checks,warn
        # 卡 66：本地检查失败单独报，不与台账生成失败混为一谈（同接入口口径）。
        local_check_error=None
        try:
            checks=dict(local_checks(store),doctor_ok=True)
        except Exception as exc:
            local_check_error='本地检查失败：'+type(exc).__name__
            workday=warn(store,'local_check_failed')
        else:
            try:workday=rebuild_view(store,config=config,checks=checks)
            except Exception:workday=warn(store,'generation_failed')
        notifications = store.pending_notifications(acknowledge=True)
        # A pending manual review must reach the user even on a fully successful
        # session start, so the reminder rides along with the success payload.  The
        # live probe above still runs in full: the reminder is never a reason to
        # skip a real background probe.
        retained = local_health.get("retained_objects") or {}
        _print(
            {
                "status": "ready" if probe["ok"] else probe["session_mode"],
                "session": session,
                "workday": workday,
                "doctor_ok": True,
                "live_probe": probe,
                "notifications": notifications,
                **({"local_check_error": local_check_error} if local_check_error else {}),
                "retained_manual_review": {
                    "required": bool(retained.get("manual_action_required")),
                    "pending_count": int(retained.get("pending_count") or 0),
                    "resolved_count": int(retained.get("resolved_count") or 0),
                    "pending_move_intent_count": int(
                        retained.get("pending_move_intent_count") or 0
                    ),
                    "list_command": "memory-retained-list",
                    "resolve_command": (
                        "memory-retained-resolve <retained_path> --trash|--confirm-deleted "
                        "[--expect-dev <dev> --expect-ino <ino>]"
                    ),
                    "enumeration_ok": bool(retained.get("enumeration_ok", False)),
                    "note": (
                        "有文件待人工核对：请亲自查看 memory-retained-list；"
                        "这一项不阻断本次会话或接单，但仍需你逐个核对保留物"
                    ),
                },
                "ready_for_external_tasks": bool(probe["ok"]),
            },
            True,
        )
        return 0 if probe["ok"] else 1
    if args.command == "task-resume":
        from ..pool_dispatch import retested_nodes
        try:
            result = store.resume_task(
                args.task_id,
                args.freeze_hash,
                receipt_root=config.runtime_dir / "receipts",
                enabled_names=[agent.name for agent in config.agents if agent.enabled],
                health_max_age_seconds=float(
                    config.worker.get("health_max_age_seconds", 86400)
                ),
                retested_nodes=retested_nodes(store),
            )
        except (KeyError, ValueError) as exc:
            _print({"status": "blocked", "error": str(exc), "task_id": args.task_id}, True)
            return 1
        _print(result, True)
        return 0
    if args.command in ("task-abandon", "task-cancel"):
        try:
            result = (store.abandon_task(args.task_id, args.freeze_hash) if args.command == "task-abandon"
                      else store.cancel_task(args.task_id))
        except (KeyError, ValueError) as exc:
            _print({"status": "blocked", "error": str(exc), "task_id": args.task_id}, True)
            return 1
        _print(result, True)
        return 0
    if args.command == "session-close":
        session_id = args.session_id
        if not session_id:
            active = store.active_session()
            session_id = str(active["id"]) if active else None
        if not session_id:
            _print({"status": "no_active_session", "tasks": [], "pending_final_reviews": 0}, True)
            return 0
        try:
            packet = store.session_review_packet(session_id)
        except KeyError:
            _print({"status": "blocked", "error": "session not found"}, True)
            return 1
        packet["status"] = (
            "closed" if packet["session"].get("status") == "closed"
            else "awaiting_final_review" if packet["pending_final_reviews"]
            else "closing_without_final_reviews"
        )
        packet["answer_provenance"] = "agentos_review"
        _print(packet, True)
        return 0
    if args.command == "intake":
        if args.delivery_receipt:
            from ..experience_loop import record_delivery
            try:
                if not args.message_id or args.submit or args.force_route:
                    raise ValueError("delivery intake requires a stable user message ID and cannot submit or force a route")
                path = Path(args.delivery_receipt)
                if path.stat().st_size > 65536:
                    raise ValueError("delivery receipt exceeds 64 KiB")
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("delivery receipt must be a JSON object")
                value.update(origin="user", user_message=args.prompt, message_id=args.message_id)
                from ..delivery import submit as submit_delivery, user_decision, legacy_request
                if not args.user_decision:
                    raise ValueError("--user-decision must explicitly declare this user selection")
                request = legacy_request(store, value)
                result = submit_delivery(config, store, request, authority=user_decision(request, channel="cli_user_declaration", explicit=True))
                if not result.get("ok"):
                    _print(result, True)
                    return 1
                _print({"status": "delivery_recorded", "receipt": result['receipt'],
                        "experience_approval": "human_final_review_required"}, True)
                return 0
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
                _print({"status": "blocked", "error": str(exc)}, True)
                return 1
        try:
            decision = decide_route(
                args.prompt,
                active_task_id=args.active_task_id,
                relation=args.relation,
                risk=args.risk,
                requires_action=args.requires_action or args.needs_web or bool(args.requires_capability),
                memory_worthy=args.memory_worthy,
                explicit_agentos=args.explicit_agentos,
                force_route=args.force_route,
            )
        except ValueError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 2
        session = store.active_session() or store.start_session()
        signals = {
            "relation": args.relation,
            "risk": args.risk,
            "requires_action": args.requires_action,
            "memory_worthy": args.memory_worthy,
            "explicit_agentos": args.explicit_agentos,
            "forced": args.force_route is not None,
        }
        try:
            route = store.record_route(
                prompt=args.prompt,
                route=decision.route,
                reason_code=decision.reason_code,
                confidence=decision.confidence,
                signals=signals,
                session_id=str(session["id"]),
                message_id=args.message_id,
                active_task_id=args.active_task_id,
            )
        except ValueError as exc:
            _print({"status": "conflict", "error": str(exc)}, True)
            return 2
        response = {
            "status": "routed",
            "route_id": route["id"],
            "route": route["route"],
            "reason_code": route["reason_code"],
            "confidence": route["confidence"],
            "session_id": session["id"],
            "value_queue_status": session.get("value_queue_status") or store.session_value_queue_status(),
            "answer_provenance": _answer_provenance(str(route["route"])),
            "response_label": (
                "本回答：会话模型直接回答"
                if route["route"] == DIRECT
                else adjudicated_label(len(_primary_agents(config)))
                if route["route"] == AGENTOS_MULTI
                else "本回答：小八 AgentOS 用户审核流程"
                if route["route"] == REVIEW
                else "需要用户补充任务范围"
            ),
        }
        from ..experience_workflow import suggest_trials
        response.update(suggest_trials(store, args.problem_key, args.prompt))
        blockers = execution_blockers(config, store) if args.submit and route["route"] == AGENTOS_MULTI else []
        if blockers and not args.queue_when_blocked:
            response.update({
                "status": "blocked_local_health",
                "submission": "not_submitted_execution_blocked",
                "blockers": blockers,
                "hint": "后台当前无法执行，任务未入队。修复后重新提交，或加 --queue-when-blocked 明确要求先入队等待。",
            })
            _print(response, True)
            return 1
        if args.submit and route["route"] == AGENTOS_MULTI:
            try:
                task_id = _submit_task(
                    config,
                    store,
                    args.prompt,
                    args.priority,
                    session_id=str(session["id"]),
                    route_decision_id=str(route["id"]),
                    idempotency_key=f"intake:{route['id']}",
                    required_capabilities=tuple(args.requires_capability), needs_web=args.needs_web,
                    problem_key=args.problem_key,
                task_context=read_context_file(args.context_file),
                )
            except (SkillRegistryError, ValueError, OSError) as exc:
                _print({"status": "blocked", "error": str(exc), **response}, True)
                return 1
            response.update(_submission_result(store, task_id))
            if blockers:
                response.update({"execution_blocked": True, "blockers": blockers})
        elif args.submit:
            response["submission"] = "not_submitted_for_selected_route"
        _print(response, True)
        return 0
    if args.command in ("collaborate", "collaborate-review"):
        from ..workflow import work_plan, review_plan
        try:
            context = read_context_file(args.context_file)
            if args.command == "collaborate":
                plan = work_plan(config, json.loads(Path(args.assignments_file).read_text(encoding="utf-8")), context)
            else:
                plan = review_plan(config, store, args.work_task_id, context, args.artifact)
            session = store.active_session() or store.start_session()
            route = store.record_route(prompt=args.prompt, route=AGENTOS_MULTI,
                reason_code="explicit_collaboration_" + plan["phase"], confidence=1.0,
                signals={"explicit_agentos": True, "workflow_phase": plan["phase"]},
                session_id=str(session["id"]), message_id=args.message_id)
            task_id = _submit_task(config, store, args.prompt, session_id=str(session["id"]),
                route_decision_id=str(route["id"]), idempotency_key="workflow:" + str(route["id"]),
                task_context=context, workflow=plan)
            _print({"status": "pending", "task_id": task_id, "phase": plan["phase"], "route_id": route["id"]}, True)
            return 0
        except (SkillRegistryError, ValueError, OSError, KeyError) as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command in {'task-work-record', 'task-host-complete', 'task-host-bind'}:
        try:
            if args.command == 'task-host-bind':
                from ..task_participants import bind_host
                bind_host(store, args.task_id, {'id': args.host_id, 'entry': args.host_entry, 'model': args.host_model})
                result = {'status': 'host_bound', 'task_id': args.task_id}
            elif args.command == 'task-work-record':
                store.record_task_participant(args.task_id, args.actor_id, args.role)
                result = {'status': 'recorded', 'task_id': args.task_id}
            else:
                from ..host_completion import complete_host
                result = complete_host(config, store, args.task_id, json.loads(Path(args.receipt).read_text()), user_chose_host=args.user_chose_host)
            _print(result, True)
            return 0
        except (KeyError, ValueError, OSError) as exc:
            _print({'status': 'blocked', 'error': str(exc)}, True)
            return 1
    if args.command == "submit":
        session = store.active_session() or store.start_session()
        decision = decide_route(args.prompt, explicit_agentos=True)
        route = store.record_route(
            prompt=args.prompt,
            route=decision.route,
            reason_code="explicit_submit_command",
            confidence=1.0,
            signals={"explicit_submit": True},
            session_id=str(session["id"]),
        )
        try:
            task_id = _submit_task(
                config,
                store,
                args.prompt,
                args.priority,
                session_id=str(session["id"]),
                route_decision_id=str(route["id"]),
                required_capabilities=tuple(args.requires_capability), needs_web=args.needs_web,
                task_context=read_context_file(args.context_file),
                material_files=tuple(args.material),
                experience_kind=args.memory_kind, problem_key=args.problem_key,
                task_purpose=args.task_purpose,
                work_task_id=args.work_task,
                no_work_task=getattr(args, 'no_work_task', False),
                release_acceptance=args.release_acceptance,
                host=({'id': args.host_id, 'entry': args.host_entry, 'model': args.host_model}
                      if any((args.host_id,args.host_entry,args.host_model)) else None),
                needs_review=not args.no_review,
            )
        except (SkillRegistryError, ValueError, OSError) as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
        _print({
            **_submission_result(store, task_id),
            "route_id": route["id"],
            "answer_provenance": "agentos_multi_adjudicated",
        })
        return 0
    if args.command == "status":
        schema = store.schema_status()
        if not schema["ok"]:
            _print({"database_schema": schema, "runtime": _worker_runtime_status(config)}, True)
            return 1
        _print({
            "summary": store.summary(),
            "runtime": _worker_runtime_status(config),
            **_health_report(config, store),
            "repair_jobs": store.repair_jobs(args.limit),
            "notifications": store.pending_notifications(acknowledge=False),
            "tasks": store.list_tasks(args.limit),
        }, True)
        return 0
    if args.command == "show":
        if not store.schema_status()["ok"]:
            print("数据库需要先迁移", file=sys.stderr)
            return 1
        task = store.get_task(args.task_id)
        if not task:
            print("任务不存在", file=sys.stderr)
            return 1
        _print({
            "task": task,
            "runs": store.task_runs(args.task_id),
            "memory_candidate": store.get_memory_candidate(args.task_id),
            "constraint_hold": store.get_constraint_hold(args.task_id),
        }, True)
        return 0
    return None
