"""W4-P1: Maintenance, workday ledger, skills/knowledge, install, and worker run CLI command handlers."""
from __future__ import annotations

from typing import Any, Optional


def dispatch_maintenance_and_ops(cli: Any, config: Any, store: Any, args: Any) -> Optional[int]:
    # Bind live attributes from xiaoba_agentos.cli so test monkeypatches apply verbatim.
    Any = cli.Any
    AppConfig = cli.AppConfig
    ConfigError = cli.ConfigError
    EventStore = cli.EventStore
    KnowledgeCatalog = cli.KnowledgeCatalog
    Orchestrator = cli.Orchestrator
    Path = cli.Path
    SkillRegistry = cli.SkillRegistry
    SkillRegistryError = cli.SkillRegistryError
    UnixBridge = cli.UnixBridge
    _build_runner_app = cli._build_runner_app
    _content_index_command = cli._content_index_command
    _content_notes_sync = cli._content_notes_sync
    _demo_config = cli._demo_config
    _health_report = cli._health_report
    _install_service = cli._install_service
    _print = cli._print
    _render_launchagent = cli._render_launchagent
    _secure_directory = cli._secure_directory
    _set_agents = cli._set_agents
    _write_worker_state = cli._write_worker_state
    json = cli.json
    signal = cli.signal
    subprocess = cli.subprocess
    sys = cli.sys
    time = cli.time

    if args.command in {'workday-rebuild','workday-read','workday-record'}:
        from ..workday_ledger import bound_ledger,rebuild_view,read_workday,warn
        try:
            if args.command=='workday-rebuild':result=rebuild_view(store,day=args.day)
            elif args.command=='workday-read':result=read_workday(store,args.day)
            else:
                ledger=bound_ledger(store)
                result=ledger.record(args.path,node=args.node,task_id=args.task_id,agent=args.agent,
                                     model=args.model,kind=args.kind,status=args.status) if ledger else warn(store,'unconfigured')
            _print(result,True)
            return 0
        except Exception:
            # 台账命令一律返回 0；参数不合格的具体原因由 record 的返回值里的 reason 带出。
            _print(warn(store,'generation_failed'),True)
            return 0
    if args.command == "init":
        _secure_directory(config.runtime_dir)
        _secure_directory(config.runtime_dir / "logs")
        _print({"status": "initialized", "database": str(config.database_path), "event_chain": store.verify_event_chain()})
        return 0
    if args.command == "repair-status":
        _print(
            {
                **_health_report(config, store),
                # Peer auto-repair is retired: these rows are history, and no
                # poll leases, retries or terminates them any more.
                "peer_auto_repair": "retired",
                "repair_jobs": store.repair_jobs(args.limit),
                "notifications": store.pending_notifications(acknowledge=False),
            },
            True,
        )
        return 0
    if args.command == "skill-verify":
        result = SkillRegistry(config, entity_root=args.entity_root).verify()
        _print(result, True)
        return 0 if result["ok"] else 1
    if args.command == "skill-list":
        try:
            _print({"skills": SkillRegistry(config, entity_root=args.entity_root).list()}, True)
            return 0
        except SkillRegistryError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "skill-match":
        try:
            _print({"prompt": args.prompt, "matches": SkillRegistry(config, entity_root=args.entity_root).match(args.prompt)}, True)
            return 0
        except SkillRegistryError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "content-notes-sync":
        return _content_notes_sync(config, args.verify)
    if args.command in ("skill-review-prompt", "skill-publish", "knowledge-index-rebuild"):
        return _content_index_command(config, args)
    if args.command == "knowledge-verify":
        result = KnowledgeCatalog(config).verify()
        _print(result, True)
        return 0 if result["ok"] else 1
    if args.command == "knowledge-list":
        try:
            _print({"knowledge": KnowledgeCatalog(config).list()}, True)
            return 0
        except RuntimeError as exc:
            _print({"status": "blocked", "error": str(exc)}, True)
            return 1
    if args.command == "knowledge-retrieve":
        receipt = KnowledgeCatalog(config).retrieve_with_receipt(args.query)
        _print(receipt, True)
        return 0 if receipt["retrieval_status"] != "degraded" else 1
    if args.command == "review-ui-install":
        from ..review_ui_install import run
        try:
            _print(run(config, apply=args.apply), True)
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            _print({"ok": False, "error": str(exc)}, True)
            return 1
    if args.command == "bridge":
        UnixBridge(Path(args.socket) if args.socket else config.runtime_dir / "agentos.sock", store, config).serve_once()
        return 0
    if args.command == "install-plan":
        path = _render_launchagent(config)
        _print({"status": "rendered_only", "path": str(path), "next": f"审核后复制到 ~/Library/LaunchAgents/{path.name} 并执行 launchctl bootstrap"})
        return 0
    if args.command == "install-runner":
        try:
            result = _build_runner_app(config)
            result["next"] = "在系统设置 → 隐私与安全性 → 完全磁盘访问权限中添加并启用“小八 AgentOS Runner”，然后执行 install-service"
            _print(result, True)
            return 0
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command == "install-service":
        try:
            _print(_install_service(config, store, backup_database=args.backup_database), True)
            return 0
        except (OSError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    if args.command in ("agent-enable", "agent-disable"):
        try:
            _print(_set_agents(config, args.names, args.command == "agent-enable"), True)
            return 0
        except ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.command == "demo":
        demo_config = _demo_config(config)
        demo_store = EventStore(demo_config.database_path)
        demo_store.initialize()
        task_id = demo_store.submit(args.prompt, 1)
        result = Orchestrator(demo_config, demo_store).run_once()
        _print({"task_id": task_id, "production_loop": result, "event_chain": demo_store.verify_event_chain(), "database": str(demo_store.path)}, True)
        return 0
    if args.command == "run":
        if args.once:
            result = Orchestrator(config, store).run_once()
            _print(result, True)
            return 0 if result["status"] in (
                "idle", "completed", "retry", "frozen_paused", "degraded_health"
            ) else 1
        worker_state = _write_worker_state(config, store)
        print(json.dumps({"status": "worker_started", **worker_state}, ensure_ascii=False), flush=True)
        stopping = False

        def stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        idle_polls = 0
        total_polls = 0
        last_report_key = None
        while not stopping:
            try:
                reloaded = AppConfig.load(config.path)
                if reloaded.database_path != store.path:
                    result = {
                        "status": "blocked_configuration",
                        "error": "运行时数据库路径已改变；为避免跨库执行，请重启 Worker",
                    }
                else:
                    config = reloaded
                    heartbeat_polls = max(1, int(config.worker.get(
                        "preflight_heartbeat_polls", config.worker.get("log_heartbeat_polls", 60)
                    )))
                    # Full preflight on the first poll and on every heartbeat;
                    # in between, an empty queue costs one read-only query.
                    result = Orchestrator(config, store).run_once(
                        preflight_when_idle=(total_polls % heartbeat_polls == 0)
                    )
            except ConfigError as exc:
                result = {"status": "blocked_configuration", "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - the worker must outlive one bad poll
                # Exiting here would only hide the failure behind launchd
                # restarts. The lease is released below so the task is
                # recovered on the next poll instead of after its full lease.
                result = {
                    "status": "worker_error",
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:500],
                }
                try:
                    # Do not make the task wait out its whole lease.
                    result["leases_released"] = store.expire_running_leases()
                except Exception as release_exc:  # noqa: BLE001
                    result["lease_release_error"] = type(release_exc).__name__
            total_polls += 1
            report_key = (result.get("status"), result.get("error"))
            heartbeat = max(1, int(config.worker.get("log_heartbeat_polls", 60)))
            quiet_statuses = ("idle", "blocked_configuration", "degraded_health", "worker_error", "awaiting_host", "ask_user", "blocked_review_nodes")
            if result["status"] not in quiet_statuses or report_key != last_report_key or total_polls % heartbeat == 0:
                print(json.dumps(result, ensure_ascii=False), flush=True)
                last_report_key = report_key
            if result["status"] in quiet_statuses:
                idle_polls += 1
                if args.max_idle_polls and idle_polls >= args.max_idle_polls:
                    break
                time.sleep(int(config.worker["poll_seconds"]))
            else:
                idle_polls = 0
        return 0
    return None
