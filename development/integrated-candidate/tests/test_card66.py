"""Card66 core leftovers: _write cleanup and session-start local-check reporting."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from contextlib import redirect_stdout

from xiaoba_agentos import workday_ledger as ledger
from xiaoba_agentos.cli import main
from xiaoba_agentos.store import EventStore


class WriteCleanupTests(unittest.TestCase):
    def test_replace_failure_removes_temporary(self):
        with tempfile.TemporaryDirectory() as root:
            # Resolve so macOS /var -> /private/var does not trip the symlink guard.
            folder = Path(root).resolve()
            target = folder / '台账.md'
            target.write_text('keep', encoding='utf-8')
            with patch('xiaoba_agentos.workday_ledger.os.replace', side_effect=OSError('TEST')):
                with self.assertRaises(OSError):
                    ledger._write(target, 'new\n')
            self.assertEqual({'台账.md'}, {p.name for p in folder.iterdir()})
            self.assertEqual('keep', target.read_text(encoding='utf-8'))


class SessionStartLocalCheckTests(unittest.TestCase):
    def test_local_check_failure_is_reported_separately(self):
        store = MagicMock()
        store.start_session.return_value = {'id': 'session', 'status': 'active'}
        store.pending_notifications.return_value = []
        config = SimpleNamespace()
        warned = {'ok': False, 'warning_code': 'local_check_failed', 'message': '本地检查失败'}
        with patch('xiaoba_agentos.cli._load', return_value=(config, store)), patch(
            'xiaoba_agentos.cli.doctor', return_value={'ok': True, 'retained_objects': {}}
        ), patch(
            'xiaoba_agentos.cli.start_session_health',
            return_value={
                'ok': True, 'session_mode': 'dual_agent', 'agents': {},
                'healthy_agents': ['a', 'b'], 'failed_agents': [],
                'repair_jobs': [], 'peer_auto_repair': 'retired',
            },
        ), patch('xiaoba_agentos.workday_ledger.local_checks', side_effect=RuntimeError('TEST')), patch(
            'xiaoba_agentos.workday_ledger.warn', return_value=warned
        ) as warn, patch('xiaoba_agentos.workday_ledger.rebuild_view') as rebuild:
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['session-start'])
        self.assertEqual(0, code)
        payload = json.loads(output.getvalue())
        self.assertEqual('本地检查失败：RuntimeError', payload['local_check_error'])
        self.assertEqual('local_check_failed', payload['workday']['warning_code'])
        warn.assert_called()
        rebuild.assert_not_called()


class InterpreterMatrixRegression(unittest.TestCase):
    def test_file_digest_matches_real_temp_file_bytes(self):
        # 卡 65 复核未关的第三子项：断言要钉住真实文件摘要能力，而不是字面量自洽。
        import hashlib
        import tomllib
        import sys
        data = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())
        self.assertEqual('>=3.11', data['project']['requires-python'])
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sample.bin'
            path.write_bytes(b'TEST card66 file digest')
            with path.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
