import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from xiaoba_agentos.task_context import freeze_context, render_context, read_context_file
from xiaoba_agentos.cli import _submit_task, build_parser
from xiaoba_agentos.bridge import _submit, dispatch
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.adapters import AgentResult
from xiaoba_agentos.store import EventStore
import test_orchestrator

class TaskContextTest(unittest.TestCase):
    def test_input_and_integrity(self):
        for value in [None, 'x' * 65537, 'Authorization: Bearer dummy-test-secret']:
            with self.assertRaises(ValueError): freeze_context(value)
        record = freeze_context('来源：doctor，2026-09-08；ok=true')
        self.assertIn('doctor', render_context(record))
        record['text'] += 'changed'
        with self.assertRaises(ValueError): render_context(record)
        self.assertEqual('', render_context(None))

    def test_file_is_frozen_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'context.txt'
            path.write_text('original')
            frozen = freeze_context(read_context_file(path))
            path.write_text('changed')
            self.assertIn('original', render_context(frozen))
            path.write_text('x' * 65537)
            with self.assertRaises(ValueError): read_context_file(path)
        for command in ['intake','submit']:
            self.assertEqual('proof.txt', build_parser().parse_args([command,'task','--context-file','proof.txt']).context_file)

    def test_both_submission_paths_reach_both_agents(self):
        for submitter, module in [(_submit_task, 'cli'), (_submit, 'bridge')]:
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                cfg=test_orchestrator.OrchestratorTest()._config(root,[{'name':n,'type':'mock','enabled':True,'role':'','timeout_seconds':1} for n in ['a','b']])
                cfg.raw["release"]={"expected_version":"1.3.2"}
                store=EventStore(cfg.database_path); store.initialize()
                with patch('xiaoba_agentos.'+module+'.MemoryBridge') as memory, patch('xiaoba_agentos.'+module+'.SkillRegistry') as skills:
                    memory.return_value.agent_contexts.return_value={'hits':[], 'contexts':{'a':'memory-A','b':'memory-B'}}
                    memory.return_value.render_hits.return_value='fallback memory'
                    skills.return_value.select_context.return_value={'selected':[], 'context':'skill text','context_sha256':'test'}
                    kwargs=dict(task_context='source: doctor at test-time; ok=true',session_id=None,route_decision_id=None)
                    task_id=submitter(cfg,store,'diagnose',**kwargs) if module=='cli' else submitter('diagnose',store,cfg,**kwargs)
                captured={}
                class Adapter:
                    def __init__(self, agent): self.agent=agent
                    def run(self,prompt,context):
                        captured[self.agent.name]=(prompt,context)
                        return AgentResult(self.agent.name,'diagnose verified: doctor ok=true; source test-time',1)
                with patch('xiaoba_agentos.orchestrator.build_adapter',side_effect=Adapter):
                    result=Orchestrator(cfg,store).run_once()
                self.assertEqual('completed',result['status'])
                for name in ['a','b']:
                    self.assertEqual('diagnose',captured[name][0])
                    self.assertIn('doctor at test-time',captured[name][1])
                    self.assertIn('memory-'+name.upper(),captured[name][1])
                    self.assertNotIn('memory-'+('B' if name=='a' else 'A'),captured[name][1])
                self.assertIn('task_context',json.loads(store.get_task(task_id)['execution_snapshot_json']))

    def test_tampered_context_never_calls_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            cfg=test_orchestrator.OrchestratorTest()._config(root,[{'name':n,'type':'mock','enabled':True,'role':'','timeout_seconds':1} for n in ['a','b']])
            store=EventStore(cfg.database_path);store.initialize()
            record=freeze_context('original');record['text']='tampered'
            store.submit('test',2,snapshot={'task_context':record})
            with patch('xiaoba_agentos.orchestrator.build_adapter') as build:
                self.assertEqual('failed',Orchestrator(cfg,store).run_once()['status'])
                build.assert_not_called()

    def test_bridge_dispatch_forwards_context_once(self):
        for command in ['submit', 'intake']:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as directory:
                store=EventStore(Path(directory)/'store.sqlite3');store.initialize()
                request={'command':command,'prompt':'execute bounded work','task_context':'source: verified fixture','submit':True,'requires_action':True}
                with patch('xiaoba_agentos.bridge._submit',return_value='task-test') as submit, patch('xiaoba_agentos.bridge.execution_blockers',return_value=[]):
                    result=dispatch(request,store,MagicMock())
                self.assertTrue(result['ok'],result)
                submit.assert_called_once()
                self.assertEqual('source: verified fixture',submit.call_args.kwargs['task_context'])
