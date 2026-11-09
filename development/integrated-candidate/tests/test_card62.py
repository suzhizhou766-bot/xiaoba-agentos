"""Card62: isolated SQLite view and nonblocking regression cases."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_card51_scoring as scoring
import test_card52b_formal_review as formal_fixture
from xiaoba_agentos.store import EventStore


class WorkdayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = EventStore(self.root/'runtime/db.sqlite'); self.store.initialize()
        self.configure(self.store)

    def configure(self, store):
        from types import SimpleNamespace
        from xiaoba_agentos.workday_ledger import configure
        configure(store,SimpleNamespace(memory={'obsidian_vault':str(self.root/'assistant/vault')}))

    def ledger(self):
        from xiaoba_agentos.workday_ledger import WorkdayLedger
        return WorkdayLedger(self.store, self.root/'assistant')

    def test_A1_rebuild_is_deterministic_and_only_references_products(self):
        ledger = self.ledger()
        product = self.root/'product.md'; product.write_text('TEST original')
        task = self.store.submit('TEST work', 2)
        with patch('xiaoba_agentos.workday_ledger.time.time',return_value=1794182400):
            ledger.record(product, node='TEST node', task_id=task, agent='TEST agent', model='TEST model', kind='回执', status='pending')
        first = ledger.rebuild('2026-11-09')
        folder = Path(first['directory'])
        before = {str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        ledger.rebuild('2026-11-09')
        self.assertEqual(before, {str(p.relative_to(folder)):p.read_bytes() for p in folder.rglob('*') if p.is_file()})
        self.assertEqual('TEST original', product.read_text())
        self.assertNotIn(b'TEST original', b''.join(before.values()))
        rows = [json.loads(v) for v in (folder/'_索引.jsonl').read_text().splitlines()]
        self.assertEqual(hashlib.sha256(product.read_bytes()).hexdigest(), rows[0]['sha256'])
        self.assertEqual('TEST model', rows[0]['model'])

    def test_A2_cleared_ledger_is_rebuildable_from_database(self):
        ledger=self.ledger(); p=self.root/'p';p.write_text('TEST')
        with patch('xiaoba_agentos.workday_ledger.time.time',return_value=1794182400):
            ledger.record(p,node='TEST',task_id=None,agent='TEST',model='TEST',kind='新经验',status='done')
        day=ledger.rebuild('2026-11-09'); folder=Path(day['directory'])
        # Model user clearing by moving the disposable view, never its records.
        folder.rename(self.root/'cleared-view')
        ledger.rebuild('2026-11-10')
        index=(self.root/'assistant/工具/每日台账索引总目录/总目录.md').read_text()
        self.assertIn('已清理、可重新生成',index)
        ledger.rebuild('2026-11-09')
        self.assertTrue((folder/'_索引.jsonl').read_text().strip())

    def test_A3_first_output_marks_pending_read_generates_and_session_checks_are_files(self):
        ledger=self.ledger();p=self.root/'p';p.write_text('TEST')
        ledger.record(p,node='TEST',task_id=None,agent='TEST',model='TEST',kind='回执',status='done')
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        folder=self.root/'assistant/工作区'/(day+'-小八台账')
        self.assertFalse((folder/'台账.md').exists())
        from xiaoba_agentos.workday_ledger import read_workday
        self.assertIn('directory',read_workday(self.store,day=day))
        self.assertTrue((folder/'台账.md').exists())
        ledger.rebuild(day,checks={'model_tasks_sent':0,'nodes':[]})
        self.assertEqual(0,json.loads((folder/'本地检测报告.json').read_text())['model_tasks_sent'])

    def test_A4_continuation_uses_latest_database_day_and_task_id(self):
        ledger=self.ledger()
        p=self.root/'p';p.write_text('TEST')
        with patch('xiaoba_agentos.workday_ledger.time.time',return_value=1793923200):
            task=self.store.submit('TEST continuation',2)
            ledger.record(p,node='TEST',task_id=task,agent='TEST',model='TEST',kind='回执',status='pending')
        value=ledger.rebuild('2026-11-09');folder=Path(value['directory'])
        self.assertIn(task,(folder/'接续清单.md').read_text())
        self.store.complete_manually(task,'TEST completed')
        ledger.rebuild('2026-11-09')
        self.assertNotIn(task,(folder/'接续清单.md').read_text())

    def test_A5_concurrent_hosts_keep_all_records_and_no_schema_change(self):
        from concurrent.futures import ThreadPoolExecutor
        with self.store.read_only() as db:
            schema=list(db.execute('SELECT sql FROM sqlite_master ORDER BY name'));version=db.execute('PRAGMA user_version').fetchone()[0]
        files=[]
        for n in range(8):
            p=self.root/str(n);p.write_text('TEST '+str(n));files.append(p)
        def write(p):
            other=EventStore(self.store.path)
            self.configure(other)
            from xiaoba_agentos.workday_ledger import WorkdayLedger
            return WorkdayLedger(other,self.root/'assistant').record(p,node='TEST',task_id=None,agent='TEST',model='TEST',kind='回执',status='done')
        with ThreadPoolExecutor(max_workers=4) as hosts:list(hosts.map(write,files))
        value=self.ledger().rebuild()
        rows=[json.loads(v) for v in Path(value['directory'],'_索引.jsonl').read_text().splitlines()]
        self.assertEqual(8,len(rows));self.assertEqual(8,len({r['record_id'] for r in rows}))
        with self.store.read_only() as db:
            self.assertEqual([tuple(r) for r in schema],[tuple(r) for r in db.execute('SELECT sql FROM sqlite_master ORDER BY name')])
            self.assertEqual(version,db.execute('PRAGMA user_version').fetchone()[0])
            self.assertTrue(self.store.verify_event_chain_tx(db)['ok'])

    def test_A6_bound_native_candidate_triggers_without_session(self):
        from xiaoba_agentos.workday_ledger import bind_root
        bind_root(self.store,self.root/'assistant')
        task=self.store.submit('TEST candidate',2);p=self.root/'candidate';p.write_text('TEST candidate')
        self.store.complete_with_memory_candidate(task,'TEST candidate',str(p),hashlib.sha256(p.read_bytes()).hexdigest(),[],expected_attempt=0)
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        folder=self.root/'assistant/工作区'/(day+'-小八台账')
        from xiaoba_agentos.workday_ledger import read_workday
        self.assertIn('directory',read_workday(self.store,day=day))
        rows=[json.loads(v) for v in (folder/'_索引.jsonl').read_text().splitlines()]
        self.assertEqual(task,rows[0]['task_id']);self.assertEqual('新经验',rows[0]['kind'])

    def test_A7_reuse_reference_binds_original_file_hash_in_sqlite(self):
        source=self.store.submit('TEST source',2);task=self.store.submit('TEST actual use',2)
        p=self.root/'formal.md';p.write_text('TEST published bytes')
        self.store.record_memory_reuse(task,source,str(p))
        expected=hashlib.sha256(p.read_bytes()).hexdigest()
        p.write_text('TEST subsequent edit must not alter recorded hash')
        value=self.ledger().rebuild()
        rows=[json.loads(v) for v in Path(value['directory'],'_索引.jsonl').read_text().splitlines()]
        self.assertEqual(expected,next(v['sha256'] for v in rows if v['kind']=='复用经验'))

    def test_A8_native_execution_receipt_is_projected_from_sqlite(self):
        task=self.store.submit('TEST execution',2);run=self.store.start_agent_run(task,'TEST worker')
        p=self.store.path.parent/'receipts'/task/(run+'.json');p.parent.mkdir(parents=True);p.write_text('{"TEST":"receipt"}')
        expected=hashlib.sha256(p.read_bytes()).hexdigest()
        self.store.finish_agent_run(run,'completed','TEST body',None,1)
        p.write_text('TEST later alteration')
        value=self.ledger().rebuild()
        rows=[json.loads(v) for v in Path(value['directory'],'_索引.jsonl').read_text().splitlines()]
        row=next(v for v in rows if v['path']==str(p))
        self.assertEqual(expected,row['sha256']);self.assertEqual('TEST worker',row['node'])

    def test_A9_new_revision_after_reverting_bytes_is_not_deduplicated(self):
        ledger=self.ledger();p=self.root/'p'
        for body in ('TEST A','TEST B','TEST B','TEST A'):
            p.write_text(body);ledger.record(p,node='TEST',task_id=None,agent='TEST',model='TEST',kind='产出',status='done')
        self.assertIn('directory',ledger.rebuild())
        with self.store.read_only() as db:
            self.assertEqual(3,db.execute("SELECT COUNT(*) FROM events WHERE event_type='workday.output_recorded'").fetchone()[0])

    def test_A10_external_task_ids_continue_without_native_task_rows(self):
        ledger=self.ledger();p=self.root/'external';p.write_text('TEST')
        with patch('xiaoba_agentos.workday_ledger.time.time',return_value=1793923200):
            ledger.record(p,node='TEST',task_id='TEST-external',agent='TEST',model='TEST',kind='回执',status='pending')
        value=ledger.rebuild('2026-11-09')
        self.assertIn('TEST-external',Path(value['directory'],'接续清单.md').read_text())
        ledger.record(p,node='TEST',task_id='TEST-external',agent='TEST',model='TEST',kind='回执',status='completed')
        value=ledger.rebuild('2026-11-09')
        self.assertNotIn('TEST-external',Path(value['directory'],'接续清单.md').read_text())


class SmallFixTests(unittest.TestCase):
    setUp=scoring.Card51ScoringTests.setUp

    def test_fixture_vault_cannot_point_at_real_workday_root(self):
        self.assertEqual(self.root.resolve(),Path(self.config.memory['obsidian_vault']).resolve().parent)

    def test_B4_admitted_hash_is_stored_result(self):
        task=self.store.submit('TEST admitted',2)
        p=self.root/'candidate';p.write_text('TEST original')
        original=self.store._insert_candidate_tx
        def insert(db,*args,**kw):
            result=original(db,*args,**kw)
            db.execute('UPDATE tasks SET result=? WHERE id=?',('TEST normalized',task))
            return result
        with patch.object(self.store,'_insert_candidate_tx',side_effect=insert):
            self.store.complete_with_memory_candidate(task,'TEST original',str(p),hashlib.sha256(p.read_bytes()).hexdigest(),[],expected_attempt=0)
        with self.store.read_only() as db:
            value=json.loads(db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='task.completed'",(task,)).fetchone()[0])
        self.assertEqual(hashlib.sha256(b'TEST normalized').hexdigest(),value['result_sha256'])

    def test_B7_list_and_count_share_filter(self):
        with patch.object(self.store,'_pending_notifications_tx',return_value=[]) as common:
            self.assertEqual([],self.store.pending_notifications())
            self.assertEqual(0,self.store.summary()['pending_notifications'])
            self.assertEqual(2,common.call_count)

    def test_B11_reason_changes_append_but_identical_reason_does_not(self):
        task=self.store.submit('TEST reasons',2);p=self.root/'candidate';p.write_text('TEST body')
        def reason(db,tid,reason_out):
            reason_out.append(current[0]);return None
        current=['host_draft']
        with patch('xiaoba_agentos.value_scoring.native_value',side_effect=reason):
            for value in ('host_draft','host_draft','no_review_receipts','no_review_receipts','host_draft'):
                current[0]=value
                with self.store.connect() as db:self.store._insert_candidate_tx(db,task,str(p),'a'*64,[],1)
        with self.store.read_only() as db:
            events=[json.loads(v[0]) for v in db.execute("SELECT payload_json FROM events WHERE event_type='memory.value_unscored' ORDER BY sequence")]
        self.assertEqual(['host_draft','no_review_receipts','host_draft'],[v['reason'] for v in events])
        self.assertEqual('host_draft',events[1]['previous_reason'])


class DispatchTests(unittest.TestCase):
    for _name in ("setUp", "file", "candidate", "application", "delivery", "event_count", "formal", "app", "deliver", "events", "request", "items"):
        locals()[_name] = getattr(formal_fixture.FormalReviewTests, _name)
    # Keep only this card's case; reuse setup/helpers without inheriting cases.
    def test_B7_dispatch_events_preserve_open_page_token(self):
        _,rid=self.request();before=self.items()[rid]['expected']
        self.store.append_event(None,'memory.formal_dispatch_attempt',{'request_id':rid})
        self.assertEqual(before,self.items()[rid]['expected'])
        item=self.items()[rid]
        self.store.append_event(None,'memory.formal_dispatch_status',{'request_id':rid,'draft_sha256':item['evidence_sha256'],'dispatch_status':'retry_pending'})
        self.assertEqual(before,self.items()[rid]['expected'])


if __name__=='__main__':
    unittest.main()
