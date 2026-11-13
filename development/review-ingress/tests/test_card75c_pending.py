"""卡 75C：业务提交后、台账入队回调前的崩溃窗口（库内待登记表候选修复）。

反例用真实 ReviewStore.transaction()＋真实 AgentWorkspace.begin/finish＋
BackendRegistry.adapter_for 的真实 on_receipt 接线，在子进程里于提交后、
队列写盘前 SIGKILL；重启后走正常启动入口（ReviewStore 构造）与常规台账
渲染路径补登记。全部临时库与临时文件，不碰真实 8799、生产库与节点模型。
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from test_agy_cli import CliFixture

REPO = Path(__file__).resolve().parents[3]

CHILD = r'''
import hashlib, json, os, signal, sqlite3, sys
from pathlib import Path
root = Path(sys.argv[2]); repo = Path(sys.argv[3]); phase = sys.argv[1]
sys.path[:0] = [str(repo/'development/integrated-candidate/src'),
                str(repo/'architecture-package-20261027/shared/工具/小八监看'),
                str(repo/'development/review-ingress')]
sys.dont_write_bytecode = True
from types import SimpleNamespace
from xiaoba_agentos.store import EventStore
from xiaoba_agentos import workday_ledger as ledger
from xiaoba_review.store import ReviewStore
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.resource_stdio import json_stdio_factory

(root/'workspaces').mkdir(parents=True, exist_ok=True)
(root/'memory-outbox').mkdir(parents=True, exist_ok=True)
(root/'assistant'/'vault').mkdir(parents=True, exist_ok=True)
native = EventStore(root/'agentos.sqlite3')
native.initialize()
ledger.configure(native, SimpleNamespace(memory={'obsidian_vault': str(root/'assistant/vault')}))
store = ReviewStore(native, root/'memory-outbox', root/'workspaces')

if phase == 'restart':
    rendered = ledger.rebuild_view(native)
    with native.read_only() as db:
        events = [json.loads(r[0]) for r in db.execute(
            "SELECT payload_json FROM events WHERE event_type='workday.output_recorded' ORDER BY sequence")]
        started = db.execute("SELECT COUNT(*) FROM events WHERE event_type='workday.started'").fetchone()[0]
        try:
            pending = db.execute('SELECT COUNT(*) FROM ri_pending_references').fetchone()[0]
        except sqlite3.OperationalError:
            pending = 'no-table'
    queue = native.path.parent/'.workday-references'
    queued = sorted(p.name for p in queue.glob('*.json')) if queue.is_dir() else []
    receipts = sorted((root/'workspaces'/'C75C'/'任务记录').glob('*/执行回执-*.json'))
    print(json.dumps({'events': events, 'started': started, 'pending': pending, 'queued': queued,
                      'receipt_count': len(receipts),
                      'receipt_sha256': hashlib.sha256(receipts[0].read_bytes()).hexdigest() if receipts else None,
                      'receipt_path': str(receipts[0]) if receipts else None,
                      'rendered_ok': bool(rendered.get('ok'))}, ensure_ascii=False))
    raise SystemExit(0)

backends = BackendRegistry(store, resource_factory=lambda s: json_stdio_factory(s, root/'workspaces'))
pool = AgentPool(store, backends)
entry = root/'entry'; entry.write_text('#!/bin/sh\nexit 0\n'); entry.chmod(0o700)
source = pool.add({'node_id':'C75C','name':'C75C','agent':'C75C','kind':'review',
                   'interface':'cli','entry':str(entry),'model':'TEST','agent_type':'agy'})
pool.change('C75C', source['node']['revision'], 'toggle', True)
with native.read_only() as db:
    adapter = backends.adapter_for(store.source(db, 'C75C'))
workspace = adapter.workspace
record = workspace.begin('review', 'ASSIGN-75C',
    {'task_id':'TASK-75C','step_code':'REVIEW-01','model':'TEST','instruction':'TEST 隔离反例','context':{}})
result = {'body':'TEST card75c receipt','status':'completed','metadata':{},'evidence_refs':['TEST']}
evidence = {'provider':'test','model':'TEST','route_source':'native_cli_per_step'}
mode = sys.argv[4]
if mode == 'before-callback':
    # 崩溃点：业务已提交，回调刚开始、队列文件尚未写盘。
    ledger.record_reference = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)
elif mode == 'after-queue':
    original = ledger.record_reference
    def queued(*a, **k):
        original(*a, **k)
        os.kill(os.getpid(), signal.SIGKILL)
    ledger.record_reference = queued
with store.transaction() as db:
    native._append_event_tx(db, None, 'workday.started', {'message_id':'CARD75C','plan_digest':'TEST'})
    if mode == 'before-commit':
        os.kill(os.getpid(), signal.SIGKILL)
    workspace.finish(record, 'execution_completed', result, evidence)
print(json.dumps({'unexpected': 'child survived the crash window'}))
'''


def run_child(root, phase, mode=None):
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    argv = [sys.executable, '-B', '-c', CHILD, phase, str(root), str(REPO)]
    if mode:
        argv.append(mode)
    return subprocess.run(argv, cwd='/tmp', env=env, capture_output=True, text=True, timeout=120)


class CrashWindowSubprocessTests(unittest.TestCase):
    """真实事务正常入口＋子进程：提交后回调前退出，重启后正常入口补登记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def crash(self, mode):
        child = run_child(self.root, 'crash', mode)
        self.assertEqual(child.returncode, -9, '子进程应在崩溃窗口被 SIGKILL：\n' + child.stdout + child.stderr)
        return child

    def restart(self):
        child = run_child(self.root, 'restart')
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        return json.loads(child.stdout.strip().splitlines()[-1])

    def test_crash_after_commit_before_callback_recovers_on_restart(self):
        # 卡 75C 主反例：修复前引用永久遗漏（events 为空），修复后重启补登记。
        self.crash('before-callback')
        state = self.restart()
        self.assertEqual(state['started'], 1, '业务事务必须已提交')
        self.assertEqual(state['receipt_count'], 1, '工作区回执文件必须已落盘')
        self.assertEqual(len(state['events']), 1, '提交后崩溃的引用必须在重启后补登记：' + json.dumps(state, ensure_ascii=False))
        ref = state['events'][0]
        self.assertEqual(ref['path'], state['receipt_path'])
        self.assertEqual(ref['sha256'], state['receipt_sha256'])
        self.assertEqual(ref['node'], 'C75C')
        self.assertEqual(ref['task_id'], 'TASK-75C')
        self.assertEqual(ref['kind'], '回执')
        self.assertEqual(ref['status'], 'execution_completed')
        self.assertEqual(state['pending'], 0, '补登记后待登记表必须清空')
        self.assertEqual(state['queued'], [], '渲染后队列必须清空')
        again = self.restart()
        self.assertEqual(len(again['events']), 1, '重复重启不得重复结算')
        self.assertEqual(again['pending'], 0)

    def test_crash_before_commit_leaves_no_reference(self):
        self.crash('before-commit')
        state = self.restart()
        self.assertEqual(state['started'], 0, '提交前退出：业务必须回滚')
        self.assertEqual(state['receipt_count'], 0)
        self.assertEqual(state['events'], [])
        self.assertIn(state['pending'], (0, 'no-table'))
        self.assertEqual(state['queued'], [])

    def test_crash_after_queue_before_confirm_settles_once(self):
        self.crash('after-queue')
        queue = self.root / '.workday-references'
        self.assertTrue(queue.is_dir() and any(queue.glob('*.json')), '崩溃前队列文件应已写盘')
        state = self.restart()
        self.assertEqual(state['started'], 1)
        self.assertEqual(len(state['events']), 1, '排队后确认前退出：重启补登记不得重复结算')
        self.assertEqual(state['events'][0]['sha256'], state['receipt_sha256'])
        self.assertEqual(state['pending'], 0)
        again = self.restart()
        self.assertEqual(len(again['events']), 1)


class PendingOutboxTests(CliFixture, unittest.TestCase):
    """待登记表生命周期：回滚、重试、死信、兼容、隔离、批次与并发。"""

    def add_node(self, node_id='C75C-BOX'):
        source = self.pool.add({'node_id': node_id, 'name': node_id, 'agent': node_id, 'kind': 'review',
                                'interface': 'cli', 'entry': str(self.entry), 'model': 'TEST', 'agent_type': 'agy'})
        self.pool.change(node_id, source['node']['revision'], 'toggle', True)
        return source

    def adapter(self, node_id='C75C-BOX'):
        with self.native.read_only() as db:
            return self.backends.adapter_for(self.store.source(db, node_id))

    def receipt(self, name='receipt-75c.json'):
        path = self.root / name
        path.write_text('{"ok":true}', encoding='utf-8')
        return path

    def pending_rows(self):
        with self.native.read_only() as db:
            return [dict(r) for r in db.execute('SELECT * FROM ri_pending_references ORDER BY created_at,rowid')]

    def queue_files(self):
        queue = self.native.path.parent / '.workday-references'
        return sorted(queue.glob('*.json')) if queue.is_dir() else []

    def output_events(self):
        with self.native.read_only() as db:
            return [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM events WHERE event_type='workday.output_recorded' ORDER BY sequence")]

    def stage(self, adapter, path, task='TASK-75C'):
        with self.store.transaction() as db:
            adapter.workspace.on_receipt(path, {'task_id': task}, {'model': 'TEST'}, 'completed')

    def test_commit_stages_row_in_same_transaction_then_callback_clears_it(self):
        self.add_node()
        adapter = self.adapter()
        path = self.receipt()
        with self.store.transaction() as db:
            adapter.workspace.on_receipt(path, {'task_id': 'TASK-75C'}, {'model': 'TEST'}, 'completed')
            rows = [dict(r) for r in db.execute('SELECT * FROM ri_pending_references')]
            self.assertEqual(len(rows), 1, '待登记行必须与业务同事务写入')
            self.assertEqual(self.queue_files(), [], '提交前不得写队列文件')
        self.assertEqual(self.pending_rows(), [], '落盘成功后必须删除待登记行')
        files = self.queue_files()
        self.assertEqual(len(files), 1)
        ref = json.loads(files[0].read_text())
        self.assertEqual(ref['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_rollback_leaves_no_pending_row(self):
        self.add_node()
        adapter = self.adapter()
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                adapter.workspace.on_receipt(self.receipt(), {'task_id': 'TASK-75C'}, {'model': 'TEST'}, 'completed')
                raise RuntimeError('TEST rollback')
        self.assertEqual(self.pending_rows(), [])
        self.assertEqual(self.queue_files(), [])

    def test_failed_queue_write_is_retried_by_recovery(self):
        self.add_node()
        adapter = self.adapter()
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            self.stage(adapter, self.receipt())
        rows = self.pending_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['attempts'], 1)
        self.assertEqual(self.store.recover_pending_references(), 1)
        self.assertEqual(self.pending_rows(), [])
        self.assertEqual(len(self.queue_files()), 1)
        self.assertEqual(self.store.recover_pending_references(), 0)

    def test_empty_recover_skips_write_lock_nonempty_keeps_one(self):
        """空队列不取写锁；有待办时仍只开一次写事务（全链校验路径保留）。"""
        counter = {'begin_immediate': 0}
        real_connect = sqlite3.connect

        class CountingConnection:
            def __init__(self, conn):
                object.__setattr__(self, '_conn', conn)

            def execute(self, sql, parameters=()):
                text = sql if isinstance(sql, str) else str(sql)
                if text.lstrip().upper().startswith('BEGIN IMMEDIATE'):
                    counter['begin_immediate'] += 1
                return self._conn.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __setattr__(self, name, value):
                if name == '_conn':
                    object.__setattr__(self, name, value)
                else:
                    setattr(self._conn, name, value)

        def counting_connect(*args, **kwargs):
            return CountingConnection(real_connect(*args, **kwargs))

        with patch('sqlite3.connect', counting_connect):
            counter['begin_immediate'] = 0
            self.assertEqual(self.store.recover_pending_references(), 0)
            self.assertEqual(counter['begin_immediate'], 0, '空队列不得开启写事务')

            self.add_node()
            adapter = self.adapter()
            with patch('xiaoba_agentos.workday_ledger.record_reference',
                       return_value={'ok': False, 'warning_code': 'registration_failed'}):
                self.stage(adapter, self.receipt())
            before = counter['begin_immediate']
            with patch('xiaoba_agentos.workday_ledger.record_reference',
                       return_value={'ok': False, 'warning_code': 'registration_failed'}):
                self.assertEqual(self.store.recover_pending_references(), 0)
            self.assertEqual(counter['begin_immediate'] - before, 1, '非空时写事务恰一次')

    def test_unconfigured_root_keeps_row_without_counting(self):
        self.add_node()
        adapter = self.adapter()
        root = self.native.workday_root
        del self.native.workday_root
        try:
            self.stage(adapter, self.receipt())
            self.assertEqual(self.pending_rows()[0]['attempts'], 0, '未配置不是引用失败，不得计次')
            self.assertEqual(self.store.recover_pending_references(), 0)
            self.assertEqual(self.pending_rows()[0]['attempts'], 0)
        finally:
            self.native.workday_root = root
        self.assertEqual(self.store.recover_pending_references(), 1)
        self.assertEqual(self.pending_rows(), [])

    def test_three_failures_move_to_dead_letter_with_warning(self):
        self.add_node()
        adapter = self.adapter()
        path = self.receipt()
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            self.stage(adapter, path)
            self.assertEqual(self.store.recover_pending_references(), 0)
            self.assertEqual(self.pending_rows()[0]['attempts'], 2)
            self.assertEqual(self.store.recover_pending_references(), 0)
        rows = self.pending_rows()
        self.assertEqual(len(rows), 1, '满 3 次失败必须保留唯一待登记材料')
        self.assertEqual(rows[0]['state'], 'dead')
        self.assertEqual(rows[0]['attempts'], 3)
        self.assertIn('registration_failed', rows[0]['last_error'] or '')
        status = json.loads((self.native.path.parent / '.workday-status.json').read_text())
        self.assertEqual(status['warning_code'], 'pending_reference_held')
        self.assertIn('未移入侧车死信目录', status['message'])
        self.assertNotEqual(status['warning_code'], 'reference_dead')
        self.assertFalse((self.native.path.parent / '.workday-references-dead').is_dir())
        self.assertEqual(self.queue_files(), [])
        # 默认恢复不得无休止重试已死信行；故障修复后需显式参数。
        self.assertEqual(self.store.recover_pending_references(), 0)
        self.assertEqual(self.store.recover_pending_references(rows[0]['id']), 0)
        self.assertEqual(self.store.recover_pending_references(rows[0]['id'], retry_dead=True), 1)
        self.assertEqual(self.pending_rows(), [])
        self.assertEqual(len(self.queue_files()), 1)
        ref = json.loads(self.queue_files()[0].read_text())
        self.assertEqual(ref['path'], str(path))
        self.assertEqual(ref['recorded_at'], rows[0]['created_at'])

    def test_replay_still_validates_path_and_hash(self):
        self.add_node()
        adapter = self.adapter()
        path = self.receipt()
        # 先让入队失败留下待登记行，再删文件，恢复时路径校验必须计失败。
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            self.stage(adapter, path)
        path.unlink()
        self.assertEqual(self.store.recover_pending_references(), 0)
        rows = self.pending_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['attempts'], 2, '文件丢失必须按失败计次并告警')
        status = json.loads((self.native.path.parent / '.workday-status.json').read_text())
        self.assertEqual(status['warning_code'], 'registration_failed')
        self.assertEqual(self.queue_files(), [])

    def test_replay_preserves_original_recorded_at(self):
        self.add_node()
        adapter = self.adapter()
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            self.stage(adapter, self.receipt())
        created_at = self.pending_rows()[0]['created_at']
        time.sleep(0.01)
        self.assertEqual(self.store.recover_pending_references(), 1)
        ref = json.loads(self.queue_files()[0].read_text())
        self.assertEqual(ref['recorded_at'], created_at, '补登记必须保留原登记时间，跨天重启不得把同一回执记到两天')

    def test_replayed_reference_deduplicates_settlement(self):
        self.add_node()
        adapter = self.adapter()
        path = self.receipt()
        self.stage(adapter, path)
        self.assertEqual(len(self.queue_files()), 1)
        from xiaoba_agentos import workday_ledger as ledger
        ledger.rebuild_view(self.native)
        self.assertEqual(len(self.output_events()), 1)
        # 模拟“队列已写、确认前崩溃”：同一引用再次入队，结算仍恰好一次。
        recorded_at = time.time()
        with self.store.transaction() as db:
            db.execute(
                'INSERT INTO ri_pending_references(id,data,created_at,attempts,state,last_error) '
                'VALUES (?,?,?,?,?,?)',
                ('replay', json.dumps({'path': str(path), 'node': 'C75C-BOX', 'task_id': 'TASK-75C',
                 'agent': 'C75C-BOX', 'model': 'TEST', 'kind': '回执', 'status': 'completed'},
                 ensure_ascii=False), recorded_at, 0, 'pending', None))
        self.assertEqual(self.store.recover_pending_references(), 1)
        ledger.rebuild_view(self.native)
        self.assertEqual(len(self.output_events()), 1, '重放不得重复结算')
        self.assertEqual(self.pending_rows(), [])

    def test_concurrent_recovery_settles_each_row_once(self):
        self.add_node()
        adapter = self.adapter()
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            for index in range(6):
                self.stage(adapter, self.receipt('receipt-75c-%d.json' % index), task='TASK-75C-%d' % index)
        self.assertEqual(len(self.pending_rows()), 6)
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = [f.result(30) for f in [executor.submit(self.store.recover_pending_references) for _ in range(4)]]
        self.assertEqual(sum(results), 6, '并发恢复合计恰好结算 6 条：' + str(results))
        self.assertEqual(self.pending_rows(), [])
        self.assertEqual(len(self.queue_files()), 6)
        from xiaoba_agentos import workday_ledger as ledger
        ledger.rebuild_view(self.native)
        self.assertEqual(len(self.output_events()), 6)

    def test_recovery_batch_is_bounded(self):
        import xiaoba_review.store as store_module
        self.add_node()
        adapter = self.adapter()
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            for index in range(5):
                self.stage(adapter, self.receipt('receipt-batch-%d.json' % index), task='TASK-BATCH-%d' % index)
        with patch.object(store_module, 'PENDING_REFERENCE_BATCH', 2):
            self.assertEqual(self.store.recover_pending_references(), 2)
            self.assertEqual(len(self.pending_rows()), 3)
            self.assertEqual(self.store.recover_pending_references(), 2)
            self.assertEqual(self.store.recover_pending_references(), 1)
        self.assertEqual(self.pending_rows(), [])

    def test_old_database_without_table_stays_compatible(self):
        self.add_node()
        with self.native.read_only() as db:
            before = db.execute('SELECT COUNT(*) FROM ri_sources').fetchone()[0]
        with sqlite3.connect(str(self.native.path)) as db:
            db.execute('DROP TABLE ri_pending_references')
        fresh = __import__('xiaoba_review.store', fromlist=['ReviewStore']).ReviewStore(
            self.native, self.root / 'memory-outbox')
        self.assertEqual(fresh.recover_pending_references(), 0)
        with self.native.read_only() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ri_sources').fetchone()[0], before)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM ri_pending_references').fetchone()[0], 0)
        adapter = self.adapter()
        self.stage(adapter, self.receipt())
        self.assertEqual(len(self.queue_files()), 1)

    def test_ledger_import_failure_keeps_business_and_node_qualification(self):
        self.add_node()
        self.probe('C75C-BOX')
        self.assertTrue(self.node('C75C-BOX')['readiness']['ready'])
        adapter = self.adapter()
        original = __builtins__['__import__'] if isinstance(__builtins__, dict) else __builtins__.__import__

        def importing(name, *args, **kwargs):
            if name == 'xiaoba_agentos.workday_ledger':
                raise ImportError('TEST module')
            return original(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=importing):
            self.stage(adapter, self.receipt())
            self.assertEqual(len(self.pending_rows()), 1, '台账模块损坏不得丢引用')
            self.assertEqual(self.pending_rows()[0]['attempts'], 0, '模块损坏不得计失败次数')
            self.assertEqual(self.store.recover_pending_references(), 0)
            result = self.pool.runtime.test_run('C75C-BOX', self.node('C75C-BOX')['revision'], 'TEST-75C-import')
            # test_run 若也走到 on_receipt，损坏模块下同样只留待登记、不计次。
            self.assertTrue(all(row['attempts'] == 0 for row in self.pending_rows()))
        self.assertEqual(result['status'], 'passed', '台账异常不得破坏节点测试资格')
        self.assertTrue(self.native.verify_event_chain()['ok'])
        pending = len(self.pending_rows())
        self.assertGreaterEqual(pending, 1, '台账恢复前至少保留手动 stage 的待登记行')
        self.assertEqual(self.store.recover_pending_references(), pending)
        self.assertEqual(self.pending_rows(), [])

    def test_recovery_error_does_not_break_business_settlement(self):
        self.add_node()
        adapter = self.adapter()
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            self.stage(adapter, self.receipt())
        self.assertEqual(len(self.pending_rows()), 1)
        with patch.object(self.native, 'read_only', side_effect=OSError('TEST view')):
            self.assertEqual(self.store.recover_pending_references(), 0)
        with self.store.transaction() as db:
            self.native._append_event_tx(db, None, 'workday.started', {'message_id': 'TEST-75C', 'plan_digest': 'TEST'})
        self.assertTrue(self.native.verify_event_chain()['ok'])
        self.assertEqual(self.store.recover_pending_references(), 1)

    def test_corrupt_payload_is_held_for_inspection(self):
        self.add_node()
        with self.store.transaction() as db:
            db.execute(
                'INSERT INTO ri_pending_references(id,data,created_at,attempts,state,last_error) '
                'VALUES (?,?,?,?,?,?)',
                ('bad-json', '{not-json', time.time(), 0, 'pending', None))
        self.assertEqual(self.store.recover_pending_references(), 0)
        rows = self.pending_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['state'], 'dead')
        self.assertEqual(rows[0]['data'], '{not-json')
        status = json.loads((self.native.path.parent / '.workday-status.json').read_text())
        self.assertEqual(status['warning_code'], 'pending_reference_corrupt')
        self.assertEqual(self.store.recover_pending_references(), 0)

    def test_monitor_idle_and_projection_replay_bounded_pending(self):
        from types import SimpleNamespace
        from xiaoba_review.monitor import OpenReviewModule
        self.add_node()
        adapter = self.adapter()
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            self.stage(adapter, self.receipt('monitor-pending.json'), task='TASK-MONITOR')
        self.assertEqual(len(self.pending_rows()), 1)
        module = OpenReviewModule(SimpleNamespace(log=lambda *a, **k: None, page_token='TEST'),
                                  self.store, self.inbox, self.backends)
        # 投影读路径（state）与写收尾（perform）均应有界补登记。
        code, body = module.state(SimpleNamespace())
        self.assertEqual(code, 200)
        self.assertEqual(body['ok'], True)
        self.assertEqual(self.pending_rows(), [])
        self.assertEqual(len(self.queue_files()), 1)
        with patch('xiaoba_agentos.workday_ledger.record_reference',
                   return_value={'ok': False, 'warning_code': 'registration_failed'}):
            self.stage(adapter, self.receipt('monitor-perform.json'), task='TASK-PERFORM')
        code, body = module.perform(lambda: {'ping': True})
        self.assertEqual(code, 200)
        self.assertEqual(self.pending_rows(), [])
        self.assertGreaterEqual(len(self.queue_files()), 2)


DUAL_CHILD = r'''
import json, os, signal, sys, time
from pathlib import Path
root = Path(sys.argv[2]); repo = Path(sys.argv[3]); phase = sys.argv[1]
sys.path[:0] = [str(repo/'development/integrated-candidate/src'),
                str(repo/'architecture-package-20261027/shared/工具/小八监看'),
                str(repo/'development/review-ingress')]
sys.dont_write_bytecode = True
from types import SimpleNamespace
from xiaoba_agentos.store import EventStore
from xiaoba_agentos import workday_ledger as ledger
from xiaoba_review.store import ReviewStore

native = EventStore(root/'agentos.sqlite3')
if phase == 'seed':
    native.initialize()
    (root/'assistant'/'vault').mkdir(parents=True, exist_ok=True)
    (root/'memory-outbox').mkdir(parents=True, exist_ok=True)
    ledger.configure(native, SimpleNamespace(memory={'obsidian_vault': str(root/'assistant/vault')}))
    store = ReviewStore(native, root/'memory-outbox')
    created = []
    # 直接写入待登记行，避免 stage 后自动 flush 清表；再模拟“队列已写、确认前崩溃”。
    with store.transaction() as db:
        for index in range(2):
            path = root / ('dual-%d.json' % index)
            path.write_text('{"ok":true,"n":%d}' % index, encoding='utf-8')
            pending_id = 'dual-%d' % index
            created_at = time.time() + index * 0.01
            db.execute(
                'INSERT INTO ri_pending_references(id,data,created_at,attempts,state,last_error) '
                'VALUES (?,?,?,?,?,?)',
                (pending_id, json.dumps({
                    'path': str(path), 'node': 'C75C-DUAL', 'task_id': 'TASK-DUAL-%d' % index,
                    'agent': 'C75C-DUAL', 'model': 'TEST', 'kind': '回执', 'status': 'completed'},
                    ensure_ascii=False), created_at, 0, 'pending', None))
            created.append({'id': pending_id, 'path': str(path), 'created_at': created_at,
                            'task_id': 'TASK-DUAL-%d' % index})
    first = created[0]
    ledger.record_reference(native, first['path'], node='C75C-DUAL', task_id=first['task_id'],
                            agent='C75C-DUAL', model='TEST', kind='回执', status='completed',
                            recorded_at=first['created_at'])
    sys.stdout.write(json.dumps({'seeded': created}, ensure_ascii=False) + '\n')
    sys.stdout.flush(); sys.stderr.flush()
    os.kill(os.getpid(), signal.SIGKILL)
elif phase == 'recover':
    ledger.configure(native, SimpleNamespace(memory={'obsidian_vault': str(root/'assistant/vault')}))
    store = ReviewStore(native, root/'memory-outbox')
    # 构造期 recover 已跑一轮；再显式调用证明重复恢复不重记。
    again = store.recover_pending_references()
    rendered = ledger.rebuild_view(native)
    with native.read_only() as db:
        events = [json.loads(r[0]) for r in db.execute(
            "SELECT payload_json FROM events WHERE event_type='workday.output_recorded' ORDER BY sequence")]
        pending = [dict(r) for r in db.execute(
            'SELECT id,state,attempts,created_at FROM ri_pending_references ORDER BY created_at,rowid')]
    queue = native.path.parent/'.workday-references'
    queued = sorted(p.name for p in queue.glob('*.json')) if queue.is_dir() else []
    sys.stdout.write(json.dumps({'again': again, 'events': events, 'pending': pending, 'queued': queued,
                                 'rendered_ok': bool(rendered.get('ok'))}, ensure_ascii=False) + '\n')
    sys.stdout.flush()
else:
    raise SystemExit('unknown phase')
'''


class DualProcessPendingTests(unittest.TestCase):
    """真实双进程：两条待登记、确认前崩溃、并发/重复恢复不重记且保留原时间。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def run_phase(self, phase):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run(
            [sys.executable, '-B', '-c', DUAL_CHILD, phase, str(self.root), str(REPO)],
            cwd='/tmp', env=env, capture_output=True, text=True, timeout=120)

    def test_two_pending_survive_confirm_crash_and_multiprocess_recover_once(self):
        seed = self.run_phase('seed')
        self.assertEqual(seed.returncode, -9, seed.stdout + seed.stderr)
        seeded = json.loads(seed.stdout.strip().splitlines()[-1])['seeded']
        self.assertEqual(len(seeded), 2)
        # 确认前崩溃后：队列至少有第一条，pending 表仍有两行。
        queue = self.root / '.workday-references'
        self.assertTrue(queue.is_dir() and any(queue.glob('*.json')))
        # 双进程同时走正常启动 recover + 显式 recover。
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        procs = [
            subprocess.Popen(
                [sys.executable, '-B', '-c', DUAL_CHILD, 'recover', str(self.root), str(REPO)],
                cwd='/tmp', env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for _ in range(2)]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, out + err)
            results.append(json.loads(out.strip().splitlines()[-1]))
        events = max(results, key=lambda item: len(item['events']))['events']
        self.assertEqual(len(events), 2, json.dumps(results, ensure_ascii=False))
        by_task = {event['task_id']: event for event in events}
        self.assertEqual(set(by_task), {'TASK-DUAL-0', 'TASK-DUAL-1'})
        for item in seeded:
            self.assertEqual(by_task[item['task_id']]['recorded_at'], item['created_at'])
            self.assertEqual(by_task[item['task_id']]['path'], item['path'])
        for item in results:
            self.assertEqual(item['pending'], [])
            self.assertEqual(item['queued'], [])
        # 再开一轮恢复：事件仍恰好两条。
        again = self.run_phase('recover')
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        state = json.loads(again.stdout.strip().splitlines()[-1])
        self.assertEqual(len(state['events']), 2)
        self.assertEqual(state['again'], 0)
        self.assertEqual(state['pending'], [])


if __name__ == '__main__':
    unittest.main()