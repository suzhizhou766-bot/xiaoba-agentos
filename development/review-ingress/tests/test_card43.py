"""卡 43：节点测试期限与失败诊断（隔离子进程夹具，不调用真实模型）。

覆盖卡 43 验收的三条：
1. 测试期限跟节点掉线时限走（上限 300 秒，未设仍 90 秒），AGY 两处一致；
2. 失败记录保留脱敏错误尾部，原始 stdout 不保存；
3. 失败分类只看诊断文字，stdout 结构化事件里的源码字样不参与匹配。
"""
import json
import os
import signal
import sys
import textwrap
import unittest
from pathlib import Path

import test_review as fixtures
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.cli_identity import (diagnostic_text, error_tail, non_event_lines,
                                        specific_failure, test_timeout_seconds)
from xiaoba_review.resource_agy import AgyInteractiveResource
from xiaoba_review.resource_agy_cli import AgyCliResource
from xiaoba_review.resource_cli import CliWorklogResource
from xiaoba_review.resource_stdio import json_stdio_factory

# 假密钥：形如 sk- 开头的固定串，只用于验证脱敏；原始 stdout 里出现同样的串。
SECRET = 'sk-c43TESTsecret1234567890'
RAW_STDOUT = 'TEST_STDOUT_RAW_C43'


class Card43Tests(fixtures.FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.workspaces = self.root/'workspaces'; self.workspaces.mkdir()
        self.registry = BackendRegistry(self.store, lambda s: json_stdio_factory(s, self.workspaces))
        self.addCleanup(self.registry.close)
        self.pool = AgentPool(self.store, self.registry)
        self.entry = self.fake()

    # -- fixtures -----------------------------------------------------------
    def fake(self):
        entry = self.root/'fake-cli'
        entry.write_text('#!'+sys.executable+'\nROOT='+repr(str(self.root))+
            '\nSECRET='+repr(SECRET)+'\nRAW_STDOUT='+repr(RAW_STDOUT)+'\n'+textwrap.dedent(r'''
            import json,os,re,sys,time
            from pathlib import Path
            root=Path(ROOT)
            prompt=sys.stdin.read()
            with (root/'calls').open('a') as out:
                out.write(json.dumps({'argv':sys.argv[1:],'cwd':os.getcwd(),'prompt':prompt})+'\n')
            def flag(name):return (root/name).exists()
            # 卡 43：冷启动夹具。真实冷启动 120 秒由测试把期限缩短来模拟，
            # 这里只等 1 秒，足够覆盖"启动阶段占用期限"的行为。
            if flag('coldstart'):
                time.sleep(1.0)
            if flag('stderr-key'):
                print(json.dumps({'event':'assistant','message':{'content':RAW_STDOUT}}),flush=True)
                sys.stderr.write('error: unexpected response from upstream secret='+SECRET+'\n')
                sys.exit(2)
            if flag('stdout-events-unknown-option'):
                # 流式 CLI：读过的源码片段装在结构化事件里（卡 42 第七次发卡现场）。
                print(json.dumps({'event':'assistant','message':{'content':
                    'diff: unknown option --model'+SECRET}}),flush=True)
                sys.exit(3)
            if flag('stderr-unknown-option'):
                sys.stderr.write('unknown option --model\n');sys.exit(3)
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
        entry.chmod(0o700); return entry

    def add(self, kind='qoder', limit=None, node_id=None):
        node_id = node_id or 'C43-'+kind
        value = {'node_id':node_id,'name':node_id,'agent':kind,'agent_type':kind,
                 'kind':'peer','interface':'cli','entry':str(self.entry),'model':'TEST model'}
        if limit is not None:value['no_progress_seconds']=limit
        source=self.pool.add(value)
        self.pool.change(node_id,source['node']['revision'],'toggle',True)
        return node_id

    def node(self,node):
        return next(v for v in self.pool.state()['nodes'] if v['node_id']==node)

    def source(self,node):
        with self.native.read_only() as db:return self.store.source(db,node)

    def probe(self,node,test_id=None):
        return self.pool.runtime.test_run(node,self.node(node)['revision'],test_id or 'probe-'+node)

    def stored(self):
        with self.native.read_only() as db:
            events=' '.join(r[0] for r in db.execute('SELECT payload_json FROM events'))
        files=''.join(p.read_text() for p in self.workspaces.rglob('*') if p.is_file())
        return events+files

    # -- 期限 ---------------------------------------------------------------
    def test_timeout_follows_node_offline_limit_and_caps_at_300(self):
        self.assertEqual(test_timeout_seconds({}),90)
        self.assertEqual(test_timeout_seconds({'no_progress_seconds':120}),120)
        self.assertEqual(test_timeout_seconds({'no_progress_seconds':300}),300)
        self.assertEqual(test_timeout_seconds({'no_progress_seconds':1800}),300)
        self.assertEqual(test_timeout_seconds({'no_progress_seconds':0}),90)
        self.assertEqual(test_timeout_seconds({'no_progress_seconds':True}),90)
        self.assertEqual(test_timeout_seconds(None),90)

    def test_every_cli_test_call_uses_the_shared_timeout(self):
        for kind in ('qoder','agy'):
            with self.subTest(kind=kind):
                node=self.add(kind,1800,node_id='C43-LONG-'+kind)
                adapter=self.registry.adapter_for(self.source(node))
                self.assertIsInstance(adapter,CliWorklogResource)
                self.assertEqual(adapter._test_timeout_seconds,300)
                node_short=self.add(kind,None,node_id='C43-DEFAULT-'+kind)
                short=self.registry.adapter_for(self.source(node_short))
                self.assertEqual(short._test_timeout_seconds,90)
        node=self.add('agy',1800,node_id='C43-AGY-INTERACTIVE')
        interactive=json_stdio_factory(self.source(node),self.workspaces,agy_mode='interactive')
        self.assertIsInstance(interactive,AgyInteractiveResource)
        self.assertEqual(interactive._test_timeout_seconds,300)

    def test_agy_cli_print_timeout_uses_the_computed_value(self):
        node=self.add('agy',1800,node_id='C43-AGY-ARGV')
        adapter=AgyCliResource(self.source(node),self.workspaces)
        argv=adapter._command(str(self.entry),adapter._test_timeout_seconds)
        self.assertEqual(argv[argv.index('--print-timeout')+1],'300s')
        argv=adapter._command(str(self.entry),90)
        self.assertEqual(argv[argv.index('--print-timeout')+1],'90s')
        argv=adapter._command(str(self.entry),None)
        self.assertEqual(argv[argv.index('--print-timeout')+1],'0s')

    def test_cold_start_120_seconds_passes_with_long_offline_limit(self):
        # 120 秒冷启动 < 300 上限；夹具用 1 秒真实等待代表启动阶段，期限取节点值。
        (self.root/'coldstart').touch();node=self.add('qoder',1800,node_id='C43-COLD-OK')
        result=self.probe(node)
        self.assertEqual(result['status'],'passed',result)
        self.assertEqual(self.node(node)['test']['status'],'passed')

    def test_cold_start_times_out_when_node_keeps_90_second_limit(self):
        (self.root/'coldstart').touch();node=self.add('qoder',None,node_id='C43-COLD-TIMEOUT')
        adapter=self.registry.adapter_for(self.source(node))
        self.assertEqual(adapter._test_timeout_seconds,90)
        # 真实 90 秒不等待：把期限缩到 0.4 秒，证明期限到点会被结束（同一代码路径）。
        adapter._test_timeout_seconds=0.4
        result=self.probe(node)
        self.assertEqual(result['status'],'failed',result)
        self.assertEqual(result['reason'],'cli_timeout')
        self.assertIn('测试期限',result['message'])

    # -- 错误尾部 -----------------------------------------------------------
    def test_failed_test_keeps_redacted_error_tail_without_raw_stdout(self):
        (self.root/'stderr-key').touch();node=self.add('qoder',node_id='C43-ERRTAIL')
        result=self.probe(node)
        self.assertEqual(result['status'],'failed',result)
        self.assertEqual(result['reason'],'cli_failed')
        tail=result.get('error_tail')
        self.assertTrue(tail,tail)
        self.assertIn('[REDACTED]',tail)
        self.assertNotIn(SECRET,tail)
        stored=self.stored()
        self.assertNotIn(SECRET,stored)
        self.assertNotIn(RAW_STDOUT,stored)

    def test_error_tail_prefers_stderr_and_skips_structured_events(self):
        self.assertIn('[REDACTED]',error_tail('', 'key '+SECRET))
        self.assertNotIn(SECRET,error_tail('', 'key '+SECRET))
        # stderr 为空时只取 stdout 末尾的非事件行；结构化的 JSON 事件整行丢弃。
        stdout='\n'.join([json.dumps({'event':'x','text':SECRET}) for _ in range(3)])
        self.assertEqual(error_tail(stdout,''),'')
        self.assertIn('plain diagnostic',error_tail(stdout+'\nplain diagnostic\n',''))

    def test_diagnostic_text_ignores_json_event_payload(self):
        stdout=json.dumps({'event':'assistant','message':{'content':'unknown option --m'}})
        self.assertNotIn('unknown option',diagnostic_text(stdout,''))
        self.assertNotIn('unknown option',non_event_lines(stdout))
        self.assertIn('unknown option',diagnostic_text('','unknown option --m'))

    # -- 分类 ---------------------------------------------------------------
    def test_stdout_event_payload_is_not_classified_as_unsupported_entry(self):
        (self.root/'stdout-events-unknown-option').touch();node=self.add('qoder',node_id='C43-EVENT')
        result=self.probe(node)
        self.assertEqual(result['status'],'failed',result)
        self.assertEqual(result['reason'],'cli_failed',result)
        self.assertNotIn('error_tail',result)

    def test_same_text_in_stderr_is_still_unsupported_entry(self):
        (self.root/'stderr-unknown-option').touch();node=self.add('qoder',node_id='C43-STDERR')
        result=self.probe(node)
        self.assertEqual(result['reason'],'unsupported_entry',result)

    def test_classification_order_is_unchanged(self):
        self.assertEqual(specific_failure('qoder','X ','m','','unknown option --m',
            returncode=-signal.SIGTERM).code,'call_terminated')
        self.assertEqual(specific_failure('qoder','X ','m','','permission check failed',
            returncode=1).code,'permission_denied')
        self.assertEqual(specific_failure('qoder','X ','m','','not logged in',
            returncode=1).code,'cli_not_logged_in')
        self.assertEqual(specific_failure('qoder','X ','m','','unknown model x',
            returncode=1).code,'model_not_recognized')
        self.assertIsNone(specific_failure('qoder','X ','m',
            json.dumps({'event':'x','text':'unknown option'}),'',returncode=1))


if __name__=='__main__':
    unittest.main()
