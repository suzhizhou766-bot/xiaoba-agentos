"""备用后台补位的行为边界。

全部离线：临时库、mock 适配器、写进库里的健康行，不启动进程、不访问任何后台。

1. 健康门禁：两个主后台健康就能接单，备用不健康不阻断接单；一个主后台不健康
   而备用健康时可以接单；两个主后台都不健康时即使备用健康也阻断。
2. 分派前补位：某侧探针不健康且备用健康时，这一侧在分派前换成备用。
3. 中途补位：恰好一个主后台外部故障、另一个给出合格结论、本任务未用过备用时，
   备用替失败侧跑一次，两份来自不同后台的结论进入裁判。
4. 不补的情况逐条锁定：已有结论（含 FAIL）、内部故障、输出预算耗尽、思考过程
   泄露被隔离、两个主后台都失败、本任务已用过备用、备用未启用或不健康。
5. 留痕：事件、通知、judge.decided 与任务结果里的 standby_substitution 齐全，
   同一任务最多一次。
6. 恢复：task-resume 补跑失败侧时同样适用补位规则，且没有任何自动解冻。
7. 没有备用的旧配置行为与现在完全一致。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import (
    AgentResult,
    PermanentAdapterError,
    TransientAdapterError,
    _OUTPUT_LIMIT_ERROR,
    _output_limit_failure,
)
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.health import execution_blockers
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore


PRIMARIES = [
    {"name": "a", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
    {"name": "b", "type": "mock", "enabled": True, "role": "", "timeout_seconds": 1},
]
STANDBY = {
    "name": "s", "type": "mock", "enabled": True, "role": "",
    "timeout_seconds": 1, "standby": True,
}
# A real conclusion, including a FAIL verdict, is never re-reviewed by anybody else.
FAIL_VERDICT = "验收结论：FAIL，理由是缺少可复跑的验证步骤"
LEAKED_ANSWER = "The user is asking me to add numbers. Let me think. So the answer is 1517."


def _external(message):
    """A fault from outside this codebase, which is the only kind a standby covers."""
    error = PermanentAdapterError(message)
    error.external_upstream = True
    return error


def _transport(message):
    """The shape production actually raises for a timeout, 5xx or reset."""
    error = TransientAdapterError(message)
    error.external_upstream = True
    return error


class Scripted:
    """mock 适配器：按名字决定回答还是抛错，并记下每个后台被调用了几次。"""

    def __init__(self, agent, script, calls):
        self.agent = agent
        self.script = script
        self.calls = calls

    def run(self, prompt, _context):
        name = self.agent.name
        self.calls[name] = self.calls.get(name, 0) + 1
        behaviour = self.script.get(name)
        if callable(behaviour) and not isinstance(behaviour, BaseException):
            behaviour = behaviour()  # a side effect first, e.g. losing the lease
        if isinstance(behaviour, BaseException):
            raise behaviour
        output = behaviour if isinstance(behaviour, str) else f"{prompt} 的结论来自 {name}"
        return AgentResult(name, output, 1, "mock provider", f"model-{name}", "static")


class ReportingScripted(Scripted):
    """Like Scripted, but wired like a real CLI adapter: the orchestrator gives
    it a progress callback (lease check, receipt, progress event), and it
    reports once before answering."""

    progress_callback = None
    progress_interval = 5.0

    def run(self, prompt, context):
        if self.progress_callback is not None:
            self.progress_callback({"stage": "model_activity", "pid": 1, "elapsed_seconds": 1.0,
                                    "stdout_bytes": 10, "activity_events": 1,
                                    "no_progress_seconds": 0.0, "idle_timeout_seconds": 300})
        return super().run(prompt, context)


class StandbySubstitutionTest(unittest.TestCase):
    def _config(self, root, agents, minimum=2, budget=6):
        raw = {
            "schema_version": 1,
            "runtime_dir": str(root / "runtime"),
            "project_dir": str(root),
            "worker": {
                "poll_seconds": 1, "lease_seconds": 60, "max_attempts": 3,
                "retry_base_seconds": 0, "retry_max_seconds": 0, "parallelism": 2,
                "minimum_successful_agents": minimum, "max_agent_invocations_per_task": budget,
            },
            "memory": {
                "knowlp_enabled": False, "obsidian_vault": str(root),
                "obsidian_subdir": "AgentOS", "publish_enabled": False,
            },
            "integrations": {},
            "agents": agents,
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        }
        path = root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return AppConfig.load(path)

    def _store(self, config):
        store = EventStore(config.database_path)
        store.initialize()
        return store

    def _probe(self, store, **statuses):
        """One health row per name; an unhealthy row carries an external fault."""
        for name, status in statuses.items():
            healthy = status == "healthy"
            store.record_agent_health(
                name,
                status,
                failure_class=None if healthy else "external_upstream",
                error=None if healthy else f"{name} 上游 503 service unavailable",
                model=f"probe-model-{name}",
            )

    def _run(self, config, store, script):
        calls = {}
        with patch(
            "xiaoba_agentos.orchestrator.build_adapter",
            side_effect=lambda agent: Scripted(agent, script, calls),
        ):
            result = Orchestrator(config, store).run_once()
        return result, calls

    def _events(self, store, task_id, event_type):
        with store.read_only() as connection:
            return [
                json.loads(row["payload_json"] or "{}")
                for row in connection.execute(
                    "SELECT payload_json FROM events WHERE task_id=? AND event_type=? "
                    "ORDER BY sequence",
                    (task_id, event_type),
                )
            ]

    def _substitutions(self, store, task_id):
        return self._events(store, task_id, "standby.substituted")

    def _notifications(self, store, kind="standby.substituted"):
        return [item for item in store.pending_notifications() if item["kind"] == kind]

    # ------------------------------------------------------------ 接单门禁

    def test_healthy_pair_runs_alone_and_never_calls_the_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            self.assertEqual([], execution_blockers(config, store))
            task_id = store.submit("两个主后台都健康", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("completed", result["status"])
            self.assertEqual({"a": 1, "b": 1}, calls)
            self.assertIsNone(result["standby_substitution"])
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertEqual([], self._notifications(store))
            decided = self._events(store, task_id, "judge.decided")
            self.assertEqual(1, len(decided))
            self.assertIn("standby_substitution", decided[0])
            self.assertIsNone(decided[0]["standby_substitution"])
            self.assertEqual({"a", "b"}, set(decided[0]["scores"]))

    def test_unhealthy_standby_does_not_block_two_healthy_primaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="degraded")
            # Not a blocker for intake, and the state is still on the record for
            # doctor to report honestly instead of being quietly dropped.
            self.assertEqual([], execution_blockers(config, store))
            self.assertEqual(
                "degraded",
                {row["agent_name"]: row["status"] for row in store.agent_health()}["s"],
            )
            store.submit("备用坏了也要能接单", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("completed", result["status"])
            self.assertEqual({"a": 1, "b": 1}, calls)
            self.assertIsNone(result["standby_substitution"])

    def test_one_unhealthy_primary_with_a_healthy_standby_may_take_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="degraded", b="healthy", s="healthy")
            self.assertEqual([], execution_blockers(config, store))

    def test_two_unhealthy_primaries_block_even_with_a_healthy_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="degraded", b="external_blocked", s="healthy")
            # A standby may never pair with itself, so two downed primaries leave
            # no way to reach two independent conclusions.
            blockers = execution_blockers(config, store)
            self.assertEqual(2, len(blockers))
            self.assertTrue(blockers[0].startswith("a: "), blockers)
            self.assertTrue(blockers[1].startswith("b: "), blockers)
            task_id = store.submit("两个主后台都不健康", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("degraded_health", result["status"])
            self.assertEqual({}, calls)
            self.assertEqual("pending", store.get_task(task_id)["status"])
            self.assertEqual(0, store.get_task(task_id)["attempts"])
            self.assertEqual([], self._substitutions(store, task_id))

    def test_unhealthy_standby_leaves_one_unhealthy_primary_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="degraded", b="healthy", s="external_blocked")
            blockers = execution_blockers(config, store)
            self.assertEqual(1, len(blockers))
            self.assertTrue(blockers[0].startswith("a: "), blockers)
            task_id = store.submit("备用也不健康", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("degraded_health", result["status"])
            self.assertEqual({}, calls)
            self.assertEqual("pending", store.get_task(task_id)["status"])

        # The same task shape with the standby disabled is the pre-standby behaviour.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [dict(STANDBY, enabled=False)])
            store = self._store(config)
            self._probe(store, a="degraded", b="healthy", s="healthy")
            self.assertEqual(1, len(execution_blockers(config, store)))
            store.submit("备用未启用", 3)
            result, calls = self._run(config, store, {})
            self.assertEqual("degraded_health", result["status"])
            self.assertEqual({}, calls)

    def test_an_unhealthy_standby_is_never_called_mid_round(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="degraded")
            task_id = store.submit("备用不健康时中途外部故障", 3)

            result, calls = self._run(config, store, {"b": _external("b 上游 503")})

            self.assertNotIn("s", calls)
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertIsNone(result.get("standby_substitution"))
            self.assertEqual("frozen_paused", result["status"])
            self.assertEqual(["b"], result["failed_agents"])

    # ------------------------------------------------------------ 分派前补位

    def test_unhealthy_primary_is_replaced_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="degraded", b="healthy", s="healthy")
            task_id = store.submit("a 探针不健康", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("completed", result["status"])
            self.assertEqual({"b": 1, "s": 1}, calls)
            events = self._substitutions(store, task_id)
            self.assertEqual(1, len(events))
            self.assertEqual(
                {
                    "replaced": "a",
                    "standby": "s",
                    "reason": "health_probe_failed",
                    "failure_class": "external_upstream",
                    "model": "probe-model-s",
                },
                {key: events[0][key] for key in
                 ("replaced", "standby", "reason", "failure_class", "model")},
            )
            self.assertIn("503", events[0]["error"])
            self.assertEqual(events[0], result["standby_substitution"])
            decided = self._events(store, task_id, "judge.decided")[0]
            self.assertEqual(events[0], decided["standby_substitution"])
            # Two conclusions, and they come from two different backends.
            self.assertEqual({"b", "s"}, set(decided["scores"]))
            self.assertEqual({"b", "s"}, set(store.latest_agent_runs(task_id)))
            notifications = self._notifications(store)
            self.assertEqual(1, len(notifications))
            message = notifications[0]["payload"]["message"]
            for expected in ("主后台 a 本次不可用", "备用后台 s，模型", "health_probe_failed"):
                self.assertIn(expected, message)
            self.assertEqual(2, store.agent_invocation_count(task_id))

    def test_a_side_that_already_concluded_is_never_re_reviewed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("复审判定", 3)
            # Round 1: a concludes FAIL, b hits a retryable non-external fault.
            first, first_calls = self._run(
                config, store, {"a": FAIL_VERDICT, "b": TransientAdapterError("temporary")}
            )
            self.assertEqual("retry", first["status"])
            self.assertEqual({"a": 1, "b": 1}, first_calls)
            self.assertEqual([], self._substitutions(store, task_id))

            # a's probe goes stale before round 2, but a already answered: covering
            # a failed probe is not a licence to have the standby re-review a
            # conclusion that exists, FAIL included.
            self._probe(store, a="degraded", b="healthy", s="healthy")
            second, second_calls = self._run(config, store, {})

            self.assertEqual("completed", second["status"])
            self.assertEqual({"b": 1}, second_calls)
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertIsNone(second["standby_substitution"])
            self.assertEqual(
                FAIL_VERDICT, store.latest_agent_runs(task_id)["a"]["output"]
            )
            decided = self._events(store, task_id, "judge.decided")[0]
            self.assertEqual({"a", "b"}, set(decided["scores"]))
            self.assertIsNone(decided["standby_substitution"])

    # ------------------------------------------------------------ 中途补位

    def test_external_failure_mid_round_is_covered_and_reaches_the_judge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("中途外部故障", 3)

            result, calls = self._run(config, store, {"b": _external("b 上游 503")})

            self.assertEqual("completed", result["status"])
            self.assertEqual({"a": 1, "b": 1, "s": 1}, calls)
            events = self._substitutions(store, task_id)
            self.assertEqual(1, len(events))
            self.assertEqual(
                {
                    "replaced": "b",
                    "standby": "s",
                    "reason": "external_failure",
                    "failure_class": "external_upstream",
                    "model": "model-s",
                },
                {key: events[0][key] for key in
                 ("replaced", "standby", "reason", "failure_class", "model")},
            )
            self.assertIn("503", events[0]["error"])
            self.assertEqual(events[0], result["standby_substitution"])
            decided = self._events(store, task_id, "judge.decided")[0]
            self.assertEqual(events[0], decided["standby_substitution"])
            # The pair that reached the judge is the remaining primary plus the
            # standby: two different backends, never one backend twice.
            self.assertEqual({"a", "s"}, set(decided["scores"]))
            self.assertEqual({"a", "b", "s"}, set(store.latest_agent_runs(task_id)))
            notifications = self._notifications(store)
            self.assertEqual(1, len(notifications))
            message = notifications[0]["payload"]["message"]
            for expected in ("主后台 b 本次不可用", "备用后台 s，模型", "external_failure", "external_upstream"):
                self.assertIn(expected, message)
            self.assertEqual(3, store.agent_invocation_count(task_id))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_external_auth_failure_is_covered_too(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("中途鉴权故障", 3)

            result, calls = self._run(
                config, store, {"b": PermanentAdapterError("401 unauthorized")}
            )

            self.assertEqual("completed", result["status"])
            self.assertEqual({"a": 1, "b": 1, "s": 1}, calls)
            events = self._substitutions(store, task_id)
            self.assertEqual(1, len(events))
            self.assertEqual("external_auth", events[0]["failure_class"])
            self.assertEqual({"a", "s"}, set(
                self._events(store, task_id, "judge.decided")[0]["scores"]
            ))

    def test_the_substitute_answers_the_same_frozen_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            prompts = {}

            class Recording(Scripted):
                def run(self, prompt, context):
                    prompts[self.agent.name] = (prompt, context)
                    return Scripted.run(self, prompt, context)

            store.submit("同一题面", 3)
            calls = {}
            with patch(
                "xiaoba_agentos.orchestrator.build_adapter",
                side_effect=lambda agent: Recording(agent, {"b": _external("b 上游 503")}, calls),
            ):
                result = Orchestrator(config, store).run_once()

            self.assertEqual("completed", result["status"])
            self.assertEqual(prompts["a"][0], prompts["s"][0])
            self.assertEqual(prompts["b"][0], prompts["s"][0])
            self.assertEqual(prompts["a"][1], prompts["s"][1])

    def test_a_failed_substitute_freezes_and_lists_both_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("备用也挂了", 3)

            result, calls = self._run(
                config,
                store,
                {"b": _external("b 上游 503"), "s": _external("s 上游 503")},
            )

            self.assertEqual({"a": 1, "b": 1, "s": 1}, calls)
            self.assertEqual("frozen_paused", result["status"])
            self.assertEqual(["a"], result["completed_agents"])
            self.assertEqual({"b", "s"}, set(result["failed_agents"]))
            self.assertTrue(result["requires_explicit_resume"])
            self.assertEqual("retired", result["peer_auto_repair"])
            self.assertEqual("frozen_paused", store.get_task(task_id)["status"])
            events = self._substitutions(store, task_id)
            self.assertEqual(1, len(events))
            self.assertEqual(events[0], result["standby_substitution"])
            # Nothing unfreezes itself and nothing is queued for somebody else.
            self.assertFalse(store.has_runnable_work())
            self.assertEqual([], store.repair_jobs())
            notifications = self._notifications(store)
            self.assertEqual(1, len(notifications))
            self.assertIn("没有给出合格结论", notifications[0]["payload"]["message"])
            blocked = [item for item in store.pending_notifications()
                       if item["kind"] == "task.recovery_blocked"]
            self.assertEqual(1, len(blocked), [item["kind"] for item in store.pending_notifications()])
            self.assertEqual(task_id, blocked[0]["task_id"])

    def test_a_failed_substitute_on_transport_faults_retries_before_it_freezes(self):
        # Production raises transport faults as retryable errors: the task is
        # retried within its attempt budget (the covered primary is not rerun,
        # the standby is) and only then frozen for the user.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("备用也挂了（传输故障）", 3)
            script = {"b": _transport("b 上游 503"), "s": _transport("s 上游 503")}
            statuses, total = [], {}
            for _ in range(3):
                result, calls = self._run(config, store, script)
                statuses.append(store.get_task(task_id)["status"])
                for name, count in calls.items():
                    total[name] = total.get(name, 0) + count
            self.assertEqual(["retry", "retry", "frozen_paused"], statuses)
            self.assertEqual(1, total["b"], "被补位的主后台不再重跑")
            self.assertEqual(3, total["s"])
            self.assertEqual(1, len(self._substitutions(store, task_id)), "每个任务最多补一次")
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_an_exhausted_invocation_budget_stops_cover_through_run_once(self):
        # The budget exclusion was only tested on the decision function; this
        # drives run_once so the value it passes in is checked too.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY], budget=2)
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("预算只够两次调用", 3)
            _result, calls = self._run(config, store, {"b": _transport("b 上游 503")})
            self.assertNotIn("s", calls, "预算用完不得补位")
            self.assertEqual([], self._substitutions(store, task_id))

    def test_a_cover_runs_through_the_progress_and_lease_path(self):
        # The plain Scripted adapter has no progress callback, so the cover tests
        # above never touch the lease/receipt/progress branch; this one does.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("带进度回调的补位", 3)
            calls = {}
            with patch("xiaoba_agentos.orchestrator.build_adapter",
                       side_effect=lambda agent: ReportingScripted(agent, {"b": _transport("b 上游 503")}, calls)):
                result = Orchestrator(config, store).run_once()
            self.assertEqual("completed", result["status"])
            self.assertEqual(1, calls.get("s"))
            progress_agents = {event["agent"] for event in self._events(store, task_id, "agent.progress")}
            self.assertIn("s", progress_agents, "备用的进度要经过同一条续租与回执路径")
            self.assertEqual(1, len(self._substitutions(store, task_id)))
            self.assertTrue(store.verify_event_chain()["ok"])

    # ------------------------------------------------- 失去执行权：不补、不结算

    def _steal_lease(self, store, task_id, then):
        """A script entry: when the agent runs, the lease is taken away first,
        then `then` is what the run produces (an answer string or an exception).
        Scripted.run calls any callable entry, so every use passes this callable."""
        def action():
            with store.connect() as connection:
                connection.execute("UPDATE tasks SET attempts=attempts+1 WHERE id=?", (task_id,))
            return then
        return action

    def test_losing_the_lease_after_the_primaries_stops_without_cover_or_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("租约被收回", 3)
            result, calls = self._run(config, store, {
                "b": self._steal_lease(store, task_id, _transport("b 上游 503"))})
            self.assertEqual("lease_lost", result["status"])
            self.assertIn("standby_substitution", result)
            self.assertIsNone(result["standby_substitution"])
            self.assertNotIn("s", calls, "失去执行权后不得调用备用")
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertEqual([], self._events(store, task_id, "judge.decided"))
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_losing_the_lease_while_the_standby_runs_stops_without_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("备用运行中租约被收回", 3)
            result, calls = self._run(config, store, {
                "b": _transport("b 上游 503"),
                "s": self._steal_lease(store, task_id, "备用的结论")})
            self.assertEqual("lease_lost", result["status"])
            self.assertIn("standby_substitution", result)
            self.assertIsNone(result["standby_substitution"])
            self.assertEqual(1, calls.get("s"))
            self.assertEqual([], self._events(store, task_id, "judge.decided"))
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertTrue(store.verify_event_chain()["ok"])

    # ------------------------------------------------------------ 不补的情况

    def _seed_prior_cover_for_reporting(self, store, task_id):
        # Seed only the persisted history read by result reporting. This fixture
        # does not claim to exercise the separate recovery/resume protocol.
        record = {"replaced": "b", "standby": "s", "reason": "external_failure",
                  "failure_class": "external_upstream", "error": "b 上游 503",
                  "model": "model-s"}
        store.append_event(task_id, "standby.substituted", record)
        return record

    def test_lease_lost_keeps_an_existing_cover_without_new_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("已有补位记录，丢失租约", 3)
            record = self._seed_prior_cover_for_reporting(store, task_id)
            result, calls = self._run(config, store, {
                "b": self._steal_lease(store, task_id, _transport("b 上游 503"))})
            self.assertEqual("lease_lost", result["status"])
            self.assertEqual(record, result["standby_substitution"])
            self.assertNotIn("s", calls)
            self.assertEqual([record], self._substitutions(store, task_id))
            self.assertEqual([], self._events(store, task_id, "judge.decided"))
            self.assertNotEqual("completed", store.get_task(task_id)["status"])
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_an_early_task_failure_keeps_an_existing_cover_record(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("早期校验失败也报告已有补位", 3)
            record = self._seed_prior_cover_for_reporting(store, task_id)
            with patch("xiaoba_agentos.orchestrator.validate_plan",
                       side_effect=ValueError("synthetic invalid plan")):
                result, calls = self._run(config, store, {})
            self.assertEqual("failed", result["status"])
            self.assertEqual(record, result["standby_substitution"])
            self.assertEqual({}, calls)
            self.assertEqual([record], self._substitutions(store, task_id))
            self.assertEqual([], self._events(store, task_id, "judge.decided"))
            self.assertEqual("failed", store.get_task(task_id)["status"])
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_staging_retry_keeps_cover_and_completes_only_once(self):
        from xiaoba_agentos.memory import MemoryBridge
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("暂存失败重试仍保留补位记录", 3)
            record = self._seed_prior_cover_for_reporting(store, task_id)
            with patch.object(MemoryBridge, "stage_experience", side_effect=OSError("synthetic disk full")):
                first, calls = self._run(config, store, {})
            self.assertEqual("retry", first["status"])
            self.assertEqual(record, first["standby_substitution"])
            self.assertEqual({"a": 1, "b": 1}, calls)
            self.assertIsNone(store.get_memory_candidate(task_id))
            self.assertEqual([], self._events(store, task_id, "task.completed"))
            second, calls = self._run(config, store, {})
            self.assertEqual("completed", second["status"])
            self.assertEqual(record, second["standby_substitution"])
            self.assertEqual({}, calls, "重试暂存不能重复调用已有合格结果的后台")
            self.assertEqual(1, len(self._events(store, task_id, "task.completed")))
            self.assertEqual([record], self._substitutions(store, task_id))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_existing_cover_is_reported_even_without_a_current_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), PRIMARIES)
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy")
            task_id = store.submit("历史记录的报告不依赖当前备用配置", 3)
            record = self._seed_prior_cover_for_reporting(store, task_id)
            result, calls = self._run(config, store, {})
            self.assertEqual("completed", result["status"])
            self.assertEqual(record, result["standby_substitution"])
            self.assertEqual({"a": 1, "b": 1}, calls)
            self.assertEqual([record], self._substitutions(store, task_id))
            self.assertTrue(store.verify_event_chain()["ok"])

    def test_an_internal_fault_is_not_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("内部故障", 3)

            result, calls = self._run(
                config, store, {"b": PermanentAdapterError("内部错误：本地依赖不可用")}
            )

            self.assertNotIn("s", calls)
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertEqual("frozen_paused", result["status"])
            self.assertEqual(["b"], result["failed_agents"])
            self.assertIsNone(result["standby_substitution"])

    def test_an_exhausted_output_budget_is_not_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("任务太大，一轮输出写不完", 3)

            # The budget marker carries external_upstream as well, so it has to be
            # ruled out on its own: the task was too big, the backend is fine.
            result, calls = self._run(
                config, store, {"b": _output_limit_failure(_OUTPUT_LIMIT_ERROR)}
            )

            self.assertNotIn("s", calls)
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertEqual("frozen_paused", result["status"])
            self.assertTrue(result["output_budget_exhausted"])
            self.assertIsNone(result["standby_substitution"])
            # The backend is fine, only the task was too big: the healthy probe
            # row survives untouched, so nothing here can keep an unrelated task
            # out of the queue.
            self.assertEqual(
                {"b": "healthy"},
                {row["agent_name"]: row["status"]
                 for row in store.agent_health(enabled_names=["b"])},
            )

    def test_a_quarantined_reasoning_leak_is_not_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("思考过程泄露", 3)

            # a leaks and b fails externally: without the leak gate the standby
            # would be spent on a task that output hygiene already terminates.
            result, calls = self._run(
                config, store, {"a": LEAKED_ANSWER, "b": _external("b 上游 503")}
            )

            self.assertNotIn("s", calls)
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertEqual("failed", result["status"])
            self.assertEqual(["a"], [item["agent"] for item in result["output_hygiene"]])
            self.assertEqual(1, len(self._events(store, task_id, "output_hygiene.quarantined")))
            self.assertIsNone(result["standby_substitution"])

    def test_two_failed_primaries_are_not_covered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("两个主后台都失败", 3)

            result, calls = self._run(
                config,
                store,
                {"a": _external("a 上游 503"), "b": _external("b 上游 503")},
            )

            self.assertNotIn("s", calls)
            self.assertEqual([], self._substitutions(store, task_id))
            self.assertEqual("failed", result["status"])
            self.assertIsNone(result["standby_substitution"])
            self.assertEqual({"a", "b"}, set(store.latest_agent_runs(task_id)))

    def test_a_task_spends_the_standby_at_most_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            task_id = store.submit("备用只用一次", 3)
            frozen = self._run(
                config,
                store,
                {"b": _external("b 上游 503"), "s": _external("s 上游 503")},
            )[0]
            self.assertEqual("frozen_paused", frozen["status"])
            self.assertEqual(1, len(self._substitutions(store, task_id)))

            # The user resumes: both failed sides hold a current healthy probe,
            # the exact freeze hash is presented, and only the failed side re-runs.
            store.record_agent_health("b", "healthy", model="probe-model-b")
            store.record_agent_health("s", "healthy", model="probe-model-s")
            resumed = store.resume_task(
                task_id,
                frozen["freeze_hash"],
                config.runtime_dir / "receipts",
                enabled_names=["a", "b", "s"],
                health_max_age_seconds=86400,
            )
            self.assertEqual("retry", resumed["status"])
            self.assertEqual(1, resumed["resume_generation"])

            second, calls = self._run(config, store, {"b": _external("b 上游 503 再次")})

            # The substitution rule still applies on the resumed round -- it just
            # finds the cap already spent, so the standby is not called again.
            self.assertNotIn("s", calls)
            self.assertEqual({"b": 1}, calls)
            self.assertEqual(1, len(self._substitutions(store, task_id)))
            self.assertEqual("frozen_paused", second["status"])
            self.assertEqual(["b"], second["failed_agents"])
            self.assertEqual(["a"], second["completed_agents"])
            self.assertTrue(second["requires_explicit_resume"])
            # Round 32: the cover made on the first attempt is still named here.
            self.assertEqual(self._substitutions(store, task_id)[0], second["standby_substitution"])
            self.assertEqual(1, store.get_task(task_id)["resume_generation"])

    def test_a_retried_round_does_not_announce_the_same_cover_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="degraded", b="healthy", s="healthy")
            task_id = store.submit("分派前补位后又需要重跑一轮", 3)
            # Round 1 substitutes a before dispatch; b then fails retryably, so
            # the existing flow retries instead of settling.
            first, first_calls = self._run(
                config, store, {"b": TransientAdapterError("temporary")}
            )
            self.assertEqual("retry", first["status"])
            self.assertEqual({"b": 1, "s": 1}, first_calls)
            self.assertEqual(1, len(self._substitutions(store, task_id)))
            # Round 31: the retry/fail return names the cover too, like every other.
            self.assertEqual(self._substitutions(store, task_id)[0], first["standby_substitution"])

            second, second_calls = self._run(config, store, {})

            self.assertEqual("completed", second["status"])
            # The standby's round-1 conclusion is reused, not re-earned, and the
            # cover is not announced a second time.
            self.assertEqual({"b": 1}, second_calls)
            self.assertEqual(1, len(self._substitutions(store, task_id)))
            self.assertEqual(1, len(self._notifications(store)))
            self.assertEqual(
                self._substitutions(store, task_id)[0], second["standby_substitution"]
            )
            self.assertEqual({"b", "s"}, set(
                self._events(store, task_id, "judge.decided")[0]["scores"]
            ))

    def test_a_cover_already_spent_on_one_primary_is_not_reused_for_the_other(self):
        # Round 24: the standby covered a here; on a later round a is healthy
        # again and b is the side that is down. Swapping b would carry the
        # record that says "a was replaced", so the task freezes instead.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="degraded", b="healthy", s="healthy")
            task_id = store.submit("先替 a，后来坏的是 b", 3)
            first, _ = self._run(config, store, {"b": TransientAdapterError("temporary")})
            self.assertEqual("retry", first["status"])
            self.assertEqual(["a"], [e["replaced"] for e in self._substitutions(store, task_id)])

            self._probe(store, a="healthy", b="degraded")
            second, calls = self._run(config, store, {})

            self.assertEqual("frozen_paused", second["status"])
            self.assertEqual({}, calls)
            self.assertEqual(["b"], second["failed_agents"])
            self.assertEqual(["a"], [e["replaced"] for e in self._substitutions(store, task_id)])
            self.assertEqual([], self._events(store, task_id, "judge.decided"))
            # b's probe row keeps the probe's own error.
            row = {r["agent_name"]: r for r in store.agent_health(enabled_names=["b"], max_age_seconds=86400)}["b"]
            self.assertIn("503", row["error"])

    def test_no_enabled_primary_refuses_before_leasing_even_with_a_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [dict(item, enabled=False) for item in PRIMARIES] + [STANDBY]
            config = self._config(root, agents)
            store = self._store(config)
            self._probe(store, s="healthy")
            task_id = store.submit("只有备用", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("blocked_configuration", result["status"])
            self.assertEqual({}, calls)
            self.assertEqual("pending", store.get_task(task_id)["status"])

    def test_one_enabled_primary_with_a_standby_refuses_before_leasing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [PRIMARIES[0], dict(PRIMARIES[1], enabled=False), STANDBY]
            config = self._config(root, agents)
            store = self._store(config)
            self._probe(store, a="healthy", s="healthy")
            task_id = store.submit("只有一个主后台加备用", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("blocked_configuration", result["status"])
            self.assertEqual({}, calls)
            self.assertEqual("pending", store.get_task(task_id)["status"])
            self.assertTrue(execution_blockers(config, store))

    def test_one_enabled_primary_without_a_standby_refuses_before_leasing(self):
        # Round 31: no task could ever settle, so it stays queued instead of
        # being leased and failed; the refusal does not talk about a standby.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, [PRIMARIES[0], dict(PRIMARIES[1], enabled=False)])
            store = self._store(config)
            self._probe(store, a="healthy")
            task_id = store.submit("只有一个主后台、没有备用", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("blocked_configuration", result["status"])
            self.assertNotIn("备用", result["error"])
            self.assertEqual({}, calls)
            self.assertEqual("pending", store.get_task(task_id)["status"])

    # ------------------------------------------------------------ 旧配置不变

    def test_a_config_without_a_standby_behaves_exactly_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES)
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy")
            self.assertEqual([], execution_blockers(config, store))
            first_id = store.submit("没有备用的旧配置", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("completed", result["status"])
            self.assertEqual({"a": 1, "b": 1}, calls)
            self.assertIsNone(result["standby_substitution"])
            self.assertEqual([], self._substitutions(store, first_id))

            # One unhealthy primary still blocks the queue, with no cover available.
            self._probe(store, a="degraded")
            blockers = execution_blockers(config, store)
            self.assertEqual(1, len(blockers))
            self.assertTrue(blockers[0].startswith("a: "), blockers)
            second_id = store.submit("没有备用时一个主后台不健康", 3)
            blocked, blocked_calls = self._run(config, store, {})
            self.assertEqual("degraded_health", blocked["status"])
            self.assertEqual({}, blocked_calls)
            self.assertEqual("pending", store.get_task(second_id)["status"])
            self.assertEqual([], self._substitutions(store, second_id))

    def test_a_standby_is_never_dispatched_as_a_third_reviewer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            # minimum stays 2: a healthy standby must not turn the pair into a trio.
            store.submit("备用不是第三个审核方", 3)

            result, calls = self._run(config, store, {})

            self.assertEqual("completed", result["status"])
            self.assertEqual({"a": 1, "b": 1}, calls)
            self.assertEqual(2, len(result["scores"]))

    # ------------------------------------------------------------ 判断函数本身

    def test_collaboration_and_capability_tasks_are_excluded(self):
        # These shapes never reach the decision with mock agents (the capability
        # gate blocks them first), so the decision function is asked directly.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            orchestrator = Orchestrator(config, store)
            primaries = config.primary_agents
            standby = config.standby_agents[0]
            failure = {
                "agent": "b", "error": "上游 503", "failure_class": "external_upstream",
                "repairable": False, "external": True,
            }
            answered = [AgentResult("a", "结论与验证", 1)]
            arguments = {
                "standby": standby,
                "standby_usable": True,
                "already_substituted": False,
                "workflow": {},
                "required_capabilities": [],
                "needs_web": False,
                "primaries": primaries,
                "results": answered,
                "failures": [failure],
                "leaked": [],
                "budget_left": 3,
            }
            decision = orchestrator._standby_substitution(**arguments)
            self.assertIsNotNone(decision)
            self.assertEqual("b", decision[0].name)
            self.assertEqual("external_failure", decision[1]["reason"])

            for excluded in (
                {"workflow": {"phase": "work", "agents": ["a", "b", "s"]}},
                {"required_capabilities": ["filesystem"]},
                {"needs_web": True},
                {"leaked": ["a"]},
                {"already_substituted": True},
                {"standby_usable": False},
                {"standby": None},
                {"budget_left": 0},
                {"results": []},
                {"results": answered + [AgentResult("b", "另一份结论", 1)]},
                {"failures": [failure, dict(failure, agent="a")]},
                {"failures": [dict(failure, failure_class="internal_headroom")]},
                {"failures": [dict(failure, output_limit_exhausted=True,
                                   failure_scope="task_output_budget")]},
            ):
                with self.subTest(**{key: value for key, value in excluded.items()
                                     if key != "primaries"}):
                    self.assertIsNone(
                        orchestrator._standby_substitution(**dict(arguments, **excluded))
                    )

            # Self-defence that does not rely on the caller keeping results and
            # failures disjoint (raised by the round-14 DSH review): the failed
            # side's own partial text is never the "other" primary's answer...
            self.assertIsNone(orchestrator._standby_substitution(**dict(
                arguments, results=[AgentResult("b", "中断前的部分文字", 1)])))
            # ...and with a single enabled primary there is no pair to complete,
            # whatever sits in results.
            only_b = [agent for agent in primaries if agent.name == "b"]
            self.assertIsNone(orchestrator._standby_substitution(**dict(
                arguments, primaries=only_b, results=[AgentResult("b", "部分文字", 1)])))
            self.assertIsNone(orchestrator._standby_substitution(**dict(
                arguments, primaries=only_b, results=answered)))
            # Round-40 DSH review: a "standby" that is itself one of the
            # primaries -- the surviving one or the failed one -- would either
            # count one backend twice or replace itself. Refused without
            # relying on the config's name checks.
            for same in primaries:
                with self.subTest(standby_named=same.name):
                    self.assertIsNone(orchestrator._standby_substitution(**dict(
                        arguments, standby=same)))


if __name__ == "__main__":
    unittest.main()
