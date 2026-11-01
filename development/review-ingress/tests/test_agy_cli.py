"""Per-step CLI child-process fixtures. No model or human approval is used."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import textwrap
import time
import unittest

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.agent_workspace import WorkspaceError
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_agy import AgyInteractiveResource, InteractiveError
from xiaoba_review.resource_agy_cli import AgyCliResource
from xiaoba_review.resource_stdio import JsonStdioResource, json_stdio_factory


class CliFixture(fixtures.FixtureMixin):
    """A fake `agy` that asserts the CLI contract and writes a receipt block."""

    def setUp(self):
        super().setUp()
        self.workspaces = self.root/'workspaces';self.workspaces.mkdir()
        self.backends = BackendRegistry(self.store,
            resource_factory=lambda source:json_stdio_factory(source,self.workspaces,agy_mode='cli'))
        self.addCleanup(self.backends.close)
        self.pool = AgentPool(self.store,self.backends)
        self.entry = self.root/'agy'
        self.entry.write_text('#!'+sys.executable+'\n'+textwrap.dedent(r'''
            import json,os,re,sys,time
            from pathlib import Path
            root=Path(__file__).parent
            argv=sys.argv[1:]
            forbidden=('-p','--print','--prompt','--dangerously-skip-permissions',
                       '--add-dir','--disable-slash-commands','models')
            assert not any(a in forbidden for a in argv), argv
            assert argv[:5]==['--input-format','stream-json','--output-format','stream-json','--model'], argv
            assert argv[6:10]==['--mode','accept-edits','--sandbox','--print-timeout'], argv
            assert len(argv)==11 and argv[10] in ('0s','90s'), argv
            message=json.loads(sys.stdin.readline())
            assert message['event']=='user' and isinstance(message['message']['content'],str), message
            prompt=message['message']['content']
            extra=sys.stdin.read()
            # One line per process: proof that one step is one child process.
            with (root/'calls').open('a') as out:
                out.write(json.dumps({'event':'start','pid':os.getpid(),'argv':argv,
                    'cwd':os.getcwd(),'prompt':prompt,'extra_stdin':extra})+'\n')
            def flag(name):return (root/name).exists()
            cardpath=Path(json.loads(prompt.split('XIAOBA_TASK_CARD=',1)[1]))
            card=cardpath.read_text()
            logpath=Path(re.search(r'^工作回执文件: (.+)$',card,re.M)[1])
            taskid=re.search(r'^任务ID: (.+)$',card,re.M)[1]
            step=re.search(r'^步骤码: (.+)$',card,re.M)[1]
            nonce=re.search(r'^本轮标识: (.+)$',card,re.M)[1]
            context=json.loads(card.split('## 材料\n',1)[1].split('\n\n## 范围与回执',1)[0])
            if flag('trust-prompt'):
                sys.stderr.write('Antigravity CLI TEST\nDo you trust the contents of this project?\n> Yes, I trust this folder\n')
                sys.exit(1)
            if flag('nonzero-exit'):
                sys.stderr.write('TEST transient failure: rate limit reached\n')
                sys.exit(3)
            if flag('fail-status'):
                print(json.dumps({'event':'result','result':{'status':'ERROR','error':'TEST permanent failure: invalid request'}}))
                sys.exit(0)
            if flag('stdout-flood'):
                chunk=json.dumps({'event':'assistant','message':{'content':'X'*200}})+'\n'
                for _ in range(9000):sys.stdout.write(chunk)
                sys.stdout.flush();time.sleep(30);sys.exit(0)
            if flag('stall'):
                time.sleep(120);sys.exit(0)
            if flag('no-receipt'):
                print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'TEST no receipt'}}))
                sys.exit(0)
            sys.stdout.write(json.dumps({'event':'assistant','message':{'content':'XB_STDOUT progress'}})+'\n')
            sys.stdout.flush()
            sys.stderr.write('XB_STDERR diagnostic\n');sys.stderr.flush()
            body=context['challenge'] if 'challenge' in context else 'TEST terminal fixture; no real review.'
            if flag('long-reply'):body+='\n'+'TEST long file reply\n'*300
            if not logpath.exists():
                header=card.split('先写以下文件头：\n\n',1)[1].split('\n文件已存在时',1)[0]
                logpath.write_text(header)
            if flag('rewrite-history') and '<!-- done:' in logpath.read_text():
                logpath.write_text(logpath.read_text().replace('状态: completed','状态: completed ',1))
            meta='任务ID: '+('WRONG' if flag('wrong-task-id') else taskid)+'\n步骤码: '+step+'\n状态: completed\n'
            if step=='REVIEW-01' and not flag('bad-review-fields'):
                meta+='审查结论: PASS\n证据引用: TEST cli child process file receipt\n'
            with logpath.open('a') as out:
                out.write('\n<!-- task:'+nonce+' -->\n'+meta+'---\n'+body+'\n');out.flush()
                if flag('partial-file'):time.sleep(.2)
                if not flag('partial-then-exit'):
                    out.write('<!-- done:'+nonce+' -->\n');out.flush()
            if flag('linger-after-receipt'):time.sleep(60)
            if flag('orphan-helper'):
                # A helper left in the call's process group after agy exits.
                import subprocess
                helper=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],
                    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                (root/'helper.pid').write_text(str(helper.pid))
            print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'TEST file receipt written'}}))
        '''))
        self.entry.chmod(0o700)

    def add(self, node_id='TEST-cli', model='Gemini 3.8 Flash', name='用户自定名字'):
        source=self.pool.add({'node_id':node_id,'name':name,'agent':'custom','kind':'peer',
            'interface':'cli','entry':str(self.entry),'model':model})
        self.pool.change(node_id,source['node']['revision'],'toggle',True)
        return node_id

    def source(self,node):
        with self.native.read_only() as db:return self.store.source(db,node)

    def node(self,node):return next(v for v in self.pool.state()['nodes'] if v['node_id']==node)

    def adapter(self,node):return self.backends.adapter_for(self.source(node))

    def probe(self,node,test_id='TEST-probe'):
        return self.pool.runtime.test_run(node,self.node(node)['revision'],test_id)

    def calls(self):
        p=self.root/'calls'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def starts(self):return [v for v in self.calls() if v['event']=='start']

    def review(self,adapter,task='TASK-1',assignment='ASSIGN-1',step='REVIEW-01'):
        return adapter.run({'task_id':task,'assignment_id':assignment,'step_code':step,'card_digest':'TEST'})

    def receipts(self):
        return sorted((self.workspaces/'用户自定名字').rglob('执行回执-*.json'),
            key=lambda p:p.stat().st_mtime)


class CliContractTests(CliFixture,unittest.TestCase):
    def test_health_probe_passes_with_complete_evidence_and_prepare_runs(self):
        node=self.add();result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        evidence=result['execution_evidence']
        self.assertEqual(evidence['provider'],'antigravity_cli')
        self.assertEqual(evidence['model'],'Gemini 3.8 Flash')
        self.assertEqual(evidence['route_source'],'native_cli_per_step')
        self.assertEqual(evidence['model_source'],'node_configuration')
        # 用户 2026-11-01 裁决后新回执不再写 model_verification（旧回执仍能读）。
        self.assertNotIn('model_verification',evidence)
        self.assertEqual(evidence['execution_mode'],'native_cli_per_step')
        self.assertEqual(evidence['reply_source'],'daily_worklog')
        self.assertEqual(evidence['output'],evidence['challenge'])
        self.assertEqual(result['reason'],'challenge_completed_configured_node')
        self.assertEqual(self.pool.runtime.prepare(['peer'])['route'],'run')
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_two_calls_start_two_processes_and_keep_the_same_identity(self):
        node=self.add();self.probe(node);adapter=self.adapter(node)
        identity=adapter.resource_identity()
        adapter.test_resource({},'TEST-second')
        starts=self.starts()
        self.assertEqual(len(starts),2)
        self.assertEqual(len({v['pid'] for v in starts}),2)
        self.assertEqual(adapter.resource_identity(),identity)
        self.assertEqual(adapter.terminal_snapshot()['turn'],2)
        self.assertTrue(self.node(node)['readiness']['ready'])

    def test_argv_matches_the_fixed_contract_and_carries_no_task_text(self):
        node=self.add(model='TEST not a model')
        self.probe(node);self.review(self.adapter(node))
        argv=self.starts()[0]['argv']
        self.assertEqual(argv,['--input-format','stream-json','--output-format','stream-json',
            '--model','TEST not a model','--mode','accept-edits','--sandbox','--print-timeout','90s'])
        self.assertEqual(self.starts()[1]['argv'][-1],'0s')
        for bad in ('-p','--print','--prompt','--dangerously-skip-permissions','--add-dir',
                    '--disable-slash-commands','models'):
            self.assertNotIn(bad,argv)
        self.assertFalse(any('XIAOBA_TASK_CARD' in a or '派工卡' in a or 'TEST authorized' in a for a in argv))

    def test_cwd_is_the_fixed_workspace_and_the_dispatch_forbids_old_task_search(self):
        node=self.add();self.probe(node)
        start=self.starts()[0]
        self.assertEqual(start['cwd'],str((self.workspaces/'用户自定名字').resolve()))
        self.assertIn('只执行这张卡；不要检索旧任务记录、其它日期的工作日志或小八源码。',start['prompt'])
        self.assertIn('XIAOBA_TASK_CARD=',start['prompt'])
        card=next((self.workspaces/'用户自定名字'/'任务记录').glob('*/派工卡-*.md')).read_text()
        self.assertIn('受控文件I/O探针，不是系统诊断',card)


class CliFailureTests(CliFixture,unittest.TestCase):
    def test_no_receipt_returns_missing_without_retry(self):
        (self.root/'no-receipt').touch();node=self.add();result=self.probe(node)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reason'],'receipt_missing')
        self.assertEqual(len(self.starts()),1)

    def test_partial_receipt_without_done_marker_is_not_accepted(self):
        (self.root/'partial-then-exit').touch();node=self.add();result=self.probe(node)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reason'],'receipt_missing')
        self.assertEqual(len(self.starts()),1)

    def test_wrong_task_id_in_receipt_is_rejected(self):
        (self.root/'wrong-task-id').touch();node=self.add();adapter=self.adapter(node)
        with self.assertRaises(WorkspaceError) as error:adapter.test_resource({},'TEST-challenge')
        self.assertEqual(error.exception.code,'worklog_task_mismatch')

    def test_rewriting_an_accepted_prefix_is_rejected(self):
        node=self.add();self.probe(node);adapter=self.adapter(node)
        self.assertEqual(self.review(adapter,'TASK-1','ASSIGN-1')['verdict'],'PASS')
        (self.root/'rewrite-history').touch()
        with self.assertRaises(WorkspaceError) as error:self.review(adapter,'TASK-2','ASSIGN-2')
        self.assertEqual(error.exception.code,'worklog_history_changed')

    def test_long_reply_is_collected_from_the_file(self):
        (self.root/'long-reply').touch();node=self.add();self.probe(node)
        value=self.review(self.adapter(node))
        self.assertGreater(len(value['findings']),6*1024)
        self.assertEqual(value['findings'].count('TEST long file reply'),300)
        self.assertEqual(value['execution_evidence']['reply_source'],'daily_worklog')

    def test_no_progress_kills_the_group_without_retry(self):
        (self.root/'stall').touch();node=self.add();adapter=self.adapter(node)
        # Low enough to stay fast, high enough that the child's own start-up is
        # never mistaken for "no progress" on a loaded machine.
        adapter.no_progress_seconds=1.5
        with self.assertRaises(InteractiveError) as error:adapter.test_resource({},'TEST-challenge')
        self.assertEqual(error.exception.code,'cli_no_progress')
        self.assertEqual(len(self.starts()),1)
        with self.assertRaises(ProcessLookupError):os.kill(self.starts()[0]['pid'],0)

    def test_nonzero_exit_and_failed_status_are_classified_without_raw_stderr(self):
        node=self.add()
        for marker,expected in (('nonzero-exit','transient'),('fail-status','permanent')):
            with self.subTest(marker=marker):
                (self.root/marker).touch()
                result=self.probe(node,'TEST-'+marker)
                self.assertEqual(result['status'],'failed',result)
                self.assertEqual(result['reason'],'cli_failed')
                data=json.loads(self.receipts()[-1].read_text())
                self.assertEqual(data['execution_evidence']['failure_class'],expected)
                stored=json.dumps(data,ensure_ascii=False)
                self.assertNotIn('TEST transient failure',stored)
                self.assertNotIn('TEST permanent failure',stored)
                (self.root/marker).unlink()

    def test_trust_prompt_blocks_without_automatic_confirmation(self):
        (self.root/'trust-prompt').touch();node=self.add();result=self.probe(node)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'native_workspace_trust_required')
        state=self.node(node)
        self.assertEqual(state['test']['status'],'blocked')
        self.assertEqual(state['test']['reason'],'native_workspace_trust_required')
        self.assertIsNone(state['pending_action'])
        self.assertEqual(len(self.starts()),1)
        self.assertEqual([v['extra_stdin'] for v in self.starts()],[''])

    def test_receipt_before_exit_terminates_the_lingering_process(self):
        (self.root/'linger-after-receipt').touch();node=self.add();adapter=self.adapter(node)
        adapter.receipt_grace_seconds=.4
        result=adapter.test_resource({},'TEST-challenge')
        self.assertEqual(result['output'],'TEST-challenge')
        self.assertTrue(result['process_terminated_after_receipt'])
        self.assertTrue(json.loads(self.receipts()[-1].read_text())
            ['execution_evidence']['process_terminated_after_receipt'])
        with self.assertRaises(ProcessLookupError):os.kill(self.starts()[0]['pid'],0)

    def test_helpers_left_in_the_call_group_are_ended_after_agy_exits(self):
        (self.root/'orphan-helper').touch();node=self.add();adapter=self.adapter(node)
        result=adapter.test_resource({},'TEST-challenge')
        self.assertEqual(result['output'],'TEST-challenge')
        self.assertNotIn('process_terminated_after_receipt',result)
        helper=int((self.root/'helper.pid').read_text())
        for _ in range(100):
            try:
                os.kill(helper,0)
                # A dead child of the exited agy may linger as a zombie for init.
                state=os.popen('ps -o stat= -p %d'%helper).read().strip()
                if not state or state.startswith('Z'):break
            except ProcessLookupError:break
            time.sleep(.02)
        else:
            os.kill(helper,9);self.fail('helper outlived the per-step call')

    def test_stdout_flood_hits_the_output_limit(self):
        (self.root/'stdout-flood').touch();node=self.add();adapter=self.adapter(node)
        with self.assertRaises(InteractiveError) as error:adapter.test_resource({},'TEST-challenge')
        self.assertEqual(error.exception.code,'cli_output_limit')
        self.assertEqual(len(self.starts()),1)


class CliSessionTests(CliFixture,unittest.TestCase):
    def test_same_task_step_replays_without_a_new_process(self):
        node=self.add();self.probe(node);runtime=self.pool.runtime
        payload=dict(node_id=node,expected_revision=self.node(node)['revision'],task_id='TASK-general',
            step_code='STEP-01',instruction='TEST authorized work in this owned directory',
            materials={'goal':'TEST fixture'})
        first=runtime.execute_work(**payload);calls=len(self.calls())
        second=runtime.execute_work(**payload)
        self.assertEqual(first['body'],second['body'])
        self.assertEqual(len(self.calls()),calls)
        self.assertTrue(second['execution_evidence']['replayed'])
        self.assertFalse(first['score_assigned'])

    def test_parallel_requests_are_serialized_one_process_at_a_time(self):
        node=self.add();adapter=self.adapter(node)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(adapter.test_resource,{},'TEST-'+str(i)) for i in range(2)]
            results=[f.result(30) for f in futures]
        self.assertEqual(sorted(r['output'] for r in results),['TEST-0','TEST-1'])
        self.assertEqual(len(self.starts()),2)
        self.assertEqual(adapter.terminal_snapshot()['turn'],2)

    def test_retire_rejects_new_tasks_and_a_new_adapter_requires_a_new_test(self):
        node=self.add();self.probe(node);adapter=self.adapter(node)
        adapter.retire()
        self.assertEqual(adapter.terminal_snapshot()['state'],'closed')
        with self.assertRaises(InteractiveError) as error:adapter.test_resource({},'TEST-after')
        self.assertEqual(error.exception.code,'session_closed')
        self.assertEqual(len(self.starts()),1)
        self.backends.close()
        fresh=self.backends.adapter_for(self.source(node))
        self.assertIsNot(fresh,adapter)
        self.assertEqual(self.node(node)['readiness']['reason'],'test_required')
        self.assertFalse(self.node(node)['readiness']['ready'])

    def test_terminal_snapshot_is_read_only_and_shows_the_receipt_tail(self):
        node=self.add();adapter=self.adapter(node)
        empty=adapter.terminal_snapshot()
        self.assertEqual(empty['state'],'not_started')
        self.assertTrue(empty['supported']);self.assertTrue(empty['read_only'])
        self.assertEqual(self.calls(),[])
        self.probe(node)
        snap=adapter.terminal_snapshot()
        self.assertEqual(snap['state'],'receipt_received')
        self.assertEqual(snap['turn'],1)
        self.assertEqual(snap['session_id'],adapter._call_id)
        self.assertIn('第1次调用',snap['text'])
        self.assertIn('状态: completed',snap['text'])
        self.assertIn('<!-- done:',snap['text'])
        self.assertNotIn('XB_STDOUT',snap['text'])
        self.assertNotIn('XB_STDERR',snap['text'])
        self.assertLessEqual(len(snap['text'].splitlines()),41)

    def test_factory_routes_cli_by_default_and_interactive_on_request(self):
        node=self.add();source=self.source(node)
        self.assertIsInstance(json_stdio_factory(source,self.workspaces),AgyCliResource)
        self.assertIsInstance(json_stdio_factory(source,self.workspaces,agy_mode='cli'),AgyCliResource)
        self.assertIsInstance(json_stdio_factory(source,self.workspaces,agy_mode='interactive'),
            AgyInteractiveResource)
        source['node']['entry']=str(self.root/'custom-json-resource')
        self.assertIsInstance(json_stdio_factory(source,self.workspaces),JsonStdioResource)
        with self.assertRaises(ValueError):
            json_stdio_factory(self.source(node),self.workspaces,agy_mode='other')
