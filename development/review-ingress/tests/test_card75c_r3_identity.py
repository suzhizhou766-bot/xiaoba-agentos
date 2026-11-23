"""卡 75C-r3 修订：稳定意图 + 被合并意图的持久确认。

仅临时夹具；路径 Path.resolve()；reference_id 使用真实 uuid4。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def new_id():
    return str(uuid.uuid4())


class MultistateReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        (self.root / 'assistant' / 'vault').mkdir(parents=True)
        (self.root / 'memory-outbox').mkdir()
        from xiaoba_agentos.store import EventStore
        from xiaoba_agentos import workday_ledger as ledger
        from xiaoba_review.store import ReviewStore
        self.native = EventStore(self.root / 'agentos.sqlite3')
        self.native.initialize()
        ledger.configure(self.native, SimpleNamespace(
            memory={'obsidian_vault': str(self.root / 'assistant' / 'vault')}))
        self.store = ReviewStore(self.native, self.root / 'memory-outbox')
        self.ledger = ledger
        self.receipt = (self.root / 'receipt.json').resolve()
        self.receipt.write_text('{"ok":true}', encoding='utf-8')

    def statuses(self):
        with self.native.read_only() as db:
            rows = db.execute(
                "SELECT payload_json FROM events WHERE event_type='workday.output_recorded' "
                "ORDER BY sequence").fetchall()
        return [json.loads(r[0])['status'] for r in rows]

    def acked_ids(self):
        with self.native.read_only() as db:
            rows = db.execute(
                "SELECT payload_json FROM events WHERE event_type='workday.reference_acked' "
                "ORDER BY sequence").fetchall()
        return [json.loads(r[0])['reference_id'] for r in rows]

    def enqueue(self, rid, status):
        result = self.ledger.record_reference(
            self.native, self.receipt, node='C75C-R3', task_id='TASK-R3',
            agent='C75C-R3', model='TEST', kind='回执', status=status, reference_id=rid)
        self.assertTrue(result.get('ok'), result)
        self.ledger.rebuild_view(self.native)

    def stage_pair(self):
        with self.store.transaction() as db:
            started = self.store.stage_pending_reference(db, {
                'path': str(self.receipt), 'node': 'C75C-R3', 'task_id': 'TASK-R3',
                'agent': 'C75C-R3', 'model': 'TEST', 'kind': '回执',
                'status': 'execution_started'})
            completed = self.store.stage_pending_reference(db, {
                'path': str(self.receipt), 'node': 'C75C-R3', 'task_id': 'TASK-R3',
                'agent': 'C75C-R3', 'model': 'TEST', 'kind': '回执',
                'status': 'execution_completed'})
        return started, completed

    def test_coalesced_intent_persists_ack_across_status_change(self):
        """主力反例：同内容 id2 被合并后，另一状态落账再重放 id2，不得多出 started。"""
        id1, id2, id3 = new_id(), new_id(), new_id()
        self.enqueue(id1, 'execution_started')
        self.enqueue(id2, 'execution_started')  # coalesced → durable ack
        self.assertIn(id2, self.acked_ids())
        self.enqueue(id3, 'execution_completed')
        self.enqueue(id2, 'execution_started')  # replay coalesced
        self.assertEqual(self.statuses(), ['execution_started', 'execution_completed'])
        self.assertEqual(self.acked_ids().count(id2), 1)

    def test_invalid_reference_id_rejected(self):
        for bad in ('intent original', '  padded  ', '含中文', '', 'x' * 129, 'a\nb'):
            with self.assertRaises(ValueError, msg=bad):
                self.ledger._queue_reference(
                    self.native, self.receipt, node='C75C-R3', task_id='TASK-R3',
                    agent='C75C-R3', model='TEST', kind='回执', status='execution_started',
                    reference_id=bad)

    def test_uuid_reference_id_canonicalized(self):
        raw = '1E345A37-6AB8-4EE9-9ED7-454991BB5377'
        result = self.ledger.record_reference(
            self.native, self.receipt, node='C75C-R3', task_id='TASK-R3',
            agent='C75C-R3', model='TEST', kind='回执', status='execution_started',
            reference_id=raw)
        self.assertTrue(result.get('ok'), result)
        self.ledger.rebuild_view(self.native)
        with self.native.read_only() as db:
            payload = json.loads(db.execute(
                "SELECT payload_json FROM events WHERE event_type='workday.output_recorded'").fetchone()[0])
        self.assertEqual(payload['reference_id'], raw.lower())

    def test_reference_id_does_not_exempt_sensitive_gate(self):
        from xiaoba_agentos.memory import MemoryBridge
        with patch.object(MemoryBridge, '_sensitive_reason', return_value='forced-secret') as mocked:
            result = self.ledger.record_reference(
                self.native, self.receipt, node='C75C-R3', task_id='TASK-R3',
                agent='C75C-R3', model='TEST', kind='回执', status='execution_started',
                reference_id=new_id())
        self.assertFalse(result.get('ok'))
        self.assertEqual(result.get('warning_code'), 'metadata_rejected')
        scanned = mocked.call_args[0][0]
        self.assertIn('reference_id', scanned)

    def test_production_identifier_is_uuid4(self):
        from xiaoba_review.store import identifier
        sample = identifier()
        parsed = uuid.UUID(sample)
        self.assertEqual(str(parsed), sample)
        self.assertEqual(parsed.version, 4)

    def test_valid_probe_multistate_replay_settles_once(self):
        self.stage_pair()
        writes = {'n': 0}
        original = self.ledger.record_reference

        class ConfirmCrash(BaseException):
            pass

        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            writes['n'] += 1
            if writes['n'] >= 2:
                raise ConfirmCrash()
            return result

        with patch.object(self.ledger, 'record_reference', side_effect=wrapped):
            with self.assertRaises(ConfirmCrash):
                self.store.recover_pending_references()
        with self.native.read_only() as db:
            pending = db.execute('SELECT COUNT(*) FROM ri_pending_references').fetchone()[0]
        queue = self.native.path.parent / '.workday-references'
        queued = len(list(queue.glob('*.json'))) if queue.is_dir() else 0
        self.assertEqual(pending, 2)
        self.assertEqual(queued, 2)
        self.assertEqual(self.store.recover_pending_references(), 2)
        self.ledger.rebuild_view(self.native)
        self.assertEqual(self.statuses(), ['execution_started', 'execution_completed'])

    def test_consumer_then_producer_replay_still_once(self):
        started, completed = self.stage_pair()
        self.assertEqual(self.store.recover_pending_references(), 2)
        self.ledger.rebuild_view(self.native)
        self.assertEqual(self.statuses(), ['execution_started', 'execution_completed'])
        for pending_id, status in ((started, 'execution_started'), (completed, 'execution_completed')):
            self.ledger.record_reference(
                self.native, self.receipt, node='C75C-R3', task_id='TASK-R3',
                agent='C75C-R3', model='TEST', kind='回执', status=status,
                recorded_at=time.time(), reference_id=pending_id)
        self.ledger.rebuild_view(self.native)
        self.assertEqual(self.statuses(), ['execution_started', 'execution_completed'])

    def test_legacy_queue_without_reference_id_keeps_aba(self):
        for status in ('execution_started', 'execution_completed', 'execution_started'):
            self.ledger.record_reference(
                self.native, self.receipt, node='C75C-R3', task_id='TASK-LEGACY',
                agent='C75C-R3', model='TEST', kind='回执', status=status)
        self.ledger.rebuild_view(self.native)
        self.assertEqual(
            self.statuses(),
            ['execution_started', 'execution_completed', 'execution_started'])

    def test_distinct_ids_keep_aba(self):
        for status in ('execution_started', 'execution_completed', 'execution_started'):
            self.enqueue(new_id(), status)
        self.assertEqual(
            self.statuses(),
            ['execution_started', 'execution_completed', 'execution_started'])

    def test_event_commit_before_queue_unlink_survives_coalesced_replay(self):
        id1, id2 = new_id(), new_id()
        self.enqueue(id1, 'execution_started')
        self.enqueue(id2, 'execution_started')
        self.assertIn(id2, self.acked_ids())
        queue = self.native.path.parent / '.workday-references'
        queue.mkdir(exist_ok=True)
        payload = {
            'path': str(self.receipt), 'node': 'C75C-R3', 'task_id': 'TASK-R3',
            'agent': 'C75C-R3', 'model': 'TEST', 'kind': '回执',
            'status': 'execution_started',
            'sha256': hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
            'reference_id': id2, 'recorded_at': time.time(),
        }
        (queue / 'leftover-coalesced.json').write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
        self.ledger.rebuild_view(self.native)
        self.assertEqual(self.statuses(), ['execution_started'])
        self.assertEqual(self.acked_ids().count(id2), 1)


SIGKILL_CHILD = r'''
import hashlib, json, os, signal, sys, time, uuid
from pathlib import Path
from types import SimpleNamespace
root = Path(sys.argv[2]).resolve(); repo = Path(sys.argv[3]); phase = sys.argv[1]
sys.path[:0] = [str(repo/'development/integrated-candidate/src'),
                str(repo/'monitor'),
                str(repo/'development/review-ingress')]
sys.dont_write_bytecode = True
from xiaoba_agentos.store import EventStore
from xiaoba_agentos import workday_ledger as ledger
from xiaoba_review.store import ReviewStore

native = EventStore(root/'agentos.sqlite3')
ids_path = root/'ids.json'

def configure():
    ledger.configure(native, SimpleNamespace(
        memory={'obsidian_vault': str((root/'assistant'/'vault').resolve())}))

if phase == 'seed-confirm-crash':
    # 写完两条队列后、pending 确认提交前真实 SIGKILL（消费者交错窗口）
    native.initialize()
    (root/'assistant'/'vault').mkdir(parents=True, exist_ok=True)
    (root/'memory-outbox').mkdir(parents=True, exist_ok=True)
    configure()
    store = ReviewStore(native, root/'memory-outbox')
    receipt = (root/'receipt.json').resolve()
    receipt.write_text('{"ok":true}', encoding='utf-8')
    with store.transaction() as db:
        for status in ('execution_started', 'execution_completed'):
            store.stage_pending_reference(db, {
                'path': str(receipt), 'node': 'C75C-R3', 'task_id': 'TASK-R3',
                'agent': 'C75C-R3', 'model': 'TEST', 'kind': '回执', 'status': status})
    n = {'c': 0}
    original = ledger.record_reference
    def wrap(*a, **k):
        result = original(*a, **k)
        n['c'] += 1
        if n['c'] >= 2:
            sys.stdout.write(json.dumps({'queued': n['c']}) + '\n'); sys.stdout.flush()
            os.kill(os.getpid(), signal.SIGKILL)
        return result
    ledger.record_reference = wrap
    store.recover_pending_references()
    raise SystemExit('unexpected survive')

elif phase == 'seed-coalesce-ack-crash':
    # 合并确认已写入事件链后，残留队列未 unlink；再塞入 completed 后 SIGKILL
    native.initialize()
    (root/'assistant'/'vault').mkdir(parents=True, exist_ok=True)
    configure()
    receipt = (root/'receipt.json').resolve()
    receipt.write_text('{"ok":true}', encoding='utf-8')
    id1, id2, id3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    ids_path.write_text(json.dumps({'id1': id1, 'id2': id2, 'id3': id3}), encoding='utf-8')
    for rid, status in ((id1, 'execution_started'), (id2, 'execution_started')):
        assert ledger.record_reference(
            native, receipt, node='C75C-R3', task_id='TASK-R3', agent='C75C-R3',
            model='TEST', kind='回执', status=status, reference_id=rid).get('ok')
    ledger.rebuild_view(native)
    with native.read_only() as db:
        acked = [json.loads(r[0])['reference_id'] for r in db.execute(
            "SELECT payload_json FROM events WHERE event_type='workday.reference_acked'")]
    assert id2 in acked, acked
    queue = native.path.parent / '.workday-references'
    queue.mkdir(exist_ok=True)
    body = {
        'path': str(receipt), 'node': 'C75C-R3', 'task_id': 'TASK-R3', 'agent': 'C75C-R3',
        'model': 'TEST', 'kind': '回执', 'status': 'execution_started',
        'sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(),
        'reference_id': id2, 'recorded_at': time.time(),
    }
    (queue / 'coalesced-replay.json').write_text(
        json.dumps(body, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    assert ledger.record_reference(
        native, receipt, node='C75C-R3', task_id='TASK-R3', agent='C75C-R3',
        model='TEST', kind='回执', status='execution_completed', reference_id=id3).get('ok')
    sys.stdout.write(json.dumps({
        'seeded': True, 'ids': {'id1': id1, 'id2': id2, 'id3': id3},
        'acked_before_kill': acked}) + '\n')
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)

elif phase == 'recover':
    configure()
    outbox = root / 'memory-outbox'
    if outbox.is_dir():
        store = ReviewStore(native, outbox)
        recovered = store.recover_pending_references()
    else:
        recovered = 0
    ledger.rebuild_view(native)
    ledger.rebuild_view(native)
    with native.read_only() as db:
        statuses = [json.loads(r[0])['status'] for r in db.execute(
            "SELECT payload_json FROM events WHERE event_type='workday.output_recorded' ORDER BY sequence")]
        acked = [json.loads(r[0])['reference_id'] for r in db.execute(
            "SELECT payload_json FROM events WHERE event_type='workday.reference_acked' ORDER BY sequence")]
        pending = db.execute('SELECT COUNT(*) FROM ri_pending_references').fetchone()[0] if outbox.is_dir() else 0
    ids = json.loads(ids_path.read_text()) if ids_path.is_file() else {}
    sys.stdout.write(json.dumps({
        'statuses': statuses, 'acked': acked, 'ids': ids,
        'pending': pending, 'recovered': recovered,
    }, ensure_ascii=False) + '\n')
    sys.stdout.flush()
else:
    raise SystemExit('bad phase')
'''


class RealCrashAndMultiprocessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = Path(__file__).resolve().parents[3]
        if not (self.repo / 'development/review-ingress').is_dir():
            self.repo = Path(os.environ['XIAOBA_R3_FIXTURE_ROOT']).resolve()

    def run_phase(self, phase, root=None):
        target = root or self.root
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run(
            [sys.executable, '-B', '-c', SIGKILL_CHILD, phase, str(target), str(self.repo)],
            cwd='/tmp', env=env, capture_output=True, text=True, timeout=120)

    def _parallel_recover(self, root):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        procs = [
            subprocess.Popen(
                [sys.executable, '-B', '-c', SIGKILL_CHILD, 'recover', str(root), str(self.repo)],
                cwd='/tmp', env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, out + err)
            results.append(json.loads(out.strip().splitlines()[-1]))
        return results

    def test_sigkill_before_confirm_then_multiprocess_recover(self):
        seed = self.run_phase('seed-confirm-crash')
        self.assertEqual(seed.returncode, -9, seed.stdout + seed.stderr)
        results = self._parallel_recover(self.root)
        for item in results:
            self.assertEqual(item['statuses'], ['execution_started', 'execution_completed'], item)
            self.assertEqual(item['pending'], 0)
        again = self.run_phase('recover')
        state = json.loads(again.stdout.strip().splitlines()[-1])
        self.assertEqual(state['statuses'], ['execution_started', 'execution_completed'])
        self.assertEqual(state['recovered'], 0)

    def test_sigkill_after_coalesce_ack_then_multiprocess_recover(self):
        seed = self.run_phase('seed-coalesce-ack-crash')
        self.assertEqual(seed.returncode, -9, seed.stdout + seed.stderr)
        seeded = json.loads(seed.stdout.strip().splitlines()[-1])
        results = self._parallel_recover(self.root)
        for item in results:
            self.assertEqual(item['statuses'], ['execution_started', 'execution_completed'], item)
            self.assertIn(seeded['ids']['id2'], item['acked'])
            self.assertEqual(item['acked'].count(seeded['ids']['id2']), 1)
        again = self.run_phase('recover')
        state = json.loads(again.stdout.strip().splitlines()[-1])
        self.assertEqual(state['statuses'], ['execution_started', 'execution_completed'])
        self.assertEqual(state['acked'].count(seeded['ids']['id2']), 1)

    def test_consumer_interleave_coalesce_and_status_change(self):
        """生产者交错：进程 A 合并确认后崩溃；进程 B 落 completed 并消化残留。"""
        root_a = self.root
        seed = self.run_phase('seed-coalesce-ack-crash', root=root_a)
        self.assertEqual(seed.returncode, -9, seed.stdout + seed.stderr)
        seeded = json.loads(seed.stdout.strip().splitlines()[-1])
        # 第二消费者立即 recover（交错）
        recover = self.run_phase('recover', root=root_a)
        self.assertEqual(recover.returncode, 0, recover.stdout + recover.stderr)
        state = json.loads(recover.stdout.strip().splitlines()[-1])
        self.assertEqual(state['statuses'], ['execution_started', 'execution_completed'])
        self.assertIn(seeded['ids']['id2'], state['acked'])
        # 再重放被合并 ID（模拟迟到的生产者）
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        replay = r'''
import json, sys
from pathlib import Path
from types import SimpleNamespace
root = Path(sys.argv[1]).resolve(); repo = Path(sys.argv[2])
sys.path[:0] = [str(repo/'development/integrated-candidate/src'),
                str(repo/'monitor'),
                str(repo/'development/review-ingress')]
from xiaoba_agentos.store import EventStore
from xiaoba_agentos import workday_ledger as ledger
native = EventStore(root/'agentos.sqlite3')
ledger.configure(native, SimpleNamespace(memory={'obsidian_vault': str((root/'assistant'/'vault').resolve())}))
ids = json.loads((root/'ids.json').read_text())
receipt = (root/'receipt.json').resolve()
assert ledger.record_reference(
    native, receipt, node='C75C-R3', task_id='TASK-R3', agent='C75C-R3',
    model='TEST', kind='回执', status='execution_started',
    reference_id=ids['id2']).get('ok')
ledger.rebuild_view(native)
with native.read_only() as db:
    statuses = [json.loads(r[0])['status'] for r in db.execute(
        "SELECT payload_json FROM events WHERE event_type='workday.output_recorded' ORDER BY sequence")]
    acked = [json.loads(r[0])['reference_id'] for r in db.execute(
        "SELECT payload_json FROM events WHERE event_type='workday.reference_acked' ORDER BY sequence")]
print(json.dumps({'statuses': statuses, 'acked_count_id2': acked.count(ids['id2'])}))
'''
        late = subprocess.run(
            [sys.executable, '-B', '-c', replay, str(root_a), str(self.repo)],
            cwd='/tmp', env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(late.returncode, 0, late.stdout + late.stderr)
        final = json.loads(late.stdout.strip().splitlines()[-1])
        self.assertEqual(final['statuses'], ['execution_started', 'execution_completed'])
        self.assertEqual(final['acked_count_id2'], 1)


if __name__ == '__main__':
    unittest.main()
