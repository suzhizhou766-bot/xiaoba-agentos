import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import AgentResult, PermanentAdapterError, TransientAdapterError
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.orchestrator import DeterministicJudge, Orchestrator
from xiaoba_agentos.output_hygiene import detect_reasoning_leak
from xiaoba_agentos.store import EventStore


class OrchestratorTest(unittest.TestCase):
    def _config(self, root, agents, minimum=2, budget=3):
        raw = {
            "schema_version": 1,
            "runtime_dir": str(root / "runtime"),
            "project_dir": str(root),
            "worker": {
                "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 3,
                "retry_base_seconds": 0, "retry_max_seconds": 0, "parallelism": 2,
                "minimum_successful_agents": minimum, "max_agent_invocations_per_task": budget,
            },
            "memory": {"knowlp_enabled": False, "obsidian_vault": str(root), "obsidian_subdir": "AgentOS", "publish_enabled": False},
            "integrations": {},
            "agents": agents,
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        }
        path = root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return AppConfig.load(path)

    def test_disabled_agents_do_not_consume_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": 1,
                "runtime_dir": str(root / "runtime"),
                "project_dir": str(root),
                "worker": {"poll_seconds": 1, "lease_seconds": 60, "max_attempts": 2, "retry_base_seconds": 1, "retry_max_seconds": 2, "parallelism": 1, "minimum_successful_agents": 1},
                "memory": {"knowlp_enabled": False, "obsidian_vault": str(root), "obsidian_subdir": "AgentOS", "publish_enabled": False},
                "integrations": {},
                "agents": [{"name": "disabled", "type": "mock", "enabled": False, "role": "", "timeout_seconds": 1}],
                "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
            }
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps(raw), encoding="utf-8")
            config = AppConfig.load(cfg_path)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("不要消费", 2)
            result = Orchestrator(config, store).run_once()
            self.assertEqual("blocked_configuration", result["status"])
            task = store.get_task(task_id)
            self.assertEqual("pending", task["status"])
            self.assertEqual(0, task["attempts"])

    def test_release_binding_failure_blocks_before_recovery_or_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [{"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1}]
            config = self._config(root, agents, minimum=1, budget=1)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("must remain pending", 2)
            binding = {"ok": False, "problems": ["runtime source mismatch"]}
            with patch("xiaoba_agentos.orchestrator.release_binding_status", return_value=binding), patch.object(
                store, "recover_expired"
            ) as recover:
                result = Orchestrator(config, store).run_once()
            self.assertEqual("blocked_configuration", result["status"])
            self.assertEqual(binding, result["release_binding"])
            recover.assert_not_called()
            self.assertEqual("pending", store.get_task(task_id)["status"])
            self.assertEqual(0, store.get_task(task_id)["attempts"])

    def test_offline_parallel_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = {
                "schema_version": 1,
                "runtime_dir": str(root / "runtime"),
                "project_dir": str(root),
                "worker": {"poll_seconds": 1, "lease_seconds": 60, "max_attempts": 2, "retry_base_seconds": 1, "retry_max_seconds": 2, "parallelism": 3, "minimum_successful_agents": 2},
                "memory": {"knowlp_enabled": False, "obsidian_vault": str(root), "obsidian_subdir": "AgentOS", "publish_enabled": False},
                "integrations": {},
                "agents": [
                    {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1, "template": "任务 {prompt} 的方案 A"},
                    {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1, "template": "任务 {prompt} 的方案 B，包含验证"},
                ],
                "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
            }
            cfg_path = root / "config.json"
            cfg_path.write_text(json.dumps(raw), encoding="utf-8")
            config = AppConfig.load(cfg_path)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("测试", 2)
            result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual(task_id, result["task_id"])
            self.assertTrue((config.runtime_dir / "memory-outbox" / f"{task_id}.md").exists())
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_retry_reuses_success_and_respects_invocation_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self._config(root, agents)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("修复认证失败", 3)
            calls = {"a": 0, "b": 0}

            class FakeAdapter:
                def __init__(self, agent):
                    self.agent = agent

                def run(self, _prompt, _context):
                    calls[self.agent.name] += 1
                    if self.agent.name == "b" and calls["b"] == 1:
                        raise TransientAdapterError("temporary")
                    return AgentResult(self.agent.name, f"修复认证失败：{self.agent.name} 验证完成", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: FakeAdapter(agent)):
                self.assertEqual("retry", Orchestrator(config, store).run_once()["status"])
                self.assertEqual("completed", Orchestrator(config, store).run_once()["status"])
            self.assertEqual({"a": 1, "b": 2}, calls)
            self.assertEqual(3, store.agent_invocation_count(task_id))

    def test_external_upstream_transient_can_reach_third_task_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [{"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1}]
            config = self._config(root, agents, minimum=1, budget=3)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("slow third-party provider", 3)
            calls = 0

            class FakeAdapter:
                def run(self, _prompt, _context):
                    nonlocal calls
                    calls += 1
                    if calls < 3:
                        error = TransientAdapterError("upstream disconnected")
                        error.external_upstream = True
                        raise error
                    return AgentResult("a", "completed after upstream recovery", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", return_value=FakeAdapter()):
                self.assertEqual("retry", Orchestrator(config, store).run_once()["status"])
                self.assertEqual("retry", Orchestrator(config, store).run_once()["status"])
                self.assertEqual("completed", Orchestrator(config, store).run_once()["status"])
            self.assertEqual(3, calls)
            with closing(sqlite3.connect(config.database_path)) as connection, connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='execution.upstream_retry'",
                    (task_id,),
                ).fetchone()[0]
            self.assertEqual(1, count)

    def test_permanent_error_freezes_completed_peer_when_threshold_is_impossible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self._config(root, agents, minimum=2, budget=6)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("test", 3)

            class FakeAdapter:
                def __init__(self, agent):
                    self.agent = agent

                def run(self, _prompt, _context):
                    if self.agent.name == "b":
                        raise PermanentAdapterError("bad auth")
                    return AgentResult(self.agent.name, "test 结论与验证", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: FakeAdapter(agent)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("frozen_paused", result["status"])
            self.assertEqual("retired", result["peer_auto_repair"])
            self.assertTrue(result["requires_explicit_resume"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])
            self.assertEqual(2, store.agent_invocation_count(task_id))

    def test_internal_failure_freezes_without_peer_repair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self._config(root, agents, minimum=2, budget=6)
            config.raw["recovery"] = {
                "repair_max_attempts": 2, "repair_retry_seconds": 1,
                "resume_requires_user": True,
                "automatic_actions": ["restart_owned_headroom_and_reprobe"],
            }
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("headroom repair", 3)

            class FakeAdapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    if self.agent.name == "b":
                        error = TransientAdapterError("Headroom unavailable")
                        error.failure_class = "internal_headroom"
                        error.repairable = True
                        raise error
                    return AgentResult(self.agent.name, "headroom repair verified", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: FakeAdapter(agent)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("frozen_paused", result["status"])
            self.assertEqual("retired", result["peer_auto_repair"])
            self.assertFalse(result["output_budget_exhausted"])
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])
            self.assertEqual([], store.repair_jobs())
            # Still a real fault: it keeps the queue blocked until a fresh probe
            # says otherwise. What is gone is the self-healing, not the gate.
            health = {row["agent_name"]: row for row in store.agent_health()}
            self.assertEqual("degraded", health["b"]["status"])
            self.assertEqual("internal_headroom", health["b"]["failure_class"])
            self.assertEqual(
                ["task.recovery_blocked"],
                [item["kind"] for item in store.pending_notifications()],
            )
            self.assertEqual(
                ["headroom repair verified"],
                [run["output"] for run in store.task_runs(task_id) if run["status"] == "completed"],
            )

    def test_external_partial_failure_freezes_without_repair_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self._config(root, agents, minimum=2, budget=2)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("external outage", 3)

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    if self.agent.name == "b":
                        error = TransientAdapterError("429 upstream rate limit")
                        error.external_upstream = True
                        raise error
                    return AgentResult("a", "external outage progress saved", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda a: Adapter(a)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("frozen_paused", result["status"])
            self.assertEqual("retired", result["peer_auto_repair"])
            self.assertEqual([], store.repair_jobs())
            self.assertEqual("external_blocked", store.agent_health()[0]["status"])
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])

    def test_resume_gets_failed_agent_budget_and_reuses_completed_peer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self._config(root, agents, minimum=2, budget=6)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("resume end to end", 3, snapshot={"evidence": "fixed"})
            store.lease_next(60)
            run_a = store.start_agent_run(task_id, "a")
            store.finish_agent_run(run_a, "completed", "resume end to end result A", None, 1)
            for _index in range(5):
                run_b = store.start_agent_run(task_id, "b")
                store.finish_agent_run(
                    run_b, "permanent_error", None, "Headroom unavailable", 1,
                    failure_class="internal_headroom",
                )
            frozen = store.freeze_task(
                task_id, failure_class="internal_headroom",
                failures=[{
                    "agent": "b", "error": "Headroom unavailable",
                    "failure_class": "internal_headroom", "repairable": True, "external": False,
                }],
            )
            store.record_agent_health("a", "healthy")
            store.record_agent_health("b", "healthy")
            resumed = store.resume_task(
                task_id, frozen["freeze_hash"],
                enabled_names=["a", "b"],
                health_max_age_seconds=float(config.worker.get("health_max_age_seconds", 86400)),
            )
            self.assertEqual("frozen_paused", resumed["resumed_from"])
            self.assertEqual(1, resumed["resume_generation"])
            calls = []

            class Adapter:
                def __init__(self, agent): self.agent = agent
                def run(self, _prompt, _context):
                    calls.append(self.agent.name)
                    return AgentResult(self.agent.name, "resume end to end result B 验证", 1)

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda a: Adapter(a)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual(["b"], calls)
            self.assertEqual(7, store.agent_invocation_count(task_id))
            self.assertEqual("resume end to end result A", store.latest_agent_runs(task_id)["a"]["output"])

    def test_recorded_degraded_health_leaves_task_unleased(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self._config(root, agents, minimum=2, budget=6)
            store = EventStore(config.database_path); store.initialize()
            task_id = store.submit("must remain queued", 3)
            store.record_agent_health("a", "healthy")
            store.record_agent_health("b", "external_blocked", failure_class="external_upstream")
            result = Orchestrator(config, store).run_once()
            self.assertEqual("degraded_health", result["status"])
            self.assertEqual("pending", store.get_task(task_id)["status"])

    def test_judge_does_not_reward_irrelevant_verbosity(self):
        concise = AgentResult("concise", "结论：修复认证失败。依据：令牌无效。验证：重新登录后测试。", 1)
        verbose = AgentResult("verbose", "天气晴朗，旅行、美食和电影都很有趣。" * 300, 1)
        winner, _scores = DeterministicJudge.choose("修复认证失败并验证", [concise, verbose])
        self.assertEqual("concise", winner.agent)

    def test_judge_is_called_without_any_weight_argument(self):
        """The retired strategy row was the only source of a per-agent weight.

        Strategy evolution was retired on 2026-10-11, so the judge is called with
        the prompt and the candidates and nothing else; a third argument would
        mean a weight path came back. `strategy_version` stays on the task row as
        history and keeps its default of 1.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [{"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1}]
            config = self._config(root, agents, minimum=1, budget=1)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("pinned judge", 2)
            with patch.object(DeterministicJudge, "choose", wraps=DeterministicJudge.choose) as choose:
                result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual((), choose.call_args.args[2:])
            self.assertEqual(1, store.get_task(task_id)["strategy_version"])

    def _hygiene_config(self, root, minimum):
        agents = [
            {"name": "clean", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            {"name": "leaky", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
        ]
        return self._config(root, agents, minimum=minimum, budget=6)

    def _hygiene_adapter(self, leaked):
        class FakeAdapter:
            def __init__(self, agent):
                self.agent = agent

            def run(self, prompt, _context):
                if self.agent.name == "leaky":
                    return AgentResult("leaky", leaked, 1, "AMD ai", "MiniCPM5-2B", "local_harness")
                return AgentResult(
                    "clean",
                    "结论：两项相加等于 1517。依据：逐位相加。风险：无。验证：重算一次。步骤：个位、十位、百位。",
                    1,
                )

        return FakeAdapter

    def test_leaked_reasoning_quarantines_candidate_and_fails_terminally(self):
        leaked = (
            "The user is asking me to add 1234 and 283. Let me think step by step. "
            "But wait, the prompt says to answer concisely, so the answer is 1517.\n</think>"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._hygiene_config(root, minimum=2)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("计算 1234 + 283", 2)
            with patch(
                "xiaoba_agentos.orchestrator.build_adapter",
                side_effect=self._hygiene_adapter(leaked),
            ):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("failed", result["status"])
            self.assertIn("output_hygiene", result["errors"][0])
            self.assertEqual(["leaky"], [item["agent"] for item in result["output_hygiene"]])
            task = store.get_task(task_id)
            self.assertEqual("failed", task["status"])
            self.assertIn("output_hygiene", task["last_error"])
            self.assertFalse((config.runtime_dir / "memory-outbox" / f"{task_id}.md").exists())
            with closing(sqlite3.connect(config.database_path)) as connection, connection:
                quarantined = connection.execute(
                    "SELECT COUNT(*) FROM events WHERE task_id=? AND event_type='output_hygiene.quarantined'",
                    (task_id,),
                ).fetchone()[0]
                repair_jobs = connection.execute("SELECT COUNT(*) FROM repair_jobs").fetchone()[0]
            self.assertEqual(1, quarantined)
            self.assertEqual(0, repair_jobs)

    def test_clean_peer_still_delivers_and_memory_stays_uncontaminated(self):
        leaked = "The user is asking me to add numbers. Let me think. So the answer is 1517."
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._hygiene_config(root, minimum=1)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("计算 1234 + 283", 2)
            with patch(
                "xiaoba_agentos.orchestrator.build_adapter",
                side_effect=self._hygiene_adapter(leaked),
            ):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual("clean", result["winner"])
            self.assertEqual({"clean"}, set(result["scores"]))
            self.assertNotIn("let me think", result["result"].lower())
            candidate = (config.runtime_dir / "memory-outbox" / f"{task_id}.md").read_text(encoding="utf-8")
            self.assertEqual([], detect_reasoning_leak(candidate))
            self.assertIn("1517", candidate)

    def test_partial_output_contract_holds_delivery_until_explicit_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [
                {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
                {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
            ]
            config = self._config(root, agents, minimum=2, budget=2)
            store = EventStore(config.database_path)
            store.initialize()
            task_id = store.submit("只输出最终的阿拉伯数字", 2)

            class FakeAdapter:
                def __init__(self, agent):
                    self.agent = agent

                def run(self, _prompt, _context):
                    return AgentResult(self.agent.name, "1517", 1, "test", "test-model", "local")

            with patch("xiaoba_agentos.orchestrator.build_adapter", side_effect=lambda agent: FakeAdapter(agent)):
                result = Orchestrator(config, store).run_once()

            self.assertEqual("constraint_hold", result["status"])
            self.assertIsNone(store.get_task(task_id)["result"])
            hold = store.get_constraint_hold(task_id, include_result=True)
            self.assertEqual("pending", hold["status"])
            self.assertIn("1517", hold["result"])
            self.assertIsNone(store.get_memory_candidate(task_id))
            with closing(sqlite3.connect(config.database_path)) as connection, connection:
                event = connection.execute(
                    "SELECT payload_json FROM events WHERE task_id=? AND event_type='constraint_detection_miss'",
                    (task_id,),
                ).fetchone()
            self.assertIsNotNone(event)

    def test_route_change_is_audited_and_notified_without_blocking_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(
                root,
                [{"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1}],
                minimum=1,
                budget=1,
            )
            store = EventStore(config.database_path)
            store.initialize()
            calls = {"count": 0}

            class FakeAdapter:
                def run(self, _prompt, _context):
                    calls["count"] += 1
                    provider = "first-provider" if calls["count"] == 1 else "second-provider"
                    return AgentResult("a", "结论：已完成。", 1, provider, "model", "local")

            with patch("xiaoba_agentos.orchestrator.build_adapter", return_value=FakeAdapter()):
                first = store.submit("first", 1)
                second = store.submit("second", 1)
                self.assertEqual("completed", Orchestrator(config, store).run_once()["status"])
                self.assertEqual("completed", Orchestrator(config, store).run_once()["status"])

            self.assertEqual("completed", store.get_task(first)["status"])
            self.assertEqual("completed", store.get_task(second)["status"])
            notifications = store.pending_notifications()
            self.assertEqual(1, len(notifications))
            self.assertEqual("agent.route_changed", notifications[0]["kind"])
            self.assertEqual("first-provider", notifications[0]["payload"]["from"]["provider"])
            self.assertEqual("second-provider", notifications[0]["payload"]["to"]["provider"])

    def test_real_worker_repeated_error_survives_restart(self):
        from xiaoba_agentos.orchestrator import Orchestrator
        from xiaoba_agentos.store import EventStore
        from xiaoba_agentos.adapters import TransientAdapterError
        with tempfile.TemporaryDirectory() as d:
            cfg=OrchestratorTest()._config(Path(d),[{'name':'a','type':'mock','enabled':True,'role':'','timeout_seconds':1}],minimum=1,budget=5)
            store=EventStore(cfg.database_path);store.initialize(); tid=store.submit('test',5)
            with patch('xiaoba_agentos.orchestrator.build_adapter') as adapter:
                adapter.return_value.run.side_effect=TransientAdapterError('same tool error')
                first=Orchestrator(cfg,store).run_once()
                second=Orchestrator(cfg,store).run_once()
                self.assertEqual('retry',first['status'])
                self.assertEqual('failed',second['status'])
                self.assertEqual('idle',Orchestrator(cfg,store).run_once()['status'])
                self.assertEqual(2,adapter.return_value.run.call_count)
            receipts=list((cfg.runtime_dir/'receipts'/tid).glob('*.json'))
            self.assertEqual(2,len(receipts))
            self.assertTrue(all(next(iter(json.loads(p.read_text())['receipts'].values()))['status']=='failed' for p in receipts))

    def test_worker_capability_block_releases_lease(self):
        from xiaoba_agentos.orchestrator import Orchestrator
        from xiaoba_agentos.store import EventStore
        with tempfile.TemporaryDirectory() as d:
            cfg=OrchestratorTest()._config(Path(d),[{'name':'a','type':'mock','enabled':True,'role':'','timeout_seconds':1}],minimum=1,budget=3)
            store=EventStore(cfg.database_path);store.initialize()
            tid=store.submit('write',3,snapshot={'required_capabilities':['shell_write_workspace']})
            with patch('xiaoba_agentos.orchestrator.build_adapter') as adapter:
                self.assertEqual('blocked',Orchestrator(cfg,store).run_once()['status'])
                adapter.assert_not_called()
            self.assertEqual('failed',store.get_task(tid)['status'])


if __name__ == "__main__":
    unittest.main()
