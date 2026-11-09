"""Card64 side-channel regressions; all databases and views are temporary."""
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from xiaoba_agentos.store import EventStore
from xiaoba_agentos import workday_ledger as ledger


class LedgerSideChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.store=EventStore(self.root/'runtime/db.sqlite');self.store.initialize()
        self.cfg=SimpleNamespace(memory={'obsidian_vault':str(self.root/'assistant/vault')},runtime_dir=self.root/'runtime')
        ledger.configure(self.store,self.cfg)

    def test_P1_committed_task_survives_every_refresh_exception(self):
        for error in (sqlite3.OperationalError('TEST'),ValueError('TEST'),RuntimeError('TEST')):
            with self.subTest(error=type(error).__name__):
                task=self.store.submit('TEST committed task',1)
                with patch.object(ledger,'refresh_after_write',side_effect=error):
                    self.store.complete_manually(task,'TEST completed')
                self.assertEqual('completed',self.store.get_task(task)['status'])

    def test_P1_registration_and_sensitive_metadata_are_only_warnings(self):
        p=self.root/'receipt';p.write_text('TEST receipt')
        args=dict(node='TEST',task_id=None,agent='TEST',model='TEST',kind='回执',status='completed')
        for error in (sqlite3.OperationalError('TEST'),ValueError('TEST'),RuntimeError('TEST')):
            with self.subTest(error=type(error).__name__),patch.object(ledger,'_register_reference',side_effect=error):
                ledger.record_reference(self.store,p,**args)
        with patch('xiaoba_agentos.memory.MemoryBridge._sensitive_reason',return_value='TEST classified metadata'):
            ledger.record_reference(self.store,p,**args)
        self.assertFalse(ledger.workday_status(self.store)['ok'])

    def test_P1_view_only_file_hash_failure_never_rolls_back_business_writes(self):
        # 卡 64 复查（Qoder 第四节）：台账专用的原文件哈希读不到时，业务事务照常提交，只是省略该字段。
        task=self.store.submit('TEST run',1)
        run=self.store.start_agent_run(task,'TEST-agent')
        receipt=self.store.path.parent/'receipts'/task/(run+'.json')
        receipt.parent.mkdir(parents=True,exist_ok=True);receipt.write_text('{}')
        source=self.root/'formal.md';source.write_text('TEST formal')
        original=Path.read_bytes
        def unreadable(path,*a,**k):
            if path in (receipt,source):raise PermissionError('TEST unreadable')
            return original(path,*a,**k)
        with patch.object(Path,'read_bytes',unreadable):
            self.store.finish_agent_run(run,'completed','TEST output',None,1)
            self.store.record_memory_reuse(task,'TEST-source-task',str(source))
        with self.store.connect() as db:
            status=db.execute('SELECT status FROM agent_runs WHERE id=?',(run,)).fetchone()[0]
            finished=json.loads(db.execute("SELECT payload_json FROM events WHERE event_type='agent.finished' AND task_id=?",(task,)).fetchone()[0])
            reused=json.loads(db.execute("SELECT payload_json FROM events WHERE event_type='memory.reused' AND task_id=?",(task,)).fetchone()[0])
        self.assertEqual('completed',status)
        self.assertNotIn('receipt_file_sha256',finished)
        self.assertIsNone(reused['source_sha256'])

    def test_review_obs2_relative_store_path_never_creates_directories(self):
        # 卡 64 复查观察 2：替身或相对路径的 store 不在工作目录下建诊断目录。
        import os
        fake=SimpleNamespace(path='MagicMock/mock.path')
        cwd=os.getcwd();os.chdir(self.root)
        try:
            ledger.warn(fake,'generation_failed')
            ledger.refresh_after_write(fake,['task.completed'])
        except Exception:
            pass
        finally:os.chdir(cwd)
        self.assertFalse((self.root/'MagicMock').exists())

    def test_review_obs1_doctor_tolerates_status_without_message(self):
        from xiaoba_agentos import health
        from test_pool_dispatch import _config
        root=self.root/'doctor';root.mkdir()
        config=_config(root,[])
        store=EventStore(config.database_path);store.initialize()
        for state in ({'ok':False},{'ok':True,'last_warning':{}}):
            with self.subTest(state=state),patch('xiaoba_agentos.workday_ledger.workday_status',return_value=state):
                result=health.doctor(config,store)
                self.assertEqual(state,result['workday'])
                self.assertIsInstance(result['warnings'],list)

    def test_P2_missing_config_never_uses_bound_root_as_a_default(self):
        ledger.bind_root(self.store,self.root/'legacy')
        self.store.workday_root=None
        self.assertIsNone(ledger.bound_ledger(self.store))
        for result in (ledger.rebuild_view(self.store),ledger.WorkdayLedger(self.store,self.root/'legacy').rebuild()):
            self.assertFalse(result['ok'])
            self.assertEqual('unconfigured',result['warning_code'])
            self.assertNotIn('directory',result)
        self.assertFalse((self.root/'legacy/工作区').exists())
        ledger.configure(self.store,self.cfg)
        self.assertIn('directory',ledger.rebuild_view(self.store))

    def test_P2_conflict_does_not_fail_session_or_normal_commands(self):
        from xiaoba_agentos.cli import main
        for first in ('ingress','core'):
            with self.subTest(first=first):
                store=EventStore(self.root/first/'db.sqlite');store.initialize()
                ledger.bind_root(store,self.root/first/'legacy').rebuild()
                ledger.configure(store,self.cfg)
                with patch('xiaoba_agentos.cli._load',return_value=(self.cfg,store)),patch('xiaoba_agentos.cli.doctor',return_value={'ok':True}),patch('xiaoba_agentos.cli.start_session_health',return_value={'ok':True,'session_mode':'ready'}):
                    out=io.StringIO()
                    with redirect_stdout(out):result=main(['session-start'])
                self.assertEqual(0,result)
                value=json.loads(out.getvalue())
                self.assertEqual('binding_conflict',value['workday']['warning_code'])
                self.assertIn('需要用户决定',ledger.workday_status(store)['message'])
                task=store.submit('TEST normal command remains possible',1);store.complete_manually(task,'TEST done')
                self.assertEqual('completed',store.get_task(task)['status'])
                ledger.configure(store,SimpleNamespace(memory={'obsidian_vault':str(self.root/first/'legacy/vault')}))
                self.assertIn('directory',ledger.rebuild_view(store))

    def test_P3_writes_mark_dirty_without_render_or_chain_scan(self):
        ledger.bind_root(self.store,self.root/'assistant')
        with patch.object(ledger.WorkdayLedger,'rebuild',side_effect=AssertionError('no sync render')) as render,patch.object(self.store,'verify_event_chain_tx',side_effect=AssertionError('no scan')):
            self.store.append_event(None,'memory.TEST_card64',{'TEST':True})
        render.assert_not_called()
        self.assertTrue(ledger.workday_status(self.store)['pending'])

    def test_P3_lock_timeout_is_bounded_and_visible(self):
        import fcntl
        view=ledger.WorkdayLedger(self.store,self.root/'assistant')
        view.catalog.mkdir(parents=True)
        with (view.catalog/'.workday.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            started=time.monotonic()
            value=view.rebuild(lock_timeout=.05)
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual('lock_timeout',value['warning_code'])

    def test_P3_5000_events_single_check_and_writer_not_blocked_by_render(self):
        with self.store.connect() as db:
            for n in range(5000):self.store._append_event_tx(db,None,'TEST.scale',{'n':n})
        view=ledger.WorkdayLedger(self.store,self.root/'assistant')
        entered=threading.Event();release=threading.Event();results=[]
        original_write=ledger._write
        def slow(path,text):
            if Path(path).name=='台账.md':entered.set();release.wait(3)
            return original_write(path,text)
        before=time.monotonic()
        with patch.object(ledger,'_write',side_effect=slow),patch.object(self.store,'verify_event_chain_tx',wraps=self.store.verify_event_chain_tx) as verify:
            thread=threading.Thread(target=lambda:results.append(view.rebuild()));thread.start()
            try:
                self.assertTrue(entered.wait(3))
                with sqlite3.connect(self.store.path,timeout=.1) as db:
                    self.store._append_event_tx(db,None,'TEST.concurrent_writer',{})
            finally:release.set();thread.join(4)
            self.assertEqual(1,verify.call_count)
        self.assertFalse(thread.is_alive());self.assertLess(time.monotonic()-before,5)
        self.assertIn('directory',results[0])

    def test_O1_missing_historical_hash_has_readable_label(self):
        source=self.store.submit('TEST source',1);task=self.store.submit('TEST reuse',1)
        self.store.record_memory_reuse(task,source,str(self.root/'missing-history.md'))
        value=ledger.WorkdayLedger(self.store,self.root/'assistant').rebuild()
        text=Path(value['directory'],'台账.md').read_text()
        self.assertNotIn('SHA256 None',text)
        self.assertIn('历史记录未保存文件哈希',text)


if __name__=='__main__':unittest.main()
