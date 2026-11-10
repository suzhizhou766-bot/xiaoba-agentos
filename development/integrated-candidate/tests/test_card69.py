"""卡 69：台账引用队列的错误分类，以及 _write 在 fsync 失败时的临时文件清理。"""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos import workday_ledger as ledger
from xiaoba_agentos.store import EventStore


class WriteFsyncCleanupTests(unittest.TestCase):
    def test_fsync_failure_removes_temporary(self):
        # 修复前：temporary 在 fsync 之后才赋值，fsync 抛错时 finally 看不到它，
        # 同目录会留下 .workday-XXXXXXXX（卡 69 第 3 项）。
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root).resolve()
            target = folder / '台账.md'
            target.write_text('keep', encoding='utf-8')
            with patch('xiaoba_agentos.workday_ledger.os.fsync', side_effect=OSError('TEST fsync')):
                with self.assertRaises(OSError):
                    ledger._write(target, 'new\n')
            self.assertEqual({'台账.md'}, {p.name for p in folder.iterdir()})
            self.assertEqual('keep', target.read_text(encoding='utf-8'))


class ContentionClassificationTests(unittest.TestCase):
    def test_only_locked_and_busy_are_transient(self):
        def error(message, code=None):
            exc = sqlite3.OperationalError(message)
            if code is not None:
                exc.sqlite_errorcode = code
            return exc

        self.assertTrue(ledger._transient_contention(error('database is locked', 5)))
        self.assertTrue(ledger._transient_contention(error('database table is locked', 6)))
        self.assertTrue(ledger._transient_contention(error('database is locked', 517)))   # SQLITE_BUSY_SNAPSHOT
        self.assertTrue(ledger._transient_contention(error('database table is locked', 262)))  # SQLITE_LOCKED_SHAREDCACHE
        self.assertFalse(ledger._transient_contention(error('disk I/O error', 10)))
        self.assertFalse(ledger._transient_contention(error('attempt to write a readonly database', 8)))
        # 没有 sqlite_errorcode 时按错误信息判断。
        self.assertTrue(ledger._transient_contention(error('database is locked')))
        self.assertFalse(ledger._transient_contention(error('disk I/O error')))


class FlushReferenceFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = EventStore(self.root / 'runtime/db.sqlite')
        self.store.initialize()
        self.store.workday_root = self.root / 'assistant'
        self.queue = self.root / 'runtime' / '.workday-references'
        self.dead = self.root / 'runtime' / '.workday-references-dead'
        product = self.root / 'product.md'
        product.write_text('TEST product', encoding='utf-8')
        result = ledger.record_reference(
            self.store, product, node='TEST node', task_id=None, agent='TEST agent',
            model='TEST model', kind='回执', status='completed')
        self.assertTrue(result.get('ok'), result)
        self.assertEqual(1, len(list(self.queue.glob('*.json'))))

    def flush(self, error):
        with patch.object(self.store, 'connect', side_effect=error):
            ledger._flush_references(self.store)

    def test_disk_io_error_counts_and_goes_to_dead_letter(self):
        # 修复前：所有 OperationalError 都被当成瞬时竞争，磁盘 I/O 错误永远不计次数，
        # 队列里这一条既不会成功也不会进死信（卡 69 第 2 项）。
        for _ in range(ledger.REFERENCE_FAILURE_LIMIT):
            self.flush(sqlite3.OperationalError('disk I/O error'))
        self.assertEqual([], list(self.queue.glob('*.json')))
        self.assertEqual(1, len(list(self.dead.glob('*.json'))))

    def test_locked_and_busy_stay_queued_without_counting(self):
        for _ in range(ledger.REFERENCE_FAILURE_LIMIT + 2):
            self.flush(sqlite3.OperationalError('database is locked'))
        self.assertEqual(1, len(list(self.queue.glob('*.json'))))
        self.assertFalse(self.dead.is_dir() and list(self.dead.glob('*.json')))


if __name__ == '__main__':
    unittest.main()
