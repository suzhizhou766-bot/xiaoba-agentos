"""桌面和网页节点的用户介入通道反例：小八出卡、用户发送、用户贴回、小八校验。

夹具只在本机临时目录和隔离数据库上跑，不调用真实模型、不启动任何 Agent。
"""
import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

import test_review as fixtures
from xiaoba_agentos.store import EventStore
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.store import ReviewStore, parse_manual_receipt


class ManualFixture(fixtures.FixtureMixin):
    def setUp(self):
        super().setUp()
        self.workspaces = self.root / 'workspaces'
        self.workspaces.mkdir()
        self.store = ReviewStore(self.native, self.root / 'memory-outbox', self.workspaces)
        self.backends = BackendRegistry(self.store)
        self.pool = AgentPool(self.store, self.backends)
        self.desktop = self.node('TEST-manual-desktop', 'dsh', 'desktop')
        self.web = self.node('TEST-manual-web', 'peer', 'web')

    def node(self, node_id, kind, interface, entry=None, model='TEST model', name=None):
        if entry is None:
            entry = ('/Applications/TEST-%s.app' % node_id if interface == 'desktop'
                     else 'https://example.test/%s' % node_id)
        source = self.pool.add({'node_id': node_id, 'name': name or node_id, 'agent': 'TEST agent',
                                'kind': kind, 'interface': interface, 'entry': entry, 'model': model})
        self.pool.change(node_id, source['node']['revision'], 'toggle', True)
        return node_id

    def request(self, **kwargs):
        return self.store.create(self.candidate['task_id'], self.desktop, self.web,
                                 'TEST 人工介入通道审查', [{'ref': 'TEST evidence', 'sha256': 'a' * 64}],
                                 review_type='revalidation', **kwargs)

    @staticmethod
    def assignment(request, role):
        return next(a for a in request['assignments'] if a['role'] == role)

    def ticket(self, assignment_id):
        with self.native.read_only() as db:
            rows = db.execute("SELECT payload_json FROM events WHERE event_type='open_review.manual_prepared'").fetchall()
        for row in rows:
            payload = json.loads(row[0])
            if payload.get('assignment_id') == assignment_id and payload.get('ticket'):
                ticket = dict(payload['ticket'])
                ticket['log'] = Path(ticket['log'])
                ticket['card'] = Path(ticket['card'])
                return ticket
        return None

    def write_file_receipt(self, ticket, task=None, step=None, body='TEST 文件回执审查正文',
                           verdict='PASS', done=True, refs=('TEST evidence',), v2=True):
        prefix = (ticket['log'].read_bytes()[:ticket['prefix_size']].decode()
                  if ticket['prefix_size'] else ticket['header'])
        meta = ('任务ID: ' + (task or ticket['task_id']) + '\n步骤码: ' + (step or ticket['step_code'])
                + '\n状态: completed\n')
        if verdict is not None:
            meta += '审查结论: ' + verdict + '\n'
        for ref in refs:
            meta += '证据引用: ' + ref + '\n'
        if v2:
            meta += '审查类型: revalidation\n评分: 8\n适用条件: 仅夹具适用条件说明\n重复经验: \n'
        value = prefix + '\n' + ticket['marker'] + '\n' + meta + '---\n' + body + '\n'
        if done:
            value += ticket['done'] + '\n'
        ticket['log'].write_text(value)

    @staticmethod
    def paste_text(assignment_id, verdict='PASS', refs=('TEST evidence',), body='TEST 贴回审查正文',
                   done=True, review_type='revalidation', score='8', applicability='仅夹具适用条件说明',
                   marker_id=None, extra_lines=()):
        marker_id = marker_id or assignment_id
        lines = ['<!-- receipt:' + marker_id + ' -->']
        if verdict is not None:
            lines.append('审查结论: ' + verdict)
        for ref in refs:
            lines.append('证据引用: ' + ref)
        if review_type is not None:
            lines.append('审查类型: ' + review_type)
        if score is not None:
            lines.append('评分: ' + str(score))
        if applicability is not None:
            lines.append('适用条件: ' + applicability)
        lines.extend(extra_lines)
        lines += ['---', body]
        if done:
            lines.append('<!-- done:' + marker_id + ' -->')
        return '\n'.join(lines)


class DesktopFileChannelTests(ManualFixture, unittest.TestCase):
    def test_desktop_dispatch_card_and_file_receipt_enter_identity_pending(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        package = self.store.manual_prepare(target['id'])
        self.assertEqual(package['mode'], 'file')
        self.assertTrue(package['send_text'].startswith('去读这张卡，按卡写回执：'))
        self.assertIn(package['card_path'], package['send_text'])
        self.assertTrue(Path(package['card_path']).is_file())
        card_text = Path(package['card_path']).read_text()
        self.assertIn('审查结论: PASS 或 FAIL 或 NEEDS_INFO', card_text)
        self.assertIn('评分: 0–10 的数', card_text)  # 卡 2 合同第 2 版字段
        sent = self.store.manual_mark_sent(target['id'])
        ticket = self.ticket(target['id'])
        self.write_file_receipt(ticket)
        outcome = self.store.manual_check(target['id'])
        self.assertEqual(outcome['status'], 'received')
        self.assertEqual(outcome['receipt']['status'], 'identity_pending')
        current = self.store.get(request['id'])
        receipt = next(r for r in current['receipts'] if r['assignment_id'] == target['id'])
        self.assertEqual(receipt['status'], 'identity_pending')
        self.assertIsNone(receipt['attestation'])  # 不能跳过来源确认
        self.assertIsNone(current['assignments'][0]['manual']['status'])
        self.assertEqual(receipt['data']['started_at'], sent['sent_at'])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_file_receipt_with_wrong_task_id_or_step_is_rejected(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        self.store.manual_prepare(target['id'])
        ticket = self.ticket(target['id'])
        for field in ('task', 'step'):
            with self.subTest(field=field):
                self.write_file_receipt(ticket, **{field: 'WRONG-' + field})
                with self.assertRaises(ValueError) as error:
                    self.store.manual_check(target['id'])
                self.assertIn('不符', str(error.exception))
                self.assertEqual(self.count('ri_receipts'), 0)

    def test_incomplete_file_receipt_keeps_waiting_status(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        self.store.manual_prepare(target['id'])
        self.store.manual_mark_sent(target['id'])
        ticket = self.ticket(target['id'])
        self.write_file_receipt(ticket, done=False)
        self.assertEqual(self.store.manual_check(target['id'])['status'], 'waiting')
        current = self.store.get(request['id'])
        self.assertEqual(target['role'], 'dsh')
        manual = next(a['manual'] for a in current['assignments'] if a['id'] == target['id'])
        self.assertEqual(manual['status'], '等待用户贴回')
        self.assertEqual(self.count('ri_receipts'), 0)

    def test_corrected_file_receipt_is_collected_after_an_invalid_one(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        self.store.manual_prepare(target['id'])
        ticket = self.ticket(target['id'])
        self.write_file_receipt(ticket, refs=('TEST 不在任务卡里的证据',))
        first = self.store.manual_check(target['id'])
        self.assertEqual(first['receipt']['status'], 'invalid')
        self.assertEqual(self.store.manual_check(target['id'])['receipt']['id'], first['receipt']['id'])  # 没改文件：不多记一份
        self.assertEqual(self.count('ri_receipts'), 1)
        self.write_file_receipt(ticket)
        self.assertEqual(self.store.manual_check(target['id'])['receipt']['status'], 'identity_pending')
        self.store.manual_check(target['id'])
        self.assertEqual(self.count('ri_receipts'), 2)

    def test_unwritable_workspace_falls_back_to_paste(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        self.store.workspace_root = None
        package = self.store.manual_prepare(target['id'])
        self.assertEqual(package['mode'], 'paste')
        self.assertTrue(package['fallback_reason'])
        self.assertIn('<!-- receipt:' + target['id'], package['send_text'])
        current = self.store.get(request['id'])
        manual = next(a['manual'] for a in current['assignments'] if a['id'] == target['id'])
        self.assertEqual(manual['mode'], 'paste')

    def test_mark_sent_is_persisted_and_switch_to_paste_is_persisted(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        first = self.store.manual_prepare(target['id'])
        again = self.store.manual_prepare(target['id'])
        self.assertEqual(first['send_text'], again['send_text'])  # 幂等，不重复生成派工卡
        self.store.manual_mark_sent(target['id'])
        self.store.manual_prepare(target['id'], mode='paste')
        manual = next(a['manual'] for a in self.store.get(request['id'])['assignments'] if a['id'] == target['id'])
        self.assertEqual(manual['mode'], 'paste')
        self.assertIsNotNone(manual['sent_at'])


class PasteChannelTests(ManualFixture, unittest.TestCase):
    def test_paste_receipt_enters_identity_pending_and_cannot_skip_attestation(self):
        request = self.request()
        outcomes = []
        for assignment in request['assignments']:
            sent = self.store.manual_mark_sent(assignment['id'])
            self.store.manual_prepare(assignment['id'])
            result = self.store.manual_paste(assignment['id'], self.paste_text(assignment['id']))
            self.assertEqual(result['status'], 'identity_pending')
            self.assertFalse(result.get('replayed'))
            outcomes.append((assignment, result, sent))
        self.store.deliver(self.inbox)
        self.assertEqual(self.count('ri_inbox'), 0)  # 来源未确认，不进裁决材料
        for assignment, result, sent in outcomes:
            receipt = self.store.get(request['id'])['receipts']
            row = next(r for r in receipt if r['id'] == result['id'])
            self.assertEqual(row['data']['started_at'], sent['sent_at'])  # 用户点发送的时间
            self.store.attest(result['id'], result['digest'], 'TEST 操作者核对交接来源')
        self.store.deliver(self.inbox)
        self.assertEqual(self.count('ri_inbox'), 1)

    def test_forged_marker_is_rejected_and_forged_identity_is_overridden(self):
        request = self.request()
        dsh = self.assignment(request, 'dsh')
        peer = self.assignment(request, 'peer')
        forged = self.paste_text(dsh['id'], marker_id=peer['id'])
        with self.assertRaisesRegex(ValueError, '不符'):
            self.store.manual_paste(dsh['id'], forged)
        self.assertEqual(self.count('ri_receipts'), 0)
        text = self.paste_text(dsh['id'], extra_lines=(
            'card_digest: ' + 'f' * 64, 'assignment_id: ' + peer['id'], 'reviewer_id: TEST-impostor',
            'source_ref: TEST-forged', 'receipt_id: TEST-forged'))
        result = self.store.manual_paste(dsh['id'], text)
        row = next(r for r in self.store.get(request['id'])['receipts'] if r['id'] == result['id'])
        card = dsh['card']
        self.assertEqual(row['assignment_id'], dsh['id'])      # 不写入别的任务卡
        self.assertEqual(row['data']['card_digest'], card['card_digest'])  # 身份取服务端任务卡
        self.assertEqual(row['data']['reviewer_id'], card['reviewer_id'])
        self.assertNotEqual(row['id'], 'TEST-forged')
        self.assertEqual(self.count('ri_receipts'), 1)

    def test_missing_items_are_reported_and_no_receipt_is_created(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        cases = [(self.paste_text(target['id'], verdict=None), '审查结论'),
                 (self.paste_text(target['id'], refs=()), '证据引用'),
                 (self.paste_text(target['id'], done=False), 'done 标记'),
                 (self.paste_text(target['id'], applicability=None), '适用条件')]
        for text, wanted in cases:
            with self.subTest(item=wanted):
                with self.assertRaises(ValueError) as error:
                    self.store.manual_paste(target['id'], text)
                self.assertIn(wanted, str(error.exception))
                self.assertIn('原文', str(error.exception))
        self.assertEqual(self.count('ri_receipts'), 0)

    def test_sensitive_paste_is_not_stored_and_b19_paste_is_marked_invalid(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        secret = self.paste_text(target['id'], body='TEST 正文\npassword: TESTPASSWORD123456')
        with self.assertRaisesRegex(ValueError, '敏感'):
            self.store.manual_paste(target['id'], secret)
        self.assertEqual(self.count('ri_receipts'), 0)
        leak = self.paste_text(target['id'], body='TEST 正文\n<invoke tool="x"></invoke>')
        result = self.store.manual_paste(target['id'], leak)
        self.assertEqual(result['status'], 'invalid')  # 不是答案的内容不合格
        self.assertIn('B19', result['reason'])

    def test_duplicate_paste_is_idempotent(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        text = self.paste_text(target['id'])
        first = self.store.manual_paste(target['id'], text)
        second = self.store.manual_paste(target['id'], text)
        self.assertEqual(first['id'], second['id'])
        self.assertTrue(second.get('replayed'))
        self.assertEqual(self.count('ri_receipts'), 1)

    def test_waiting_status_is_not_offline_after_300_seconds(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        self.store.manual_prepare(target['id'])
        self.store.manual_mark_sent(target['id'])
        events = self.count('events')
        with mock.patch('time.time', return_value=time.time() + 3600):
            self.store.collect_manual_receipts()
            current = self.store.get(request['id'])
        manual = next(a['manual'] for a in current['assignments'] if a['id'] == target['id'])
        self.assertEqual(manual['status'], '等待用户贴回')  # 不判掉线，不计失败
        self.assertEqual(self.count('events'), events)
        self.assertEqual(self.count('ri_receipts'), 0)
        other = self.assignment(request, 'peer')
        self.store.manual_prepare(other['id'])
        with mock.patch('time.time', return_value=time.time() + 3600):
            current = self.store.get(request['id'])
        manual = next(a['manual'] for a in current['assignments'] if a['id'] == other['id'])
        self.assertEqual(manual['status'], '等待用户发送')

    def test_sent_state_survives_restart(self):
        request = self.request()
        target = self.assignment(request, 'dsh')
        self.store.manual_prepare(target['id'])
        sent = self.store.manual_mark_sent(target['id'])
        restarted = ReviewStore(EventStore(self.native.path), self.root / 'memory-outbox', self.workspaces)
        manual = next(a['manual'] for a in restarted.get(request['id'])['assignments'] if a['id'] == target['id'])
        self.assertEqual(manual['sent_at'], sent['sent_at'])
        self.assertEqual(manual['status'], '等待用户贴回')

    def test_cli_nodes_keep_the_existing_behavior_without_manual_channel(self):
        request = self.store.create(self.candidate['task_id'], 'fixture-dsh', 'fixture-peer',
                                    'TEST 旧通道不受影响', [{'ref': 'fixture-execution', 'sha256': 'a' * 64}],
                                    review_type='revalidation')
        for assignment in request['assignments']:
            self.assertNotIn('manual', assignment)
        with self.assertRaisesRegex(ValueError, '人工通道'):
            self.store.manual_prepare(request['assignments'][0]['id'])
        with self.assertRaisesRegex(ValueError, '人工通道'):
            self.store.manual_paste(request['assignments'][0]['id'], 'TEST')
        self.complete(request)
        self.store.deliver(self.inbox)
        self.assertEqual(self.store.get(request['id'])['state'], 'submitted')


class ManualHttpTests(ManualFixture, unittest.TestCase):
    fetch = fixtures.HttpTest.fetch

    def setUp(self):
        super().setUp()
        self.ctx = fixtures.Context(fixtures.config.load(self.root / 'none.json', {'port': 0}),
                                    fixtures.logs.setup(None), 0)
        module = fixtures.OpenReviewModule(self.ctx, self.store, self.inbox, self.backends)
        app = fixtures.App(self.ctx)
        app.register(module)
        self.server = app.server()
        port = self.server.server_port
        self.ctx.port = port
        self.ctx.hosts = {'127.0.0.1:' + str(port)}
        self.ctx.origins = {'http://127.0.0.1:' + str(port)}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_manual_endpoints_require_csrf_and_reject_bad_input(self):
        for route in ('prepare', 'sent', 'check', 'paste'):
            self.assertEqual(self.fetch('/api/open-review/manual/' + route, {}, False)[0], 403, route)
        request = self.request()
        target = self.assignment(request, 'dsh')
        code, value = self.fetch('/api/open-review/manual/paste',
                                 {'assignment_id': target['id'], 'text': '缺 everything'})
        self.assertEqual(code, 400, value)
        self.assertIn('缺项', value['error'])

    def test_http_paste_loop_and_state_refresh_autocollects_file_receipts(self):
        request = self.request()
        peer = self.assignment(request, 'peer')
        code, value = self.fetch('/api/open-review/manual/prepare', {'assignment_id': peer['id']})
        self.assertEqual(code, 200, value)
        self.assertEqual(value['result']['mode'], 'paste')
        self.fetch('/api/open-review/manual/sent', {'assignment_id': peer['id']})
        code, value = self.fetch('/api/open-review/manual/paste',
                                 {'assignment_id': peer['id'], 'text': self.paste_text(peer['id'])})
        self.assertEqual(code, 200, value)
        self.assertEqual(value['result']['status'], 'identity_pending')
        dsh = self.assignment(request, 'dsh')
        self.fetch('/api/open-review/manual/prepare', {'assignment_id': dsh['id']})
        ticket = self.ticket(dsh['id'])
        self.write_file_receipt(ticket)
        code, state = self.fetch('/api/open-review/state')  # 监看页面刷新即收回执
        self.assertEqual(code, 200, state)
        rows = state['requests'][0]['receipts']
        self.assertEqual({r['status'] for r in rows}, {'identity_pending'})
        self.assertEqual(len(rows), 2)
        self.assertTrue(self.native.verify_event_chain()['ok'])


class ParseUnitTests(unittest.TestCase):
    def test_identity_fields_in_paste_are_ignored_and_case_is_strict(self):
        card = {'receipt_contract': {'version': 2}, 'review_type': 'revalidation'}
        text = ('<!-- receipt:A-1 -->\n审查结论: pass\n证据引用: E\n审查类型: revalidation\n评分: 8\n适用条件: X\n---\n正文\n'
                '<!-- done:A-1 -->')
        with self.assertRaises(ValueError) as error:
            parse_manual_receipt(text, 'A-1', card)
        self.assertIn('审查结论', str(error.exception))
        text = text.replace('审查结论: pass', '审查结论: PASS')
        fields = parse_manual_receipt(text, 'A-1', card)
        self.assertEqual(fields['verdict'], 'PASS')
        self.assertEqual(fields['score'], 8.0)


if __name__ == '__main__':
    unittest.main()
