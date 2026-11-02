"""卡 39 第三轮 DSH 指出、经核实成立的真实路径问题。"""
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from test_card37 import _isolated_config
from test_pool_dispatch import _page_source, _seed
from xiaoba_agentos import cli as _cli
from xiaoba_agentos.cli import _install_service, main
_service_ready = getattr(_cli, "_service_ready", lambda *a: None)
from xiaoba_agentos.store import EventStore


class FrozenTaskResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = _isolated_config(Path(self.tmp.name), [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()
        self.task = self.store.submit('TEST 节点失败后冻结', 3)
        self.store.lease_next(60)
        self.frozen = self.store.freeze_task(
            self.task, failure_class='external_upstream',
            failures=[{'agent': 'node-a', 'error': '429', 'external': True}])

    def node(self, finished_at, enabled=True):
        source = _page_source('node-a', 'cli:fake', 'm', agent_type='other')
        source['node']['test']['finished_at'] = finished_at
        source['node']['enabled'] = enabled
        _seed(self.store, [source])

    def resume(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return main(['--config', str(self.config.path), 'task-resume', self.task,
                         '--freeze-hash', self.frozen['freeze_hash']])

    def test_node_retested_after_freeze_can_resume(self):
        self.node(time.time() + 1)
        self.assertEqual(0, self.resume())
        self.assertEqual('retry', self.store.get_task(self.task)['status'])

    def test_test_from_before_freeze_does_not_count(self):
        self.node(time.time() - 3600)
        self.assertEqual(1, self.resume())
        self.assertEqual('frozen_paused', self.store.get_task(self.task)['status'])

    def test_disabled_node_does_not_count(self):
        self.node(time.time() + 1, enabled=False)
        self.assertEqual(1, self.resume())
        self.assertEqual('frozen_paused', self.store.get_task(self.task)['status'])


class ServiceWithoutNodesTests(unittest.TestCase):
    def test_install_preflight_does_not_require_review_nodes(self):
        preflight = {'paths': {'a': True}, 'event_chain': {'ok': True},
                     'local_cli_routes': {'ok': True}, 'memory_probe': {'ok': True},
                     'review_nodes': {'enough': False}}
        config = SimpleNamespace(runtime_dir=Path('/nonexistent'), agents=[])
        with patch('xiaoba_agentos.cli.doctor', return_value=preflight), patch(
                'xiaoba_agentos.cli._secure_directory', side_effect=RuntimeError('preflight passed')):
            with self.assertRaisesRegex(RuntimeError, 'preflight passed'):
                _install_service(config, MagicMock())

    def test_after_install_only_local_health_decides_for_real_config(self):
        config = SimpleNamespace(agents=[])
        self.assertTrue(_service_ready(config, {'ok': True, 'ready_for_external_tasks': False}))
        self.assertFalse(_service_ready(config, {'ok': False, 'ready_for_external_tasks': False}))


if __name__ == '__main__':
    unittest.main()
