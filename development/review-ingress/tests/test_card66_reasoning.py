"""Card66: optional reasoning_effort on pool nodes and vendor CLI argv."""
import unittest

from xiaoba_review.agent_pool import REASONING_EFFORT, digest, node_state, resource_plan
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

    def test_effort_tables_are_subsets_of_recorded_cli_output(self):
        # v1.6.13 验收 Qoder 阻断 2：白名单要对照 CLI 自己的输出（固定在夹具里），而不是和自己比。
        import re
        from pathlib import Path
        from xiaoba_agentos.gateway import CLI_REASONING_EFFORTS, _QODER_EFFORTS
        text = (Path(__file__).with_name('fixtures') / 'cli-effort-evidence.txt').read_text(encoding='utf-8')
        qoder = set(re.search(r'Valid values are: ([a-z, ]+)\.', text).group(1).replace(' ', '').split(','))
        codebuddy = set(re.search(r'--effort <level> \(([a-z, ]+)\)', text).group(1).replace(' ', '').split(','))
        self.assertLessEqual(REASONING_EFFORT['qoder'], qoder)
        self.assertLessEqual(REASONING_EFFORT['codebuddy'], codebuddy)
        self.assertEqual(set(_QODER_EFFORTS), set(REASONING_EFFORT['qoder']))
        self.assertEqual({k: set(v) for k, v in CLI_REASONING_EFFORTS.items()}, {k: set(v) for k, v in REASONING_EFFORT.items()})
        self.assertIn('xhigh', REASONING_EFFORT['qoder']); self.assertIn('high', REASONING_EFFORT['codebuddy'])

    def _add_enabled(self, agent_type, effort=None, node_id='C66-NODE'):
        source = self._add(agent_type, effort, node_id)
        return self.pool.change(source['reviewer_id'], node_state(source)['revision'], 'toggle', enabled=True)

    def test_resource_plan_preserves_reasoning_effort_matching_spec(self):
        # v1.6.20 热修：卡 66 在 spec() 加入 reasoning_effort 时遗漏了 resource_plan()，
        # 导致设置思考强度的节点执行开放审查/价值评分任务卡必报“冻结任务卡与当前节点配置不符”。
        qoder = self._add_enabled('qoder', 'xhigh', 'C66-RP-QODER')
        cb = self._add_enabled('codebuddy', 'high', 'C66-RP-CB')
        unset = self._add_enabled('qoder', None, 'C66-RP-UNSET')
        for source, expected_effort in ((qoder, 'xhigh'), (cb, 'high'), (unset, None)):
            plan = resource_plan(source)
            cur = spec(source)
            self.assertEqual(expected_effort, plan.get('reasoning_effort'))
            self.assertEqual(expected_effort, cur.get('reasoning_effort'))
            for key, value in cur.items():
                if key != 'kind':
                    self.assertEqual(value, plan.get(key), f"mismatch on key {key}")

    def test_frozen_card_allows_matching_effort_and_blocks_modified_or_cleared_effort(self):
        import hashlib
        from unittest.mock import patch

        qoder = self._add_enabled('qoder', 'xhigh', 'C66-EXEC-QODER')
        cb = self._add_enabled('codebuddy', 'high', 'C66-EXEC-CB')
        self.store.outbox_root.mkdir(parents=True, exist_ok=True)
        ev = self.store.outbox_root / 'candidate-v1620.md'
        ev.write_text('# candidate\n', encoding='utf-8')
        ev_sha = hashlib.sha256(ev.read_bytes()).hexdigest()
        tid = self.native.submit('TEST v1620 effort review', 1)
        self.native.complete_manually(tid, 'TEST result')
        self.native.stage_memory_candidate(tid, str(ev), ev_sha)
        created = self.store.create(
            tid, qoder['reviewer_id'], cb['reviewer_id'], 'TEST effort card',
            [{'ref': str(ev), 'sha256': ev_sha}],
            dsh_channel='backend_adapter', peer_channel='backend_adapter',
            review_type='new_experience',
        )
        asg_id = next(a['id'] for a in created['assignments'] if a['source_id'] == qoder['reviewer_id'])
        card = next(a['card'] for a in created['assignments'] if a['id'] == asg_id)
        self.assertEqual('xhigh', card['resource_plan'].get('reasoning_effort'))

        # Matching effort passes the spec-vs-resource_plan gate (fails only at stubbed adapter run, not config mismatch)
        stub_adapter = type('StubAdapter', (), {
            'supports_resource_plan': True,
            'run': lambda _self, _card: (_ for _ in ()).throw(RuntimeError('REACHED_ADAPTER_OK')),
        })()
        with patch('xiaoba_review.node_runtime.readiness', return_value={'ready': True}), \
             patch.object(self.backends, 'adapter_for', return_value=stub_adapter):
            with self.assertRaisesRegex(RuntimeError, 'REACHED_ADAPTER_OK'):
                self.backends.execute(asg_id)

        # Clearing reasoning_effort after card freeze must still be rejected as config mismatch
        with self.store.transaction() as db:
            row = self.store.source(db, qoder['reviewer_id'])
            row['node'].pop('reasoning_effort', None)
            db.execute('UPDATE ri_sources SET data=? WHERE id=?', (canonical(row), qoder['reviewer_id']))
        with patch('xiaoba_review.node_runtime.readiness', return_value={'ready': True}), \
             patch.object(self.backends, 'adapter_for', return_value=stub_adapter):
            with self.assertRaisesRegex(ValueError, '冻结任务卡与当前节点配置不符'):
                self.backends.execute(asg_id)

        # Changing reasoning_effort after card freeze (xhigh -> high) must also be rejected
        with self.store.transaction() as db:
            row = self.store.source(db, qoder['reviewer_id'])
            row['node']['reasoning_effort'] = 'high'
            db.execute('UPDATE ri_sources SET data=? WHERE id=?', (canonical(row), qoder['reviewer_id']))
        with patch('xiaoba_review.node_runtime.readiness', return_value={'ready': True}), \
             patch.object(self.backends, 'adapter_for', return_value=stub_adapter):
            with self.assertRaisesRegex(ValueError, '冻结任务卡与当前节点配置不符'):
                self.backends.execute(asg_id)


if __name__ == '__main__':
    unittest.main()
