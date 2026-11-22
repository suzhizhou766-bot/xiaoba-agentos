"""Product CLI flows with real Seatbelt execution and synthetic TEST reviewer adapters."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from fixtures_review import _admit_authored_candidate, _config,_run_cli
from fixtures_validation import bound_diff
from test_pool_dispatch import _seed, _source
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.store import EventStore
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.experience_workflow import verify_final_qualification,execute_trial


class _TrialPoolResource:
    def __init__(self, source, **_kwargs):
        self.source = source

    def run_task(self, task_id, _step_code, _instruction, _materials):
        verdict = self.source["node"]["template"].split("TRIAL_VALIDATION: ", 1)[-1]
        return {
            "status": "completed",
            "body": "审查结论：PASS\n评分：8\n重复经验：无\nFINAL_ACCEPTANCE: PASS\n"
                    "TRIAL_VALIDATION: " + verdict + "\nTEST independent pool review",
            "execution_evidence": {"provider": "TEST", "route_source": "TEST fixture"},
        }


def _trial_pool_factory(source, _workspace=None, **kwargs):
    return _TrialPoolResource(source, **kwargs)


class CliValidationFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve(); self.cfg=_config(self.root)
        raw=json.loads(self.cfg.path.read_text()); raw['release']={'expected_version':'test-mock'}
        for agent,name in zip(raw['agents'],('agy_review','dsh_review')):
            agent['name']=name;agent['template']='TEST independent reviewer\nTRIAL_VALIDATION: PASS'
        self.cfg.path.write_text(json.dumps(raw));self.cfg=AppConfig.load(self.cfg.path)
        self.store=EventStore(self.cfg.database_path);self.store.initialize();self.store.apply_validation_extension()
        _seed(self.store, [
            _source('TEST-validation-review', 'cli:TEST-review', 'TEST model review', kind='review'),
            _source('TEST-validation-both', 'cli:TEST-both', 'TEST model both', kind='both'),
        ])
        self.c=self.store.create_authored_candidate('TEST候选','仅用于CLI流程测试',self.cfg.runtime_dir/'memory-outbox',author='test',experience_kind='method')
        self.c=_admit_authored_candidate(self.store,self.c)
        self.store.review_memory(self.c['task_id'],'delivery','approve','TEST初审',self.c['evidence_sha256'])
        self.count=0

    def cli(self,*args): return _run_cli(['--config',str(self.cfg.path),*args])

    def card(self):
        self.count+=1; ws=self.cfg.runtime_dir/'trial-workspaces'/str(self.count);ws.mkdir(parents=True)
        value={'title':'CLI隔离测试','problem_key':'cli-fixture','objective':'核查正常产品入口','scope':'仅TEST夹具','acceptance':'真实断言和独立审查全通过','stop_conditions':'失败停止采用','workspace':str(ws),
               'cases':[{'argv':[str(Path(sys.executable).resolve()),'-I','-B','-c',"assert 6*7==42; print('CLI_CASE_PASS')"],'expect_stdout':'CLI_CASE_PASS'}]}
        path=self.root/(str(self.count)+'-card.json');path.write_text(json.dumps(value));return path

    def run_trial(self,verdict='PASS',settle=True):
        with self.store.connect() as db:
            rows = db.execute('SELECT id,data FROM ri_sources ORDER BY id').fetchall()
            for row in rows:
                source = json.loads(row['data'])
                source['node']['template'] = 'TEST independent reviewer\nTRIAL_VALIDATION: ' + verdict
                db.execute('UPDATE ri_sources SET data=? WHERE id=?', (json.dumps(source), row['id']))
        code,out=self.cli('memory-trial-run',self.c['task_id'],'--card',str(self.card()))
        self.assertEqual(code,0,out)
        if settle:self.assertEqual(self.settle_trial()['status'],'completed')
        return out

    def settle_trial(self):
        with patch('xiaoba_agentos.adapters._pool_cli_factory', return_value=_trial_pool_factory):
            return Orchestrator(self.cfg,self.store).run_once()

    def gate(self):
        with self.store.read_only() as db:return verify_final_qualification(db,self.c['task_id'])

    def test_first_real_cli_round_has_bound_receipt_and_task(self):
        queued=self.run_trial();self.assertTrue(self.gate()['qualified']);rec=self.store.get_validation_records(self.c['task_id'])[0]
        self.assertEqual(rec['task_id'],queued['validation_task_id']);self.assertTrue(Path(queued['execution_receipt']).is_file())

    def test_second_cli_round_then_bound_materials_without_extra_execution(self):
        self.run_trial('FAIL');self.run_trial();self.assertEqual(self.gate()['state'],'diff_materials_required')
        path=self.root/'diff.json';path.write_text(json.dumps(bound_diff(self.store,self.c['task_id'])))
        with patch('xiaoba_agentos.experience_workflow.execute_trial',wraps=execute_trial) as execute:
            for _ in range(2):
                code,out=self.cli('memory-trial-diff',self.c['task_id'],'--materials',str(path));self.assertEqual(code,0,out)
            execute.assert_not_called()
        self.assertTrue(self.gate()['qualified']);self.assertEqual(self.gate()['rounds_used'],2)

    def test_queued_repeated_cli_blocks_before_execution(self):
        self.run_trial(settle=False)
        with patch('xiaoba_agentos.experience_workflow.execute_trial',wraps=execute_trial) as execute:
            for _ in range(2):
                code,out=self.cli('memory-trial-run',self.c['task_id'],'--card',str(self.card()));self.assertEqual(code,1,out)
            execute.assert_not_called()
        self.assertEqual(len(self.store.get_validation_records(self.c['task_id'])),1)
        self.assertEqual(self.store.get_validation_lineage(self.c['task_id'])['rounds_used'],0)

    def test_fail_fail_third_cli_is_refused_before_execution(self):
        self.run_trial('FAIL');self.run_trial('FAIL')
        with patch('xiaoba_agentos.experience_workflow.execute_trial',wraps=execute_trial) as execute:
            code,out=self.cli('memory-trial-run',self.c['task_id'],'--card',str(self.card()));self.assertEqual(code,1,out);execute.assert_not_called()
        self.assertEqual(self.store.get_memory_candidate(self.c['task_id'])['status'],'retired')

    def test_cli_pause_resume_preserves_original_task_and_round(self):
        first=self.run_trial(settle=False);rec=self.store.get_validation_records(self.c['task_id'])[0]
        code,out=self.cli('memory-trial-pause',self.c['task_id'],'--attempt-id',rec['id'],'--reason','TEST暂停');self.assertEqual(code,0,out)
        code,out=self.cli('memory-trial-resume',self.c['task_id']);self.assertEqual(code,0,out);self.assertEqual(out['validation_task_id'],first['validation_task_id'])
        self.assertEqual(self.settle_trial()['status'],'completed');self.assertTrue(self.gate()['qualified'])

    def test_future_schema_or_missing_column_stops_before_execution(self):
        for mutation in ("UPDATE experience_validation_meta SET value='999' WHERE key='version'",'ALTER TABLE experience_validation_records DROP COLUMN execution_digest'):
            with self.subTest(mutation=mutation):
                with self.store.connect() as db:db.execute(mutation)
                with patch('xiaoba_agentos.experience_workflow.execute_trial',wraps=execute_trial) as execute:
                    code,out=self.cli('memory-trial-run',self.c['task_id'],'--card',str(self.card()));self.assertEqual(code,1,out);execute.assert_not_called()
                self.store.rollback_validation_extension();self.store.apply_validation_extension()

    def test_unmigrated_database_stops_before_execution(self):
        self.store.rollback_validation_extension()
        with patch('xiaoba_agentos.experience_workflow.execute_trial',wraps=execute_trial) as execute:
            code,out=self.cli('memory-trial-run',self.c['task_id'],'--card',str(self.card()));self.assertEqual(code,1,out);execute.assert_not_called()

    def test_conflicting_pass_fail_markers_do_not_qualify(self):
        self.run_trial('PASS\nTRIAL_VALIDATION: FAIL');self.assertFalse(self.gate()['qualified'])

    def test_broken_chain_stops_before_execution(self):
        with self.store.connect() as db:db.execute("UPDATE events SET payload_json='{}' WHERE sequence=1")
        with patch('xiaoba_agentos.experience_workflow.execute_trial',wraps=execute_trial) as execute:
            code,out=self.cli('memory-trial-run',self.c['task_id'],'--card',str(self.card()));self.assertEqual(code,1,out);execute.assert_not_called()


    def test_abandon_returns_the_trial_attempt_to_the_same_round_without_delivery(self):
        first=self.run_trial(settle=False)
        trial_task=first['validation_task_id']
        original=self.store.get_validation_records(self.c['task_id'])[0]
        frozen=self.store.freeze_task(trial_task,failure_class='external_upstream',
            failures=[{'agent':'TEST reviewer','error':'TEST user stopped this trial','external':True}])
        code,out=self.cli('task-abandon',trial_task,'--freeze-hash',frozen['freeze_hash'])
        self.assertEqual(0,code,out)
        record=self.store.get_validation_records(self.c['task_id'])[0]
        lineage=self.store.get_validation_lineage(self.c['task_id'])
        self.assertEqual('pending',record['status'])
        self.assertIsNone(record['task_id'])
        self.assertIsNone(record['execution_digest'])
        self.assertEqual(1,record['round'])
        self.assertEqual((1,0,'pending_round_1'),
            (lineage['current_round'],lineage['rounds_used'],lineage['lineage_status']))
        with self.store.read_only() as db:
            released=db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.validation_attempt_released'",
                (self.c['task_id'],)).fetchone()
            deliveries=db.execute("SELECT 1 FROM events WHERE task_id=? AND event_type='task.delivery_recorded'",
                (trial_task,)).fetchall()
        self.assertIsNotNone(released)
        self.assertEqual(trial_task,json.loads(released[0])['abandoned_task_id'])
        self.assertEqual(original['id'],json.loads(released[0])['attempt_id'])
        self.assertEqual([],deliveries)

        body=self.root/'abandoned-trial-method.md'
        receipt=Path(first['execution_receipt'])
        body.write_text('# TEST 新经验\n\n来自未交付试错的执行证据：%s\nSHA-256: %s\n' % (
            receipt, __import__('hashlib').sha256(receipt.read_bytes()).hexdigest()),encoding='utf-8')
        code,new_candidate=self.cli('memory-candidate-create','--title','TEST 未交付试错产生的方法',
            '--body-file',str(body),'--author','TEST host','--kind','method','--source-task-id',trial_task,
            '--problem-key','trial-abandon-fixture')
        self.assertEqual(0,code,new_candidate)
        self.assertEqual('awaiting_value_review',new_candidate['status'])
        with self.store.read_only() as db:
            authored=json.loads(db.execute("SELECT payload_json FROM events WHERE task_id=? AND event_type='memory.candidate_authored'",
                (new_candidate['task_id'],)).fetchone()[0])
        self.assertEqual([trial_task],authored['source_task_ids'])

        second=self.run_trial(settle=False)
        retried=self.store.get_validation_records(self.c['task_id'])[0]
        self.assertEqual(1,retried['round'])
        self.assertEqual('running',retried['status'])
        self.assertEqual(second['validation_task_id'],retried['task_id'])
        self.assertNotEqual(original['id'],retried['id'])
