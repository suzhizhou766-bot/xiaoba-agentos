import json
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.config import AppConfig, ConfigError


class RecoveryConfigRetirementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runtime").mkdir()
        (self.root / "releases").mkdir()
        (self.root / "releases/index.json").write_text("{}", encoding="utf-8")
        (self.root / "vault").mkdir()
        (self.root / "skills").mkdir()
        (self.root / "skills/index.json").write_text("{}", encoding="utf-8")
        (self.root / "knowledge").mkdir()
        (self.root / "knowledge/index.json").write_text("{}", encoding="utf-8")
        (self.root / "port").write_text("1234\n", encoding="utf-8")
        (self.root / "desktop.yml").write_text("patch: true\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _base_raw_config(self) -> dict:
        return {
            "schema_version": 2,
            "runtime_dir": str(self.root / "runtime"),
            "project_dir": str(self.root),
            "release": {
                "index_path": str(self.root / "releases/index.json"),
                "expected_version": "v1.5.1",
            },
            "worker": {
                "poll_seconds": 10,
                "lease_seconds": 1800,
                "max_attempts": 3,
                "retry_base_seconds": 30,
                "retry_max_seconds": 21600,
                "parallelism": 2,
                "minimum_successful_agents": 2,
                "health_max_age_seconds": 86400,
                "max_agent_invocations_per_task": 6,
                "log_heartbeat_polls": 60,
            },
            "recovery": {
                "repair_max_attempts": 2,
                "repair_retry_seconds": 30,
                "resume_requires_user": True,
            },
            "memory": {
                "obsidian_vault": str(self.root / "vault"),
                "obsidian_subdir": "70_复利规则/经验",
                "catalog_note": "70_复利规则/经验索引.md",
                "publish_enabled": False,
                "retire_after_failed_reuses": 2,
            },
            "skills": {
                "root_dir": str(self.root / "skills"),
                "index_path": str(self.root / "skills/index.json"),
                "max_selected": 4,
            },
            "knowledge": {
                "index_path": "knowledge/index.json",
                "allowed_roots": [str(self.root / "knowledge")],
            },
            "integrations": {
                "node_command": "/bin/sh",
                "agy_command": "/bin/sh",
                "dsh_command": "/bin/sh",
            },
            "cli_routes": {
                "local_agy": {
                    "driver": "agy",
                    "selection": "configured_model",
                    "model": "Gemini 3.8 Flash (High)",
                },
                "local_dsh": {
                    "driver": "dsh",
                    "selection": "local_harness",
                    "desktop_config": str(self.root / "desktop.yml"),
                },
            },
            "agents": [
                {
                    "name": "agy_review",
                    "type": "local_cli",
                    "driver": "agy",
                    "cli_route": "local_agy",
                    "enabled": True,
                    "timeout_seconds": 300,
                },
                {
                    "name": "dsh_review",
                    "type": "local_cli",
                    "driver": "dsh",
                    "cli_route": "local_dsh",
                    "enabled": True,
                    "timeout_seconds": 300,
                },
            ],
            "evolution": {
                "minimum_samples": 3,
                "promotion_margin": 0.05,
                "latency_penalty": 0.05,
            },
        }

    def _write_and_load(self, raw: dict) -> AppConfig:
        config_path = self.root / "test_config.json"
        config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return AppConfig.load(config_path)

    def test_default_automatic_actions_passes_and_is_equivalent_to_empty(self):
        raw = self._base_raw_config()
        self.assertNotIn("automatic_actions", raw["recovery"])
        config = self._write_and_load(raw)
        self.assertNotIn("automatic_actions", config.recovery)

    def test_empty_list_automatic_actions_passes(self):
        raw = self._base_raw_config()
        raw["recovery"]["automatic_actions"] = []
        config = self._write_and_load(raw)
        self.assertEqual([], config.recovery["automatic_actions"])

    def test_legacy_restart_action_is_rejected_with_retirement_message(self):
        raw = self._base_raw_config()
        raw["recovery"]["automatic_actions"] = ["restart_owned_headroom_and_reprobe"]
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw)
        self.assertIn("同伴自动修复已退役", str(caught.exception))
        self.assertNotIn("重启服务", str(caught.exception))

    def test_unknown_action_is_rejected(self):
        raw = self._base_raw_config()
        raw["recovery"]["automatic_actions"] = ["custom_arbitrary_action"]
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw)
        self.assertIn("同伴自动修复已退役", str(caught.exception))

    def test_string_action_is_rejected_as_invalid_type(self):
        for string_value in ("restart_owned_headroom_and_reprobe", ""):
            with self.subTest(string_value=string_value):
                raw = self._base_raw_config()
                raw["recovery"]["automatic_actions"] = string_value
                with self.assertRaises(ConfigError) as caught:
                    self._write_and_load(raw)
                self.assertIn("必须是列表", str(caught.exception))

    def test_null_action_is_rejected_as_invalid_type(self):
        raw = self._base_raw_config()
        raw["recovery"]["automatic_actions"] = None
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw)
        self.assertIn("必须是列表", str(caught.exception))

    def test_object_action_is_rejected_as_invalid_type(self):
        raw = self._base_raw_config()
        raw["recovery"]["automatic_actions"] = {"action": "restart"}
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw)
        self.assertIn("必须是列表", str(caught.exception))

    def test_disabling_user_resume_is_rejected(self):
        for bad_resume in (False, None, "false", 0):
            with self.subTest(bad_resume=bad_resume):
                raw = self._base_raw_config()
                raw["recovery"]["resume_requires_user"] = bad_resume
                with self.assertRaises(ConfigError) as caught:
                    self._write_and_load(raw)
                self.assertIn("recovery.resume_requires_user 必须为 true", str(caught.exception))

    def test_shipped_candidate_config_loads_cleanly(self):
        candidate_config_path = Path(__file__).resolve().parents[1] / "config" / "agentos.json"
        config = AppConfig.load(candidate_config_path)
        self.assertEqual(2, config.raw["schema_version"])
        self.assertNotIn("automatic_actions", config.recovery)
        self.assertTrue(config.recovery["resume_requires_user"])
        self.assertEqual(0, len(config.primary_agents))
        self.assertEqual([], [a.name for a in config.standby_agents])

    def test_unrelated_config_constraints_continue_to_hold(self):
        # 1. recovery 必须是对象
        raw_non_dict_recovery = self._base_raw_config()
        raw_non_dict_recovery["recovery"] = "not_a_dict"
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw_non_dict_recovery)
        self.assertIn("recovery 必须是对象", str(caught.exception))

        # 2. 遗留 repair_max_attempts <= 0 依然拒绝
        raw_bad_attempts = self._base_raw_config()
        raw_bad_attempts["recovery"]["repair_max_attempts"] = 0
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw_bad_attempts)
        self.assertIn("recovery.repair_max_attempts 必须大于 0", str(caught.exception))

        # 3. 遗留 repair_retry_seconds <= 0 依然拒绝
        raw_bad_retry = self._base_raw_config()
        raw_bad_retry["recovery"]["repair_retry_seconds"] = -5
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw_bad_retry)
        self.assertIn("recovery.repair_retry_seconds 必须大于 0", str(caught.exception))

        # 4. worker 参数校验依然生效
        raw_bad_worker = self._base_raw_config()
        raw_bad_worker["worker"]["parallelism"] = 0
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw_bad_worker)
        self.assertIn("worker.parallelism 必须大于 0", str(caught.exception))

        # 5. 重复 Agent 名称校验依然生效
        raw_dup_agents = self._base_raw_config()
        raw_dup_agents["agents"].append(dict(raw_dup_agents["agents"][0]))
        with self.assertRaises(ConfigError) as caught:
            self._write_and_load(raw_dup_agents)
        self.assertIn("Agent 名称不能为空或重复", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
