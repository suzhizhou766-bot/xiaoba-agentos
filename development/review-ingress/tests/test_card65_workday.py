"""Card65 ingress finalization and post-commit marker regressions."""
import builtins
import json
import unittest
from unittest.mock import patch
from test_agy_cli import CliFixture

class WorkdayFinalizationTests(CliFixture,unittest.TestCase):
    def check_finished(self,message,result):
        self.assertEqual('blocked',result['route']);self.assertIn('error_type',result)
        with self.native.read_only() as db:
            self.assertEqual(result,json.loads(db.execute('SELECT data FROM ri_workday_starts WHERE message_id=?',(message,)).fetchone()[0]))
            self.assertEqual(1,db.execute("SELECT COUNT(*) FROM events WHERE event_type='workday.finished' AND json_extract(payload_json,'$.message_id')=?",(message,)).fetchone()[0])
        self.assertEqual(result,self.pool.runtime.start_workday('上班了小八助理',message,['peer']))
    def test_D1_start_session_failure_finishes_and_retry_is_not_running(self):
        with patch.object(self.native,'start_session',side_effect=RuntimeError('TEST session')):
            result=self.pool.runtime.start_workday('上班了小八助理','TEST-session-failure',['peer'])
        self.check_finished('TEST-session-failure',result)
    def test_D1_import_failure_finishes_and_retry_is_not_running(self):
        original=builtins.__import__
        def importing(name,*args,**kwargs):
            if name=='xiaoba_agentos.workday_ledger':raise ImportError('TEST module')
            return original(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=importing):
            result=self.pool.runtime.start_workday('上班了小八助理','TEST-import-failure',['peer'])
        self.check_finished('TEST-import-failure',result)
    def test_D1_invalid_check_result_still_finalizes_blocked(self):
        with patch('xiaoba_agentos.workday_ledger.local_checks',return_value={'nodes':[object()]}):
            result=self.pool.runtime.start_workday('上班了小八助理','TEST-invalid-check-result',['peer'])
        self.check_finished('TEST-invalid-check-result',result)

    def test_D4_review_transaction_marks_relevant_event_only_after_commit(self):
        from xiaoba_agentos import workday_ledger as ledger
        ledger.rebuild_view(self.native)
        self.assertFalse(ledger.workday_status(self.native)['pending'])
        with self.store.transaction() as db:self.native._append_event_tx(db,None,'workday.started',{'message_id':'TEST transaction'})
        self.assertTrue(ledger.workday_status(self.native)['pending'])
        ledger.rebuild_view(self.native)
        with self.assertRaises(ValueError):
            with self.store.transaction() as db:
                self.native._append_event_tx(db,None,'workday.finished',{'message_id':'TEST rollback'});raise ValueError('TEST')
        self.assertFalse(ledger.workday_status(self.native)['pending'])
    def test_D4_marker_failure_does_not_undo_committed_transaction(self):
        from xiaoba_agentos import workday_ledger as ledger
        with patch.object(ledger,'mark_dirty',side_effect=RuntimeError('TEST marker')),self.store.transaction() as db:
            self.native._append_event_tx(db,None,'workday.finished',{'message_id':'TEST marker failure'})
        with self.native.read_only() as db:self.assertEqual(1,db.execute("SELECT COUNT(*) FROM events WHERE json_extract(payload_json,'$.message_id')='TEST marker failure'").fetchone()[0])
