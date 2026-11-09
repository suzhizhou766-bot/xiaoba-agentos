"""Real fake-CLI receipts remain settled when the ledger observer fails."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from test_agy_cli import CliFixture


class ObserverIsolationTests(CliFixture,unittest.TestCase):
    def test_P1_each_exception_preserves_health_and_one_successful_receipt(self):
        for index,error in enumerate((sqlite3.OperationalError('TEST'),ValueError('TEST'),RuntimeError('TEST'))):
            with self.subTest(error=type(error).__name__):
                node=self.add(node_id='TEST-observer-'+str(index),name='TEST observer '+str(index));self.probe(node,test_id='TEST-probe-'+str(index))
                prior=self.source(node)['node']['test']
                request=self.store.create(self.candidate['task_id'],'fixture-dsh',node,'TEST review',
                    [{'ref':'TEST fixture execution','sha256':'a'*64}],peer_channel='backend_adapter')
                assignment=next(v for v in request['assignments'] if v['role']=='peer')
                workspace=self.backends.adapter_for(self.source(node)).workspace
                journal=workspace.path/'记录.jsonl'
                count=lambda:sum(json.loads(v)['event']=='task_finished' for v in journal.read_text().splitlines())
                before=count()
                with patch('xiaoba_agentos.workday_ledger._register_reference',side_effect=error):
                    value=self.backends.execute(assignment['id'],require_test=True)
                self.assertEqual('valid',value['status'])
                self.assertEqual(prior,self.source(node)['node']['test'])
                self.assertEqual(before+1,count())
                self.assertEqual('completed',self.native.get_task(self.candidate['task_id'])['status'])

    def test_P2_actual_entrant_orders_and_unconfigured_skip(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from xiaoba_agentos import workday_ledger as ledger
        from xiaoba_agentos.cli import main
        for first in ('ingress','core'):
            with self.subTest(first=first):
                host=ObserverIsolationTests('runTest');host.setUp()
                try:
                    node=host.add()
                    cfg=SimpleNamespace(memory={'obsidian_vault':str(host.root/'assistant/vault')})
                    def core():
                        with patch('xiaoba_agentos.cli._load',return_value=(cfg,host.native)),patch('xiaoba_agentos.cli.doctor',return_value={'ok':True}),patch('xiaoba_agentos.cli.start_session_health',return_value={'ok':True,'session_mode':'ready'}):
                            out=io.StringIO()
                            with redirect_stdout(out):self.assertEqual(0,main(['session-start']))
                            return json.loads(out.getvalue())['workday']
                    def ingress(message):
                        return host.pool.runtime.start_workday('上班了小八助理',message,['peer'],{'peer':node})
                    initial=ingress('TEST-first')['ledger'] if first=='ingress' else core()
                    self.assertIn('directory',initial)
                    cfg.memory['obsidian_vault']=str(host.root/'other/vault')
                    ledger.configure(host.native,cfg)
                    if first=='core':
                        second=ingress('TEST-second')
                        self.assertEqual('ready',second['route'])
                        self.assertEqual('binding_conflict',second['ledger']['warning_code'])
                    result=core()
                    self.assertEqual('binding_conflict',result['warning_code'])
                    self.assertIn('需要用户决定',result['message'])
                    ledger.configure(host.native,SimpleNamespace(memory={}))
                    skipped=ingress('TEST-unconfigured')
                    self.assertEqual('ready',skipped['route'])
                    self.assertEqual('unconfigured',skipped['ledger']['warning_code'])
                    self.assertNotIn('directory',skipped['ledger'])
                    with host.native.read_only() as db:
                        self.assertEqual(0,db.execute('SELECT COUNT(*) FROM ri_workday_starts WHERE data IS NULL').fetchone()[0])
                finally:host.doCleanups()

    def test_P2_start_finishes_record_even_when_view_raises(self):
        node=self.add()
        with patch('xiaoba_agentos.workday_ledger.WorkdayLedger.rebuild',side_effect=RuntimeError('TEST')):
            result=self.pool.runtime.start_workday('上班了小八助理','TEST-side-channel-start',['peer'],{'peer':node})
        self.assertEqual('ready',result['route'])
        with self.native.read_only() as db:
            self.assertIsNotNone(db.execute('SELECT data FROM ri_workday_starts WHERE message_id=?',('TEST-side-channel-start',)).fetchone()[0])

    def test_review_obs4_local_check_failure_is_reported_as_such_and_start_finishes(self):
        # 卡 64 复查观察 4：本地检查故障不伪装成台账失败，开工记录也不停在 running。
        node=self.add()
        with patch('xiaoba_agentos.workday_ledger.local_checks',side_effect=RuntimeError('TEST config broken')):
            result=self.pool.runtime.start_workday('上班了小八助理','TEST-local-check-fails',['peer'],{'peer':node})
        self.assertEqual('blocked',result['route'])
        self.assertEqual('RuntimeError',result['error_type'])
        self.assertIn('本地检查失败',result['local_check_error'])
        self.assertEqual('local_check_failed',result['ledger']['warning_code'])
        with self.native.read_only() as db:
            self.assertIsNotNone(db.execute('SELECT data FROM ri_workday_starts WHERE message_id=?',('TEST-local-check-fails',)).fetchone()[0])


if __name__=='__main__':unittest.main()
