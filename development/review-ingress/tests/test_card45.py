"""卡 45：内置 WorkBuddy 引擎退役后的登记、测试运行与派活边界。

只用夹具与直接写库造的旧节点记录，不起真实进程、不读桌面登录材料。
"""
import importlib.util
import json
import sys
import unittest

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.cli_identity import TYPES, agent_type, source_family
from xiaoba_review.resource_agy import InteractiveError
from xiaoba_review.resource_stdio import json_stdio_factory
from xiaoba_review.store import canonical
from xiaoba_agentos.pool_dispatch import WORKBUDDY_RETIRED_CODE, WORKBUDDY_RETIRED_MESSAGE


class Card45Tests(fixtures.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.workspaces = self.root / 'workspaces'
        self.workspaces.mkdir()
        self.backends = BackendRegistry(
            self.store, resource_factory=lambda s: json_stdio_factory(s, self.workspaces))
        self.addCleanup(self.backends.close)
        self.pool = AgentPool(self.store, self.backends)

    def value(self, node_id='TEST-new-engine', **changes):
        return dict({'node_id': node_id, 'name': node_id, 'agent': 'TEST WorkBuddy',
                     'kind': 'peer', 'interface': 'cli',
                     'entry': str(self.entry(node_id)), 'model': 'TEST model'}, **changes)

    def entry(self, name):
        """A CLI-shaped fixture that records being started; the engine must never run it."""
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n'
                        + 'from pathlib import Path\n'
                        + 'Path(__file__).with_name(' + repr(name + '.started') + ').write_text("started")\n')
        path.chmod(0o700)
        return path

    def started(self, name):
        return (self.root / (name + '.started')).exists()

    def legacy_node(self, node_id='TEST-old-engine'):
        """造一条 v1.6.7 之前登记的内置引擎节点：数据按旧值写库，不改类型。"""
        entry = self.entry(node_id)
        source = self.pool.add(dict(self.value(node_id), agent_type='codebuddy'))
        source['node']['agent_type'] = 'workbuddy'
        with self.store.transaction() as db:
            db.execute('UPDATE ri_sources SET data=? WHERE id=?', (canonical(source), node_id))
        self.pool.change(node_id, source['node']['revision'], 'toggle', True)
        return self.source(node_id)

    def source(self, node_id):
        with self.native.read_only() as db:
            return self.store.source(db, node_id)

    def view(self, node_id):
        return next(v for v in self.pool.state()['nodes'] if v['node_id'] == node_id)

    def events(self, event_type):
        with self.native.read_only() as db:
            return [json.loads(r[0]) for r in db.execute(
                'SELECT payload_json FROM events WHERE event_type=? ORDER BY sequence', (event_type,))]

    def test_new_node_cannot_pick_the_retired_builtin_engine(self):
        with self.assertRaises(ValueError) as error:
            self.pool.add(dict(self.value(), agent_type='workbuddy'))
        self.assertIn('退役', str(error.exception))
        self.assertIn('CodeBuddy', str(error.exception))
        self.assertNotIn('TEST-new-engine', [n['node_id'] for n in self.pool.state()['nodes']])
        # 旧数据仍要能识别这个类型，只是新建不再提供。
        self.assertIn('workbuddy', TYPES)

    def test_editing_a_node_to_the_retired_engine_is_refused(self):
        source = self.pool.add(dict(self.value('TEST-other-node'), agent_type='codex'))
        with self.assertRaises(ValueError) as error:
            self.pool.edit({'node_id': 'TEST-other-node', 'expected_revision': source['node']['revision'],
                            'agent_type': 'workbuddy'})
        self.assertIn('退役', str(error.exception))
        self.assertEqual('codex', self.source('TEST-other-node')['node']['agent_type'])

    def test_old_engine_node_is_marked_retired_and_keeps_its_data(self):
        before = self.legacy_node()
        current = self.source('TEST-old-engine')
        self.assertEqual(canonical(before), canonical(current))
        self.assertEqual('workbuddy', self.view('TEST-old-engine')['agent_type'])
        readiness = self.view('TEST-old-engine')['readiness']
        self.assertFalse(readiness['ready'])
        self.assertEqual(WORKBUDDY_RETIRED_CODE, readiness['reason'])
        self.assertEqual(canonical(before), canonical(self.source('TEST-old-engine')))

    def test_old_engine_node_test_run_is_refused_with_engine_retired(self):
        before = self.legacy_node()
        result = self.pool.runtime.test_run('TEST-old-engine', before['node']['revision'], 'TEST-probe')
        self.assertEqual('blocked', result['status'], result)
        self.assertEqual(WORKBUDDY_RETIRED_CODE, result['reason'], result)
        self.assertEqual(WORKBUDDY_RETIRED_MESSAGE, result['message'], result)
        self.assertEqual([], self.events('agent_pool.test_started'))
        self.assertFalse(self.started('TEST-old-engine'))
        self.assertEqual(canonical(before), canonical(self.source('TEST-old-engine')))

    def test_old_engine_node_dispatch_is_refused_with_engine_retired(self):
        before = self.legacy_node()
        with self.assertRaises(InteractiveError) as error:
            self.pool.runtime.execute_work('TEST-old-engine', before['node']['revision'],
                                           'TEST-task', 'STEP', 'TEST instruction', {})
        self.assertEqual(WORKBUDDY_RETIRED_CODE, error.exception.code)
        self.assertEqual(WORKBUDDY_RETIRED_MESSAGE, error.exception.user_message)
        self.assertFalse(self.started('TEST-old-engine'))
        self.assertEqual(canonical(before), canonical(self.source('TEST-old-engine')))

    def test_retired_engine_resource_refuses_every_call_without_starting_it(self):
        self.legacy_node()
        source = self.source('TEST-old-engine')
        resource = json_stdio_factory(source, self.workspaces)
        self.assertEqual(WORKBUDDY_RETIRED_MESSAGE, getattr(resource, 'retired_message', None))
        for call in (lambda: resource.resource_identity(),
                     lambda: resource.test_resource({}, {}),
                     lambda: resource.run({})):
            with self.assertRaises(InteractiveError) as error:
                call()
            self.assertEqual(WORKBUDDY_RETIRED_CODE, error.exception.code)
        self.assertFalse(self.started('TEST-old-engine'))

    def test_engine_module_is_gone_and_nothing_imports_it(self):
        self.assertIsNone(importlib.util.find_spec('xiaoba_review.resource_workbuddy'))
        self.assertIsNone(importlib.util.find_spec('xiaoba_review.workbuddy_login'))
        from xiaoba_review import resource_vendor_cli
        self.assertNotIn('workbuddy', resource_vendor_cli.RESOURCES)
        self.assertIn('codebuddy', resource_vendor_cli.RESOURCES)
        source = self.legacy_node()
        self.assertIsNotNone(json_stdio_factory(source, self.workspaces))

    def test_codebuddy_entry_still_infers_codebuddy_and_stays_one_source(self):
        self.assertEqual('codebuddy', agent_type({'entry': '/TEST/bin/codebuddy'}))
        self.assertEqual('workbuddy', agent_type({'entry': '/TEST/bin/workbuddy'}))
        self.assertEqual('codebuddy', source_family('workbuddy'))
        from xiaoba_review.resource_vendor_cli import CodeBuddyCliResource
        source = self.pool.add(dict(self.value('TEST-codebuddy-node'), agent_type='codebuddy'))
        adapter = self.backends.adapter_for(self.source('TEST-codebuddy-node'))
        self.assertIsInstance(adapter, CodeBuddyCliResource)
        self.assertIsNone(getattr(adapter, 'retired_message', None))
        self.assertEqual('codebuddy', self.view('TEST-codebuddy-node')['agent_type'])
        self.assertTrue(source['node']['enabled'] is False)
