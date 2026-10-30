"""File-return and weekly-scoring acceptance with owned real child processes."""
from datetime import datetime,timedelta
import json
import os
import time
import unittest
import threading
from zoneinfo import ZoneInfo

from test_agy_cli import CliFixture as InteractiveFixture
from xiaoba_review.agent_workspace import WorkspaceError
from xiaoba_review.resource_agy import InteractiveError,ReviewReceiptError
from xiaoba_review.store import Conflict
import test_review as fixtures


def today():return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


class FileReceiptTests(InteractiveFixture,unittest.TestCase):
    def test_health_card_is_only_file_io_and_not_agent_self_diagnosis(self):
        adapter,record,ticket=self.ticket()
        probe=adapter.workday.prepare(record,'TASK-PROBE','HEALTH-01','TEST file I/O only',{'challenge':'TEST'},health_only=True)
        card=probe['card'].read_text()
        self.assertIn('受控文件I/O探针，不是系统诊断',card)
        self.assertIn('不为本卡运行shell命令',card)
        self.assertIn('步骤码只是本卡关联编号',card)
        self.assertNotIn('上述当日工作文件中追加本次工作',card)
        self.assertIn('指定回执文件不存在时，由你直接新建',card)
        self.assertIn('---\nTEST\n<!-- done:',card)
        self.assertNotIn('本次过程、实际步骤、结果、产物引用、验证、返工和限制写入本节',card)

    def ticket(self,task='TASK-1',step='STEP-1'):
        node=self.add();adapter=self.backends.adapter_for(self.source(node));workspace=adapter.workspace
        record=workspace.begin('review',task,{'task_id':task,'model':'Gemini 3.8 Flash'})
        ticket=adapter.workday.prepare(record,task,step,'TEST authorized fixture',{})
        return adapter,record,ticket

    def write(self,ticket,body='TEST complete Markdown',task=None,step=None,done=True):
        prefix=ticket['log'].read_bytes()[:ticket['prefix_size']].decode() if ticket['prefix_size'] else ticket['header']
        value=(prefix+'\n'+ticket['marker']+'\n任务ID: '+(task or ticket['task_id'])+
            '\n步骤码: '+(step or ticket['step_code'])+'\n状态: completed\n---\n'+body+'\n')
        if done:value+=ticket['done']+'\n'
        ticket['log'].write_text(value)

    def test_partial_file_waits_for_done_and_plain_markdown_needs_no_json(self):
        adapter,record,ticket=self.ticket();self.write(ticket,done=False)
        self.assertIsNone(adapter.workday.read(ticket))
        with ticket['log'].open('a') as out:out.write(ticket['done']+'\n')
        value=adapter.workday.read(ticket)
        self.assertEqual(value['body'],'TEST complete Markdown')
        self.assertEqual(value['worklog']['task_id'],'TASK-1')

    def test_wrong_task_id_and_step_code_cannot_be_received(self):
        for field in ('task','step'):
            with self.subTest(field=field):
                adapter,record,ticket=self.ticket('TASK-'+field)
                self.write(ticket,**{field:'WRONG'})
                with self.assertRaises(WorkspaceError) as error:adapter.workday.read(ticket)
                self.assertEqual(error.exception.code,'worklog_task_mismatch')

    def test_symlink_and_hardlink_receipts_cannot_read_external_files(self):
        adapter,record,ticket=self.ticket();external=self.root/'outside';external.write_text('TEST outside')
        ticket['log'].symlink_to(external)
        with self.assertRaises(WorkspaceError):adapter.workday.read(ticket)
        ticket['log'].unlink();os.link(external,ticket['log'])
        with self.assertRaises(WorkspaceError):adapter.workday.read(ticket)
        self.assertEqual(external.read_text(),'TEST outside')

    def test_accepted_prefix_can_be_appended_but_cannot_be_rewritten(self):
        adapter,record,ticket=self.ticket();self.write(ticket)
        value=adapter.workday.read(ticket)
        adapter.workspace.finish(record,'execution_completed',value,{'worklog':value['worklog']})
        with ticket['log'].open('a') as out:out.write('\nLater work is allowed.\n')
        self.assertEqual(adapter.workspace.verify()[1],2)
        ticket['log'].write_text(ticket['log'].read_text().replace('TEST complete','TEST changed'))
        with self.assertRaises(WorkspaceError) as error:adapter.workspace.verify()
        self.assertEqual(error.exception.code,'workspace_journal_invalid')

    def test_stale_completed_block_cannot_satisfy_a_new_attempt(self):
        adapter,record,ticket=self.ticket();self.write(ticket)
        next_ticket=adapter.workday.prepare(record,'TASK-1','STEP-1','TEST another attempt',{})
        self.assertIsNone(adapter.workday.read(next_ticket))

    def test_task_card_change_is_rejected(self):
        adapter,record,ticket=self.ticket();self.write(ticket);ticket['card'].write_text('TEST changed task')
        with self.assertRaises(WorkspaceError) as error:adapter.workday.read(ticket)
        self.assertEqual(error.exception.code,'worklog_card_changed')


class WorkdayFlowTests(InteractiveFixture,unittest.TestCase):
    def business(self,node,task='TASK-business',assignment='ASSIGN-1',step='REVIEW-01'):
        adapter=self.backends.adapter_for(self.source(node))
        return adapter.run({'task_id':task,'assignment_id':assignment,'step_code':step,
            'resource_plan':{'model':self.source(node)['node']['model']},'card_digest':'TEST'})

    def test_literal_start_checks_selected_node_and_agent_creates_daily_log_once(self):
        node=self.add();runtime=self.pool.runtime
        for text in ('用户说“上班了小八助理”','上班了小八助理，请说明流程'):
            with self.assertRaises(ValueError):runtime.start_workday(text,'START', ['peer'],{'peer':node})
        self.assertEqual(self.calls(),[])
        one=runtime.start_workday('上班了小八助理','START',['peer'],{'peer':node})
        self.assertEqual(one['route'],'ready',one)
        self.assertTrue(one['work_logs_initialized'])
        logfile=self.workspaces/'用户自定名字'/(today()+'-工作内容.md')
        self.assertTrue(logfile.is_file())
        events=self.count('events')
        self.assertEqual(runtime.start_workday('上班了小八助理','START',['peer'],{'peer':node}),one)
        self.assertEqual(self.count('events'),events)
        self.assertEqual([v['event'] for v in self.calls()],['start','start'])  # One process per step.
        facts=runtime.workday_inputs(node,today())['tasks']
        self.assertEqual(len(facts),2);self.assertTrue(all(v['health_check'] for v in facts))

    def test_missing_chain_configuration_sends_no_model_task(self):
        self.add()
        result=self.pool.runtime.start_workday('上班了小八助理','START',['dsh'])
        self.assertEqual(result['model_tasks_sent'],0);self.assertEqual(self.calls(),[])

    def test_long_file_reply_is_complete_even_when_the_preview_is_bounded(self):
        node=self.add();self.probe(node);(self.root/'long-reply').touch()
        result=self.business(node)
        self.assertEqual(result['findings'].count('TEST long file reply'),300)
        preview=self.pool.runtime.preview(node)
        # The per-step CLI preview projects only the last 40 receipt lines, so a
        # long reply can never be read back from it as if it were the result.
        self.assertLessEqual(len(preview['text'].splitlines()),41)
        self.assertEqual(result['execution_evidence']['reply_source'],'daily_worklog')
        self.assertEqual(len({v['pid'] for v in self.calls()}),2)  # Probe + review, one process each.

    def test_partial_file_is_not_accepted_before_final_marker(self):
        node=self.add();(self.root/'partial-file').touch();started=time.monotonic()
        result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertGreaterEqual(time.monotonic()-started,.2)

    def test_missing_business_fields_preserves_full_reply_and_connection_health(self):
        node=self.add();self.probe(node);(self.root/'bad-review-fields').touch()
        with self.assertRaises(ReviewReceiptError):self.business(node)
        self.assertTrue(self.node(node)['readiness']['ready'])
        facts=self.pool.runtime.workday_inputs(node,today())['tasks']
        business=next(v for v in facts if not v['health_check'])
        self.assertIn('TEST terminal fixture',business['body'])
        self.assertFalse(business['user_delivery_accepted'])

    def test_weekly_assessment_is_once_per_task_and_is_revisable_without_lifetime_score(self):
        node=self.add();self.probe(node);self.business(node)
        scorer=self.pool.runtime.scoring
        values=dict(node_id=node,day=today(),task_id='TASK-business',task_type='code-review',
            outcome='good',evidence_refs=['TEST host independently checked fixture'],expected_revision=0,assessment_id='ASSESS-1')
        one=scorer.assess(**values);self.assertEqual(one['points'],1)
        self.assertEqual(scorer.assess(**values),one)
        week=scorer.weekly(today(),'code-review');row=week['ranking'][0]
        self.assertEqual((row['points'],row['tasks'],row['success_rate']),(1,1,1))
        self.assertFalse(week['lifetime_score']);self.assertFalse(week['resource_inputs_collected'])
        revised=scorer.assess(**dict(values,outcome='not_good',expected_revision=1,assessment_id='ASSESS-2'))
        self.assertEqual(revised['points'],0)
        self.assertEqual(scorer.weekly(today())['ranking'][0]['tasks'],1)
        with self.assertRaises(Conflict):scorer.assess(**dict(values,assessment_id='STALE'))
        next_week=(datetime.now(ZoneInfo('Asia/Shanghai')).date()+timedelta(days=7)).isoformat()
        self.assertEqual(scorer.weekly(next_week)['ranking'],[])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_health_probe_cannot_earn_business_points_and_agent_prose_cannot_self_score(self):
        node=self.add();self.probe(node);facts=self.pool.runtime.scoring.inputs(node,today())
        with self.assertRaises(ValueError):self.pool.runtime.scoring.assess(node,today(),facts[0]['task_id'],
            'health','good',['TEST'],0,'ASSESS')
        self.business(node)
        self.assertEqual(self.pool.runtime.scoring.weekly(today())['ranking'],[])
        with self.assertRaises(ValueError):self.pool.runtime.scoring.assess(node,today(),'TASK-business',
            'code-review','good',[],0,'ASSESS')

    def test_multiple_steps_share_one_task_score_and_accepted_log_tamper_blocks_ranking(self):
        node=self.add();self.probe(node);self.business(node,assignment='ASSIGN-1')
        self.business(node,assignment='ASSIGN-2')
        scorer=self.pool.runtime.scoring
        self.assertEqual(len([v for v in scorer.inputs(node,today()) if not v['health_check']]),2)
        scorer.assess(node,today(),'TASK-business','code-review','good',['TEST verified both steps'],0,'ASSESS')
        self.assertEqual(scorer.weekly(today())['ranking'][0]['tasks'],1)
        path=self.workspaces/'用户自定名字'/(today()+'-工作内容.md')
        path.write_text(path.read_text().replace('TEST terminal fixture','TEST changed fixture'))
        with self.assertRaises(WorkspaceError):scorer.weekly(today())

    def test_different_configured_models_have_separate_weekly_buckets(self):
        node=self.add();self.probe(node);self.business(node)
        scorer=self.pool.runtime.scoring
        scorer.assess(node,today(),'TASK-business','code-review','good',['TEST verified model one'],0,'ASSESS-1')
        updated=self.pool.add({'node_id':'MODEL-TWO','name':'第二模型工作区','agent':'custom','kind':'peer',
            'interface':'cli','entry':str(self.entry),'model':'TEST model two'})
        self.pool.change('MODEL-TWO',updated['node']['revision'],'toggle',True)
        self.assertEqual(self.probe('MODEL-TWO','TEST-model-two')['status'],'passed')
        self.business('MODEL-TWO',task='TASK-two',assignment='ASSIGN-two')
        scorer.assess('MODEL-TWO',today(),'TASK-two','code-review','not_good',['TEST verified model two'],0,'ASSESS-2')
        rows=scorer.weekly(today())['ranking']
        self.assertEqual({v['model']:v['points'] for v in rows},{'Gemini 3.8 Flash':1,'TEST model two':0})

    def test_forged_score_cache_is_not_used_for_selection(self):
        node=self.add();self.probe(node);self.business(node);scorer=self.pool.runtime.scoring
        scorer.assess(node,today(),'TASK-business','code-review','good',['TEST verified'],0,'ASSESS')
        with self.native.connect() as db:
            row=db.execute('SELECT id,data FROM ri_task_assessments').fetchone();value=json.loads(row[1]);value['points']=999
            db.execute('UPDATE ri_task_assessments SET data=? WHERE id=?',(json.dumps(value),row[0]))
        with self.assertRaises(ValueError):scorer.weekly(today())

    def test_weekly_data_selects_among_healthy_nodes_and_never_revives_disabled_winner(self):
        node=self.add();self.probe(node);self.business(node);scorer=self.pool.runtime.scoring
        scorer.assess(node,today(),'TASK-business','code-review','good',['TEST verified'],0,'ASSESS')
        other=self.pool.add({'node_id':'SECOND','name':'第二工作区','agent':'custom','kind':'peer',
            'interface':'cli','entry':str(self.entry),'model':'TEST second model'})
        self.pool.change('SECOND',other['node']['revision'],'toggle',True)
        self.assertEqual(self.probe('SECOND','TEST-other')['status'],'passed')
        self.assertEqual(self.pool.runtime.prepare(['peer'],task_type='code-review')['selections'],{'peer':node})
        self.pool.change(node,self.node(node)['revision'],'toggle',False)
        self.assertEqual(self.pool.runtime.prepare(['peer'],task_type='code-review')['selections'],{'peer':'SECOND'})

    def test_failed_work_without_file_is_retained_for_zero_point_assessment(self):
        node=self.add();adapter=self.backends.adapter_for(self.source(node));(self.root/'stall').touch()
        with self.assertRaises(InteractiveError):
            adapter._turn('TEST failed task',json.dumps({}),timeout=.2,record_id='TASK-failed')
        facts=self.pool.runtime.scoring.inputs(node,today())
        self.assertEqual(facts[0]['status'],'failed')
        self.assertEqual(self.pool.runtime.scoring.assess(node,today(),'TASK-failed','code-review',
            'not_good',['TEST host checked timeout'],0,'ASSESS')['points'],0)
        with self.assertRaises(ValueError):self.pool.runtime.scoring.assess(node,today(),'TASK-failed','code-review',
            'good',['TEST'],1,'ASSESS-bad')

    def test_exception_during_start_is_saved_and_same_message_does_not_retry(self):
        from unittest.mock import patch
        node=self.add();runtime=self.pool.runtime
        with patch.object(runtime,'test_run',side_effect=RuntimeError('TEST bounded failure')) as probe:
            result=runtime.start_workday('上班了小八助理','START-FAIL',['peer'],{'peer':node})
            self.assertEqual(result['route'],'blocked')
            self.assertEqual(runtime.start_workday('上班了小八助理','START-FAIL',['peer'],{'peer':node}),result)
            self.assertEqual(probe.call_count,1)

    def test_general_work_card_uses_markdown_and_duplicate_step_does_not_execute_twice(self):
        node=self.add();self.probe(node);runtime=self.pool.runtime
        payload=dict(node_id=node,expected_revision=self.node(node)['revision'],task_id='TASK-general',
            step_code='STEP-01',instruction='TEST authorized work in this owned directory',materials={'goal':'TEST fixture'})
        first=runtime.execute_work(**payload);calls=len(self.calls())
        second=runtime.execute_work(**payload)
        self.assertEqual(first['body'],second['body']);self.assertEqual(len(self.calls()),calls)
        self.assertTrue(second['execution_evidence']['replayed']);self.assertFalse(first['score_assigned'])
        self.assertEqual(len({v['pid'] for v in self.calls()}),2)  # Probe + one work call; the replay adds none.
        with self.assertRaises(WorkspaceError):runtime.execute_work(**dict(payload,instruction='TEST conflicting work'))

    def test_general_work_without_health_does_not_dispatch(self):
        node=self.add()
        with self.assertRaises(ValueError):self.pool.runtime.execute_work(node,self.node(node)['revision'],
            'TASK-general','STEP-01','TEST authorized work',{})
        self.assertEqual(self.calls(),[])


class WorkdayHttpTests(InteractiveFixture,unittest.TestCase):
    fetch=fixtures.HttpTest.fetch

    def setUp(self):
        super().setUp()
        from monitor_core.web import Route,Response
        self.ctx=fixtures.Context(fixtures.config.load(self.root/'none.json',{'port':0}),fixtures.logs.setup(None),0)
        module=fixtures.OpenReviewModule(self.ctx,self.store,self.inbox,self.backends)
        app=fixtures.App(self.ctx);app.register(module)
        app.routes[('GET','/')]=Route('GET','/',lambda req:Response(200,
            ('<body data-page-token="'+self.ctx.page_token+'"></body>').encode(),'text/html'),'page')
        self.server=app.server();port=self.server.server_port
        self.ctx.port=port;self.ctx.hosts={'127.0.0.1:'+str(port)};self.ctx.origins={'http://127.0.0.1:'+str(port)}
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)

    def test_new_workday_and_score_routes_keep_csrf_and_do_not_dispatch_from_reads(self):
        node=self.add();events=self.count('events')
        for route in ('workday-start','workday-inputs','task-assess','weekly-scores'):
            code,_=self.fetch('/api/agent-pool/'+route,{},False)
            self.assertEqual(code,403,route)
        code,value=self.fetch('/api/agent-pool/workday-inputs',{'node_id':node,'day':today()})
        self.assertEqual(code,200,value);self.assertEqual(value['result']['tasks'],[])
        code,value=self.fetch('/api/agent-pool/weekly-scores',{'day':today()})
        self.assertEqual(code,200,value);self.assertEqual(value['result']['ranking'],[])
        self.assertEqual(self.calls(),[]);self.assertEqual(self.count('events'),events)

    def test_candidate_host_entry_daily_file_and_weekly_assessment_http_loop(self):
        import workday_session
        node=self.add();url=next(iter(self.ctx.origins))
        value=workday_session.start(url,'START-HTTP',['peer'],{'peer':node})
        self.assertEqual(value['result']['route'],'ready',value)
        adapter=self.backends.adapter_for(self.source(node))
        adapter.run({'task_id':'TASK-HTTP','assignment_id':'ASSIGN-HTTP','card_digest':'TEST'})
        code,value=self.fetch('/api/agent-pool/task-assess',{'node_id':node,'day':today(),
            'task_id':'TASK-HTTP','task_type':'code-review','outcome':'good',
            'evidence_refs':['TEST host verified complete task'],'expected_revision':0,'assessment_id':'ASSESS-HTTP'})
        self.assertEqual(code,200,value);self.assertEqual(value['result']['points'],1)
        code,value=self.fetch('/api/agent-pool/weekly-scores',{'day':today(),'task_type':'code-review'})
        self.assertEqual(code,200,value);self.assertEqual(value['result']['ranking'][0]['tasks'],1)
        self.assertEqual(len({v['pid'] for v in self.calls()}),3)  # Probe + workday init + review, one process each.
        self.assertEqual(self.count('memory_reviews'),1)  # Only authored fixture setup.

    def test_normal_backend_execution_reads_file_and_receives_bound_review(self):
        node=self.add();self.probe(node)
        request=self.store.create(self.candidate['task_id'],'fixture-dsh',node,'TEST review',
            [{'ref':'TEST fixture execution','sha256':'a'*64}],peer_channel='backend_adapter')
        assignment=next(v for v in request['assignments'] if v['role']=='peer')
        result=self.backends.execute(assignment['id'],require_test=True)
        self.assertEqual(result['status'],'valid',result)
        receipt=self.store.get(request['id'])['receipts'][0]
        self.assertEqual(receipt['data']['execution_evidence']['reply_source'],'daily_worklog')
        self.assertEqual(receipt['data']['execution_evidence']['worklog']['task_id'],request['id'])
        self.assertEqual(self.count('memory_reviews'),1)

    def test_normal_backend_business_format_failure_keeps_connection_health(self):
        node=self.add();self.probe(node);(self.root/'bad-review-fields').touch()
        request=self.store.create(self.candidate['task_id'],'fixture-dsh',node,'TEST review',
            [{'ref':'TEST fixture execution','sha256':'a'*64}],peer_channel='backend_adapter')
        assignment=next(v for v in request['assignments'] if v['role']=='peer')
        with self.assertRaisesRegex(ValueError,'工作正文已收取'):
            self.backends.execute(assignment['id'],require_test=True)
        self.assertTrue(self.node(node)['readiness']['ready'])
        self.assertEqual(self.count('ri_receipts'),0)
