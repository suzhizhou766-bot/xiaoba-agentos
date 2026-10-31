"""Offline attended-channel tests: real fake-tmux process, isolated files/SQLite."""
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool, independence, resource_plan
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_tmux import TmuxChannel, validate_session
from xiaoba_review.store import Conflict


class TmuxTests(fixtures.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        self.pool = AgentPool(self.store, BackendRegistry(self.store))
        self.log = self.root/'argv.jsonl'
        self.mode = self.root/'mode'
        self.mode.write_text('ok')
        self.fake = self.root/'fake-tmux'
        self.fake.write_text('#!'+sys.executable+'\n'+
            'import json,sys\nfrom pathlib import Path\n'+
            'p=Path('+repr(str(self.log))+')\n'+
            "with p.open('a') as f:f.write(json.dumps(sys.argv[1:])+\"\\n\")\n"+
            'mode=Path('+repr(str(self.mode))+').read_text()\n'+
            "sys.exit(1 if (mode=='missing' and sys.argv[1]=='has-session') or (mode=='send-fail' and sys.argv[1]=='send-keys') else 0)\n")
        self.fake.chmod(0o700)
        self.pool.tmux = TmuxChannel(self.store, str(self.fake))
        self.value = {'node_id':'TEST-tmux','name':'TEST attended','agent':'TEST CLI',
            'kind':'collaborator','interface':'tmux','entry':'/TEST/bin/agent',
            'model':'TEST-model','tmux_session':'TEST_session-1','working_directory':str(self.root)}
        source = self.pool.add(self.value)
        self.source = self.pool.change(source['reviewer_id'],1,'toggle',True)

    def submit(self, task='TEST-task', **changes):
        args = dict(node_id='TEST-tmux',expected_revision=2,task_id=task,
                    step_code='step1',instruction='只处理夹具材料并写文件回执。',materials={'fixture':True})
        args.update(changes)
        return self.pool.runtime.execute_work(**args)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def job(self):
        with self.store.native.read_only() as db:
            return next(iter(self.pool.tmux.jobs(db).values()))

    def receipt(self, body='隔离夹具执行完成。', status='completed'):
        ticket = self.job()['ticket']
        Path(ticket['log']).write_text(ticket['header']+'\n'+ticket['marker']+
            '\n任务ID: '+ticket['task_id']+'\n步骤码: '+ticket['step_code']+'\n状态: '+status+
            '\n---\n'+body+'\n'+ticket['done']+'\n')

    def test_literal_argv_and_no_screen_or_confirmation(self):
        result = self.submit()
        self.assertEqual(result['status'],'waiting_user_agent')
        calls = self.calls()
        self.assertEqual(calls[0],['has-session','-t','=TEST_session-1'])
        self.assertEqual(calls[1][:4],['send-keys','-t','=TEST_session-1:','-l'])
        self.assertEqual(calls[1][4],'请读取并执行这张任务卡：'+json.dumps(result['task_card'],ensure_ascii=False)+'；按卡内格式写回执。')
        self.assertEqual(calls[2],['send-keys','-t','=TEST_session-1:','Enter'])
        self.assertEqual(len(calls),3)

    def test_reject_quote_semicolon_and_target_syntax(self):
        for value in ["bad'name",'bad"name','bad;name','-x','x:y','x.y','x\ny',' x','x y','x'*65]:
            with self.subTest(value=value), self.assertRaises(ValueError):validate_session(value)
        self.assertEqual(self.calls(),[])

    def test_missing_session_never_records_sent_or_creates_card(self):
        self.mode.write_text('missing')
        with self.assertRaises(ValueError):self.submit()
        with self.store.native.read_only() as db:self.assertEqual(self.pool.tmux.jobs(db),{})
        self.assertEqual(len(self.calls()),1)
        self.assertFalse(any(self.root.glob('.xiaoba-tmux-*')))

    def test_wait_survives_300_seconds_and_duplicate_never_resends(self):
        one = self.submit()
        with patch('time.time',return_value=time.time()+3600):
            for _ in range(3):self.pool.tmux.collect()
        self.assertEqual(self.job()['status'],'waiting_user_agent')
        self.assertEqual(self.submit()['job_id'],one['job_id'])
        self.assertEqual(len(self.calls()),3)
        with self.assertRaises(Conflict):self.submit(instruction='不同工作')

    def test_receipt_settles_once_after_restart_without_send(self):
        self.submit();self.receipt()
        restarted = AgentPool(self.store,BackendRegistry(self.store))
        restarted.tmux.collect();restarted.tmux.collect()
        self.assertEqual(self.job()['status'],'completed')
        self.assertEqual(self.job()['result']['body'],'隔离夹具执行完成。')
        workspace = restarted.tmux.workspace(self.source)
        self.assertEqual(workspace.verify()[1],2)
        self.assertEqual(len(self.calls()),3)
        self.assertFalse(self.job()['execution_evidence']['context_isolated'])

    def test_cancel_ignores_late_receipt_and_does_not_type(self):
        one = self.submit()
        self.pool.tmux.cancel('TEST-tmux',one['job_id'])
        self.receipt();self.pool.tmux.collect()
        self.assertEqual(self.job()['status'],'cancelled')
        self.assertEqual(self.submit()['status'],'cancelled')
        self.assertEqual(len(self.calls()),3)

    def test_invalid_receipt_waits_shows_reason_and_can_be_corrected(self):
        self.submit();self.receipt(status='unknown')
        self.pool.tmux.collect()
        job = self.job()
        self.assertEqual(job['status'],'waiting_user_agent')
        self.assertIn('状态',job['receipt_error'])
        self.assertTrue(Path(job['ticket']['log']).is_file())
        def events():
            with self.store.native.read_only() as db:
                return db.execute('SELECT count(*) FROM events').fetchone()[0]
        before = events()
        self.pool.tmux.collect()
        self.assertEqual(events(),before)
        self.receipt()
        self.pool.tmux.collect()
        self.assertEqual(self.job()['status'],'completed')
        self.assertNotIn('receipt_error',self.job())
        self.assertEqual(len(self.calls()),3)

    def test_collect_without_waiting_job_takes_no_write_lock(self):
        with patch.object(self.store,'transaction',side_effect=AssertionError('write lock')):
            self.pool.tmux.collect()
        self.assertEqual(self.calls(),[])

    def test_collect_failure_does_not_block_review_delivery(self):
        from xiaoba_review.monitor import OpenReviewModule
        import threading
        module = object.__new__(OpenReviewModule)
        delivered = threading.Event()
        class Pool:
            class tmux:
                @staticmethod
                def collect():raise RuntimeError('boom')
        class Store:
            def state(self):pass
            def deliver(self,inbox):delivered.set()
        class Log:
            def log(self,*a,**k):pass
        module.pool=Pool;module.store=Store();module.inbox=None;module.backends=self.pool.backends
        module.stopping=threading.Event();module.thread=None;module.ctx=Log()
        module.start()
        try:self.assertTrue(delivered.wait(5))
        finally:module.stop()

    def test_partial_receipt_is_waiting_not_error(self):
        self.submit();self.receipt()
        path = Path(self.job()['ticket']['log'])
        path.write_text(path.read_text().replace(self.job()['ticket']['done'],''))
        self.pool.tmux.collect()
        self.assertEqual(self.job()['status'],'waiting_user_agent')

    def test_send_failure_is_uncertain_and_never_retried(self):
        self.mode.write_text('send-fail')
        with self.assertRaises(ValueError):self.submit()
        self.assertEqual(self.submit()['status'],'send_uncertain')
        self.pool.tmux.collect()
        self.assertEqual(len(self.calls()),2)
        self.assertNotIn('sent_at',self.job())

    def test_sending_record_after_crash_is_never_replayed(self):
        with patch.object(self.pool.tmux,'_command',side_effect=[None,KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):self.submit()
        self.assertEqual(self.job()['status'],'sending')
        self.assertEqual(self.submit()['status'],'sending')
        self.pool.tmux.collect()
        self.assertEqual(self.calls(),[])

    def test_attended_node_rejected_by_every_review_gate(self):
        with self.assertRaises(ValueError):resource_plan(self.source)
        with self.assertRaises(ValueError):independence(self.source)
        with self.assertRaises(ValueError):
            self.store.create(self.candidate['task_id'],'fixture-dsh','TEST-tmux',
                'TEST review',[{'ref':'TEST evidence','sha256':'a'*64}])
        self.assertNotEqual(self.pool.runtime.prepare(['peer'])['route'],'run')
        with self.assertRaises(ValueError):self.pool.runtime.test_run('TEST-tmux',2,'TEST-probe')
        self.assertEqual(self.calls(),[])

    def test_role_registration_and_directory_are_enforced(self):
        with self.assertRaises(ValueError):self.pool.add(dict(self.value,node_id='TEST-other',kind='peer'))
        with self.assertRaises(ValueError):self.pool.add(dict(self.value,node_id='TEST-other',working_directory='relative'))
        with self.assertRaises(Conflict):self.pool.add(dict(self.value,tmux_session='different'))

    def test_busy_session_blocks_other_task_and_disabled_blocks_new_send(self):
        self.submit()
        with self.assertRaises(Conflict):self.submit(task='TEST-task2')
        self.pool.change('TEST-tmux',2,'toggle',False)
        with self.assertRaises(ValueError):self.submit(task='TEST-task2',expected_revision=3)
        self.assertEqual(len(self.calls()),3)

    def test_state_lists_wait_and_api_route_is_guarded(self):
        self.submit()
        node = next(n for n in self.pool.state()['nodes'] if n['node_id']=='TEST-tmux')
        self.assertFalse(node['context_isolated'])
        self.assertEqual(node['tmux_jobs'][0]['message'],'等待用户/Agent')
        from xiaoba_review.monitor import OpenReviewModule
        module = object.__new__(OpenReviewModule)
        route = next(r for r in module.routes() if r.path=='/api/agent-pool/tmux-cancel')
        self.assertEqual(route.method,'POST')
        # Route follows the same write gate as work/test, no bypass endpoint.
        self.assertEqual(route.guard,'write')

    def test_collector_runs_in_monitor_even_without_open_page(self):
        self.submit();self.receipt()
        from xiaoba_review.monitor import OpenReviewModule
        module = object.__new__(OpenReviewModule)
        module.pool=self.pool;module.store=self.store;module.inbox=self.inbox
        module.backends=self.pool.backends
        import threading
        module.stopping=threading.Event();module.thread=None
        class Log:
            def log(self,*a,**k):pass
        module.ctx=Log()
        module.start()
        try:
            deadline=time.monotonic()+5
            while self.job()['status']=='waiting_user_agent' and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertEqual(self.job()['status'],'completed')
        finally:module.stop()
