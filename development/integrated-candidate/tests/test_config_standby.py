"""Standby backend configuration: schema, validation and legacy compatibility.

The standby (`qoder_review`) is a real but optional part of the config.  A
config without it must load exactly as before, and a standby must never be
counted as one of the two primary review backends.
"""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from xiaoba_agentos.config import AppConfig, ConfigError

PROJECT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((PROJECT / "config/agentos.json").read_text(encoding="utf-8"))
# The shipped template no longer lists backends. These tests still load the
# historical pair, then add the old standby field, to prove that shape loads.
if not BASELINE.get("agents"):
    BASELINE["agents"] = [
        {"name": "agy_review", "type": "local_cli", "driver": "agy", "cli_route": "local_agy",
         "enabled": True, "timeout_seconds": 300, "role": "historical"},
        {"name": "dsh_review", "type": "local_cli", "driver": "dsh", "cli_route": "local_dsh",
         "enabled": True, "timeout_seconds": 300, "role": "historical"},
    ]
BASELINE["agents"] = [a for a in BASELINE["agents"] if not a.get("standby")]
BASELINE["cli_routes"].pop("local_qoder", None)
BASELINE["integrations"].pop("qoder_command", None)


def baseline():
    return copy.deepcopy(BASELINE)


def with_standby(raw, settings_path="/Users/fixture/.qoder/settings.json"):
    raw["integrations"]["qoder_command"] = "/Users/fixture/.local/bin/qodercli"
    raw["cli_routes"]["local_qoder"] = {
        "driver": "qoder",
        "selection": "qodercli_settings",
        "settings_path": settings_path,
    }
    raw["agents"].append({
        "name": "qoder_review",
        "type": "local_cli",
        "driver": "qoder",
        "cli_route": "local_qoder",
        "enabled": True,
        "standby": True,
        "timeout_seconds": 300,
        "role": "备用只读审查员",
    })
    return raw


class StandbyConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # The settings path must be exactly the user's own ~/.qoder/settings.json;
        # the fixture user's home is /Users/fixture.
        home = patch.object(Path, "home", return_value=Path("/Users/fixture"))
        home.start()
        self.addCleanup(home.stop)

    def load(self, raw):
        path = self.root / "agentos.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return AppConfig.load(path)

    # --- legacy compatibility ---------------------------------------------

    def test_config_without_standby_loads_exactly_as_before(self):
        config = self.load(baseline())
        self.assertEqual(["agy_review", "dsh_review"], [agent.name for agent in config.agents])
        self.assertEqual(["agy_review", "dsh_review"], [agent.name for agent in config.primary_agents])
        self.assertEqual([], config.standby_agents)
        self.assertTrue(all(agent.standby is False for agent in config.agents))

    def test_legacy_config_keeps_the_runtime_health_gate_not_a_load_error(self):
        # Disabling a primary in a config without a standby is still a runtime
        # health question, exactly as before this change.
        raw = baseline()
        next(item for item in raw["agents"] if item["name"] == "dsh_review")["enabled"] = False
        config = self.load(raw)
        self.assertEqual(["agy_review"], [agent.name for agent in config.primary_agents])

    # --- standby wiring ---------------------------------------------------

    def test_standby_is_listed_separately_from_the_two_primary_backends(self):
        config = self.load(with_standby(baseline()))
        self.assertEqual(["agy_review", "dsh_review"], [agent.name for agent in config.primary_agents])
        self.assertEqual(["qoder_review"], [agent.name for agent in config.standby_agents])
        standby = config.standby_agents[0]
        self.assertTrue(standby.standby)
        self.assertEqual("qoder", standby.driver)
        self.assertEqual("local_qoder", standby.raw["cli_route"])

    def test_disabled_standby_is_not_offered_for_substitution(self):
        raw = with_standby(baseline())
        raw["agents"][-1]["enabled"] = False
        config = self.load(raw)
        self.assertEqual([], config.standby_agents)
        self.assertEqual(2, len(config.primary_agents))

    def test_settings_file_is_only_read_at_call_time(self):
        # /Users/fixture/.qoder/settings.json does not exist on disk.
        config = self.load(with_standby(baseline()))
        self.assertEqual("qoder_review", config.standby_agents[0].name)

    def test_disabling_a_primary_with_a_standby_still_loads_and_never_promotes_the_standby(self):
        raw = with_standby(baseline())
        next(item for item in raw["agents"] if item["name"] == "dsh_review")["enabled"] = False
        config = self.load(raw)
        self.assertEqual(["agy_review"], [agent.name for agent in config.primary_agents])
        self.assertEqual(["qoder_review"], [agent.name for agent in config.standby_agents])

    # --- rejections -------------------------------------------------------

    def test_two_standbys_are_rejected(self):
        raw = with_standby(baseline())
        raw["agents"].append({**raw["agents"][-1], "name": "qoder_review_two"})
        with self.assertRaises(ConfigError) as caught:
            self.load(raw)
        self.assertIn("备用后台最多只能有一个", str(caught.exception))

    def test_standby_must_point_at_a_qoder_route(self):
        raw = with_standby(baseline())
        raw["agents"][-1]["cli_route"] = "local_dsh"
        with self.assertRaises(ConfigError) as caught:
            self.load(raw)
        # The route/driver mismatch check is what stops this; name the exact rule
        # so the assertion tells which gate held (the standby-policy line after
        # it is defensive and not normally reached).
        self.assertIn("qoder_review 的 CLI 驱动与路由不一致", str(caught.exception))

    def test_standby_must_be_a_real_boolean(self):
        # A truthy 1/"yes" used to count as a standby at run time while the policy
        # check only counted `is True`, letting AGY become a standby and the
        # primaries collapse (v1.6.5 pre-review).
        for value in (1, "yes", [1], 0, "no", None):
            for target in ("agy_review", "qoder_review"):
                with self.subTest(target=target, value=value):
                    raw = with_standby(baseline())
                    next(a for a in raw["agents"] if a["name"] == target)["standby"] = value
                    with self.assertRaises(ConfigError) as caught:
                        self.load(raw)
                    self.assertIn("standby 必须是布尔值", str(caught.exception))

    def test_standby_cannot_use_another_driver(self):
        raw = with_standby(baseline())
        raw["agents"][-1].update(driver="dsh", cli_route="local_dsh")
        with self.assertRaises(ConfigError) as caught:
            self.load(raw)
        self.assertIn("只能作为 qoder 备用后台", str(caught.exception))

    def test_standby_cannot_be_a_mock_backend(self):
        raw = with_standby(baseline())
        raw["agents"][-1].update(type="mock")
        with self.assertRaises(ConfigError):
            self.load(raw)

    def test_missing_qoder_command_is_rejected(self):
        raw = with_standby(baseline())
        del raw["integrations"]["qoder_command"]
        with self.assertRaises(ConfigError) as caught:
            self.load(raw)
        self.assertIn("qoder_command", str(caught.exception))

    def test_blank_qoder_command_is_rejected(self):
        raw = with_standby(baseline())
        raw["integrations"]["qoder_command"] = "  "
        with self.assertRaises(ConfigError) as caught:
            self.load(raw)
        self.assertIn("integrations.qoder_command", str(caught.exception))

    def test_qoder_route_selection_must_be_the_settings_route(self):
        for selection in ("local_account", "local_harness", ""):
            with self.subTest(selection=selection):
                raw = with_standby(baseline())
                raw["cli_routes"]["local_qoder"]["selection"] = selection
                with self.assertRaises(ConfigError) as caught:
                    self.load(raw)
                self.assertIn("qodercli_settings", str(caught.exception))

    def test_qoder_route_rejects_upstream_model_and_credential_fields(self):
        for key in ("model", "model_pool", "provider", "provider_id", "credential",
                    "base_url", "endpoint", "api_key", "apiKey", "token", "headers"):
            with self.subTest(key=key):
                raw = with_standby(baseline())
                raw["cli_routes"]["local_qoder"][key] = "override"
                with self.assertRaises(ConfigError) as caught:
                    self.load(raw)
                self.assertIn("Qoder 路由仅允许 driver、selection、settings_path", str(caught.exception))
                self.assertNotIn("override", str(caught.exception))

    def test_qoder_route_requires_an_absolute_settings_path(self):
        # A leading "~" is expanded first, as everywhere else in this file; what
        # is rejected is an empty or genuinely relative path.
        # Also refused: anything that is not the standard ~/.qoder/settings.json
        # shape, e.g. a Qoder CN directory or another file name.
        for value in ("", "relative/settings.json", "./settings.json",
                      "/Users/fixture/.qoder-cn/settings.json", "/Users/fixture/.qoder/config.json",
                      # same shape, but not the user's own file (pre-review R9)
                      "/tmp/anywhere/.qoder/settings.json", "/Users/someone-else/.qoder/settings.json"):
            with self.subTest(value=value):
                raw = with_standby(baseline(), settings_path=value)
                with self.assertRaises(ConfigError):
                    self.load(raw)

    def test_a_qoder_route_without_settings_path_loads_like_agy(self):
        # Round 27: the gateway already falls back to ~/.qoder/settings.json, so
        # the config must not reject a route that leaves the key out.
        raw = with_standby(baseline())
        del raw["cli_routes"]["local_qoder"]["settings_path"]
        config = self.load(raw)
        self.assertNotIn("settings_path", config.cli_routes["local_qoder"])

    def test_qoder_agent_cannot_pin_a_model(self):
        raw = with_standby(baseline())
        raw["agents"][-1]["model"] = "qfmodel"
        with self.assertRaises(ConfigError) as caught:
            self.load(raw)
        self.assertIn("不在 Agent 上指定模型", str(caught.exception))

    def test_unknown_driver_is_still_rejected(self):
        raw = with_standby(baseline())
        raw["agents"][-1]["driver"] = "somecli"
        raw["cli_routes"]["local_qoder"]["driver"] = "somecli"
        raw["integrations"]["somecli_command"] = "/Users/fixture/.local/bin/somecli"
        with self.assertRaises(ConfigError) as caught:
            self.load(raw)
        self.assertIn("agy、dsh 或 qoder", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
