"""Opening a shared workday must never dispatch a node probe."""
import unittest
from unittest.mock import patch
from test_agy_cli import CliFixture


class LocalWorkdayTests(CliFixture,unittest.TestCase):
    def test_start_only_checks_locally_and_writes_report(self):
        node=self.add();runtime=self.pool.runtime
        with patch.object(runtime,'test_run',side_effect=AssertionError('must not call model')):
            result=runtime.start_workday('上班了小八助理','TEST-card62',['peer'],{'peer':node})
        self.assertEqual('ready',result['route'])
        self.assertEqual(0,result['model_tasks_sent'])
        from pathlib import Path
        self.assertTrue(Path(result['ledger']['directory'],'本地检测报告.json').exists())
        self.assertEqual([],self.calls())


if __name__=='__main__':unittest.main()
