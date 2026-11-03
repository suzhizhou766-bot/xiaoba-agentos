"""第十二轮：长审查跑完却 lease_lost，池节点调用从不续租。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_card37 import _isolated_config
from test_pool_dispatch import _FakePoolResource, _page_source, _seed
from xiaoba_agentos.adapters import PermanentAdapterError, PoolCliAdapter
from xiaoba_agentos.cli import _submit_task
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore


class SlowResource(_FakePoolResource):
    delay = 3.0

    def run_task(self, *args):
        time.sleep(self.delay)
        return super().run_task(*args)


class LeaseRenewalTests(unittest.TestCase):
    def test_review_longer_than_lease_still_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _isolated_config(root, [])
            raw = json.loads(config.path.read_text(encoding='utf-8'))
            raw['worker']['lease_seconds'] = 2
            config.path.write_text(json.dumps(raw), encoding='utf-8')
            config = AppConfig.load(config.path)
            store = EventStore(config.database_path)
            store.initialize()
            _seed(store, [_page_source(str(i), 'cli:fake' + str(i), str(i), agent_type='other') for i in range(2)])
            task = _submit_task(config, store, 'TEST 长审查', experience_kind='audit')
            with patch('xiaoba_agentos.orchestrator.release_binding_status', return_value={'ok': True}), \
                    patch('xiaoba_agentos.adapters._pool_cli_factory', return_value=lambda s, w=None, **k: SlowResource(s, w)):
                result = Orchestrator(config, store).run_once()
            self.assertNotEqual('lease_lost', result['status'], result)
            self.assertEqual('completed', store.get_task(task)['status'])

    def adapter(self, root, callback):
        config = SimpleNamespace(name='node', type='pool_cli', timeout_seconds=30, raw={
            'pool_source': {'reviewer_id': 'node'}, 'workspace_root': str(root), 'task_id': 'T', 'step_code': 'REVIEW-01'})
        adapter = PoolCliAdapter(config)
        adapter.progress_callback = callback
        adapter.progress_interval = 0.02
        return adapter

    def test_lost_lease_stops_the_call(self):
        closed = []

        class Resource(_FakePoolResource):
            def run_task(self, *args):
                deadline = time.monotonic() + 5
                while not closed and time.monotonic() < deadline:
                    time.sleep(0.01)
                raise RuntimeError('killed')

            def close(self):
                closed.append(True)

        def lost(_progress):
            raise PermanentAdapterError('task lease lost; stop this CLI without substitution')

        with tempfile.TemporaryDirectory() as directory, \
                patch('xiaoba_agentos.adapters._pool_cli_factory', return_value=lambda s, w=None, **k: Resource(s, w)):
            with self.assertRaisesRegex(PermanentAdapterError, 'lease lost'):
                self.adapter(directory, lost).run('TEST', '')
        self.assertTrue(closed)


if __name__ == '__main__':
    unittest.main()
