import tempfile
import time
import unittest
import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from xiaoba_agentos.cli import (
    _candidate_sha256,
    _code_fingerprint,
    _health_report,
    _install_service,
    _load,
    _reconcile_failed_reuses,
    _rotate_worker_logs,
    _worker_runtime_status,
    _write_worker_state,
    build_parser,
    main,
)
from xiaoba_agentos.store import EventStore
from fixtures_review import _config, _raw_config, _run_cli
from xiaoba_agentos.orchestrator import Orchestrator


class CLIRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_only_load_does_not_create_or_migrate_database(self):
        database = self.root / "runtime" / "agentos.sqlite3"
        config = SimpleNamespace(database_path=database)
        with patch("xiaoba_agentos.cli.AppConfig.load", return_value=config):
            _loaded, store = _load("ignored.json", initialize=False)
        self.assertEqual(database, store.path)
        self.assertFalse(database.exists())

    def test_worker_state_detects_source_changes_after_start(self):
        source = self.root / "src" / "xiaoba_agentos"
        source.mkdir(parents=True)
        module = source / "worker.py"
        module.write_text("VERSION = 1\n", encoding="utf-8")
        runtime = self.root / "runtime"
        store = EventStore(runtime / "agentos.sqlite3")
        store.initialize()
        config = SimpleNamespace(project_dir=self.root, runtime_dir=runtime)

        initial = _code_fingerprint(self.root)
        _write_worker_state(config, store)
        self.assertTrue(_worker_runtime_status(config)["ok"])

        module.write_text("VERSION = 2\n", encoding="utf-8")
        self.assertNotEqual(initial, _code_fingerprint(self.root))
        status = _worker_runtime_status(config)
        self.assertFalse(status["ok"])
        self.assertFalse(status["code_matches_disk"])

    def test_review_commands_are_public_and_candidate_marker_is_hashable(self):
        parser = build_parser()
        parsed = parser.parse_args(
            ["review", "task-id", "--stage", "delivery", "--decision", "approve"]
        )
        self.assertEqual("review", parsed.command)
        staged = self.root / "candidate.md"
        marker = self.root / "candidate.published"
        marker.write_text("approved", encoding="utf-8")
        self.assertEqual(64, len(_candidate_sha256({"staged_path": str(staged)})))

    def test_recovery_commands_are_public(self):
        parser = build_parser()
        resume = parser.parse_args(["task-resume", "task-id", "--freeze-hash", "a" * 64])
        self.assertEqual("task-resume", resume.command)
        self.assertEqual("a" * 64, resume.freeze_hash)
        status = parser.parse_args(["repair-status", "--limit", "5"])
        self.assertEqual("repair-status", status.command)
        self.assertEqual(5, status.limit)

    def test_task_resume_forwards_enabled_names_window_and_budget_warning(self):
        config = _config(self.root)
        store = EventStore(config.database_path)
        store.initialize()
        task_id = store.submit("恢复一个超限任务", 3)
        store.lease_next(60)
        frozen = store.freeze_task(
            task_id, failure_class="external_upstream",
            failures=[{
                "agent": "b", "error": "输出上限", "failure_class": "external_upstream",
                "external": True, "output_limit_exhausted": True,
                "failure_scope": "task_output_budget",
            }],
        )
        store.record_agent_health("b", "healthy")
        with patch.object(EventStore, "resume_task", wraps=store.resume_task) as resume:
            code, payload = _run_cli([
                "--config", str(config.path), "task-resume", task_id,
                "--freeze-hash", frozen["freeze_hash"],
            ])
        self.assertEqual(0, code)
        self.assertEqual(["a", "b"], resume.call_args.kwargs["enabled_names"])
        self.assertEqual(86400.0, resume.call_args.kwargs["health_max_age_seconds"])
        self.assertEqual(config.runtime_dir / "receipts", resume.call_args.kwargs["receipt_root"])
        self.assertEqual("frozen_paused", payload["resumed_from"])
        self.assertTrue(payload["output_budget_exhausted"])
        self.assertIn("拆小任务", payload["warning"])

    def test_task_resume_blocks_without_a_current_healthy_probe(self):
        config = _config(self.root)
        store = EventStore(config.database_path)
        store.initialize()
        task_id = store.submit("环境还没恢复", 3)
        store.lease_next(60)
        frozen = store.freeze_task(
            task_id, failure_class="external_upstream",
            failures=[{"agent": "b", "error": "429 rate limit", "external": True}],
        )
        store.record_agent_health("b", "external_blocked")
        code, payload = _run_cli([
            "--config", str(config.path), "task-resume", task_id,
            "--freeze-hash", frozen["freeze_hash"],
        ])
        self.assertEqual(1, code)
        self.assertEqual("blocked", payload["status"])
        self.assertIn("no current healthy probe", payload["error"])
        self.assertEqual("frozen_paused", store.get_task(task_id)["status"])

    def test_repair_status_reports_retirement_and_still_shows_history(self):
        config = _config(self.root)
        store = EventStore(config.database_path)
        store.initialize()
        now = time.time()
        with store.connect() as connection:
            connection.execute(
                """INSERT INTO repair_jobs(id,failed_agent,status,failure_class,error,
                       run_after,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                ("legacy-job", "b", "completed", "internal_headroom", "Headroom unavailable",
                 now, now, now),
            )
        code, payload = _run_cli(["--config", str(config.path), "repair-status"])
        self.assertEqual(0, code)
        self.assertEqual("retired", payload["peer_auto_repair"])
        self.assertEqual(
            {"legacy-job": "completed"},
            {item["id"]: item["status"] for item in payload["repair_jobs"]},
        )

    def test_session_start_reports_live_probe_and_does_not_create_task(self):
        store = MagicMock()
        store.start_session.return_value = {"id": "session", "status": "active"}
        store.pending_notifications.return_value = []
        config = SimpleNamespace()
        with patch("xiaoba_agentos.cli._load", return_value=(config, store)), patch(
            "xiaoba_agentos.cli.doctor", return_value={"ok": True}
        ), patch(
            "xiaoba_agentos.cli.start_session_health",
            return_value={
                "ok": True, "session_mode": "dual_agent", "agents": {},
                "healthy_agents": ["a", "b"], "failed_agents": [],
                "repair_jobs": [], "peer_auto_repair": "retired",
            },
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["session-start"])
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertTrue(payload["ready_for_external_tasks"])
        store.submit.assert_not_called()

    def test_session_start_stops_before_session_and_probe_when_doctor_fails(self):
        store = MagicMock()
        config = SimpleNamespace()
        with patch("xiaoba_agentos.cli._load", return_value=(config, store)), patch(
            "xiaoba_agentos.cli.doctor", return_value={"ok": False, "problems": ["vault missing"]}
        ), patch("xiaoba_agentos.cli.start_session_health") as probe:
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["session-start"])
        self.assertEqual(1, code)
        self.assertEqual("blocked_local_health", json.loads(output.getvalue())["status"])
        store.start_session.assert_not_called()
        probe.assert_not_called()

    def test_settled_failed_reuse_can_retry_automatic_retirement(self):
        source_id = "11111111-1111-1111-1111-111111111111"
        store = MagicMock()
        store.memory_reuses.side_effect = lambda _task, outcome: (
            [] if outcome == "pending" else [{"source_task_id": source_id}]
        )
        store.get_memory_candidate.return_value = {
            "task_id": source_id,
            "status": "revalidation_required",
            "published_path": "/vault/source.md",
            "failed_reuse_count": 2,
        }
        config = SimpleNamespace(memory={"retire_after_failed_reuses": 2})
        retired = {"archive_path": "/archive/source.md", "retired": 1}
        with patch("xiaoba_agentos.review_flow.MemoryBridge") as bridge_class:
            bridge_class.return_value.retire_candidate.return_value = retired
            affected, retirements, errors = _reconcile_failed_reuses(config, store, "task")
        self.assertEqual([source_id], affected)
        self.assertEqual([retired], retirements)
        self.assertEqual([], errors)
        store.mark_memory_retired.assert_called_once()

    def test_explicit_retirement_db_failure_returns_reconciliation_nonzero(self):
        store = MagicMock()
        store.get_memory_candidate.return_value = {
            "task_id": "task",
            "status": "published",
            "published_path": "/vault/task.md",
        }
        store.mark_memory_retired.side_effect = RuntimeError("database unavailable")
        config = SimpleNamespace(memory={"obsidian_vault": "/vault"})
        with patch("xiaoba_agentos.cli._load", return_value=(config, store)), patch(
            "xiaoba_agentos.cli.MemoryBridge"
        ) as bridge_class:
            bridge_class.return_value.retire_candidate.return_value = {
                "archive_path": "/archive/task.md",
                "retired": 1,
            }
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["memory-retire", "task", "--reason", "test"])
        self.assertEqual(1, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("reconciliation_required", payload["status"])

    def test_rename_notes_records_a_path_left_unrecorded_by_an_interrupted_run(self):
        # 上一次已经把文件改了名，但没来得及记进数据库：这次文件不用再改，路径仍要补记；预览不写库
        store = MagicMock()
        store.memory_lifecycle_rows.return_value = [
            {"task_id": "task", "status": "published", "published_path": "/vault/task.md"},
        ]
        config = SimpleNamespace(memory={"obsidian_vault": "/vault"})
        for extra, expected_calls in (([], 1), (["--dry-run"], 0)):
            store.record_memory_note_renamed.reset_mock()
            with patch("xiaoba_agentos.cli._load", return_value=(config, store)), patch(
                "xiaoba_agentos.cli.MemoryBridge"
            ) as bridge_class:
                bridge_class.return_value.rename_note.return_value = {
                    "task_id": "task", "from": "/vault/readable-task.md", "to": "/vault/readable-task.md", "changed": False,
                }
                with redirect_stdout(io.StringIO()):
                    code = main(["memory-rename-notes"] + extra)
            self.assertEqual(0, code)
            self.assertEqual(expected_calls, store.record_memory_note_renamed.call_count)
        store.record_memory_note_renamed.assert_not_called()

    def test_publish_reactivation_failure_returns_reconciliation_nonzero(self):
        marker = self.root / "candidate.published"
        marker.write_text("approved", encoding="utf-8")
        digest = __import__("hashlib").sha256(marker.read_bytes()).hexdigest()
        candidate = {
            "task_id": "task",
            "status": "published",
            "staged_path": str(self.root / "candidate.md"),
            "evidence_sha256": digest,
        }
        store = MagicMock()
        store.get_memory_candidate.return_value = candidate
        config = SimpleNamespace(memory={})
        # 发布现在走共用的 review_flow.publish_approved_candidate，MemoryBridge
        # 在那里构造；补丁路径随之移动，断言不变。
        with patch("xiaoba_agentos.cli._load", return_value=(config, store)), patch(
            "xiaoba_agentos.review_flow.MemoryBridge"
        ) as bridge_class, patch(
            "xiaoba_agentos.cli._reconcile_validated_reuses",
            return_value=(["source"], [], [{"task_id": "source", "error": "index failed"}]),
        ):
            bridge_class.return_value.publish_candidate.return_value = {
                "path": "/vault/task.md", "published": 1
            }
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["publish-memory", "--task-id", "task"])
        self.assertEqual(1, code)
        payload = json.loads(output.getvalue())
        self.assertEqual("reconciliation_required", payload["status"])
        self.assertEqual(digest, payload["candidate_sha256"])

    def test_rotate_worker_logs_clears_empty_placeholders(self):
        # A 0-byte worker.err.log left behind by an earlier process makes
        # launchd's xpcproxy exit EX_CONFIG before the Runner ever runs, and
        # skipping empty files meant every reinstall hit the same wall.
        logs = self.root / "runtime" / "logs"
        logs.mkdir(parents=True)
        empty = logs / "worker.err.log"
        empty.write_text("", encoding="utf-8")
        kept = logs / "worker.out.log"
        kept.write_text("previous run output\n", encoding="utf-8")
        config = SimpleNamespace(runtime_dir=self.root / "runtime")

        rotated = _rotate_worker_logs(config)

        self.assertFalse(empty.exists())
        self.assertFalse(kept.exists())
        self.assertEqual(1, len(rotated))
        backup = Path(rotated[0])
        self.assertEqual("previous run output\n", backup.read_text(encoding="utf-8"))
        self.assertEqual(0o600, backup.stat().st_mode & 0o777)

    def test_health_report_separates_live_capacity_from_stale_rows(self):
        store = EventStore(self.root / "runtime" / "agentos.sqlite3")
        store.initialize()
        for name in ("live", "retired", "stale"):
            store.record_agent_health(name, "healthy")
        with store.connect() as connection:
            connection.execute(
                "UPDATE agent_health SET last_probe_at=? WHERE agent_name='stale'",
                (time.time() - 7 * 86400,),
            )
        config = SimpleNamespace(
            worker={"health_max_age_seconds": 86400},
            agents=[
                SimpleNamespace(name="live", enabled=True),
                SimpleNamespace(name="stale", enabled=True),
                SimpleNamespace(name="retired", enabled=False),
            ],
        )

        report = _health_report(config, store)

        self.assertEqual(["live"], [row["agent_name"] for row in report["agent_health"]])
        self.assertEqual(86400.0, report["agent_health_max_age_seconds"])
        excluded = {row["agent_name"]: row for row in report["agent_health_excluded"]}
        self.assertEqual({"retired", "stale"}, set(excluded))
        self.assertEqual("not_enabled", excluded["retired"]["reason"])
        self.assertEqual("probe_too_old", excluded["stale"]["reason"])
        # The raw status is still reported: an operator has to be able to see
        # that a record says healthy while the gates refuse to count it.
        self.assertEqual("healthy", excluded["retired"]["status"])
        self.assertGreater(excluded["stale"]["age_seconds"], 6 * 86400)

    def _preflight(self, **overrides):
        preflight = {
            "paths": {"node_command": True, "agy_command": True, "dsh_command": True},
            "event_chain": {"ok": True},
            "local_cli_routes": {"ok": True, "problems": []},
            "memory_probe": {"ok": True},
            "enabled_agents": ["agy_review", "dsh_review"],
            "minimum_successful_agents": 2,
            "review_nodes": {"enough": True},
        }
        preflight.update(overrides)
        return preflight

    def test_install_service_preflight_passes_without_a_gateway_check(self):
        preflight = self._preflight()
        self.assertNotIn("gateway", preflight)
        config = SimpleNamespace(runtime_dir=self.root / "runtime")
        with patch("xiaoba_agentos.cli.doctor", return_value=preflight), patch(
            "xiaoba_agentos.cli._secure_directory",
            side_effect=RuntimeError("preflight passed"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                _install_service(config, MagicMock())
        self.assertEqual("preflight passed", str(caught.exception))

    def test_install_service_preflight_reports_local_route_problems(self):
        preflight = self._preflight(
            local_cli_routes={"ok": False, "problems": ["agy_review: agy CLI 不可执行：/missing/agy"]}
        )
        config = SimpleNamespace(runtime_dir=self.root / "runtime")
        with patch("xiaoba_agentos.cli.doctor", return_value=preflight), patch(
            "xiaoba_agentos.cli._secure_directory", side_effect=AssertionError("must not install")
        ):
            with self.assertRaises(RuntimeError) as caught:
                _install_service(config, MagicMock())
        self.assertIn("agy CLI 不可执行：/missing/agy", str(caught.exception))
        self.assertNotEqual("安装前检查未通过：安装前基础检查未通过", str(caught.exception))


class MemoryReceiptCLITest(unittest.TestCase):
    """`memory-receipt` 与 review 输出必须给出可审计的晋升回执。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        (self.vault / "AgentOS").mkdir(parents=True)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps({
            "schema_version": 1,
            "runtime_dir": str(self.root / "runtime"),
            "project_dir": str(self.root),
            "worker": {
                "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 2,
                "retry_base_seconds": 1, "retry_max_seconds": 2, "parallelism": 1,
                "minimum_successful_agents": 1,
            },
            "memory": {
                "knowlp_enabled": False, "knowlp_python": "/python",
                "knowlp_module": "/knowlp.py", "obsidian_vault": str(self.vault),
                "obsidian_subdir": "AgentOS",
                "catalog_note": "70_复利规则/经验索引.md", "publish_enabled": False,
            },
            "integrations": {},
            "agents": [{"name": "mock", "type": "mock", "enabled": True}],
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        }, ensure_ascii=False), encoding="utf-8")
        self.store = EventStore(self.root / "runtime" / "agentos.sqlite3")
        self.store.initialize()
        self.task_id = self.store.submit("source", 1)
        self.store.complete_manually(self.task_id, "result")
        self.candidate = self.root / "candidate.md"
        self.candidate.write_text("---\nstatus: pending_review\n---\n\nbody\n", encoding="utf-8")
        self.sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        self.store.stage_memory_candidate(self.task_id, str(self.candidate), self.sha)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--config", str(self.config_path), *argv])
        return code, json.loads(output.getvalue())

    def test_receipt_command_reports_provisional_candidate(self):
        code, payload = self._run(["memory-receipt", self.task_id])
        self.assertEqual(0, code)
        self.assertEqual("pending_delivery_review", payload["status"])
        self.assertFalse(payload["active_retrieval"])
        self.assertFalse(payload["fully_approved"])
        self.assertFalse(payload["publish_ready"])

    def test_receipt_command_blocks_on_unknown_candidate(self):
        code, payload = self._run(["memory-receipt", "99999999-9999-9999-9999-999999999999"])
        self.assertEqual(1, code)
        self.assertEqual("blocked", payload["status"])

    def test_review_output_carries_receipt_fields(self):
        code, payload = self._run([
            "review", self.task_id, "--stage", "delivery", "--decision", "approve",
            "--note", "交付审核通过",
        ])
        self.assertEqual(0, code)
        self.assertEqual("delivery_approved", payload["memory_status"])
        self.assertEqual("pending_delivery_review", payload["previous_status"])
        self.assertEqual("delivery_approved", payload["reviewed_status"])
        self.assertTrue(payload["review_id"])
        code, receipt = self._run(["memory-receipt", self.task_id])
        self.assertEqual(0, code)
        self.assertTrue(receipt["stopped_after_delivery_review"])
        self.assertFalse(receipt["publish_ready"])


class IdlePollTest(unittest.TestCase):
    def test_has_runnable_work_covers_due_tasks_and_expired_leases(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "runtime" / "agentos.sqlite3")
            store.initialize()
            self.assertFalse(store.has_runnable_work())
            task_id = store.submit("任务", 2)
            self.assertTrue(store.has_runnable_work())
            with store.connect() as conn:
                conn.execute("UPDATE tasks SET status='retry',run_after=? WHERE id=?", (time.time() + 3600, task_id))
            self.assertFalse(store.has_runnable_work())
            with store.connect() as conn:
                conn.execute("UPDATE tasks SET status='running',lease_until=? WHERE id=?", (time.time() + 3600, task_id))
            self.assertFalse(store.has_runnable_work())
            with store.connect() as conn:
                conn.execute("UPDATE tasks SET lease_until=? WHERE id=?", (time.time() - 1, task_id))
            self.assertTrue(store.has_runnable_work())

    def test_empty_queue_skips_preflight_only_when_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            binding = {"ok": False, "problems": ["runtime source mismatch"]}
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value=binding) as check, patch.object(
                store, "verify_event_chain"
            ) as chain:
                self.assertEqual("idle", Orchestrator(config, store).run_once(preflight_when_idle=False)["status"])
                check.assert_not_called()
                chain.assert_not_called()
                # Default behaviour is unchanged: the full preflight still runs.
                self.assertEqual("blocked_configuration", Orchestrator(config, store).run_once()["status"])
                check.assert_called_once()

    def test_pending_work_always_passes_every_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            config = _config(Path(directory))
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("必须先过门禁", 2)
            binding = {"ok": False, "problems": ["runtime source mismatch"]}
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value=binding):
                result = Orchestrator(config, store).run_once(preflight_when_idle=False)
            self.assertEqual("blocked_configuration", result["status"])
            self.assertEqual("pending", store.get_task(task_id)["status"])
            self.assertEqual("completed", Orchestrator(config, store).run_once(preflight_when_idle=False)["status"])

    def test_worker_runs_full_preflight_first_then_on_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = _raw_config(root)
            raw["worker"]["log_heartbeat_polls"] = 3
            path = root / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with patch("xiaoba_agentos.cli.Orchestrator") as orchestrator, patch(
                "xiaoba_agentos.cli.time.sleep"
            ), redirect_stdout(io.StringIO()):
                orchestrator.return_value.run_once.return_value = {"status": "idle"}
                main(["--config", str(path), "run", "--max-idle-polls", "5"])
            flags = [call.kwargs["preflight_when_idle"] for call in orchestrator.return_value.run_once.call_args_list]
            self.assertEqual([True, False, False, True, False], flags)


if __name__ == "__main__":
    unittest.main()
