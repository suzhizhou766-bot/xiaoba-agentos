"""Card66: optional reasoning_effort on pool nodes and vendor CLI argv."""
import unittest

from xiaoba_review.agent_pool import REASONING_EFFORT, digest, node_state
from xiaoba_review.node_runtime import spec
from xiaoba_review.resource_vendor_cli import CodeBuddyCliResource, QoderCliResource
from xiaoba_review.store import canonical
from test_agy_cli import CliFixture


class ReasoningEffortTests(CliFixture, unittest.TestCase):
    def _add(self, agent_type, effort=None, node_id='C66-NODE'):
        payload = {
            'node_id': node_id, 'name': node_id, 'agent': node_id, 'kind': 'review',
            'interface': 'cli', 'entry': str(self.entry), 'model': 'TEST-model',
            'agent_type': agent_type,
        }
        if effort is not None:
            payload['reasoning_effort'] = effort
        return self.pool.add(payload)

    def test_qoder_command_includes_reasoning_effort_when_set(self):
        source = self._add('qoder', 'xhigh')
        adapter = QoderCliResource(source, self.workspaces)
        argv = adapter._command(str(self.entry), 90)
        self.assertEqual(argv[argv.index('--reasoning-effort') + 1], 'xhigh')

    def test_codebuddy_command_uses_effort_flag(self):
        source = self._add('codebuddy', 'high', node_id='C66-CB')
        adapter = CodeBuddyCliResource(source, self.workspaces)
        argv = adapter._command(str(self.entry), 90)
        self.assertIn('--effort', argv)
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        self.assertNotIn('--reasoning-effort', argv)

    def test_unset_effort_keeps_legacy_argv(self):
        source = self._add('qoder')
        adapter = QoderCliResource(source, self.workspaces)
        with_effort = QoderCliResource(self._add('qoder', 'xhigh', 'C66-SET'), self.workspaces)
        unset = adapter._command(str(self.entry), 90)
        set_argv = with_effort._command(str(self.entry), 90)
        self.assertNotIn('--reasoning-effort', unset)
        self.assertEqual(unset, [a for a in set_argv if a not in ('--reasoning-effort', 'xhigh')])

    def test_invalid_effort_is_rejected_on_add_and_command(self):
        with self.assertRaisesRegex(ValueError, '思考强度取值不合法'):
            self._add('qoder', 'NOT_A_LEVEL', 'C66-BAD')
        source = self._add('qoder', 'xhigh', 'C66-BAD2')
        adapter = QoderCliResource(source, self.workspaces)
        adapter.reasoning_effort = 'NOT_A_LEVEL'
        with self.assertRaisesRegex(ValueError, '思考强度取值不合法'):
            adapter._command(str(self.entry), 90)

    def test_changing_effort_invalidates_test_qualification(self):
        source = self._add('qoder', 'high', 'C66-DIGEST')
        node = node_state(source)
        node['test'] = {
            'status': 'passed', 'spec_digest': digest(spec(source)),
            'runner_digest': 'runner', 'evidence_digest': 'ev',
            'execution_evidence': {}, 'test_id': 't1', 'event_id': 'e1',
        }
        source['node'] = node
        with self.store.transaction() as db:
            db.execute('UPDATE ri_sources SET data=? WHERE id=?',
                       (canonical(source), source['reviewer_id']))
        before = digest(spec(source))
        edited = self.pool.edit({
            'node_id': source['reviewer_id'], 'expected_revision': node['revision'],
            'name': node['name'], 'entry': node['entry'], 'model': node['model'],
            'reasoning_effort': 'xhigh',
        })
        self.assertNotEqual(before, digest(spec(edited)))
        self.assertEqual('invalidated', node_state(edited)['test']['status'])

    def test_effort_tables_match_verified_cli_help(self):
        self.assertIn('xhigh', REASONING_EFFORT['qoder'])
        self.assertIn('high', REASONING_EFFORT['codebuddy'])
        self.assertNotIn('ultracode', REASONING_EFFORT['codebuddy'])


if __name__ == '__main__':
    unittest.main()
