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
        # 发送前只读窗格元数据：前台程序和工作目录由这个文件给出，默认就是节点
        # 登记的 agy + 登记目录，所以正常路径照旧发送。
        self.pane = self.root/'pane'
        self.pane.write_text('agy\t'+str(self.root))
        self.fake.write_text('#!'+sys.executable+'\n'+
            'import json,sys\nfrom pathlib import Path\n'+
            'p=Path('+repr(str(self.log))+')\n'+
            "with p.open('a') as f:f.write(json.dumps(sys.argv[1:])+\"\\n\")\n"+
            'mode=Path('+repr(str(self.mode))+').read_text()\n'+
            'if sys.argv[1]=="display-message":\n'+
            '    if mode=="pane-fail":sys.exit(1)\n'+
            '    sys.stdout.write(Path('+repr(str(self.pane))+').read_text())\n'+
            '    sys.exit(0)\n'+
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
        # 发送前只读窗格元数据：display-message 展开格式串，不打印屏幕内容。
        self.assertEqual(calls[1],['display-message','-p','-t','=TEST_session-1:',
                                   '#{pane_current_command}\t#{pane_current_path}'])
        self.assertEqual(calls[2][:4],['send-keys','-t','=TEST_session-1:','-l'])
        self.assertEqual(calls[2][4],'请读取并执行这张任务卡：'+json.dumps(result['task_card'],ensure_ascii=False)+'；按卡内格式写回执。')
        self.assertEqual(calls[3],['send-keys','-t','=TEST_session-1:','Enter'])
        self.assertEqual(len(calls),4)
        self.assertFalse(any('capture-pane' in call for call in calls))

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
        self.assertEqual(len(self.calls()),4)
        with self.assertRaises(Conflict):self.submit(instruction='不同工作')

    def test_receipt_settles_once_after_restart_without_send(self):
        self.submit();self.receipt()
        restarted = AgentPool(self.store,BackendRegistry(self.store))
        restarted.tmux.collect();restarted.tmux.collect()
        self.assertEqual(self.job()['status'],'completed')
        self.assertEqual(self.job()['result']['body'],'隔离夹具执行完成。')
        workspace = restarted.tmux.workspace(self.source)
        self.assertEqual(workspace.verify()[1],2)
        self.assertEqual(len(self.calls()),4)
        self.assertFalse(self.job()['execution_evidence']['context_isolated'])
        self.assertNotIn('model_verification',self.job()['execution_evidence'])
        self.assertEqual('node_configuration',self.job()['execution_evidence']['model_source'])

    def test_cancel_ignores_late_receipt_and_does_not_type(self):
        one = self.submit()
        self.pool.tmux.cancel('TEST-tmux',one['job_id'])
        self.receipt();self.pool.tmux.collect()
        self.assertEqual(self.job()['status'],'cancelled')
        self.assertEqual(self.submit()['status'],'cancelled')
        self.assertEqual(len(self.calls()),4)

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
        self.assertEqual(len(self.calls()),4)

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
        self.assertEqual(len(self.calls()),3)
        self.assertEqual(1,sum(1 for call in self.calls() if call[0]=='send-keys'))
        self.assertNotIn('sent_at',self.job())

    def test_sending_record_after_crash_is_never_replayed(self):
        with patch.object(self.pool.tmux,'_command',side_effect=[None,KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):self.submit()
        self.assertEqual(self.job()['status'],'sending')
        self.assertEqual(self.submit()['status'],'sending')
        self.pool.tmux.collect()
        # has-session 被打桩，窗格元数据是只读的：崩在 send-keys 之前，
        # 所以一个字都没有被打进会话。
        self.assertEqual([call[0] for call in self.calls()],['display-message'])

    def test_a_shell_in_the_foreground_blocks_the_send(self):
        # 卡 26：任务卡被打进前台是 zsh 的窗格，卡片文字被当成命令执行。
        # 前台是 agy 且目录相符时照旧发送，见 test_literal_argv_and_no_screen_or_confirmation。
        for command in ('zsh','bash','sh','fish','dash','tcsh','ksh','login','-zsh'):
            with self.subTest(command=command):
                self.pane.write_text(command+'\t'+str(self.root))
                with self.assertRaises(ValueError) as caught:
                    self.submit(task='TEST-task-'+command.strip('-'))
                self.assertIn('前台是shell',str(caught.exception))
                self.assertIn(command,str(caught.exception))
        self.assertFalse(any(call[0]=='send-keys' for call in self.calls()))
        with self.store.native.read_only() as db:self.assertEqual(self.pool.tmux.jobs(db),{})
        self.assertFalse(any(self.root.glob('.xiaoba-tmux-*')))

    def test_a_pane_directory_that_is_not_the_registered_one_blocks_the_send(self):
        other = self.root/'elsewhere'
        other.mkdir()
        self.pane.write_text('agy\t'+str(other))
        with self.assertRaises(ValueError) as caught:self.submit()
        self.assertIn('工作目录',str(caught.exception))
        self.assertIn(str(other),str(caught.exception))
        self.assertIn(str(self.root),str(caught.exception))
        self.assertFalse(any(call[0]=='send-keys' for call in self.calls()))
        with self.store.native.read_only() as db:self.assertEqual(self.pool.tmux.jobs(db),{})
        self.assertFalse(any(self.root.glob('.xiaoba-tmux-*')))

    def test_unreadable_pane_metadata_blocks_the_send(self):
        for content in ('agy','','agy\t','\t'+str(self.root),'没有制表符'):
            with self.subTest(content=content):
                self.pane.write_text(content)
                with self.assertRaises(ValueError) as caught:
                    self.submit(task='TEST-meta-'+str(len(content)))
                self.assertIn('窗格元数据不完整',str(caught.exception))
        self.mode.write_text('pane-fail')
        with self.assertRaises(ValueError) as caught:self.submit(task='TEST-meta-fail')
        self.assertIn('窗格元数据读取失败',str(caught.exception))
        self.assertFalse(any(call[0]=='send-keys' for call in self.calls()))
        with self.store.native.read_only() as db:self.assertEqual(self.pool.tmux.jobs(db),{})

    def test_a_configured_tmux_path_replaces_the_service_path_lookup(self):
        # 服务进程的 PATH 里可能没有 tmux；受信宿主配置指定后必须生效，空值恢复查找。
        from xiaoba_review import resource_tmux
        self.addCleanup(resource_tmux.configure_executable,None)
        channel = TmuxChannel(self.store)
        with patch('shutil.which',return_value=None):
            with self.assertRaises(ValueError) as caught:channel._command('has-session','-t','=x')
            self.assertIn('本机未安装tmux',str(caught.exception))
            self.assertEqual(str(self.fake),resource_tmux.configure_executable(str(self.fake)))
            channel._command('has-session','-t','=x')
        self.assertEqual([['has-session','-t','=x']],self.calls())
        for bad in ('relative/tmux','/missing/tmux',str(self.mode),'x\ny',5,None):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                resource_tmux.validate_executable(bad)
        self.assertIsNone(resource_tmux.configure_executable(''))
        self.assertEqual([['has-session','-t','=x']],self.calls())

    def test_the_development_entry_refuses_a_tmux_path_it_cannot_use(self):
        # run.py --tmux-path 和监看配置走同一个校验；不可用的路径必须在建运行目录、
        # 起服务之前就停下，不留任何副作用。
        import subprocess
        entry = Path(__file__).resolve().parents[1]/'run.py'
        runtime = self.root/'run-runtime'
        done = subprocess.run([sys.executable,'-B',str(entry),'--tmux-path','/missing/tmux',
                               '--runtime',str(runtime),'--port','0'],
                              capture_output=True,text=True,timeout=120,cwd=str(entry.parent))
        self.assertEqual(2,done.returncode,done.stderr)
        self.assertIn('tmux路径必须是已存在的可执行文件',done.stderr)
        self.assertFalse(runtime.exists())

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
        self.assertEqual(len(self.calls()),4)

    def test_real_tmux_reports_a_chinese_directory_without_a_utf8_locale(self):
        # 假 tmux 测不到：没有 UTF-8 环境时真 tmux 把制表符和中文都打印成 “_”。
        real = '/Users/suzhizhou/Documents/私人助理/配置/bin/tmux'
        if not Path(real).exists():self.skipTest('tmux is not installed here')
        import os, subprocess, tempfile
        with tempfile.TemporaryDirectory(dir='/tmp') as temp:
            directory = (Path(temp)/'中文目录').resolve();directory.mkdir()
            env = {'PATH':'/usr/bin:/bin','TMUX_TMPDIR':temp,'HOME':temp}
            with patch.dict(os.environ, env, clear=True):
                subprocess.run([real,'-f','/dev/null','new-session','-d','-s','t','-c',str(directory),'cat'],check=True,env=dict(env,LC_ALL='en_US.UTF-8'))
                try:
                    time.sleep(.3)
                    command, path = TmuxChannel(self.store, real)._pane('t')
                    self.assertEqual('cat', command)
                    self.assertEqual(directory, Path(path).resolve())
                finally:
                    subprocess.run([real,'kill-server'],env=env)

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
