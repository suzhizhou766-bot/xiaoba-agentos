"""Real terminal child-process fixtures. No model or human approval is used."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import textwrap
import unittest

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.resource_agy import AgyInteractiveResource, InteractiveError
from xiaoba_review.resource_stdio import JsonStdioResource, json_stdio_factory


class InteractiveFixture(fixtures.FixtureMixin):
    def setUp(self):
        super().setUp()
        self.workspaces = self.root/'workspaces';self.workspaces.mkdir()
        self.backends = BackendRegistry(self.store, resource_factory=lambda source:json_stdio_factory(
            source,self.workspaces,agy_mode='interactive'))
        self.addCleanup(self.backends.close)
        self.pool = AgentPool(self.store,self.backends)
        self.entry = self.root/'agy'
        self.entry.write_text('#!'+sys.executable+'\n'+textwrap.dedent(r'''
            import json,os,re,sys,time,tty
            from pathlib import Path
            root=Path(__file__).parent
            def log(v):
                with (root/'calls').open('a') as f:f.write(json.dumps(v)+'\n')
            models={'gemini-3.8-flash-high':'Gemini 3.8 Flash (High)',
                    'gemini-3.8-flash-low':'Gemini 3.8 Flash (Low)'}
            assert sys.argv[1:]!=['models'], 'The user forbids model discovery'
            assert not any(x in sys.argv for x in ('--print','-p','--input-format','--dangerously-skip-permissions'))
            assert sys.argv[sys.argv.index('--mode')+1]=='accept-edits' and '--sandbox' in sys.argv
            model=sys.argv[sys.argv.index('--model')+1] if '--model' in sys.argv else 'gemini-3.8-flash-high'
            log({'event':'start','pid':os.getpid(),'argv':sys.argv[1:],'cwd':os.getcwd()})
            tty.setraw(0)
            def out(s):os.write(1,s.encode())
            if (root/'require-trust').exists():
                out('Antigravity CLI TEST\r\nDo you trust the contents of this project?\r\n> Yes, I trust this folder\r\n')
                if os.read(0,1)!=b'\r':sys.exit(1)
                log({'event':'trust_accepted','pid':os.getpid()})
                # Keep the old trust prompt visible while readiness arrives
                # in another PTY chunk, as a native TUI redraw can do.
                out('Preparing interactive prompt...\r\n');time.sleep(.04)
            ready='> Plan mode: research & plan only (shift+tab to cycle)' if (root/'plan-prompt').exists() else '> '
            if (root/'accept-edits-prompt').exists():ready='> Accept-edits mode: file edits auto-approved (shift+tab to cycle)'
            out('\x1b[36mAntigravity CLI TEST fixture\x1b[0m\r\nUnrelated banner, no model identification\r\n'+ready)
            pending=b''
            while True:
                chunk=os.read(0,65536)
                if not chunk:break
                pending+=chunk
                if b'\x1b[201~\r' not in pending:continue
                prompt,pending=pending.split(b'\x1b[201~\r',1)
                prompt=prompt.removeprefix(b'\x1b[200~').decode()
                log({'event':'turn','pid':os.getpid()})
                while (root/'hold').exists():time.sleep(.02)
                # Echo the task as a TUI would: it must not be a result.
                out('\r\n> '+prompt+'\r\n')
                cardpath=Path(json.loads(prompt.split('XIAOBA_TASK_CARD=',1)[1]))
                card=cardpath.read_text()
                logpath=Path(re.search(r'^工作回执文件: (.+)$',card,re.M)[1])
                taskid=re.search(r'^任务ID: (.+)$',card,re.M)[1]
                step=re.search(r'^步骤码: (.+)$',card,re.M)[1]
                nonce=re.search(r'^本轮标识: (.+)$',card,re.M)[1]
                context=json.loads(card.split('## 材料\n',1)[1].split('\n\n## 范围与回执',1)[0])
                if (root/'stall').exists():continue
                if (root/'bad-json').exists():value='TEST incorrect challenge, plain Markdown is allowed'
                elif 'challenge' in context:value=context['challenge']
                else:value='TEST terminal fixture; no real review.'
                if (root/'long-reply').exists():value+='\n'+'TEST long file reply\n'*300
                time.sleep(.02)
                # The terminal deliberately does NOT contain the full result.
                if (root/'repaint').exists():
                    out('old animation\r\n\x1b[1A\r\x1b[2Kworking\r\n')
                if (root/'trailing-spinner').exists():out('XB_END_fake animation is not a receipt\r\n')
                if not logpath.exists():
                    header=card.split('先写以下文件头：\n\n',1)[1].split('\n文件已存在时',1)[0]
                    logpath.write_text(header)
                meta='任务ID: '+taskid+'\n步骤码: '+step+'\n状态: completed\n'
                if step=='REVIEW-01' and not (root/'bad-review-fields').exists():
                    meta+='审查结论: PASS\n证据引用: TEST child PTY file receipt\n'
                done='<!-- done:'+nonce+' -->\n'
                with logpath.open('a') as f:
                    f.write('\n<!-- task:'+nonce+' -->\n'+meta+'---\n'+value+'\n');f.flush()
                    if (root/'partial-file').exists():time.sleep(.2)
                    f.write(done);f.flush()
                if (root/'busy-footer').exists():
                    out('> \r\nesc to cancel')
                    time.sleep(.15);out('\r\x1b[2K\x1b[1A\r'+ready)
                    continue
                out('File receipt written.\r\n'+ready)
        '''))
        self.entry.chmod(0o700)

    def add(self, node_id='TEST-interactive', model='Gemini 3.8 Flash'):
        source=self.pool.add({'node_id':node_id,'name':'用户自定名字','agent':'custom', 'kind':'peer',
            'interface':'cli','entry':str(self.entry),'model':model})
        self.pool.change(node_id,source['node']['revision'],'toggle',True)
        return node_id

    def source(self,node):
        with self.native.read_only() as db:return self.store.source(db,node)

    def node(self,node):return next(v for v in self.pool.state()['nodes'] if v['node_id']==node)

    def probe(self,node,test_id='TEST-probe'):
        return self.pool.runtime.test_run(node,self.node(node)['revision'],test_id)

    def calls(self):
        p=self.root/'calls'
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

class InteractiveTests(InteractiveFixture, unittest.TestCase):
    def test_actual_accept_edits_prompt_dispatches_the_file_task(self):
        (self.root/'accept-edits-prompt').touch();node=self.add()
        result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual([v['event'] for v in self.calls()],['start','turn'])

    def test_real_pty_challenge_and_same_resource_are_bound_to_config(self):
        node=self.add();result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        evidence=result['execution_evidence']
        self.assertEqual(evidence['model'],'Gemini 3.8 Flash')
        self.assertEqual(evidence['model_source'],'node_configuration')
        # 用户 2026-11-01 裁决后新回执不再写 model_verification（旧回执仍能读）。
        self.assertNotIn('model_verification',evidence)
        self.assertEqual(result['reason'],'challenge_completed_configured_node')
        self.assertEqual(evidence['output'],evidence['challenge'])
        self.assertEqual(self.pool.runtime.prepare(['peer'])['route'],'run')
        one=self.backends.adapter_for(self.source(node));two=self.backends.adapter_for(self.source(node))
        self.assertIs(one,two)
        self.assertEqual(len([x for x in self.calls() if x['event']=='start']),1)
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_two_actual_turns_reuse_one_pid_and_do_not_use_print(self):
        node=self.add();self.probe(node)
        adapter=self.backends.adapter_for(self.source(node))
        value=adapter.run({'card_digest':'TEST','resource_plan':{'model':'Gemini 3.8 Flash'}})
        self.assertEqual(value['verdict'],'PASS')
        self.assertEqual(value['execution_evidence']['turn'],2)
        self.assertEqual(len({x['pid'] for x in self.calls()}),1)
        self.assertEqual([x['event'] for x in self.calls()],['start','turn','turn'])

    def test_parallel_requests_are_serialized_in_one_session(self):
        node=self.add();adapter=self.backends.adapter_for(self.source(node))
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(adapter.test_resource,{},'TEST-'+str(i)) for i in range(2)]
            results=[f.result(8) for f in futures]
        self.assertEqual([r['output'] for r in results],['TEST-0','TEST-1'])
        self.assertEqual(sorted(r['turn'] for r in results),[1,2])
        self.assertEqual(len({x['pid'] for x in self.calls()}),1)

    def test_user_model_is_forwarded_verbatim_without_lookup_or_guess(self):
        node=self.add(model='TEST not a model')
        result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual(self.calls()[0]['argv'][-2:],['--model','TEST not a model'])
        self.assertEqual(result['execution_evidence']['model'],'TEST not a model')
        self.assertNotIn('model_verification',result['execution_evidence'])

    def test_explicit_model_is_session_only_and_not_claimed_as_verified(self):
        node=self.add(model='gemini-3.8-flash-low');result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual(result['execution_evidence']['model'],'gemini-3.8-flash-low')
        self.assertNotIn('model_verification',result['execution_evidence'])
        self.assertEqual(self.calls()[0]['argv'][-2:],['--model','gemini-3.8-flash-low'])

    def test_incorrect_challenge_in_file_is_rejected_without_retry(self):
        (self.root/'bad-json').touch();node=self.add();result=self.probe(node)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reason'],'challenge_mismatch')
        self.assertIsNone(result['execution_evidence'])
        self.assertEqual([x['event'] for x in self.calls()],['start','turn'])
        self.assertFalse(self.node(node)['readiness']['ready'])

    def test_timeout_closes_owned_group_without_automatic_retry(self):
        (self.root/'stall').touch();node=self.add();adapter=self.backends.adapter_for(self.source(node))
        with self.assertRaises(InteractiveError) as error:
            adapter._turn('TEST',json.dumps({'challenge':'TEST'}),timeout=.2)
        self.assertEqual(error.exception.code,'interactive_timeout')
        self.assertEqual([x['event'] for x in self.calls()],['start','turn'])
        with self.assertRaises(ProcessLookupError):os.kill(self.calls()[0]['pid'],0)

    def test_disable_ends_idle_session_and_reenable_requires_new_test(self):
        node=self.add();self.probe(node);pid=self.calls()[0]['pid']
        self.pool.change(node,self.node(node)['revision'],'toggle',False)
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)
        self.pool.change(node,self.node(node)['revision'],'toggle',True)
        self.assertFalse(self.node(node)['readiness']['ready'])
        self.assertEqual(len(self.calls()),2)  # No reconnect/test merely by opening.

    def test_process_restart_does_not_inherit_an_old_live_session_pass(self):
        node=self.add();self.probe(node);self.backends.close()
        self.assertFalse(self.node(node)['readiness']['ready'])
        self.assertEqual(len(self.calls()),2)

    def test_selection_uses_entry_not_user_display_name_and_keeps_other_protocol(self):
        node=self.add()
        self.assertIsInstance(json_stdio_factory(self.source(node),agy_mode='interactive'),AgyInteractiveResource)
        source=self.source(node);source['node']['entry']=str(self.root/'custom-json-resource')
        self.assertIsInstance(json_stdio_factory(source),JsonStdioResource)

    def test_native_consent_blocks_without_any_task_or_automatic_confirmation(self):
        (self.root/'require-trust').touch();node=self.add();result=self.probe(node)
        self.assertEqual(result['status'],'blocked')
        self.assertEqual(result['reason'],'native_workspace_trust_required')
        state=self.node(node)
        self.assertEqual(state['readiness']['reason'],'native_confirmation_required')
        self.assertTrue(state['pending_action']['directory'].startswith('/'))
        self.assertEqual([x['event'] for x in self.calls()],['start'])
        self.probe(node,'TEST-probe-again')
        self.assertEqual([x['event'] for x in self.calls()],['start'])

    def test_explicit_workspace_confirmation_is_scoped_and_does_not_dispatch(self):
        (self.root/'require-trust').touch();node=self.add();self.probe(node)
        state=self.node(node);pending=state['pending_action']
        inputs=dict(node_id=node,expected_revision=state['revision'],session_id=pending['session_id'],decision='trust')
        with self.assertRaises(ValueError):
            self.pool.runtime.confirm_interactive_workspace(action_id='wrong',**inputs)
        self.assertEqual([x['event'] for x in self.calls()],['start'])
        result=self.pool.runtime.confirm_interactive_workspace(action_id=pending['action_id'],**inputs)
        self.assertEqual(result['model_tasks_sent'],0)
        self.assertEqual([x['event'] for x in self.calls()],['start','trust_accepted'])
        adapter=self.backends.adapter_for(self.source(node))
        self.assertIsNone(adapter.pending_action)
        self.assertEqual(adapter.terminal_snapshot()['state'],'idle')
        self.assertEqual(adapter.terminal_snapshot()['turn'],0)
        self.assertFalse(self.node(node)['readiness']['ready'])
        self.assertEqual(self.probe(node,'TEST-after-consent')['status'],'passed')
        self.assertEqual(self.count('memory_reviews'),1)  # Isolated authored fixture's setup only.
        self.assertTrue(self.native.verify_event_chain()['ok'])
