"""同伴自动修复退役后的行为边界。

四组断言，全部离线：临时库、模拟适配器、模拟探针，不启动进程、不访问后台。

1. 探测失败或单侧任务失败时，不再有同伴诊断、repair job 或 Headroom 重启；
   正常双后台与正式审核流程不变。
2. 冻结任务只能由带 freeze hash 的用户命令恢复，新冻结态与旧 resume_ready
   都合法，且只重跑失败侧。
3. 旧 repair_jobs 行是历史：不可运行、不被改写；真实任务租约仍被恢复。
4. 任务级输出预算耗尽与真正的环境故障分开记账、分开放行。
"""
import hashlib
import inspect
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos import recovery as recovery_module
from xiaoba_agentos.adapters import (
    AgentResult,
    PermanentAdapterError,
    TransientAdapterError,
    _OUTPUT_LIMIT_ERROR,
    _output_limit_failure,
)
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.health import declared_automatic_actions
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.recovery import HEALTH_MARKER, start_session_health
from xiaoba_agentos.store import EventStore


AGENTS = [
    {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1,
     "template": "任务 {prompt} 的方案 A，含结论与验证"},
    {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1,
     "template": "任务 {prompt} 的方案 B，含结论与验证"},
]


class RetirementTest(unittest.TestCase):
    def _config(self, root, agents=None, minimum=2, budget=6):
        raw = {
            "schema_version": 1,
            "runtime_dir": str(root / "runtime"),
            "project_dir": str(root),
            "worker": {
                "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 3,
                "retry_base_seconds": 0, "retry_max_seconds": 0, "parallelism": 2,
                "minimum_successful_agents": minimum,
                "max_agent_invocations_per_task": budget,
            },
            "memory": {
                "knowlp_enabled": False, "obsidian_vault": str(root),
                "obsidian_subdir": "AgentOS", "publish_enabled": False,
            },
            "integrations": {},
            "agents": agents if agents is not None else AGENTS,
            "recovery": {
                "repair_max_attempts": 2, "repair_retry_seconds": 1,
                "resume_requires_user": True,
                "automatic_actions": ["restart_owned_headroom_and_reprobe"],
            },
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        }
        path = root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return AppConfig.load(path)

    def _legacy_repair_job(self, store, job_id, status, lease_until=None):
        """Seed a repair row the way the retired auto-repair used to write it."""
        now = time.time()
        with store.connect() as connection:
            connection.execute(
                """INSERT INTO repair_jobs(id,failed_agent,repair_agent,status,failure_class,error,
                       attempts,max_attempts,run_after,lease_until,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, "b", "a", status, "internal_headroom", "Headroom unavailable",
                 1, 2, now - 10, lease_until, now - 10, now - 10),
            )

    def _repair_rows(self, store):
        with store.read_only() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM repair_jobs ORDER BY id")]

    def _events(self, store, task_id=None):
        with store.read_only() as connection:
            if task_id is None:
                return connection.execute("SELECT * FROM events").fetchall()
            return connection.execute("SELECT * FROM events WHERE task_id=?", (task_id,)).fetchall()

    def _resume(self, store, task_id, freeze_hash, receipt_root=None, names=("a", "b"), window=86400):
        return store.resume_task(
            task_id, freeze_hash, receipt_root,
            enabled_names=list(names), health_max_age_seconds=window,
        )

    # ---------------------------------------------------------------- group 1

    def test_retired_write_paths_no_longer_exist(self):
        for symbol in ("RepairController", "local_cli_readiness"):
            self.assertFalse(hasattr(recovery_module, symbol), symbol)
        for method in (
            "create_repair_job", "lease_repair_job", "finish_repair_job",
            "mark_task_resume_ready", "mark_recovered_frozen_tasks",
        ):
            self.assertFalse(hasattr(EventStore, method), method)
        # The control action used to be chosen from a peer's reply text; with the
        # marker gone from the module nothing can be talked into a restart.
        source = inspect.getsource(recovery_module)
        self.assertNotIn("REPAIR_DIAGNOSIS_COMPLETE", source)
        self.assertNotIn("restart_owned_headroom_and_reprobe", source)

    def test_probe_failure_records_health_without_peer_diagnosis_or_repair_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            prompts = []

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, prompt, _context):
                    prompts.append((self.agent.name, prompt))
                    if self.agent.name == "b":
                        error = TransientAdapterError("Headroom unavailable")
                        error.failure_class = "internal_headroom"
                        error.repairable = True
                        raise error
                    return AgentResult(self.agent.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent)):
                result = start_session_health(config, store)

            self.assertEqual("degraded_blocked", result["session_mode"])
            self.assertEqual("retired", result["peer_auto_repair"])
            self.assertEqual([], result["repair_jobs"])
            # One probe call per Agent and nothing else: no peer was asked to
            # diagnose b, and b was not probed a second time after a "restart".
            self.assertEqual({"a", "b"}, {name for name, _ in prompts})
            self.assertEqual(2, len(prompts))
            for _name, prompt in prompts:
                self.assertIn(HEALTH_MARKER, prompt)
            health = {row["agent_name"]: row for row in store.agent_health()}
            self.assertEqual("healthy", health["a"]["status"])
            # repairable is still recorded, but it drives nothing now.
            self.assertEqual("degraded", health["b"]["status"])
            self.assertEqual("internal_headroom", health["b"]["failure_class"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual([], store.pending_notifications())
            self.assertEqual([], store.list_tasks())

    def test_one_sided_task_failure_freezes_without_a_third_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("单侧失败", 3)
            prompts = []

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, prompt, _context):
                    prompts.append((self.agent.name, prompt))
                    if self.agent.name == "b":
                        error = TransientAdapterError("Headroom unavailable")
                        error.failure_class = "internal_headroom"
                        error.repairable = True
                        raise error
                    return AgentResult(self.agent.name, "单侧失败 结论与验证", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: Adapter(agent)):
                result = Orchestrator(config, store).run_once()

            self.assertEqual("frozen_paused", result["status"])
            self.assertEqual("retired", result["peer_auto_repair"])
            self.assertTrue(result["requires_explicit_resume"])
            self.assertEqual(["a"], result["completed_agents"])
            self.assertEqual(["b"], result["failed_agents"])
            # a ran the task, b ran the task. Nobody was asked to diagnose b.
            self.assertEqual(2, store.agent_invocation_count(task_id))
            self.assertEqual(2, len(prompts))
            self.assertTrue(all("诊断" not in prompt for _name, prompt in prompts))
            self.assertEqual([], store.repair_jobs())
            self.assertEqual(
                ["task.recovery_blocked"],
                [item["kind"] for item in store.pending_notifications()],
            )
            self.assertEqual(
                [], [row for row in self._events(store, task_id) if row["event_type"].startswith("repair.")]
            )
            self.assertEqual("degraded", {
                row["agent_name"]: row for row in store.agent_health()
            }["b"]["status"])

    def test_normal_dual_backend_and_formal_review_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("正常双后台", 2)
            result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual(task_id, result["task_id"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual([], store.pending_notifications())
            self.assertTrue((config.runtime_dir / "memory-outbox" / f"{task_id}.md").exists())
            self.assertIsNotNone(store.get_memory_candidate(task_id))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_shipped_config_declares_no_automatic_recovery_action(self):
        root = Path(__file__).resolve().parents[1]
        config = AppConfig.load(root / "config" / "agentos.json")
        # Diagnostics read back an empty list: this release acts on nothing.
        self.assertEqual([], declared_automatic_actions(config))
        self.assertNotIn("automatic_actions", config.raw["recovery"])
        self.assertTrue(config.raw["recovery"]["resume_requires_user"])
        # Routing, the enabled pair and worker policy are untouched.
        self.assertEqual({"agy_review", "dsh_review"}, {a.name for a in config.agents if a.enabled})
        self.assertEqual(2, int(config.worker["minimum_successful_agents"]))
        self.assertEqual(86400, int(config.worker["health_max_age_seconds"]))

    def test_a_config_still_declaring_the_old_action_reports_drift_not_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            self.assertEqual(
                ["restart_owned_headroom_and_reprobe"], declared_automatic_actions(config)
            )
            store = EventStore(config.database_path); store.initialize()

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    return AgentResult(self.agent.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent)):
                result = start_session_health(config, store)
            # Declaring the retired action changes nothing about what runs.
            self.assertEqual("retired", result["peer_auto_repair"])
            self.assertEqual([], result["repair_jobs"])
            self.assertEqual([], store.repair_jobs())

    # ---------------------------------------------------------------- group 2

    def _freeze_with_receipt(self, root, store, prompt="冻结恢复"):
        """Freeze a task with one completed side, a receipt file and a provider event."""
        task_id = store.submit(prompt, 3, snapshot={"evidence": "fixed"})
        store.lease_next(60)
        run_id = store.start_agent_run(task_id, "a")
        store.finish_agent_run(run_id, "completed", "成功侧输出，必须原样保留", None, 1)
        store.append_event(task_id, "agent.provider_attempt", {"provider": "original"})
        receipt_root = root / "receipts"
        receipt_dir = receipt_root / task_id
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt = receipt_dir / f"{run_id}.json"
        receipt.write_text('{"status":"succeeded"}', encoding="utf-8")
        frozen = store.freeze_task(
            task_id,
            failure_class="internal_headroom",
            failures=[{
                "agent": "b", "error": "Headroom unavailable",
                "failure_class": "internal_headroom", "repairable": True, "external": False,
            }],
            receipts=[{
                "agent": "a", "run_id": run_id, "name": receipt.name,
                "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
            }],
        )
        return task_id, frozen, receipt_root, receipt, run_id

    def test_session_start_cannot_unfreeze_and_resume_requires_the_exact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id, frozen, receipt_root, _receipt, _run_id = self._freeze_with_receipt(root, store)

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    return AgentResult(self.agent.name, HEALTH_MARKER, 1)

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda agent: Adapter(agent)):
                probe = start_session_health(config, store)
            self.assertTrue(probe["ok"])
            self.assertEqual("dual_agent", probe["session_mode"])
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])
            self.assertEqual([], store.pending_notifications())
            self.assertIsNone(store.lease_next(60))
            self.assertFalse(store.has_runnable_work())

            with self.assertRaisesRegex(ValueError, "explicit freeze hash is required"):
                self._resume(store, task_id, None, receipt_root)
            with self.assertRaisesRegex(ValueError, "freeze hash mismatch"):
                self._resume(store, task_id, "b" * 64, receipt_root)
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])

            resumed = self._resume(store, task_id, frozen["freeze_hash"], receipt_root)
            self.assertEqual("retry", resumed["status"])
            self.assertEqual("frozen_paused", resumed["resumed_from"])
            self.assertEqual(1, resumed["resume_generation"])
            self.assertEqual("explicit_user_command", [
                json.loads(row["payload_json"])["resumed_by"]
                for row in self._events(store, task_id) if row["event_type"] == "task.resumed"
            ][0])

    def test_resume_rejects_tampered_receipt_and_provider_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id, frozen, receipt_root, receipt, run_id = self._freeze_with_receipt(root, store)
            store.record_agent_health("b", "healthy")

            receipt.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "receipt hash mismatch"):
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root)
            receipt.write_text('{"status":"succeeded"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "receipt root is required"):
                self._resume(store, task_id, frozen["freeze_hash"], None)

            with store.connect() as connection:
                connection.execute(
                    "UPDATE events SET payload_json='{}' WHERE task_id=? "
                    "AND event_type='agent.provider_attempt'", (task_id,)
                )
            with self.assertRaisesRegex(ValueError, "event content hash mismatch"):
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root)

            with store.connect() as connection:
                connection.execute("UPDATE agent_runs SET output='tampered' WHERE id=?", (run_id,))
            with self.assertRaises(ValueError):
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root)
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])
            self.assertFalse(any(
                row["event_type"] == "task.resumed" for row in self._events(store, task_id)
            ))

    def test_failed_side_must_hold_a_current_enabled_healthy_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id, frozen, receipt_root, _receipt, _run_id = self._freeze_with_receipt(root, store)

            with self.assertRaisesRegex(ValueError, "no current healthy probe"):
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root)

            store.record_agent_health("b", "degraded", failure_class="internal_headroom")
            with self.assertRaisesRegex(ValueError, "no current healthy probe"):
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root)

            store.record_agent_health("b", "healthy")
            with self.assertRaisesRegex(ValueError, "no current healthy probe"):
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root, names=["a"])

            with store.connect() as connection:
                connection.execute(
                    "UPDATE agent_health SET last_probe_at=? WHERE agent_name='b'",
                    (time.time() - 7 * 86400,),
                )
            with self.assertRaisesRegex(ValueError, "no current healthy probe"):
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root)
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])

            store.record_agent_health("b", "healthy")
            self.assertEqual(
                "retry",
                self._resume(store, task_id, frozen["freeze_hash"], receipt_root)["status"],
            )

    def test_legacy_resume_ready_is_resumable_by_user_and_never_by_the_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id, frozen, receipt_root, _receipt, _run_id = self._freeze_with_receipt(root, store)
            store.record_agent_health("b", "healthy")
            # A row the retired auto-repair left behind: it looks ready, but
            # lease_next only takes pending/retry, so it is not self-executing.
            with store.connect() as connection:
                connection.execute(
                    "UPDATE tasks SET status='resume_ready' WHERE id=?", (task_id,)
                )
            self.assertFalse(store.has_runnable_work())
            self.assertIsNone(store.lease_next(60))
            self.assertEqual({"status": "idle"}, Orchestrator(config, store).run_once(preflight_when_idle=False))
            resumed = self._resume(store, task_id, frozen["freeze_hash"], receipt_root)
            self.assertEqual("resume_ready", resumed["resumed_from"])
            self.assertEqual("retry", resumed["status"])
            self.assertEqual(1, resumed["resume_generation"])

    def test_resume_reruns_only_the_failed_side_and_keeps_the_completed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id, frozen, receipt_root, _receipt, _run_id = self._freeze_with_receipt(root, store)
            store.record_agent_health("a", "healthy")
            store.record_agent_health("b", "healthy")
            self._resume(store, task_id, frozen["freeze_hash"], receipt_root)
            calls = {"a": 0, "b": 0}

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    calls[self.agent.name] += 1
                    return AgentResult(self.agent.name, f"{self.agent.name} 的结论与验证", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: Adapter(agent)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual({"a": 0, "b": 1}, calls)
            self.assertEqual(
                "成功侧输出，必须原样保留",
                store.latest_agent_runs(task_id)["a"]["output"],
            )
            self.assertEqual(1, store.get_task(task_id)["resume_generation"])

    # ---------------------------------------------------------------- group 3

    def test_legacy_repair_rows_are_never_run_never_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            self._legacy_repair_job(store, "job-pending", "pending")
            self._legacy_repair_job(store, "job-running", "running", lease_until=time.time() - 1)
            self._legacy_repair_job(store, "job-blocked", "external_blocked")
            seeded = self._repair_rows(store)

            # Nothing to run but history: an idle worker must not report busy.
            self.assertFalse(store.has_runnable_work())
            self.assertEqual({"status": "idle"}, Orchestrator(config, store).run_once(preflight_when_idle=False))
            self.assertEqual(0, store.recover_expired())

            task_id = store.submit("历史行不阻塞新任务", 2)
            result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual(task_id, result["task_id"])

            self.assertEqual(seeded, self._repair_rows(store))
            self.assertEqual(
                {"job-pending": "pending", "job-running": "running", "job-blocked": "external_blocked"},
                {item["id"]: item["status"] for item in store.repair_jobs()},
            )
            self.assertEqual(
                [], [row for row in self._events(store) if row["event_type"].startswith("repair.")]
            )
            with store.read_only() as connection:
                self.assertEqual(
                    EventStore.DATABASE_SCHEMA_VERSION,
                    connection.execute("PRAGMA user_version").fetchone()[0],
                )
                self.assertEqual(
                    3, connection.execute("SELECT COUNT(*) FROM repair_jobs").fetchone()[0]
                )
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_expired_task_lease_still_recovers_while_repair_history_sits_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            self._legacy_repair_job(store, "job-running", "running", lease_until=time.time() - 1)
            seeded = self._repair_rows(store)
            task_id = store.submit("租约恢复", 3)
            store.lease_next(-1)
            run_id = store.start_agent_run(task_id, "a")
            self.assertTrue(store.has_runnable_work())
            self.assertEqual(1, store.recover_expired())
            self.assertEqual("retry", store.get_task(task_id)["status"])
            run = next(item for item in store.task_runs(task_id) if item["id"] == run_id)
            self.assertEqual("interrupted", run["status"])
            self.assertIsNotNone(run["finished_at"])
            self.assertEqual(seeded, self._repair_rows(store))

    # ---------------------------------------------------------------- group 4

    def test_g2_output_budget_marker_is_the_only_budget_evidence(self):
        # Interface dependency on G2: the adapter sets the structured attribute,
        # and this card reads only that attribute -- never the error text.
        error = _output_limit_failure(_OUTPUT_LIMIT_ERROR)
        self.assertIs(True, getattr(error, "output_limit_exhausted", None))
        self.assertEqual("external_upstream", getattr(error, "failure_class", None))
        self.assertIsInstance(error, PermanentAdapterError)
        plain = PermanentAdapterError("上游返回上限相关字样")
        self.assertIsNone(getattr(plain, "output_limit_exhausted", None))

    def test_budget_failure_keeps_health_clean_and_does_not_block_the_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, minimum=2, budget=2)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("任务太大，一轮输出写不完", 3)

            class OverLimitAdapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    if self.agent.name == "b":
                        raise _output_limit_failure(_OUTPUT_LIMIT_ERROR)
                    return AgentResult(self.agent.name, "任务太大 结论与验证", 1)

            class HealthyAdapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    return AgentResult(self.agent.name, f"{self.agent.name} 的结论与验证", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: OverLimitAdapter(agent)):
                result = Orchestrator(config, store).run_once()

            self.assertEqual("frozen_paused", result["status"])
            self.assertTrue(result["output_budget_exhausted"])
            self.assertTrue(result["requires_explicit_resume"])
            frozen_failures = json.loads(store.get_task(task_id)["freeze_json"])["failures"]
            self.assertEqual(
                [{"agent": "b", "output_limit_exhausted": True, "failure_scope": "task_output_budget"}],
                [
                    {
                        "agent": item["agent"],
                        "output_limit_exhausted": item.get("output_limit_exhausted"),
                        "failure_scope": item.get("failure_scope"),
                    }
                    for item in frozen_failures
                ],
            )
            # The backend is fine; only the task was too big. No global row,
            # so nothing here can keep an unrelated task out of the queue.
            self.assertEqual([], store.agent_health())
            notification = store.pending_notifications()[0]
            self.assertEqual("task.recovery_blocked", notification["kind"])
            self.assertEqual("task_output_budget", notification["payload"]["failure_scope"])

            # A later ordinary task still runs once a valid probe exists.
            store.record_agent_health("a", "healthy")
            store.record_agent_health("b", "healthy")
            other = store.submit("下一条普通任务", 2)
            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: HealthyAdapter(agent)):
                second = Orchestrator(config, store).run_once()
            self.assertEqual("completed", second["status"])
            self.assertEqual(other, second["task_id"])
            # The over-limit task was not re-sent on its own.
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])
            self.assertEqual(2, store.agent_invocation_count(task_id))

    def test_budget_resume_keeps_the_split_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, minimum=2, budget=2)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("任务太大，一轮输出写不完", 3)

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    if self.agent.name == "b":
                        raise _output_limit_failure(_OUTPUT_LIMIT_ERROR)
                    return AgentResult(self.agent.name, "任务太大 结论与验证", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: Adapter(agent)):
                Orchestrator(config, store).run_once()
            freeze_hash = store.get_task(task_id)["freeze_hash"]
            # Skipping the health write is not a free pass: resume still needs a
            # real probe of the failed side before it will move the task.
            with self.assertRaisesRegex(ValueError, "no current healthy probe"):
                self._resume(store, task_id, freeze_hash, config.runtime_dir / "receipts")
            store.record_agent_health("b", "healthy")
            resumed = self._resume(store, task_id, freeze_hash, config.runtime_dir / "receipts")
            self.assertTrue(resumed["output_budget_exhausted"])
            self.assertIn("拆小任务", resumed["warning"])
            self.assertIn("b", resumed["warning"])
            self.assertIn("再次超限", resumed["warning"])

    def test_real_environment_failures_still_gate_the_queue(self):
        for message, expected in (
            ("429 upstream rate limit", "external_blocked"),
            ("401 unauthorized", "external_blocked"),
            ("connection reset by peer", "external_blocked"),
            ("Headroom unavailable", "degraded"),
        ):
            with self.subTest(message=message):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    config = self._config(root, minimum=2, budget=2)
                    store = EventStore(config.database_path); store.initialize()
                    task_id = store.submit("真实环境故障", 3)

                    class Adapter:
                        def __init__(self, agent): self.agent = agent
                        def run(self, _prompt, _context):
                            if self.agent.name == "b":
                                raise TransientAdapterError(message)
                            return AgentResult(self.agent.name, "真实环境故障 结论与验证", 1)

                    with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: Adapter(agent)):
                        result = Orchestrator(config, store).run_once()
                    self.assertEqual("frozen_paused", result["status"])
                    self.assertFalse(result["output_budget_exhausted"])
                    health = {row["agent_name"]: row for row in store.agent_health()}["b"]
                    self.assertEqual(expected, health["status"])
                    blocked = store.submit("下一条任务", 2)
                    gated = Orchestrator(config, store).run_once()
                    self.assertEqual("degraded_health", gated["status"])
                    self.assertEqual("pending", store.get_task(blocked)["status"])
                    self.assertEqual("frozen_paused", store.get_task(task_id)["status"])

    def test_mixed_budget_and_environment_failure_is_not_let_through(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = AGENTS + [
                {"name": "c", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1,
                 "template": "任务 {prompt} 的方案 C，含结论与验证"},
            ]
            config = self._config(root, agents=agents, minimum=2, budget=3)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("混合失败", 3)

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    if self.agent.name == "b":
                        raise _output_limit_failure(_OUTPUT_LIMIT_ERROR)
                    if self.agent.name == "c":
                        raise TransientAdapterError("429 upstream rate limit")
                    return AgentResult(self.agent.name, "混合失败 结论与验证", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: Adapter(agent)):
                result = Orchestrator(config, store).run_once()

            self.assertEqual("frozen_paused", result["status"])
            # The environment fault names the freeze, not the budget failure.
            self.assertFalse(result["output_budget_exhausted"])
            self.assertEqual("external_upstream", store.get_task(task_id)["failure_class"])
            self.assertEqual({"b", "c"}, set(result["failed_agents"]))
            self.assertEqual(["a"], result["completed_agents"])
            health = {row["agent_name"]: row for row in store.agent_health()}
            self.assertNotIn("b", health)
            self.assertEqual("external_blocked", health["c"]["status"])
            self.assertEqual(
                ["task.recovery_blocked"],
                [item["kind"] for item in store.pending_notifications()],
            )
            self.assertNotIn("failure_scope", store.pending_notifications()[0]["payload"])
            blocked = store.submit("下一条任务", 2)
            self.assertEqual("degraded_health", Orchestrator(config, store).run_once()["status"])
            self.assertEqual("pending", store.get_task(blocked)["status"])
            # The user may still resume it, and is told the budget half too.
            # Both failed sides need a fresh probe first, exactly as before.
            store.record_agent_health("b", "healthy")
            store.record_agent_health("c", "healthy")
            resumed = self._resume(
                store, task_id, store.get_task(task_id)["freeze_hash"],
                config.runtime_dir / "receipts", names=("a", "b", "c"),
            )
            self.assertTrue(resumed["output_budget_exhausted"])
            self.assertIn("拆小任务", resumed["warning"])

    def test_unstructured_legacy_failure_keeps_the_conservative_gate(self):
        # A freeze written before the marker existed carries only text. Text that
        # happens to mention a limit must not be read as a task-level budget:
        # the environment gate stays closed and no split warning is invented.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("旧冻结记录", 3)
            store.lease_next(60)
            run_id = store.start_agent_run(task_id, "a")
            store.finish_agent_run(run_id, "completed", "旧冻结记录 结论与验证", None, 1)
            store.freeze_task(
                task_id,
                failure_class="external_upstream",
                failures=[{"agent": "b", "error": "输出上限已耗尽", "external": True}],
            )
            store.record_agent_health("b", "external_blocked", failure_class="external_upstream")
            blocked = store.submit("下一条任务", 2)
            self.assertEqual("degraded_health", Orchestrator(config, store).run_once()["status"])
            self.assertEqual("pending", store.get_task(blocked)["status"])

            store.record_agent_health("b", "healthy")
            resumed = self._resume(store, task_id, store.get_task(task_id)["freeze_hash"])
            self.assertEqual("retry", resumed["status"])
            self.assertNotIn("warning", resumed)
            self.assertNotIn("output_budget_exhausted", resumed)


if __name__ == "__main__":
    unittest.main()
