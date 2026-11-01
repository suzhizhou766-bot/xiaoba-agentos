"""Real child-process fixtures for every supported CLI; never call a model."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import textwrap
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool, independence
from xiaoba_review.agent_workspace import AgentWorkspace
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.cli_identity import agent_type, cli_environment, entry_warning, specific_failure
from xiaoba_review.resource_cli import CliWorklogResource
from xiaoba_review.resource_stdio import JsonStdioResource, json_stdio_factory


class CliPoolTests(fixtures.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.workspaces=self.root/'workspaces';self.workspaces.mkdir()
        self.registry=BackendRegistry(self.store,lambda s:json_stdio_factory(s,self.workspaces))
        self.addCleanup(self.registry.close)
        self.pool=AgentPool(self.store,self.registry)

    def fake(self, name, build='first'):
        entry=self.root/name;entry.parent.mkdir(parents=True,exist_ok=True)
        entry.write_text('#!'+sys.executable+'\nROOT='+repr(str(self.root))+'\nBUILD='+repr(build)+'\n'+textwrap.dedent(r'''
            import json,os,re,sys,time
            from pathlib import Path
            root=Path(ROOT)
            if '--serve' in sys.argv and not os.environ.get('TEST_WB_SERVER_CHILD'):
                import http.server,subprocess,queue
                notifications=queue.Queue()
                (root/'server.pid').write_text(str(os.getpid()))
                class Handler(http.server.BaseHTTPRequestHandler):
                    def log_message(self,*args):pass
                    def reply(self,value,sse=False):
                        self.send_response(200)
                        self.send_header('Content-Type','text/event-stream' if sse else 'application/json')
                        self.end_headers()
                        body=json.dumps(value).encode()
                        self.wfile.write(b'data: '+body+b'\n\n' if sse else body)
                    def authorized(self):
                        assert self.headers.get('Authorization')=='Bearer '+os.environ['CODEBUDDY_GATEWAY_PASSWORD']
                        assert self.headers.get('X-CodeBuddy-Request')=='1'
                        assert not self.headers.get('Origin')
                    def do_GET(self):
                        self.authorized()
                        if self.path=='/api/v1/health':self.reply({'ok':True});return
                        if self.path=='/api/v1/auth/account/status':
                            mode=(root/'mode').read_text() if (root/'mode').exists() else ''
                            self.reply({'authenticated':mode!='wb-login'});return
                        assert self.path=='/api/v1/acp'
                        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                        self.wfile.write(b': ready\n\n');self.wfile.flush()
                        while True:
                            try:event=notifications.get(timeout=1)
                            except queue.Empty:continue
                            self.wfile.write(b'data: '+json.dumps(event).encode()+b'\n\n');self.wfile.flush()
                    def do_POST(self):
                        self.authorized()
                        request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                        if self.path=='/api/v1/acp/connect':
                            self.reply({'connectionId':'TEST-connection','sessionToken':'TEST-session-secret'});return
                        assert self.path=='/api/v1/acp'
                        assert self.headers.get('acp-connection-id')=='TEST-connection'
                        assert self.headers.get('acp-session-token')=='TEST-session-secret'
                        method=request['method'];params=request['params']
                        with (root/'rpc-methods').open('a') as out:out.write(method+'\n')
                        mode=(root/'mode').read_text() if (root/'mode').exists() else ''
                        result={}
                        if method=='initialize':
                            assert params['clientCapabilities']=={}
                            result={'protocolVersion':1}
                        elif method=='session/new':
                            assert params=={'cwd':os.getcwd(),'mcpServers':[]}
                            if mode=='wb-login':
                                self.reply({'jsonrpc':'2.0','id':request['id'],'error':
                                    {'code':-32000,'message':'Authentication required TEST_PRIVATE_RAW'}},True);return
                            result={'sessionId':'TEST-session'}
                        elif method=='session/prompt':
                            assert params['sessionId']=='TEST-session'
                            if mode=='wb-permission':
                                self.reply({'jsonrpc':'2.0','id':'approval','method':'session/request_permission',
                                    'params':{'secret':'TEST_PRIVATE_RAW'}},True);return
                            if mode=='wb-missing':
                                result={'stopReason':'end_turn'}
                            elif mode=='wb-result-login':
                                result={'stopReason':'end_turn','_meta':
                                    {'codebuddy.ai/errorMessage':'Authentication required TEST_PRIVATE_RAW'}}
                            elif mode=='wb-notify-permission':
                                notifications.put({'jsonrpc':'2.0','id':'approval',
                                    'method':'session/request_permission','params':{'secret':'TEST_PRIVATE_RAW'}})
                                time.sleep(30);return
                            elif mode=='wb-stall':
                                time.sleep(30);return
                            else:
                                prompt=params['prompt'][0]['text']
                                subprocess.run([sys.executable,sys.argv[0],*sys.argv[1:]],
                                    input=prompt.encode(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                    env=dict(os.environ,TEST_WB_SERVER_CHILD='1'),check=True)
                                result={'stopReason':'end_turn'}
                        elif method=='authenticate':
                            assert params=={'methodId':'external'}
                            # Native streamable HTTP can send notifications on
                            # the active POST stream instead of the GET stream.
                            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                            for event in ({'jsonrpc':'2.0','method':'_codebuddy.ai/authUrl',
                                'params':{'authUrl':'https://www.workbuddy.ai/TEST-login?state=TEST_PRIVATE_RAW'}},
                                {'jsonrpc':'2.0','id':request['id'],'result':
                                    {'_meta':{'userinfo':{'token':'TEST_PRIVATE_RAW'}}}}):
                                self.wfile.write(b'data: '+json.dumps(event).encode()+b'\n\n');self.wfile.flush()
                            return
                        else:raise AssertionError(method)
                        self.reply({'jsonrpc':'2.0','id':request['id'],'result':result},True)
                # The fixture uses exactly the selected loopback port, with the
                # same password/request guard as the real bundled CLI.
                assert sys.argv[sys.argv.index('--host')+1]=='127.0.0.1'
                assert sys.argv[sys.argv.index('--auth')+1]=='password'
                print('TEST_PRIVATE_RAW '+os.environ['CODEBUDDY_GATEWAY_PASSWORD'],flush=True)
                print('TEST_PRIVATE_RAW '+os.environ['CODEBUDDY_GATEWAY_PASSWORD'],file=sys.stderr,flush=True)
                http.server.ThreadingHTTPServer(('127.0.0.1',int(sys.argv[sys.argv.index('--port')+1])),Handler).serve_forever()
                sys.exit(0)
            stdin=sys.stdin.read()
            prompt=json.loads(stdin)['message']['content'] if '--input-format' in sys.argv else stdin
            with (root/'calls').open('a') as out:
                out.write(json.dumps({'argv':sys.argv[1:],'entry':sys.argv[0], 'build':BUILD,
                    'cwd':os.getcwd(),'prompt':prompt,'pid':os.getpid()})+'\n')
            mode=(root/'mode').read_text() if (root/'mode').exists() else ''
            if mode in ('login','model','exit','unsupported'):
                errors={'login':'not logged in TEST_PRIVATE_RAW',
                    'model':'unknown model TEST_PRIVATE_RAW','exit':'rate limit TEST_PRIVATE_RAW',
                    'unsupported':"Cannot find module TEST_PRIVATE_RAW"}
                sys.stderr.write(errors[mode]);sys.exit(2)
            if mode=='noise':
                for i in range(200):
                    print(json.dumps({'event':'progress','text':str(i)+'X'*400}),flush=True);time.sleep(.03)
                sys.exit(0)
            cardpath=Path(json.loads(prompt.split('XIAOBA_TASK_CARD=',1)[1]))
            card=cardpath.read_text()
            log=Path(re.search(r'^工作回执文件: (.+)$',card,re.M)[1])
            task=re.search(r'^任务ID: (.+)$',card,re.M)[1]
            step=re.search(r'^步骤码: (.+)$',card,re.M)[1]
            nonce=re.search(r'^本轮标识: (.+)$',card,re.M)[1]
            context=json.loads(card.split('## 材料\n',1)[1].split('\n\n## 范围与回执',1)[0])
            if not log.exists():log.write_text(card.split('先写以下文件头：\n\n',1)[1].split('\n文件已存在时',1)[0])
            body=context.get('challenge','TEST authorized receipt')
            with log.open('a') as out:
                out.write('\n<!-- task:'+nonce+' -->\n任务ID: '+task+'\n步骤码: '+step+
                    '\n状态: completed\n---\n'+body+'\n<!-- done:'+nonce+' -->\n')
            print(json.dumps({'event':'result','result':{'status':'SUCCESS'}}))
        '''))
        entry.chmod(0o700);return entry

    def add(self, kind, entry=None, model='TEST explicit model', node_id=None):
        node_id=node_id or 'TEST-'+kind
        source=self.pool.add({'node_id':node_id,'name':node_id,'agent':kind,'agent_type':kind,
            'kind':'peer','interface':'cli','entry':str(entry or self.fake('entry-'+kind)), 'model':model})
        self.pool.change(node_id,source['node']['revision'],'toggle',True)
        return node_id

    def source(self,node):
        with self.native.read_only() as db:return self.store.source(db,node)

    def node(self,node):return next(v for v in self.pool.state()['nodes'] if v['node_id']==node)

    def probe(self,node):return self.pool.runtime.test_run(node,self.node(node)['revision'],'probe-'+node)

    def calls(self):
        path=self.root/'calls'
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def test_seven_types_probe_and_work_with_exact_argv_and_no_task_text(self):
        expected={
          'agy':['--input-format','stream-json','--output-format','stream-json','--model','TEST explicit model',
                 '--mode','accept-edits','--sandbox','--print-timeout','90s'],
          'dsh':['--profile','headless','--patch',str(Path(os.environ.get('DSH_HOME') or Path.home()/'.dsh')/'profiles/desktop/cordis.patch.yml'),
                 '--json','--permission-mode','workspace-write','--approval-policy','never','-'],
          'qoder':['-p','--model','TEST explicit model','--permission-mode','accept_edits','--tools',
                 'Read,Write,Edit,Glob,Grep','--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                 '--output-format','stream-json'],
          'workbuddy':['--serve','--host','127.0.0.1','--port','TEST-port','--auth','password',
                 '--model','TEST explicit model','--permission-mode','acceptEdits','--no-session-persistence',
                 '--tools','Read,Write,Edit,Glob,Grep','--strict-mcp-config','--mcp-config','{"mcpServers":{}}'],
          'codebuddy':['-p','--model','TEST explicit model','--permission-mode','acceptEdits','--tools',
                 'Read,Write,Edit,Glob,Grep','--allowedTools','Read,Write,Edit,Glob,Grep','--no-session-persistence',
                 '--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--output-format','stream-json','--verbose'],
          'claude':['-p','--model','TEST explicit model','--permission-mode','acceptEdits','--tools',
                 'Read,Write,Edit,Glob,Grep','--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                 '--output-format','stream-json','--verbose']}
        for kind in ('agy','dsh','qoder','workbuddy','codebuddy','codex','claude'):
            with self.subTest(kind=kind):
                node=self.add(kind);self.assertEqual(self.probe(node)['status'],'passed')
                record=self.pool.runtime.execute_work(node,self.node(node)['revision'], 'TASK-'+kind,'STEP',
                    'TEST authorized file work',{'goal':'TEST material'})
                self.assertEqual(record['body'],'TEST authorized receipt')
                calls=self.calls()[-2:]
                if kind=='codex':expected[kind]=['exec','-m','TEST explicit model','-s','workspace-write','-C',
                    str((self.workspaces/node).resolve()),'--skip-git-repo-check','--ephemeral','--json','-']
                if kind=='workbuddy':
                    for call in calls:
                        self.assertGreater(int(call['argv'][4]),0)
                        call['argv'][4]='TEST-port'
                self.assertEqual(calls[0]['argv'],expected[kind])
                self.assertEqual(calls[1]['argv'],expected[kind][:-1]+['0s'] if kind=='agy' else expected[kind])
                for call in calls:
                    self.assertEqual(call['cwd'],str((self.workspaces/node).resolve()))
                    self.assertFalse(any(s in ' '.join(call['argv']) for s in
                        ('TEST authorized','XIAOBA_TASK_CARD','dangerously','bypass','dontAsk','dont_ask')))
                self.assertTrue(self.node(node)['readiness']['ready'])

    def test_symlink_upgrade_and_service_restart_keep_qualification_for_seven_types(self):
        for kind in ('agy','dsh','qoder','workbuddy','codebuddy','codex','claude'):
            with self.subTest(kind=kind):
                one=self.fake(kind+'-v1', 'first');two=self.fake(kind+'-v2','second')
                link=self.root/('stable-'+kind);link.symlink_to(one)
                node=self.add(kind,link);self.assertEqual(self.probe(node)['status'],'passed')
                before=self.source(node);runner=self.registry.adapter_for(before).resource_identity()
                # Preserve both fixture links; switching creates no backup and
                # removes no object. This also simulates a real stable symlink.
                link.rename(self.root/('first-link-'+kind));link.symlink_to(two)
                self.registry.close()
                self.assertTrue(self.node(node)['readiness']['ready'])
                self.assertEqual(self.registry.adapter_for(self.source(node)).resource_identity(),runner)
                self.assertEqual(self.source(node)['source_ref'],before['source_ref'])
                self.assertEqual(self.source(node)['node']['entry'],str(link))
                self.pool.runtime.execute_work(node,self.node(node)['revision'],'UPGRADE-'+kind,'STEP','TEST write',{})
                self.assertEqual(self.calls()[-1]['build'],'second')
                self.assertEqual(self.calls()[-1]['entry'],str(link))

    def test_model_entry_type_edits_invalidate_but_rename_does_not(self):
        node=self.add('qoder');self.assertEqual(self.probe(node)['status'],'passed')
        original=self.source(node)
        self.pool.edit({'node_id':node,'expected_revision':self.node(node)['revision'],'name':'TEST renamed'})
        self.assertTrue(self.node(node)['readiness']['ready'])
        for changes in ({'model':'TEST model 2'},{'entry':str(self.fake('other-qoder'))},{'agent_type':'claude'}):
            self.pool.edit({'node_id':node,'expected_revision':self.node(node)['revision'],**changes})
            self.assertFalse(self.node(node)['readiness']['ready'])
        self.assertEqual(self.source(node)['node']['workspace_name'],original['node']['name'])

    def test_independence_uses_cli_type_and_model_even_for_legacy_source_refs(self):
        a=self.add('qoder',self.fake('qodercli'),node_id='A');b=self.add('qoder',self.fake('versions/qodercli-9.9.9'),node_id='B')
        self.assertEqual(independence(self.source(a)),independence(self.source(b)))
        source=self.source(a);source['source_ref']='agent-entry:OLD_RESOLVED_VERSION'
        source['node'].pop('agent_type')
        self.assertEqual(independence(source),independence(self.source(b)))
        source['node']['model']='another'
        self.assertNotEqual(independence(source),independence(self.source(b)))

    def test_legacy_inference_and_explicit_other_are_distinct(self):
        for entry,kind in [('agy','agy'),('antigravity','agy'),('qodercli','qoder'),('codebuddy','workbuddy'),
            ('codex.js','codex'),('claude','claude'),('/x/current/apps/cli/lib/bin.js','dsh'),('unknown','other')]:
            self.assertEqual(agent_type({'entry':entry}),kind)
        # JSON stdout instead of a file result demonstrates explicit "other".
        entry=self.fake('claude')
        entry.write_text('#!'+sys.executable+'\nprint("{}")\n')
        node=self.add('other',entry)
        self.assertIsInstance(self.registry.adapter_for(self.source(node)),JsonStdioResource)
        result=self.probe(node)
        self.assertEqual(result['reason'],'unsupported_entry');self.assertIn('JSON',result['message'])

    def test_versioned_path_warns_but_saves_and_missing_entry_blocks(self):
        entry=self.fake('versions/cli-9.8.7');node=self.add('claude',entry)
        self.assertTrue(self.node(node)['entry_warning']);self.assertEqual(self.probe(node)['status'],'passed')
        entry.rename(self.root/'fixture-no-longer-at-entry')
        self.assertEqual(self.node(node)['readiness']['reason'],'entry_unavailable')
        result=self.pool.runtime.test_run(node,self.node(node)['revision'],'missing')
        self.assertEqual(result['reason'],'entry_unavailable');self.assertIn('不存在',result['message'])
        self.assertEqual(len(self.calls()),1);self.assertIsNone(entry_warning('/stable/current/cli'))

    def test_adapter_code_change_requires_retest(self):
        node=self.add('claude');self.assertEqual(self.probe(node)['status'],'passed')
        with patch('xiaoba_review.cli_identity.implementation_digest',return_value='changed-code'):
            self.assertEqual(self.node(node)['readiness']['reason'],'test_required')

    def test_blank_model_uses_cli_default_without_model_arguments(self):
        for kind in ('agy','dsh','qoder','workbuddy','codebuddy','codex','claude'):
            with self.subTest(kind=kind):
                node=self.add(kind,model='');self.assertEqual(self.probe(node)['status'],'passed')
                self.assertNotIn('--model',self.calls()[-1]['argv']);self.assertNotIn('-m',self.calls()[-1]['argv'])
                self.assertTrue(self.node(node)['readiness']['ready'])

    def test_blank_model_counts_as_one_source_and_dsh_model_text_does_not_split_it(self):
        a=self.add('claude',self.fake('claude-a'),model='',node_id='A');b=self.add('claude',self.fake('claude-b'),model='',node_id='B')
        self.assertEqual(independence(self.source(a)),independence(self.source(b)))
        c=self.add('codex',model='',node_id='C')
        self.assertNotEqual(independence(self.source(a)),independence(self.source(c)))
        d=self.add('dsh',self.fake('dsh-a'),model='TEST one',node_id='D');e=self.add('dsh',self.fake('dsh-b'),model='TEST two',node_id='E')
        self.assertEqual(independence(self.source(d)),independence(self.source(e)))

    def test_a_blank_model_failure_stays_in_the_workday_inputs(self):
        workspace=AgentWorkspace(self.root/'blank-workspace',self.source(self.add('codex',model='',node_id='F')))
        (self.root/'blank-workspace').mkdir()
        record=workspace.begin('work','TEST-task-STEP',{'task_id':'TEST-task','step_code':'STEP','model':''})
        workspace.finish(record,'failed',evidence={'provider':'TEST','model':''},reason='cli_failed')
        day=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        self.assertEqual([v['failure_reason'] for v in workspace.workday_inputs(day)],['cli_failed'])

    def test_a_rejected_default_model_does_not_blame_an_unused_node_value(self):
        for kind,model in (('claude',''),('dsh','TEST desktop text')):
            with self.subTest(kind=kind):
                failure=specific_failure(kind,kind,model,'','unknown model TEST_PRIVATE_RAW')
                self.assertEqual(failure.code,'model_not_recognized')
                self.assertIn('当前配置的模型',failure.user_message)
                self.assertNotIn('TEST_PRIVATE_RAW',failure.user_message)
                if model:self.assertNotIn(model,failure.user_message)

    def test_login_model_and_nonzero_failures_are_safe_and_classified(self):
        for mode,reason in [('login','cli_not_logged_in'),('model','model_not_recognized'),
                             ('exit','cli_failed'),('unsupported','unsupported_entry')]:
            with self.subTest(mode=mode):
                (self.root/'mode').write_text(mode);node=self.add('qoder',node_id='FAIL-'+mode)
                result=self.probe(node);self.assertEqual(result['reason'],reason,result)
                self.assertTrue(result['message'])
                with self.native.read_only() as db:
                    stored=' '.join(r[0] for r in db.execute('SELECT payload_json FROM events'))
                stored+=''.join(p.read_text() for p in self.workspaces.rglob('*') if p.is_file())
                self.assertNotIn('TEST_PRIVATE_RAW',stored)
                self.assertEqual(len(self.calls()),len([p for p in self.workspaces.iterdir()]))

    def test_stdout_events_do_not_reset_300_second_worklog_deadline(self):
        self.assertEqual(CliWorklogResource.no_progress_seconds,300)
        (self.root/'mode').write_text('noise');node=self.add('qoder')
        adapter=self.registry.adapter_for(self.source(node));adapter.no_progress_seconds=1.5
        result=self.probe(node)
        self.assertEqual(result['reason'],'cli_no_progress',result)
        self.assertGreater(adapter._events,0)
        self.assertIn('无可验证',result['message']);self.assertEqual(len(self.calls()),1)

    def test_subprocess_environment_matches_native_scrubbing_without_session_markers(self):
        values={'CLAUDECODE':'nested','CODEX_THREAD_ID':'parent','QODER_TOKEN':'private',
                'QODER_SESSION_ID':'parent','OPENAI_API_KEY':'secret','ANTHROPIC_AUTH_TOKEN':'secret'}
        with patch.dict(os.environ,values):
            for kind in ('agy','dsh','qoder','workbuddy','codebuddy','codex','claude'):
                env=cli_environment('/TEST/bin/cli',kind)
                for key in values:self.assertNotIn(key,env)

    def test_full_300_seconds_is_classified_without_waiting_in_real_time(self):
        node=self.add('qoder');adapter=self.registry.adapter_for(self.source(node))
        from xiaoba_review.resource_agy import InteractiveError
        with patch('xiaoba_review.resource_cli.time.monotonic',side_effect=[0,301]), \
                patch.object(adapter,'_kill_group') as kill:
            with self.assertRaises(InteractiveError) as error:
                adapter._wait({'log':self.root/'not-written'},None)
        self.assertEqual(error.exception.code,'cli_no_progress')
        self.assertIn('300秒',error.exception.user_message);kill.assert_called_once()

    def test_workbuddy_loads_its_bundled_lite_variant_without_installing_modules(self):
        entry=self.fake('workbuddy-bundled-cli')
        entry.write_text(entry.read_text().replace('root=Path(ROOT)',
            "assert os.environ.get('CODEBUDDY_FORCE_LITE_WB_BUNDLE') == '1'\nroot=Path(ROOT)"))
        node=self.add('workbuddy',entry)
        self.assertEqual(self.probe(node)['status'],'passed')
        result=self.pool.runtime.execute_work(node,self.node(node)['revision'],
            'WORKBUDDY-LITE','STEP','TEST file work',{})
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(self.calls()),2)

    def test_workbuddy_cli_and_codebuddy_are_one_source_with_separate_adapters(self):
        from xiaoba_review.resource_vendor_cli import CodeBuddyCliResource
        from xiaoba_review.resource_workbuddy import WorkBuddyCliResource
        cli=self.add('codebuddy',node_id='CB');desktop=self.add('workbuddy',node_id='WB')
        adapter=self.registry.adapter_for(self.source(cli))
        self.assertIsInstance(adapter,CodeBuddyCliResource)
        self.assertIsInstance(self.registry.adapter_for(self.source(desktop)),WorkBuddyCliResource)
        # 用户 2026-11-02：WorkBuddy 的 CLI 就是 CodeBuddy，不能凑成两票。
        self.assertEqual(independence(self.source(cli)),independence(self.source(desktop)))
        from xiaoba_agentos.pool_dispatch import independence_pair
        pairs=[independence_pair({'interface':'cli','agent_type':kind,'model':'TEST explicit model'}) for kind in ('codebuddy','workbuddy')]
        self.assertEqual(*pairs)
        self.assertEqual(agent_type({'entry':'codebuddy'}),'workbuddy')  # Preserve old nodes.
        with patch.dict(os.environ,{'CODEBUDDY_FORCE_LITE_WB_BUNDLE':'1',
            'CODEBUDDY_HOST':'workbuddy-desktop','CODEBUDDY_AUTH_TOKEN':'TEST_PRIVATE_RAW',
            'CODEBUDDY_CONFIG_DIR':'/TEST/private','WORKBUDDY_CONFIG_DIR':'/TEST/private',
            'ACC_PRODUCT_CONFIG_PATH':'/TEST/private','SERVER__PORT':'9999'}):
            env=adapter._environment()
        self.assertFalse(any(k.startswith(('CODEBUDDY_','WORKBUDDY_','ACC_PRODUCT_CONFIG')) for k in env))
        self.assertNotIn('SERVER__PORT',env)
        self.assertEqual(env['DISABLE_AUTOUPDATER'],'1')

    def test_workbuddy_native_rpc_failures_do_not_leak_or_approve_or_retry(self):
        for mode,reason in [('wb-login','cli_not_logged_in'),('wb-permission','native_permission_required'),
                            ('wb-missing','receipt_missing'),('wb-stall','cli_no_progress'),
                            ('wb-notify-permission','native_permission_required'),
                            ('wb-result-login','workbuddy_execution_auth_rejected')]:
            with self.subTest(mode=mode):
                (self.root/'mode').write_text(mode)
                node=self.add('workbuddy',node_id=mode)
                adapter=self.registry.adapter_for(self.source(node))
                if mode=='wb-stall':adapter.no_progress_seconds=1
                result=self.probe(node)
                self.assertEqual(result['reason'],reason,result)
                journal=self.root/'rpc-methods'
                methods=journal.read_text().splitlines() if journal.exists() else []
                self.assertEqual(methods.count('session/new'),0 if mode=='wb-login' else 1)
                self.assertNotIn('authenticate',methods)
                self.assertIsNone(adapter._process)
                self.assertIsNone(adapter._password)
                self.assertEqual(adapter._stdout,b'')
                with self.native.read_only() as db:
                    stored=' '.join(r[0] for r in db.execute('SELECT payload_json FROM events'))
                stored+=''.join(p.read_text() for p in self.workspaces.rglob('*') if p.is_file())
                for value in ('TEST_PRIVATE_RAW','TEST-session-secret'):
                    self.assertNotIn(value,stored)
                if mode=='wb-login':self.assertIn('不能据此判断桌面端',result['message'])
                # Reset the small fixture journal between independent cases.
                (self.root/'rpc-methods').write_text('')

    def test_workbuddy_attended_login_opens_native_url_without_task_or_secret_logging(self):
        from xiaoba_review.workbuddy_login import login
        entry=self.fake('workbuddy-login')
        opened=[];messages=[]
        login(str(entry),self.root,open_browser=lambda url,**kw:opened.append(url) or True,
              announce=messages.append)
        self.assertEqual(len(opened),1)
        self.assertTrue(opened[0].startswith('https://www.workbuddy.ai/TEST-login'))
        self.assertEqual((self.root/'rpc-methods').read_text().splitlines(),['initialize','authenticate'])
        self.assertEqual(self.calls(),[])
        self.assertNotIn('TEST_PRIVATE_RAW',' '.join(messages))
        self.assertIn('认证完成',messages[-1])

    def test_workbuddy_transport_bounds_events_and_checks_response_identity(self):
        import io
        from xiaoba_review.resource_workbuddy import AcpClient
        from xiaoba_review.resource_agy import InteractiveError
        client=AcpClient(1,'TEST-own-password',1)
        client.limit=128
        with self.assertRaises(InteractiveError) as error:
            list(client.events(io.BytesIO(b'data: '+b'X'*1000)))
        self.assertEqual(error.exception.code,'workbuddy_protocol_error')
        with patch.object(client,'request',return_value={'jsonrpc':'2.0','id':999,'result':{}}):
            with self.assertRaises(InteractiveError):client.rpc('initialize',{})

    def test_workbuddy_transport_refuses_redirects_instead_of_forwarding_bearer(self):
        import http.server
        import threading
        from xiaoba_review.resource_agy import InteractiveError
        from xiaoba_review.resource_workbuddy import AcpClient
        seen=[]
        class Redirect(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                seen.append(self.path)
                self.send_response(302);self.send_header('Location','/TEST-other-destination');self.end_headers()
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Redirect)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            client=AcpClient(server.server_port,'TEST-own-password',1)
            with self.assertRaises(InteractiveError):client.request('/api/v1/health')
            self.assertEqual(seen,['/api/v1/health'])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

    def test_workbuddy_does_not_inherit_a_desktop_credential_bootstrap_or_gateway(self):
        node=self.add('workbuddy')
        adapter=self.registry.adapter_for(self.source(node))
        with patch.dict(os.environ,{'CODEBUDDY_GATEWAY_PASSWORD':'TEST_PRIVATE_RAW',
            'CODEBUDDY_DISABLE_REQUEST_VALIDATION':'1','CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET':'/TEST/private',
            'CODEBUDDY_CONFIG_DIR':'/TEST/private','WORKBUDDY_AT_REST_ENCRYPTION':'off'}):
            env=adapter._environment()
        self.assertEqual(env['CODEBUDDY_FORCE_LITE_WB_BUNDLE'],'1')
        self.assertEqual(env['CODEBUDDY_HOST'],'workbuddy-desktop')
        self.assertEqual(env['CODEBUDDY_DISABLE_PRODUCT_CACHE'],'1')
        self.assertEqual(env['CODEBUDDY_GATEWAY_AUTH'],'password')
        for key in ('CODEBUDDY_GATEWAY_PASSWORD','CODEBUDDY_DISABLE_REQUEST_VALIDATION',
                    'CODEBUDDY_SIDECAR_CREDENTIAL_BOOTSTRAP_SOCKET','CODEBUDDY_CONFIG_DIR',
                    'WORKBUDDY_AT_REST_ENCRYPTION'):
            self.assertNotIn(key,env)

    def test_old_node_without_type_and_with_old_source_ref_loads_and_runs(self):
        entry=self.fake('qodercli')
        source=self.pool.add({'node_id':'LEGACY','name':'LEGACY','agent':'custom',
            'kind':'peer','interface':'cli','entry':str(entry),'model':'TEST explicit model'})
        source['source_ref']='agent-entry:TEST-old-version-specific-identity'
        from xiaoba_review.store import canonical
        with self.store.transaction() as db:
            db.execute('UPDATE ri_sources SET data=? WHERE id=?',(canonical(source),'LEGACY'))
        self.pool.change('LEGACY',source['node']['revision'],'toggle',True)
        self.assertNotIn('agent_type',self.source('LEGACY')['node'])
        self.assertEqual(self.probe('LEGACY')['status'],'passed')
        self.assertTrue(self.node('LEGACY')['readiness']['ready'])
        self.assertEqual(self.source('LEGACY')['source_ref'],source['source_ref'])
