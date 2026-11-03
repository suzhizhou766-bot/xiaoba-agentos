"""恢复后补跑失败侧：池节点调用要带恢复次数，节点工作区才会开新记录。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from test_card37 import _isolated_config
from test_pool_dispatch import _FakePoolResource, _page_source, _seed
from xiaoba_agentos.cli import _submit_task
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.store import EventStore

CALLS = []


class RecordingResource(_FakePoolResource):
    def run_task(self, task_id, step_code, instruction, materials, attempt=0):
        CALLS.append((self.source['reviewer_id'], attempt))
        return super().run_task(task_id, step_code, instruction, materials)


class ResumeRerunTests(unittest.TestCase):
    def test_resumed_task_passes_its_resume_generation_to_the_node(self):
        CALLS.clear()
        with tempfile.TemporaryDirectory() as directory:
            config = _isolated_config(Path(directory), [])
            store = EventStore(config.database_path)
            store.initialize()
            sources = [_page_source(str(i), 'cli:fake' + str(i), str(i), agent_type='other') for i in range(2)]
            _seed(store, sources)
            task = _submit_task(config, store, 'TEST 恢复补跑', experience_kind='audit')
            store.lease_next(60)
            frozen = store.freeze_task(task, failure_class='external_upstream',
                                       failures=[{'agent': '0', 'error': '429', 'external': True}])
            for source in sources:
                source['node']['test']['finished_at'] = time.time() + 1
            _seed(store, sources)
            store.resume_task(task, frozen['freeze_hash'], enabled_names=[], health_max_age_seconds=86400,
                              retested_nodes={'0': time.time() + 1, '1': time.time() + 1})
            with patch('xiaoba_agentos.orchestrator.release_binding_status', return_value={'ok': True}), \
                    patch('xiaoba_agentos.adapters._pool_cli_factory',
                          return_value=lambda s, w=None, **k: RecordingResource(s, w)):
                Orchestrator(config, store).run_once()
            self.assertTrue(CALLS)
            self.assertTrue(all(attempt == 1 for _, attempt in CALLS), CALLS)


if __name__ == '__main__':
    unittest.main()
