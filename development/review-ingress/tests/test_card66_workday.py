"""Card66 leftovers: temp cleanup, post-commit queue, import/local-check guards."""
import builtins
import unittest
from pathlib import Path
from unittest.mock import patch

from test_agy_cli import CliFixture


class Card66WorkdayTests(CliFixture, unittest.TestCase):
    def test_write_cleans_temporary_when_replace_fails(self):
        from xiaoba_agentos import workday_ledger as ledger
        folder = self.root / 'views'
        folder.mkdir()
        target = folder / '台账.md'
        target.write_text('keep', encoding='utf-8')
        before = {p.name for p in folder.iterdir()}
        with patch('xiaoba_agentos.workday_ledger.os.replace', side_effect=OSError('TEST replace')):
            with self.assertRaises(OSError):
                ledger._write(target, 'new body\n')
        after = {p.name for p in folder.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual('keep', target.read_text(encoding='utf-8'))
        self.assertFalse(any(name.startswith('.workday-') for name in after))

    def test_on_receipt_does_not_queue_when_transaction_rolls_back(self):
        source = self.pool.add({
            'node_id': 'C66-RCPT', 'name': 'C66-RCPT', 'agent': 'C66-RCPT', 'kind': 'review',
            'interface': 'cli', 'entry': str(self.entry), 'model': 'TEST', 'agent_type': 'agy',
        })
        adapter = self.backends.adapter_for(source)
        receipt = self.root / 'receipt.json'
        receipt.write_text('{"ok":true}', encoding='utf-8')
        queue = Path(str(self.native.path) + '').parent / '.workday-references'
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                adapter.workspace.on_receipt(
                    receipt, {'task_id': 'TASK-1'}, {'model': 'TEST'}, 'completed')
                self.assertFalse(queue.exists() and any(queue.glob('*.json')))
                raise RuntimeError('TEST rollback')
        self.assertFalse(queue.exists() and any(queue.glob('*.json')))

    def test_on_receipt_queues_after_successful_commit(self):
        source = self.pool.add({
            'node_id': 'C66-OK', 'name': 'C66-OK', 'agent': 'C66-OK', 'kind': 'review',
            'interface': 'cli', 'entry': str(self.entry), 'model': 'TEST', 'agent_type': 'agy',
        })
        adapter = self.backends.adapter_for(source)
        receipt = self.root / 'receipt-ok.json'
        receipt.write_text('{"ok":true}', encoding='utf-8')
        queue = self.native.path.parent / '.workday-references'
        with self.store.transaction() as db:
            adapter.workspace.on_receipt(
                receipt, {'task_id': 'TASK-2'}, {'model': 'TEST'}, 'completed')
            self.assertFalse(queue.exists() and any(queue.glob('*.json')))
        self.assertTrue(any(queue.glob('*.json')))

    def test_workday_import_failure_can_be_swallowed_by_hosts(self):
        original = builtins.__import__
        def importing(name, *args, **kwargs):
            if name == 'xiaoba_agentos.workday_ledger':
                raise ImportError('TEST module')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=importing):
            try:
                from xiaoba_agentos.workday_ledger import configure  # noqa: F401
                ok = True
            except Exception:
                ok = False
        self.assertFalse(ok)


if __name__ == '__main__':
    unittest.main()
