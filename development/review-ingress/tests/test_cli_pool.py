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

    def test_six_types_probe_and_work_with_exact_argv_and_no_task_text(self):
        expected={
          'agy':['--input-format','stream-json','--output-format','stream-json','--model','TEST explicit model',
                 '--mode','accept-edits','--sandbox','--print-timeout','90s'],
          'dsh':['--profile','headless','--patch',str(Path(os.environ.get('DSH_HOME') or Path.home()/'.dsh')/'profiles/desktop/cordis.patch.yml'),
                 '--json','--permission-mode','workspace-write','--approval-policy','never','-'],
          'qoder':['-p','--model','TEST explicit model','--permission-mode','accept_edits','--tools',
                 'Read,Write,Edit,Glob,Grep','--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                 '--output-format','stream-json'],
          'workbuddy':['-p','--model','TEST explicit model','--permission-mode','acceptEdits','--tools',
                 'Read,Write,Edit,Glob,Grep','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--output-format','stream-json'],
          'claude':['-p','--model','TEST explicit model','--permission-mode','acceptEdits','--tools',
                 'Read,Write,Edit,Glob,Grep','--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                 '--output-format','stream-json','--verbose']}
        for kind in ('agy','dsh','qoder','workbuddy','codex','claude'):
            with self.subTest(kind=kind):
                node=self.add(kind);self.assertEqual(self.probe(node)['status'],'passed')
                record=self.pool.runtime.execute_work(node,self.node(node)['revision'], 'TASK-'+kind,'STEP',
                    'TEST authorized file work',{'goal':'TEST material'})
                self.assertEqual(record['body'],'TEST authorized receipt')
                calls=self.calls()[-2:]
                if kind=='codex':expected[kind]=['exec','-m','TEST explicit model','-s','workspace-write','-C',
                    str((self.workspaces/node).resolve()),'--skip-git-repo-check','--ephemeral','--json','-']
                self.assertEqual(calls[0]['argv'],expected[kind])
                self.assertEqual(calls[1]['argv'],expected[kind][:-1]+['0s'] if kind=='agy' else expected[kind])
                for call in calls:
                    self.assertEqual(call['cwd'],str((self.workspaces/node).resolve()))
                    self.assertFalse(any(s in ' '.join(call['argv']) for s in
                        ('TEST authorized','XIAOBA_TASK_CARD','dangerously','bypass','dontAsk','dont_ask')))
                self.assertTrue(self.node(node)['readiness']['ready'])

    def test_symlink_upgrade_and_service_restart_keep_qualification_for_six_types(self):
        for kind in ('agy','dsh','qoder','workbuddy','codex','claude'):
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
        for kind in ('agy','dsh','qoder','workbuddy','codex','claude'):
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
            for kind in ('agy','dsh','qoder','workbuddy','codex','claude'):
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
