import json
import tempfile
import unittest
from pathlib import Path

from xiaoba_agentos.capabilities import CapabilityRegistry
from xiaoba_agentos.config import AppConfig, ConfigError


class BackendOwnedSelectionTest(unittest.TestCase):
    def test_local_backends_reject_master_model_pools_and_upstream_fields(self):
        baseline = json.loads((Path(__file__).resolve().parents[1] / 'config/agentos.json').read_text())
        for route in ('local_agy', 'local_dsh'):
            # AGY 的模型就写在路由上（用户 2026-11-01 裁决），所以只有 DSH 不许带 model。
            keys = ('model_pool', 'provider', 'provider_id', 'endpoint', 'credential')
            if route == 'local_dsh':
                keys = ('model',) + keys
            for key in keys:
                raw = json.loads(json.dumps(baseline))
                raw['cli_routes'][route][key] = ['another-model'] if key == 'model_pool' else 'override'
                with self.subTest(route=route, key=key), self.assertRaises(ConfigError):
                    AppConfig(Path('/fixture/config.json'), raw)._validate_cli_routes()

    def test_backend_display_name_uses_only_its_own_declared_capabilities(self):
        registry = CapabilityRegistry()
        self.assertTrue(registry.has_capability('Antigravity', 'Gemini 3.8 Flash (Low)', 'reasoning'))
        self.assertFalse(registry.has_capability('Antigravity', 'Gemini 3.8 Flash (Low)', 'shell_write_workspace'))
        self.assertFalse(registry.has_capability('Antigravity', 'Undeclared backend model', 'reasoning'))
        # 配置里的值（用户 2026-11-01 裁决后写在高推理档）已声明；别的推理档仍不能借用。
        self.assertTrue(registry.has_capability('Antigravity', 'Gemini 3.8 Flash (High)', 'reasoning'))
        self.assertFalse(registry.has_capability('Antigravity', 'Gemini 3.8 Flash (High)', 'shell'))
        self.assertFalse(registry.has_capability('Antigravity', 'Gemini 3.8 Flash (Medium)', 'reasoning'))

    def test_ambiguous_display_name_does_not_grant_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'capability.json'
            p.write_text(json.dumps({'providers': {'backend': {
                'a-b': {'reasoning': True}, 'a_b': {'reasoning': True}
            }}, 'defaults': {'reasoning': 'unknown'}, 'capability_tokens': ['reasoning']}))
            self.assertFalse(CapabilityRegistry(p).has_capability('backend', 'A B', 'reasoning'))
