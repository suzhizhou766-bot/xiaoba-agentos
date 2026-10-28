import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.capabilities import CapabilityError, CapabilityRegistry, web_capable_agents
from xiaoba_agentos.config import AgentConfig, AppConfig
from xiaoba_agentos.routing import AGENTOS_MULTI, BLOCKED, capability_gate_result


def make_agent(name, driver="codex", cli_route="amd_codex", provider="AMD AI",
               model="DeepSeek-V4-Flash-Vision-Exp", atype="mock"):
    raw = {
        "name": name,
        "type": atype,
        "driver": driver,
        "cli_route": cli_route,
        "provider": provider,
        "model": model,
        "enabled": True,
        "timeout_seconds": 60,
        "role": "",
        "template": "agent {prompt}",
    }
    app = AppConfig(Path("/tmp/x.json"), {
        "runtime_dir": "/tmp/runtime",
        "project_dir": "/tmp",
        "worker": {"poll_seconds": 1, "lease_seconds": 60, "max_attempts": 2,
                   "retry_base_seconds": 0, "retry_max_seconds": 0, "parallelism": 1,
                   "minimum_successful_agents": 1},
        "memory": {"knowlp_enabled": False, "obsidian_vault": "/tmp/vault",
                   "obsidian_subdir": "AgentOS", "publish_enabled": False},
        "integrations": {},
        "cli_routes": {},
        "agents": [],
        "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
    })
    return AgentConfig(name, atype, True, "", timeout_seconds=60, app_config=app, raw=raw)


class CapabilityRegistryTest(unittest.TestCase):
    def test_deepseek_native_web_search_is_false(self):
        reg = CapabilityRegistry()
        self.assertFalse(reg.has_native_web_search("AMD AI", "DeepSeek-V4-Flash-Vision-Exp"))
        self.assertFalse(reg.has_native_web_search("AMD AI", "DeepSeek-V4-Flash"))

    def test_unknown_capability_is_not_treated_as_true(self):
        reg = CapabilityRegistry()
        caps = reg.capabilities_for("Mystery Corp", "Some-Model")
        self.assertEqual("unknown", caps.get("native_web_search"))
        self.assertIsNot(True, caps.get("native_web_search"))
        self.assertFalse(reg.has_capability("Mystery Corp", "Some-Model", "reasoning"))

    def test_web_filter_excludes_deepseek_for_needs_web(self):
        reg = CapabilityRegistry()
        agents = [make_agent("dsh_review", cli_route="amd_codex", provider="AMD AI",
                             model="DeepSeek-V4-Flash-Vision-Exp")]
        capable, reason = web_capable_agents(agents, reg, needs_web=True)
        self.assertEqual([], capable)
        self.assertTrue(reason)

    def test_web_filter_keeps_capable_agent(self):
        reg = CapabilityRegistry()
        agents = [make_agent("webby", cli_route="web_route", provider="P", model="M")]
        with patch.object(reg, "has_native_web_search", return_value=True):
            capable, reason = web_capable_agents(agents, reg, needs_web=True)
        self.assertEqual(["webby"], [a.name for a in capable])
        self.assertEqual("", reason)

    def test_capability_gate_returns_blocked_for_web_without_capable_agent(self):
        reg = CapabilityRegistry()
        agents = [make_agent("dsh_review", cli_route="amd_codex", provider="AMD AI",
                             model="DeepSeek-V4-Flash-Vision-Exp")]
        result = capability_gate_result(needs_web=True, agents=agents, registry=reg)
        self.assertEqual(BLOCKED, result["route"])
        self.assertEqual("native_web_search", result["capability"])

    def test_capability_gate_blocks_when_required_token_unknown(self):
        reg = CapabilityRegistry()
        agents = [make_agent("mystery", cli_route="m", provider="X", model="UnknownModel")]
        result = capability_gate_result(needs_web=False, required_tokens=("vision",),
                                        agents=agents, registry=reg)
        self.assertEqual(BLOCKED, result["route"])
        self.assertTrue(result["reason"].startswith("missing capability"))

    def test_capability_gate_passes_when_required_token_true(self):
        reg = CapabilityRegistry()
        agents = [make_agent("dsh_review", cli_route="amd_codex", provider="AMD AI",
                             model="DeepSeek-V4-Flash-Vision-Exp")]
        result = capability_gate_result(needs_web=False, required_tokens=("coding",),
                                        agents=agents, registry=reg)
        self.assertEqual(AGENTOS_MULTI, result["route"])
        self.assertIn("dsh_review", result["agents"])

    def test_capability_json_is_read_and_consumed_by_code(self):
        root = Path(__file__).resolve().parents[1]
        path = root / "CAPABILITY.json"
        self.assertTrue(path.exists(), "CAPABILITY.json must exist at source root")
        reg = CapabilityRegistry(path)
        self.assertIn("native_web_search", reg.tokens())
        self.assertTrue(reg.providers())

    def test_shell_does_not_grant_write(self):
        r = CapabilityRegistry()
        self.assertTrue(r.has_capability('AMD AI', 'DeepSeek-V4-Flash', 'shell'))
        for t in ('shell_read', 'shell_write_workspace', 'structured_editor_write'):
            self.assertFalse(r.has_capability('AMD AI', 'DeepSeek-V4-Flash', t))

    def test_malformed_capability_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'caps.json';p.write_text(json.dumps({'providers': {'p': {'m': {'shell': 'yes'}}}}))
            with self.assertRaises(CapabilityError): CapabilityRegistry(p)


if __name__ == "__main__":
    unittest.main()
