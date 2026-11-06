"""卡 45：内置 WorkBuddy 引擎退役后的 core 侧类型映射、doctor 标注与派活拒绝。

只用临时配置与夹具节点，不读线上池、不碰发布目录。
"""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from test_card37 import _isolated_config
from test_pool_dispatch import _page_source, _seed
from xiaoba_agentos import pool_dispatch
from xiaoba_agentos.adapters import PermanentAdapterError, PoolCliAdapter
from xiaoba_agentos.health import doctor
from xiaoba_agentos.store import EventStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'review-ingress'))


class Card45RetiredEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = _isolated_config(self.root, [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def test_codebuddy_entry_infers_codebuddy_and_legacy_workbuddy_stays(self):
        self.assertEqual('codebuddy', pool_dispatch.infer_agent_type({'entry': '/TEST/bin/codebuddy'}))
        self.assertEqual('workbuddy', pool_dispatch.infer_agent_type({'entry': '/TEST/bin/workbuddy'}))
        self.assertEqual('workbuddy', pool_dispatch.infer_agent_type({'agent_type': 'workbuddy'}))
        # 旧节点的类型要能识别，独立性也仍按同一个来源算。
        self.assertIn('workbuddy', pool_dispatch._CLI_TYPES)
        self.assertEqual('codebuddy', pool_dispatch.source_family('workbuddy'))
        pairs = [pool_dispatch.independence_pair(
            {'interface': 'cli', 'agent_type': kind, 'model': 'TEST model'})
            for kind in ('codebuddy', 'workbuddy')]
        self.assertEqual(*pairs)

    def test_retired_warning_names_the_node_and_the_migration_step(self):
        nodes = [
            {'node_id': 'TEST-old-engine', 'name': 'TEST 旧内置引擎', 'agent_type': 'workbuddy'},
            {'node_id': 'TEST-codebuddy', 'name': 'TEST CodeBuddy CLI', 'agent_type': 'codebuddy'},
        ]
        warnings = pool_dispatch.retired_engine_warnings(nodes)
        self.assertEqual(1, len(warnings), warnings)
        self.assertIn('TEST 旧内置引擎', warnings[0])
        self.assertNotIn('TEST CodeBuddy CLI', warnings[0])
        self.assertIn('退役', warnings[0])
        self.assertIn('CodeBuddy', warnings[0])
        self.assertEqual([], pool_dispatch.retired_engine_warnings(nodes[1:]))

    def test_doctor_marks_retired_engine_nodes_with_a_warning(self):
        clean = doctor(self.config, self.store)
        self.assertFalse([w for w in clean['warnings'] if 'WorkBuddy' in w], clean['warnings'])
        _seed(self.store, [
            _page_source('TEST-old-engine', 'cli:fake-old', '1', agent_type='workbuddy'),
            _page_source('TEST-codebuddy', 'cli:fake-new', '2', agent_type='codebuddy'),
        ])
        checks = doctor(self.config, self.store)
        retired = [w for w in checks['warnings'] if 'WorkBuddy' in w]
        self.assertEqual(1, len(retired), checks['warnings'])
        self.assertIn('TEST-old-engine', retired[0])
        self.assertNotIn('TEST-codebuddy', retired[0])
        # 退役节点只报警告，不把整个 doctor 判红。
        self.assertEqual(clean['ok'], checks['ok'])

    def test_pool_cli_adapter_refuses_a_retired_engine_node(self):
        source = {'reviewer_id': 'TEST-old-engine', 'kind': 'peer', 'source_ref': 'TEST fixture',
                  'node': {'interface': 'cli', 'entry': '/TEST/bin/workbuddy',
                           'agent_type': 'workbuddy', 'model': ''}}
        config = SimpleNamespace(
            name='node', type='pool_cli', timeout_seconds=30,
            raw={'pool_source': source, 'workspace_root': str(self.root / 'ws'),
                 'task_id': 'TEST-task', 'step_code': 'REVIEW-01'})
        with self.assertRaises(PermanentAdapterError) as error:
            PoolCliAdapter(config).run('TEST prompt', 'TEST context')
        self.assertIn(pool_dispatch.WORKBUDDY_RETIRED_CODE, str(error.exception))
        self.assertIn('退役', str(error.exception))
        self.assertFalse((self.root / 'ws').exists() and any((self.root / 'ws').iterdir()))
