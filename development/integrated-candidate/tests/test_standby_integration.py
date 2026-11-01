"""主力集成时补的四条规则（2026-10-20）。

1. 中途补位后，挂掉的主后台记为外部故障；下一个任务在分派前就换成备用，不再等它超时。
2. 启用的主后台少于两个时，接单闸门挡住：备用只补故障，不顶替被停用的主后台。
3. 上班探针与接单闸门同一口径：一个主后台加健康备用、但另一个主后台被停用，不算可上班。
4. 构包门禁只认 external_upstream / external_auth 引起的补位。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_standby_session_and_gate as gate_tests
import test_standby_substitution as sub_tests
from xiaoba_agentos.adapters import AgentResult, PermanentAdapterError
from xiaoba_agentos.health import execution_blockers
from xiaoba_agentos.recovery import HEALTH_MARKER, live_agent_probe


PRIMARIES, STANDBY, _external = sub_tests.PRIMARIES, sub_tests.STANDBY, sub_tests._external
BUILD_RELEASE_MODULE = gate_tests.BUILD_RELEASE_MODULE


def _borrow(source, names):
    """Reuse the implementers' fixture helpers without re-running their tests."""
    return {name: source.__dict__[name] for name in names}


class IntegrationRulesTest(unittest.TestCase):
    locals().update(_borrow(sub_tests.StandbySubstitutionTest,
                            ("_config", "_store", "_probe", "_run", "_events", "_substitutions")))

    def test_an_internal_task_freeze_cannot_become_a_probe_failure_on_the_next_task(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a='healthy', b='healthy', s='healthy')
            first_id = store.submit('内部输出失败', 3)
            first, _ = self._run(config, store, {'b': PermanentAdapterError('工具调用标记：内部输出无效')})
            self.assertEqual('frozen_paused', first['status'])
            second_id = store.submit('独立下一任务', 3)
            self.assertTrue(execution_blockers(config, store))
            result, calls = self._run(config, store, {})
            self.assertEqual('degraded_health', result['status'])
            self.assertEqual({}, calls)
            self.assertEqual([], self._substitutions(store, second_id))
            self.assertEqual('pending', store.get_task(second_id)['status'])
            self.assertEqual(0, store.get_task(second_id)['attempts'])
            self.assertEqual('frozen_paused', store.get_task(first_id)['status'])

    def test_an_unprobed_primary_is_not_a_failed_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a='healthy', s='healthy')
            store.submit('主后台没有探针记录', 3)
            self.assertTrue(execution_blockers(config, store))
            result, calls = self._run(config, store, {})
            self.assertEqual('degraded_health', result['status'])
            self.assertEqual({}, calls)

    def test_a_real_failed_probe_can_cover_an_internal_health_marker_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory), PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a='healthy', s='healthy')
            store.record_agent_health('b', 'degraded', failure_class='internal_adapter_candidate',
                                      error='health probe returned no health marker',
                                      details={'health_marker': HEALTH_MARKER, 'health_probe_state': 'unknown'})
            task = store.submit('真实探针失败', 3)
            self.assertEqual([], execution_blockers(config, store))
            result, calls = self._run(config, store, {})
            self.assertEqual('completed', result['status'])
            self.assertEqual({'a': 1, 's': 1}, calls)
            evidence = self._substitutions(store, task)[0]
            self.assertEqual('health_probe_failed', evidence['reason'])
            self.assertEqual(HEALTH_MARKER, evidence['health_evidence']['probe']['health_marker'])

    def test_a_covered_primary_is_marked_blocked_so_the_next_task_swaps_before_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            store = self._store(config)
            self._probe(store, a="healthy", b="healthy", s="healthy")
            store.submit("第一个任务", 3)
            first, calls = self._run(config, store, {"b": _external("b 上游 timed out")})
            self.assertEqual("completed", first["status"])
            self.assertEqual({"a": 1, "b": 1, "s": 1}, calls)
            health = {row["agent_name"]: row for row in store.agent_health()}
            self.assertEqual("external_blocked", health["b"]["status"])
            self.assertEqual("external_upstream", health["b"]["failure_class"])

            second_id = store.submit("第二个任务", 3)
            second, calls = self._run(config, store, {})
            self.assertEqual("completed", second["status"])
            # b is not tried again (no second timeout); the standby covers it up front.
            self.assertEqual({"a": 1, "s": 1}, calls)
            events = self._substitutions(store, second_id)
            self.assertEqual("health_probe_failed", events[0]["reason"])
            # Round 36: the cover rests on the recorded external fault, and the
            # event says so in its health evidence (no probe marker is claimed).
            self.assertEqual("external_blocked", events[0]["health_evidence"]["status"])
            self.assertEqual("external_upstream", events[0]["health_evidence"]["failure_class"])
            self.assertNotIn("health_marker", events[0]["health_evidence"]["probe"])

    def test_fewer_enabled_primaries_than_required_blocks_intake_even_with_a_healthy_standby(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [dict(PRIMARIES[0]), dict(PRIMARIES[1], enabled=False), dict(STANDBY)]
            config = self._config(root, agents)
            store = self._store(config)
            self._probe(store, a="healthy", s="healthy")
            blockers = execution_blockers(config, store)
            self.assertTrue(any("启用的主后台只有 1 个" in item for item in blockers), blockers)

    def test_a_legacy_config_with_one_enabled_primary_and_no_standby_gets_no_standby_wording(self):
        # Without a standby, the "only N primaries" blocker (which talks about
        # the standby) is not used; the legacy gate words its own refusal.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, [dict(PRIMARIES[0]), dict(PRIMARIES[1], enabled=False)])
            store = self._store(config)
            self._probe(store, a="healthy")
            self.assertFalse(any("启用的主后台只有" in item for item in execution_blockers(config, store)))

    def test_two_enabled_primaries_without_a_standby_still_block_on_an_unhealthy_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, [dict(PRIMARIES[0]), dict(PRIMARIES[1])])
            store = self._store(config)
            self._probe(store, a="healthy", b="degraded")
            blockers = execution_blockers(config, store)
            self.assertTrue(blockers, "没有备用时，一个主后台不健康就照旧挡住")
            self.assertFalse(any("备用" in item for item in blockers), blockers)

    def test_a_broken_standby_cli_never_blocks_doctor_readiness(self):
        # qodercli is a link to a versioned binary; a self-update can leave it
        # dangling. That must not block session-start/intake/install while both
        # primaries are fine: doctor reports the standby on its own (R8).
        import inspect
        from xiaoba_agentos import health
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root, PRIMARIES + [STANDBY])
            # Make the standby a real qodercli backend whose command is gone.
            settings = root / ".qoder" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"model": {"name": "qfmodel"}}', encoding="utf-8")
            standby_entry = next(a for a in config.raw["agents"] if a.get("standby"))
            standby_entry.update(type="local_cli", driver="qoder", cli_route="local_qoder")
            config.raw.setdefault("cli_routes", {})["local_qoder"] = {
                "driver": "qoder", "selection": "qodercli_settings", "settings_path": str(settings)}
            config.raw.setdefault("integrations", {})["qoder_command"] = str(root / "missing" / "qodercli")
            primaries, standby = health.backend_cli_readiness(config)
            self.assertTrue(primaries["ok"], primaries)
            self.assertFalse(standby["ok"], standby)
            self.assertTrue(any("qoder" in str(p) for p in standby["problems"]), standby)
        # doctor's gate uses the primaries result and only reports the standby one.
        source = inspect.getsource(health.doctor)
        self.assertIn("local_routes, standby_routes = backend_cli_readiness(config)", source)
        self.assertIn('"standby_cli_routes": standby_routes', source)
        self.assertNotIn("standby_routes.get(", source)

    def test_session_probe_uses_the_same_bar_as_the_intake_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agents = [dict(PRIMARIES[0]), dict(PRIMARIES[1], enabled=False), dict(STANDBY)]
            config = self._config(root, agents)
            store = self._store(config)

            class Healthy:
                def __init__(self, agent):
                    self.agent = agent

                def run(self, _prompt, _context):
                    return AgentResult(self.agent.name, HEALTH_MARKER, 1, "mock", "m", "static")

            with patch("xiaoba_agentos.recovery.build_adapter", side_effect=Healthy):
                result = live_agent_probe(config, store)
            self.assertFalse(result["ok"], result)
            self.assertEqual("degraded_blocked", result["session_mode"])


class GateWhitelistTest(unittest.TestCase):
    locals().update(_borrow(gate_tests.StandbyReleaseGateTest,
                            tuple(n for n in gate_tests.StandbyReleaseGateTest.__dict__
                                  if n.startswith("_") and not n.startswith("__")) + ("setUp", "tearDown")))

    def test_a_substitution_after_a_non_external_failure_class_is_rejected(self):
        for failure_class in ("transient_unknown", "dsh_process_unavailable", ""):
            with self.subTest(failure_class=failure_class):
                db = self._setup_database(
                    completed_agents=["agy_review", "qoder_review"],
                    standby_event={"replaced": "dsh_review", "standby": "qoder_review",
                                   "reason": "external_failure", "failure_class": failure_class},
                )
                with self.assertRaisesRegex(RuntimeError, "must follow an external failure"):
                    BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)

    def test_external_auth_is_an_accepted_cause(self):
        db = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            standby_event={"replaced": "dsh_review", "standby": "qoder_review",
                           "reason": "external_failure", "failure_class": "external_auth"},
        )
        with self.assertRaisesRegex(RuntimeError, "固定后台名字或备用补位"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)


if __name__ == "__main__":
    unittest.main()
