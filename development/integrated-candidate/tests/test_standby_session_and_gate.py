import hashlib
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import AgentResult, TransientAdapterError
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.recovery import (
    HEALTH_MARKER,
    live_agent_probe,
    start_session_health,
)
from xiaoba_agentos.store import EventStore

BUILD_RELEASE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_release.py"
BUILD_RELEASE_SPEC = importlib.util.spec_from_file_location("build_release", BUILD_RELEASE_SCRIPT)
BUILD_RELEASE_MODULE = importlib.util.module_from_spec(BUILD_RELEASE_SPEC)
assert BUILD_RELEASE_SPEC and BUILD_RELEASE_SPEC.loader
BUILD_RELEASE_SPEC.loader.exec_module(BUILD_RELEASE_MODULE)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StandbySessionProbeTest(unittest.TestCase):
    """验证上班探针 recovery.py 的四种组合与旧配置行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _config(self, agents=None):
        raw = {
            "schema_version": 1,
            "runtime_dir": str(self.root / "runtime"),
            "project_dir": str(self.root),
            "worker": {
                "poll_seconds": 1,
                "lease_seconds": 60,
                "max_attempts": 3,
                "retry_base_seconds": 1,
                "retry_max_seconds": 2,
                "parallelism": 3,
                "minimum_successful_agents": 2,
                "max_agent_invocations_per_task": 6,
            },
            "memory": {
                "knowlp_enabled": False,
                "obsidian_vault": str(self.root),
                "obsidian_subdir": "AgentOS",
                "publish_enabled": False,
            },
            "integrations": {},
            "agents": agents
            or [
                {
                    "name": "agy_review",
                    "type": "mock",
                    "driver": "agy",
                    "enabled": True,
                    "role": "primary",
                    "timeout_seconds": 1,
                    "standby": False,
                },
                {
                    "name": "dsh_review",
                    "type": "mock",
                    "driver": "dsh",
                    "enabled": True,
                    "role": "primary",
                    "timeout_seconds": 1,
                    "standby": False,
                },
                {
                    "name": "qoder_review",
                    "type": "mock",
                    "driver": "qoder",
                    "enabled": True,
                    "role": "standby",
                    "timeout_seconds": 1,
                    "standby": True,
                },
            ],
            "recovery": {
                "repair_max_attempts": 2,
                "repair_retry_seconds": 1,
                "resume_requires_user": True,
            },
            "evolution": {
                "minimum_samples": 1,
                "promotion_margin": 0.05,
                "latency_penalty": 0.01,
            },
        }
        path = self.root / "config.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return AppConfig.load(path)

    def test_probe_combo1_both_primary_healthy_standby_healthy(self):
        """组合1：主主健康（备用也健康） -> ok, session_mode: dual_agent"""
        config = self._config()
        store = EventStore(config.database_path)
        store.initialize()

        class Adapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
            result = start_session_health(config, store)

        self.assertTrue(result["ok"])
        self.assertEqual("dual_agent", result["session_mode"])
        self.assertEqual(["agy_review", "dsh_review", "qoder_review"], result["healthy_agents"])
        self.assertEqual([], result["failed_agents"])
        self.assertNotIn("standby_substitution", result)

    def test_a_failed_probe_is_covered_whatever_its_cause(self):
        """用户 2026-10-21 定（甲方案）：真实探针失败就补位，即使原因是内部类别。"""
        config = self._config()
        store = EventStore(config.database_path)
        store.initialize()

        class Adapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                if self.name == "agy_review":
                    raise RuntimeError("agy headroom unavailable")
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
            result = start_session_health(config, store)

        self.assertTrue(result["ok"])
        self.assertEqual("standby_substituted", result["session_mode"])
        self.assertEqual("agy_review", result["replaced"])
        row = store.agent_health(enabled_names=["agy_review"])[0]
        self.assertEqual("internal_headroom", row["failure_class"])

    def test_probe_combo2_one_primary_healthy_and_standby_healthy(self):
        """组合2：一主加备用（一主外部故障，备用健康） -> ok, session_mode: standby_substituted，写明补位信息"""
        config = self._config()
        store = EventStore(config.database_path)
        store.initialize()

        class Adapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                if self.name == "dsh_review":
                    err = TransientAdapterError("dsh connection timed out")
                    err.external_upstream = True
                    raise err
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
            result = start_session_health(config, store)

        self.assertTrue(result["ok"])
        self.assertEqual("standby_substituted", result["session_mode"])
        self.assertEqual(["agy_review", "qoder_review"], result["healthy_agents"])
        self.assertEqual(["dsh_review"], result["failed_agents"])
        self.assertEqual("dsh_review", result["replaced"])
        self.assertEqual("qoder_review", result["standby"])
        self.assertEqual(
            {
                "replaced": "dsh_review",
                "standby": "qoder_review",
                "reason": "health_probe_failed",
            },
            result["standby_substitution"],
        )

    def test_probe_combo3_only_standby_healthy_both_primary_failed(self):
        """组合3：仅备用健康（两个主后台都挂） -> 不 ok, session_mode: degraded_blocked"""
        config = self._config()
        store = EventStore(config.database_path)
        store.initialize()

        class Adapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                if self.name in {"agy_review", "dsh_review"}:
                    err = TransientAdapterError(f"{self.name} upstream 503 unavailable")
                    err.external_upstream = True
                    raise err
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
            result = start_session_health(config, store)

        self.assertFalse(result["ok"])
        self.assertEqual("degraded_blocked", result["session_mode"])
        self.assertEqual(["qoder_review"], result["healthy_agents"])
        self.assertEqual(["agy_review", "dsh_review"], result["failed_agents"])
        self.assertNotIn("standby_substitution", result)

    def test_probe_combo4_both_primary_healthy_standby_failed(self):
        """组合4：主主健康但备用坏 -> ok, session_mode: dual_agent，如实列出备用不可用"""
        config = self._config()
        store = EventStore(config.database_path)
        store.initialize()

        class Adapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                if self.name == "qoder_review":
                    err = TransientAdapterError("qodercli crashed: 503 service unavailable")
                    err.external_upstream = True
                    raise err
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
            result = start_session_health(config, store)

        self.assertTrue(result["ok"])
        self.assertEqual("dual_agent", result["session_mode"])
        self.assertEqual(["agy_review", "dsh_review"], result["healthy_agents"])
        self.assertEqual(["qoder_review"], result["failed_agents"])
        self.assertNotIn("standby_substitution", result)
        self.assertEqual("external_blocked", result["agents"]["qoder_review"]["status"])

    def test_probe_one_primary_healthy_standby_failed(self):
        """一主健康但备用也坏 -> 不 ok, session_mode: degraded_blocked"""
        config = self._config()
        store = EventStore(config.database_path)
        store.initialize()

        class Adapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                if self.name in {"dsh_review", "qoder_review"}:
                    err = TransientAdapterError("timeout")
                    err.external_upstream = True
                    raise err
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
            result = start_session_health(config, store)

        self.assertFalse(result["ok"])
        self.assertEqual("degraded_blocked", result["session_mode"])
        self.assertEqual(["agy_review"], result["healthy_agents"])
        self.assertEqual(["dsh_review", "qoder_review"], result["failed_agents"])

    def test_probe_legacy_config_without_standby_stays_unchanged(self):
        """旧配置（无备用后台）行为完全不变"""
        agents = [
            {"name": "agy_review", "type": "mock", "enabled": True, "role": "primary", "timeout_seconds": 1},
            {"name": "dsh_review", "type": "mock", "enabled": True, "role": "primary", "timeout_seconds": 1},
        ]
        config = self._config(agents=agents)
        store = EventStore(config.database_path)
        store.initialize()

        class Adapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: Adapter(a.name)):
            result = start_session_health(config, store)
        self.assertTrue(result["ok"])
        self.assertEqual("dual_agent", result["session_mode"])

        class FailingAdapter:
            def __init__(self, name):
                self.name = name

            def run(self, _prompt, _context):
                if self.name == "dsh_review":
                    raise RuntimeError("timeout")
                return AgentResult(self.name, HEALTH_MARKER, 10, provider="mock", model="fast")

        with patch("xiaoba_agentos.recovery.build_adapter", side_effect=lambda a: FailingAdapter(a.name)):
            result = start_session_health(config, store)
        self.assertFalse(result["ok"])
        self.assertEqual("degraded_blocked", result["session_mode"])


class StandbyReleaseGateTest(unittest.TestCase):
    """验证 scripts/build_release.py 发版门禁对备用补位与老路径的核验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "config").mkdir()
        (self.root / "config" / "agentos.json").write_text(
            json.dumps(
                {
                    "worker": {"minimum_successful_agents": 2},
                    "agents": [
                        {"name": "agy_review", "enabled": True, "standby": False},
                        {"name": "dsh_review", "enabled": True, "standby": False},
                        {"name": "qoder_review", "enabled": True, "standby": True},
                    ],
                }
            ),
            encoding="utf-8",
        )
        (self.root / "SOURCE_MANIFEST.json").write_text("{}\n", encoding="utf-8")
        self.source_manifest_sha256 = _sha256(self.root / "SOURCE_MANIFEST.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_database(
        self,
        task_id="task-1",
        completed_agents=None,
        standby_event=None,
        judge_payload=None,
        healthy_agents=None,
        replaced_evidence=True,
        replaced_health=None,
        replaced_run_class=None,
    ):
        """`replaced_evidence` writes what production records about the replaced
        side before a substitution: a failed run in this task (mid-round cover)
        or a failed health record (pre-dispatch cover). `replaced_health`
        overrides that health record's (status, failure_class)."""
        database_path = self.root / "test_acceptance.sqlite3"
        if database_path.exists():
            database_path.unlink()
        connection = sqlite3.connect(database_path)
        connection.execute(
            """CREATE TABLE tasks (
                id TEXT PRIMARY KEY, status TEXT, execution_mode TEXT, result TEXT,
                provenance_json TEXT, execution_snapshot_json TEXT, execution_snapshot_hash TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE agent_runs (
                task_id TEXT, agent_name TEXT, status TEXT, output TEXT, created_at REAL,
                failure_class TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE memory_candidates (
                task_id TEXT, status TEXT, evidence_sha256 TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE events (
                task_id TEXT, event_type TEXT, payload_json TEXT, sequence INTEGER PRIMARY KEY AUTOINCREMENT
            )"""
        )
        connection.execute(
            """CREATE TABLE agent_health (
                agent_name TEXT, status TEXT, provider TEXT, model TEXT, last_probe_at REAL
            )"""
        )

        completed_agents = completed_agents or ["agy_review", "dsh_review"]
        task_result = "本回答：小八 AgentOS 双 Agent 裁判\n\n测试通过"
        provenance = json.dumps(
            {"kind": "agentos_multi_adjudicated", "minimum_successful_agents": 2}
        )
        snapshot = json.dumps({"source_manifest_sha256": self.source_manifest_sha256})

        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, "completed", "production", task_result, provenance, snapshot, None),
        )

        for index, name in enumerate(completed_agents):
            connection.execute(
                "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, name, "completed", "FINAL_ACCEPTANCE: PASS\n审查通过", float(index), None),
            )

        connection.execute(
            "INSERT INTO memory_candidates VALUES (?, ?, ?)",
            (task_id, "pending_delivery_review", "a" * 64),
        )

        judge = judge_payload or {
            "winner": completed_agents[0],
            "successful_agents": len(completed_agents),
        }
        connection.execute(
            "INSERT INTO events (task_id, event_type, payload_json) VALUES (?, ?, ?)",
            (task_id, "judge.decided", json.dumps(judge)),
        )

        if standby_event:
            replaced = standby_event.get("replaced")
            if replaced_evidence and standby_event.get("reason") == "external_failure":
                connection.execute(
                    "INSERT INTO agent_runs VALUES (?, ?, ?, ?, ?, ?)",
                    (task_id, replaced, "transient_error", "", -1.0,
                     replaced_run_class or standby_event.get("failure_class")),
                )
            if replaced_evidence and (replaced_health or standby_event.get("reason") == "health_probe_failed"):
                status, failure_class = replaced_health or (
                    "degraded", standby_event.get("failure_class"))
                connection.execute(
                    "INSERT INTO events (task_id, event_type, payload_json) VALUES (?, ?, ?)",
                    (None, "health.agent_probe", json.dumps(
                        {"agent": replaced, "status": status, "failure_class": failure_class,
                         "provider": None, "model": None})),
                )
            connection.execute(
                "INSERT INTO events (task_id, event_type, payload_json) VALUES (?, ?, ?)",
                (task_id, "standby.substituted", json.dumps(standby_event)),
            )

        healthy = healthy_agents if healthy_agents is not None else completed_agents
        for name in healthy:
            connection.execute(
                "INSERT INTO agent_health VALUES (?, ?, ?, ?, ?)",
                (name, "healthy", "provider-x", "model-y", 100.0),
            )

        connection.commit()
        connection.close()
        return database_path

    def test_gate_accepts_primary_plus_standby_with_event_evidence(self):
        """门禁接受：主后台 + 有补位事件证据的备用"""
        standby_event = {
            "replaced": "dsh_review",
            "standby": "qoder_review",
            "reason": "external_failure",
            "failure_class": "external_upstream",
            "error": "upstream timeout",
            "model": "qfmodel",
        }
        db = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            standby_event=standby_event,
        )
        result = BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)
        self.assertTrue(result["standby_substituted"])
        self.assertEqual("dsh_review", result["replaced"])
        self.assertEqual("external_failure", result["reason"])
        self.assertEqual(["agy_review", "qoder_review"], result["successful_agents"])

    def test_gate_rejects_judge_only_evidence_without_the_substitution_event(self):
        """门禁拒绝：数据库里没有 standby.substituted 事件，只有 judge.decided 里的补位证据。

        生产每次补位都先写事件；只有 judge 证据时，被替换方是否真失败的独立核对无从做起（第21轮预审 R4）。"""
        judge_payload = {
            "winner": "agy_review",
            "successful_agents": 2,
            "standby_substitution": {
                "replaced": "dsh_review",
                "standby": "qoder_review",
                "reason": "health_probe_failed",
                "failure_class": "dsh_health_marker_missing",
                "health_evidence": {
                    "status": "degraded", "failure_class": "dsh_health_marker_missing",
                    "probe": {"health_marker": HEALTH_MARKER,
                              "health_probe_state": "response_without_health_marker"},
                },
            },
        }
        db = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            judge_payload=judge_payload,
        )
        with self.assertRaisesRegex(RuntimeError, "no standby.substituted event"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)

    def test_gate_rejects_primary_plus_standby_without_evidence(self):
        """门禁拒绝：主后台 + 无补位证据的备用"""
        db = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            standby_event=None,
            judge_payload={"winner": "agy_review", "successful_agents": 2},
        )
        with self.assertRaisesRegex(RuntimeError, "no standby.substituted event"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)

    def test_gate_rejects_task_degradation_disguised_as_failed_probe(self):
        event = {'replaced': 'dsh_review', 'standby': 'qoder_review',
                 'reason': 'health_probe_failed', 'failure_class': 'internal_adapter_candidate',
                 'health_evidence': {'status': 'degraded', 'failure_class': 'internal_adapter_candidate', 'probe': {}}}
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'], standby_event=event)
        with self.assertRaisesRegex(RuntimeError, 'unverified health'):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    def test_gate_rejects_probe_cover_without_its_health_source(self):
        event = {'replaced': 'dsh_review', 'standby': 'qoder_review', 'reason': 'health_probe_failed'}
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'], standby_event=event)
        with self.assertRaisesRegex(RuntimeError, 'unverified health'):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    def test_gate_rejects_inconsistent_or_successful_probe_cover(self):
        for source in (
            {'status': 'external_blocked', 'failure_class': 'external_upstream', 'probe': {}},
            {'status': 'healthy', 'failure_class': 'internal_adapter_candidate', 'probe': {}},
            {'status': 'degraded', 'failure_class': 'internal_adapter_candidate',
             'probe': {'health_marker': HEALTH_MARKER, 'health_probe_state': 'unknown', 'marker_received': True}},
        ):
            with self.subTest(source=source):
                event = {'replaced': 'dsh_review', 'standby': 'qoder_review', 'reason': 'health_probe_failed',
                         'failure_class': 'internal_adapter_candidate', 'health_evidence': source}
                db = self._setup_database(completed_agents=['agy_review', 'qoder_review'], standby_event=event)
                with self.assertRaisesRegex(RuntimeError, 'unverified health'):
                    BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    # --- the gate checks the replaced side really failed (user 2026-10-21) ----

    def _probe_cover(self, failure_class='internal_headroom'):
        return {'replaced': 'dsh_review', 'standby': 'qoder_review', 'reason': 'health_probe_failed',
                'failure_class': failure_class,
                'health_evidence': {'status': 'degraded', 'failure_class': failure_class,
                                    'probe': {'health_marker': HEALTH_MARKER, 'health_probe_state': 'unknown',
                                              'marker_received': False}}}

    def test_gate_accepts_a_real_failed_probe_whatever_its_cause(self):
        # Plan 甲: a real failed probe is covered even when its class is internal.
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'],
                                  standby_event=self._probe_cover('internal_headroom'))
        result = BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)
        self.assertTrue(result['standby_substituted'])

    def test_gate_rejects_a_probe_cover_when_the_replaced_side_was_recorded_healthy(self):
        # D4: the event said dsh failed while dsh's own record said healthy.
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'],
                                  standby_event=self._probe_cover(),
                                  replaced_health=('healthy', None))
        with self.assertRaisesRegex(RuntimeError, 'no recorded failed health'):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    def test_gate_rejects_a_probe_cover_with_no_health_record_of_the_replaced_side(self):
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'],
                                  standby_event=self._probe_cover(), replaced_evidence=False)
        with self.assertRaisesRegex(RuntimeError, 'no recorded failed health'):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    def test_gate_rejects_a_probe_cover_whose_class_differs_from_the_record(self):
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'],
                                  standby_event=self._probe_cover('internal_headroom'),
                                  replaced_health=('external_blocked', 'external_upstream'))
        with self.assertRaisesRegex(RuntimeError, 'no recorded failed health'):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    def test_gate_rejects_a_mid_round_cover_without_a_failed_run_of_the_replaced_side(self):
        event = {'replaced': 'dsh_review', 'standby': 'qoder_review', 'reason': 'external_failure',
                 'failure_class': 'external_upstream', 'error': 'upstream timeout', 'model': 'qfmodel'}
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'],
                                  standby_event=event, replaced_evidence=False)
        with self.assertRaisesRegex(RuntimeError, 'no failed run in this task'):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    def test_gate_rejects_a_mid_round_cover_whose_failed_run_has_another_class(self):
        # Round 32: a failed run of the replaced side exists, but it was an
        # internal fault while the substitution claims an external one.
        event = {'replaced': 'dsh_review', 'standby': 'qoder_review', 'reason': 'external_failure',
                 'failure_class': 'external_upstream', 'error': 'upstream timeout', 'model': 'qfmodel'}
        db = self._setup_database(completed_agents=['agy_review', 'qoder_review'],
                                  standby_event=event, replaced_run_class='internal_adapter_candidate')
        with self.assertRaisesRegex(RuntimeError, 'no failed run of class external_upstream'):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, 'task-1', self.root)

    def test_gate_rejects_standby_plus_standby(self):
        """门禁拒绝：备用 + 备用（两个备用或同一备用跑两次）"""
        # 1. 同一备用跑两次
        db = self._setup_database(
            completed_agents=["qoder_review", "qoder_review"],
        )
        with self.assertRaisesRegex(RuntimeError, "insufficient independent successful Agents"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)

        # 2. 如果配置有两个备用后台
        (self.root / "config" / "agentos.json").write_text(
            json.dumps(
                {
                    "worker": {"minimum_successful_agents": 2},
                    "agents": [
                        {"name": "agy_review", "enabled": True, "standby": False},
                        {"name": "qoder1", "enabled": True, "standby": True},
                        {"name": "qoder2", "enabled": True, "standby": True},
                    ],
                }
            ),
            encoding="utf-8",
        )
        db2 = self._setup_database(completed_agents=["qoder1", "qoder2"])
        with self.assertRaisesRegex(RuntimeError, "cannot contain multiple standby agents"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db2, "task-1", self.root)

    def test_gate_rejects_replaced_is_not_primary(self):
        """门禁拒绝：被替换者不是主后台"""
        standby_event = {
            "replaced": "non_existent_agent",
            "standby": "qoder_review",
            "reason": "external_failure",
        }
        db = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            standby_event=standby_event,
        )
        with self.assertRaisesRegex(RuntimeError, "replaced non-primary agent"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)

    def test_gate_rejects_substitution_reason_is_internal_failure(self):
        """门禁拒绝：补位原因是内部故障"""
        # reason 直接声明为 internal_failure
        standby_event1 = {
            "replaced": "dsh_review",
            "standby": "qoder_review",
            "reason": "internal_failure",
        }
        db1 = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            standby_event=standby_event1,
        )
        with self.assertRaisesRegex(RuntimeError, "invalid standby substitution reason"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db1, "task-1", self.root)

        # failure_class 声明为 internal 错误
        standby_event2 = {
            "replaced": "dsh_review",
            "standby": "qoder_review",
            "reason": "external_failure",
            "failure_class": "internal_adapter_candidate",
        }
        db2 = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            standby_event=standby_event2,
        )
        with self.assertRaisesRegex(RuntimeError, "must follow an external failure"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db2, "task-1", self.root)

    def test_gate_rejects_standby_without_health_probe_evidence(self):
        """门禁拒绝：备用后台缺少健康探针证据"""
        standby_event = {
            "replaced": "dsh_review",
            "standby": "qoder_review",
            "reason": "external_failure",
            "failure_class": "external_upstream",
        }
        # healthy_agents 只有 agy_review，缺少 qoder_review
        db = self._setup_database(
            completed_agents=["agy_review", "qoder_review"],
            standby_event=standby_event,
            healthy_agents=["agy_review"],
        )
        with self.assertRaisesRegex(RuntimeError, "no current real multi-Agent health evidence"):
            BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)

    def test_gate_legacy_agy_dsh_path_unchanged(self):
        """老路径（AGY + DSH）行为完全不变"""
        db = self._setup_database(
            completed_agents=["agy_review", "dsh_review"],
        )
        result = BUILD_RELEASE_MODULE.validate_acceptance_database(db, "task-1", self.root)
        self.assertFalse(result["standby_substituted"])
        self.assertIsNone(result["replaced"])
        self.assertIsNone(result["reason"])
        self.assertEqual(["agy_review", "dsh_review"], result["successful_agents"])


class StandbyHealthProvenanceContractTest(unittest.TestCase):
    def test_persisted_external_failure_is_distinct_from_internal_degradation(self):
        from xiaoba_agentos.standby_health import coverable_health
        # User policy has two independent allowed sources: a failed probe OR
        # a persisted external failure. Only the former needs probe markers.
        cases = [
            ({"status": "degraded", "failure_class": "external_upstream"}, True),
            ({"status": "external_blocked", "failure_class": "external_auth"}, True),
            ({"status": "degraded", "failure_class": "internal_adapter_candidate"}, False),
            ({"status": "degraded", "failure_class": None}, False),
            ({"status": "unprobed", "failure_class": "external_upstream"}, False),
            ({"status": "degraded", "failure_class": "internal_adapter_candidate",
              "probe": {"health_marker": HEALTH_MARKER, "health_probe_state": "process_unavailable",
                        "marker_received": False}}, True),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertIs(expected, coverable_health(row))


if __name__ == "__main__":
    unittest.main()
