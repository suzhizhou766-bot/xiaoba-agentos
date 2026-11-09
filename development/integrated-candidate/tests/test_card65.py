"""Card65: temporary queues/DB only; no models or production workspace writes."""
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from xiaoba_agentos.store import EventStore
from xiaoba_agentos import workday_ledger as ledger

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.store=EventStore(self.root/'runtime/db.sqlite');self.store.initialize()
        ledger.configure(self.store,SimpleNamespace(memory={'obsidian_vault':str(self.root/'assistant/vault')}))
    def queue(self,count):
        folder=self.store.path.parent/'.workday-references';folder.mkdir(exist_ok=True)
        for n in range(count):
            ref=dict(path=str(self.root/f'product-{n}'),node='TEST',task_id=None,agent='TEST',model='TEST',kind='产出',status='done',sha256='a'*64,recorded_at=time.time())
            (folder/f'{n:020}.json').write_text(json.dumps(ref))
        return folder
    def count(self):
        with self.store.read_only() as db:return db.execute("SELECT COUNT(*) FROM events WHERE event_type='workday.output_recorded'").fetchone()[0]
    def test_D2_budget_single_batch_and_pending_survives_render(self):
        folder=self.queue(7)
        with patch.object(ledger,'REFERENCE_BATCH_LIMIT',3,create=True),patch.object(self.store,'connect',wraps=self.store.connect) as connections:
            ledger._flush_references(self.store)
        self.assertEqual(1,connections.call_count);self.assertEqual(3,self.count());self.assertEqual(4,len(list(folder.glob('*.json'))))
        with patch.object(ledger,'REFERENCE_BATCH_LIMIT',1,create=True):
            self.assertIn('directory',ledger.rebuild_view(self.store))
        self.assertTrue(ledger.workday_status(self.store)['pending'])
    def test_D2_transaction_rolls_back_entire_batch(self):
        folder=self.queue(3);original=self.store._append_event_tx;calls=0
        def append(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise sqlite3.OperationalError('TEST batch failure')
            return original(*args,**kwargs)
        with patch.object(self.store,'_append_event_tx',side_effect=append):ledger._flush_references(self.store)
        self.assertEqual(0,self.count());self.assertEqual(3,len(list(folder.glob('*.json'))))
        ledger._flush_references(self.store);self.assertEqual(3,self.count())
    def test_r4_lock_contention_never_sends_valid_references_to_dead_letter(self):
        # v1.6.12 第四轮 DSH 缺陷 2：拿不到写锁是瞬时竞争，不计死信次数，锁释放后照常登记。
        folder=self.queue(3)
        holder=sqlite3.connect(self.store.path);holder.execute('BEGIN IMMEDIATE')
        try:
            for _ in range(5):ledger._flush_references(self.store)
        finally:holder.rollback();holder.close()
        self.assertEqual(3,len(list(folder.glob('*.json'))))
        self.assertFalse((folder.parent/'.workday-references-dead').exists())
        ledger._flush_references(self.store);self.assertEqual(3,self.count())
    def test_r4_workday_record_returns_the_specific_reason(self):
        # v1.6.12 第四轮 DSH 缺陷 1：参数不合法时，CLI 输出里带具体原因，仍然返回 0。
        import io,contextlib
        from xiaoba_agentos import cli
        product=self.root/'product.md';product.write_text('TEST product')
        cfg=SimpleNamespace(memory={'obsidian_vault':str(self.root/'assistant/vault')},runtime_dir=self.root/'runtime')
        out=io.StringIO()
        with patch('xiaoba_agentos.cli._load',return_value=(cfg,self.store)),contextlib.redirect_stdout(out):
            code=cli.main(['workday-record',str(product),'--node','TEST','--agent','TEST','--model','TEST','--kind','不存在的类型','--status','done'])
        self.assertEqual(0,code)
        result=json.loads(out.getvalue())
        self.assertEqual('registration_failed',result['warning_code'])
        self.assertEqual('invalid metadata',result['reason'])
    def test_D2_poison_reference_quarantined_after_three_failures(self):
        folder=self.queue(0);bad=folder/'bad.json';bad.write_text('TEST invalid JSON')
        for _ in range(3):ledger._flush_references(self.store)
        self.assertFalse(bad.exists());self.assertTrue((folder.parent/'.workday-references-dead/bad.json').is_file())
        self.assertEqual('reference_dead',ledger.workday_status(self.store)['warning_code'])
    def test_D3_unconfigured_does_not_enqueue_or_mark_dirty_warns_once(self):
        self.store.workday_root=None;p=self.root/'product';p.write_text('TEST')
        marker=self.store.path.parent/'.workday-pending';before=marker.read_bytes() if marker.exists() else None
        with patch('logging.Logger.warning') as warn:
            for _ in range(3):
                value=ledger.record_reference(self.store,p,node='TEST',task_id=None,agent='TEST',model='TEST',kind='产出',status='done')
                self.assertEqual('unconfigured',value['warning_code'])
        self.assertEqual(1,warn.call_count);self.assertFalse((marker.parent/'.workday-references').exists());self.assertEqual(before,marker.read_bytes() if marker.exists() else None)
    def test_D2_scale_2000_queued_5000_history(self):
        with self.store.connect() as db:
            for n in range(5000):self.store._append_event_tx(db,None,'TEST.history',{'n':n})
        folder=self.queue(2000);started=time.monotonic();rounds=0
        while list(folder.glob('*.json')):
            self.assertIn('directory',ledger.rebuild_view(self.store));rounds+=1
            self.assertLessEqual(rounds,4)
        elapsed=time.monotonic()-started
        self.assertEqual(4,rounds);self.assertEqual(2000,self.count());self.assertLess(elapsed,15)
    def test_D5_documented_full_snapshot_cost(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn('成本随事件总量增长',(root/'docs/RELEASE_V1.6.12.md').read_text())
    def test_D6_readme_floor(self):
        root=Path(__file__).resolve().parents[1]
        self.assertIn('Python 3.11+',(root.parent/'review-ingress/README.md').read_text())
    def test_D8_switch_document(self):
        root=Path(__file__).resolve().parents[1]
        text=(root/'docs/RELEASE_SWITCH.md').read_text()
        for value in ('随版同步的监看中心文件','monitor_core/modules/agent_pool.py','post-switch','哈希'):self.assertIn(value,text)
