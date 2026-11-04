"""冻结任务放弃、排队任务取消：任务结束为 failed，记录保留，不再运行。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from test_card37 import _isolated_config
from xiaoba_agentos.cli import main
from xiaoba_agentos.store import EventStore


class AbandonAndCancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = _isolated_config(Path(self.tmp.name), [])
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--config', str(self.config.path), *args])
        return code, json.loads(out.getvalue())

    def frozen(self):
        task = self.store.submit('TEST 冻结后放弃', 3)
        self.store.lease_next(60)
        frozen = self.store.freeze_task(task, failure_class='external_upstream',
                                        failures=[{'agent': 'node-a', 'error': '429', 'external': True}])
        return task, frozen['freeze_hash']

    def events(self, task, kind):
        with self.store.read_only() as db:
            return db.execute('SELECT payload_json FROM events WHERE task_id=? AND event_type=?', (task, kind)).fetchall()

    def test_abandon_ends_a_frozen_task_and_keeps_its_record(self):
        task, freeze = self.frozen()
        code, result = self.cli('task-abandon', task, '--freeze-hash', freeze)
        self.assertEqual(0, code, result)
        row = self.store.get_task(task)
        self.assertEqual('failed', row['status'])
        self.assertIn('用户放弃', row['last_error'])
        self.assertEqual(freeze, row['freeze_hash'])
        self.assertEqual(1, len(self.events(task, 'task.abandoned')))
        self.assertIsNone(self.store.lease_next(60))

    def test_abandon_needs_the_current_freeze_hash(self):
        task, _ = self.frozen()
        code, result = self.cli('task-abandon', task, '--freeze-hash', 'f' * 64)
        self.assertEqual(1, code)
        self.assertIn('freeze hash mismatch', result['error'])
        self.assertEqual('frozen_paused', self.store.get_task(task)['status'])

    def test_abandon_rechecks_freeze_record_when_column_hash_is_unchanged(self):
        task, freeze = self.frozen()
        row = self.store.get_task(task)
        frozen = json.loads(row['freeze_json'])
        frozen['failures'][0]['error'] = 'tampered after freeze'
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET freeze_json=? WHERE id=?', (json.dumps(frozen), task))
        code, result = self.cli('task-abandon', task, '--freeze-hash', freeze)
        self.assertEqual(1, code)
        self.assertIn('frozen task record hash mismatch', result['error'])
        self.assertEqual('frozen_paused', self.store.get_task(task)['status'])
        self.assertEqual([], self.events(task, 'task.abandoned'))

    def test_abandon_refuses_a_task_that_is_not_frozen(self):
        task = self.store.submit('TEST 没冻结', 3)
        code, result = self.cli('task-abandon', task, '--freeze-hash', 'f' * 64)
        self.assertEqual(1, code)
        self.assertIn('task is not frozen', result['error'])

    def test_cancel_ends_a_queued_task_only(self):
        task = self.store.submit('TEST 重复提交', 3)
        code, result = self.cli('task-cancel', task)
        self.assertEqual(0, code, result)
        self.assertEqual('failed', self.store.get_task(task)['status'])
        self.assertIn('用户取消', self.store.get_task(task)['last_error'])
        self.assertEqual(1, len(self.events(task, 'task.cancelled')))
        running = self.store.submit('TEST 正在跑', 3)
        self.store.lease_next(60, task_id=running)
        code, result = self.cli('task-cancel', running)
        self.assertEqual(1, code)
        self.assertEqual('running', self.store.get_task(running)['status'])


if __name__ == '__main__':
    unittest.main()
