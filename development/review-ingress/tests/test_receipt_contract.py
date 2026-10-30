from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import time
import unittest
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / 'architecture-package-20261027'
sys.path[:0] = [str(REPO / 'development/integrated-candidate/src'),
                str(PACKAGE / 'shared/工具/小八监看'),
                str(Path(__file__).resolve().parents[1]),
                str(Path(__file__).resolve().parent)]
sys.dont_write_bytecode = True

from xiaoba_agentos.store import EventStore
from xiaoba_review.store import ReviewStore, DecisionInbox, Conflict
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.agent_pool import AgentPool
from xiaoba_review.resource_agy import ReviewReceiptError
from xiaoba_review.resource_agy_cli import AgyCliResource
from test_agy_interactive import InteractiveFixture
import test_review as fixtures


def today():
    return datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()


class ReceiptContractV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.native = EventStore(self.root / 'agentos.sqlite3')
        self.native.initialize()
        self.store = ReviewStore(self.native, self.root / 'memory-outbox')
        self.inbox = DecisionInbox()
        self.candidate = self.native.create_authored_candidate(
            '方法夹具', '隔离测试，非真实经验。', self.root / 'memory-outbox',
            author='test', experience_kind='method')
        self.native.review_memory(self.candidate['task_id'], 'delivery', 'approve',
                                  '仅测试初审', self.candidate['evidence_sha256'])
        for reviewer, kind, ref in [('fixture-dsh', 'dsh', 'fixture-source-a'),
                                     ('fixture-peer', 'peer', 'fixture-source-b')]:
            self.store.register_source({'reviewer_id': reviewer, 'kind': kind,
                'source_ref': ref, 'provenance': '隔离测试夹具，未调用真实后台'})

    def v2_request(self, **kwargs):
        return self.store.create(
            self.candidate['task_id'], 'fixture-dsh', 'fixture-peer',
            '核查夹具方法', [{'ref': 'fixture-execution', 'sha256': 'a'*64}],
            review_type='new_experience', **kwargs)

    def v2_receipt(self, assignment, verdict='PASS', **kwargs):
        card = assignment['card']
        payload = {key: card[key] for key in (
            'review_request_id', 'assignment_id', 'reviewer_role', 'plan_revision',
            'reviewer_id', 'source_ref', 'candidate_sha256', 'input_digest', 'card_digest')}
        payload.update(
            receipt_id='receipt-' + assignment['id'],
            verdict=verdict,
            findings='仅测试夹具审查正文；未执行真实任务。',
            evidence_refs=['fixture-execution'],
            review_type='new_experience',
            score=8,
            applicability='仅测试夹具适用条件说明。',
            started_at=time.time() - 1,
            finished_at=time.time()
        )
        payload.update(kwargs)
        return payload

    def test_v2_evidence_refs_with_null_rejected(self):
        req = self.v2_request()
        assignment = req['assignments'][0]
        # 反例：evidence_refs 包含 null
        bad_receipt = self.v2_receipt(assignment, evidence_refs=[None])
        res = self.store.receive(bad_receipt)
        self.assertEqual(res['status'], 'invalid')
        self.assertIn('证据引用', res['reason'])
        with self.assertRaisesRegex(ValueError, '失效回执不能确认来源'):
            self.store.attest(res['id'], res['digest'], '核对测试')
        # 确认不能让 all_pass 为真
        stored = self.store.get(req['id'])
        self.assertEqual(stored['state'], 'waiting_receipts')
        self.assertEqual(stored['deliveries'], [])

    def test_v2_unmatched_evidence_refs_rejected(self):
        req = self.v2_request()
        assignment = req['assignments'][0]
        # 反例：指不到任务卡材料的引用
        bad_receipt = self.v2_receipt(assignment, evidence_refs=['non-existent-ref'])
        res = self.store.receive(bad_receipt)
        self.assertEqual(res['status'], 'invalid')
        self.assertIn('未对应任务卡材料', res['reason'])
        with self.assertRaisesRegex(ValueError, '失效回执不能确认来源'):
            self.store.attest(res['id'], res['digest'], '核对测试')
        stored = self.store.get(req['id'])
        self.assertEqual(stored['state'], 'waiting_receipts')
        self.assertEqual(stored['deliveries'], [])

    def test_v2_missing_score_rejected(self):
        req = self.v2_request()
        assignment = req['assignments'][0]
        # 反例：缺 score
        bad_receipt = self.v2_receipt(assignment)
        del bad_receipt['score']
        res = self.store.receive(bad_receipt)
        self.assertEqual(res['status'], 'invalid')
        self.assertIn('缺少评分', res['reason'])
        with self.assertRaisesRegex(ValueError, '失效回执不能确认来源'):
            self.store.attest(res['id'], res['digest'], '核对测试')
        stored = self.store.get(req['id'])
        self.assertEqual(stored['state'], 'waiting_receipts')

    def test_v2_score_eleven_rejected(self):
        req = self.v2_request()
        assignment = req['assignments'][0]
        # 反例：score=11 超出有效范围
        bad_receipt = self.v2_receipt(assignment, score=11)
        res = self.store.receive(bad_receipt)
        self.assertEqual(res['status'], 'invalid')
        self.assertIn('评分超出有效范围', res['reason'])
        with self.assertRaisesRegex(ValueError, '失效回执不能确认来源'):
            self.store.attest(res['id'], res['digest'], '核对测试')
        # 负数评分同样被拒绝
        neg_receipt = self.v2_receipt(assignment, receipt_id='receipt-neg-' + assignment['id'], score=-1)
        res_neg = self.store.receive(neg_receipt)
        self.assertEqual(res_neg['status'], 'invalid')

    def test_v2_missing_applicability_rejected(self):
        req = self.v2_request()
        assignment = req['assignments'][0]
        # 反例：缺 applicability
        bad_receipt = self.v2_receipt(assignment)
        del bad_receipt['applicability']
        res = self.store.receive(bad_receipt)
        self.assertEqual(res['status'], 'invalid')
        self.assertIn('缺少适用条件', res['reason'])
        # 空白文本同样被拒绝
        empty_receipt = self.v2_receipt(assignment, receipt_id='receipt-empty-' + assignment['id'], applicability='   ')
        res_empty = self.store.receive(empty_receipt)
        self.assertEqual(res_empty['status'], 'invalid')
        with self.assertRaisesRegex(ValueError, '失效回执不能确认来源'):
            self.store.attest(res['id'], res['digest'], '核对测试')

    def test_v1_legacy_receipt_still_accepted(self):
        # 第 1 版旧任务卡：不含 score、applicability、review_type，且引用非卡内 ref 仍可通过
        req = self.store.create(
            self.candidate['task_id'], 'fixture-dsh', 'fixture-peer',
            '核查夹具方法', [{'ref': 'fixture-execution', 'sha256': 'a'*64}],
            contract_version=1)
        for a in req['assignments']:
            card = a['card']
            self.assertEqual(card['receipt_contract']['version'], 1)
            payload = {key: card[key] for key in (
                'review_request_id', 'assignment_id', 'reviewer_role', 'plan_revision',
                'reviewer_id', 'source_ref', 'candidate_sha256', 'input_digest', 'card_digest')}
            payload.update(
                receipt_id='receipt-v1-' + a['id'],
                verdict='PASS',
                findings='第 1 版历史夹具审查正文。',
                evidence_refs=['legacy-unbound-ref'],
                started_at=time.time() - 1,
                finished_at=time.time()
            )
            res = self.store.receive(payload)
            self.assertEqual(res['status'], 'identity_pending')
            self.store.attest(res['id'], res['digest'], '测试第 1 版来源核对')

        # 两份旧版回执核验完成后，正常形成材料包且 all_pass 为真
        stored = self.store.get(req['id'])
        self.assertEqual(stored['state'], 'pending_center')
        self.assertEqual(len(stored['deliveries']), 1)
        with self.store.transaction() as db:
            packet_row = db.execute("SELECT data FROM ri_outbox WHERE request_id=?", (req['id'],)).fetchone()
            packet = json.loads(packet_row['data'])
            self.assertTrue(packet['all_pass'])

    def test_v2_valid_receipt_accepted_and_packet_carries_scores(self):
        # 正例：第 2 版合格回执正常计分并分开携带 scores
        req = self.v2_request()
        scores = {'dsh': 7, 'peer': 9}
        for a in req['assignments']:
            role = a['role']
            rec = self.v2_receipt(a, verdict='PASS', score=scores[role])
            res = self.store.receive(rec)
            self.assertEqual(res['status'], 'identity_pending')
            self.store.attest(res['id'], res['digest'], f'核对 {role} 来源')

        stored = self.store.get(req['id'])
        self.assertEqual(stored['state'], 'pending_center')
        self.assertEqual(len(stored['deliveries']), 1)
        with self.store.transaction() as db:
            packet_row = db.execute("SELECT data FROM ri_outbox WHERE request_id=?", (req['id'],)).fetchone()
            packet = json.loads(packet_row['data'])
            self.assertTrue(packet['all_pass'])
            # 审查材料包把每位审查员的 score 分开带上
            self.assertEqual(packet.get('scores'), {'dsh': 7, 'peer': 9})


class TaskScoringQualificationTests(InteractiveFixture, unittest.TestCase):
    def business(self, node, task='TASK-business', assignment='ASSIGN-1', step='REVIEW-01'):
        adapter = self.backends.adapter_for(self.source(node))
        return adapter.run({'task_id': task, 'assignment_id': assignment, 'step_code': step,
            'resource_plan': {'model': self.source(node)['node']['model']}, 'card_digest': 'TEST'})

    def test_unqualified_review_cannot_be_scored_good_in_task_scoring(self):
        node = self.add()
        self.probe(node)
        (self.root / 'bad-review-fields').touch()
        # 结构不合格回执（缺少有效审查字段）抛出 ReviewReceiptError
        with self.assertRaises(ReviewReceiptError):
            self.business(node, task='TASK-unqualified')

        facts = self.pool.runtime.workday_inputs(node, today())['tasks']
        business = next(v for v in facts if not v['health_check'])
        # 工作事实标明审查是否合格
        self.assertTrue(business.get('is_review'))
        self.assertFalse(business.get('review_qualified'))

        scorer = self.pool.runtime.scoring
        values = dict(
            node_id=node, day=today(), task_id='TASK-unqualified', task_type='code-review',
            outcome='good', evidence_refs=['核验依据'], expected_revision=0, assessment_id='ASSESS-FAIL-GOOD')

        # 对不合格审查拒绝评 good
        with self.assertRaisesRegex(ValueError, '审查不合格的回执不能记为完成得好'):
            scorer.assess(**values)

        # 评分未增加，周分为 0
        week = scorer.weekly(today(), 'code-review')
        self.assertEqual(week['ranking'], [])

        # not_good 仍可记
        not_good_values = dict(values, outcome='not_good', assessment_id='ASSESS-NOT-GOOD')
        recorded = scorer.assess(**not_good_values)
        self.assertEqual(recorded['points'], 0)
        self.assertEqual(recorded['outcome'], 'not_good')

    def test_qualified_review_scores_good_in_task_scoring(self):
        node = self.add('TEST-qualified-node')
        self.probe(node)
        # 合格审查执行
        self.business(node, task='TASK-qualified')

        facts = self.pool.runtime.workday_inputs(node, today())['tasks']
        business = next(v for v in facts if not v['health_check'])
        self.assertTrue(business.get('is_review'))
        self.assertTrue(business.get('review_qualified'))

        scorer = self.pool.runtime.scoring
        values = dict(
            node_id=node, day=today(), task_id='TASK-qualified', task_type='code-review',
            outcome='good', evidence_refs=['核验依据通过'], expected_revision=0, assessment_id='ASSESS-QUAL-GOOD')

        # 合格审查回执正常计分 +1
        recorded = scorer.assess(**values)
        self.assertEqual(recorded['points'], 1)
        self.assertEqual(recorded['outcome'], 'good')
        week = scorer.weekly(today(), 'code-review')
        self.assertEqual(week['ranking'][0]['points'], 1)


class Card1FollowupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.native = EventStore(self.root / 'agentos.sqlite3')
        self.native.initialize()
        self.store = ReviewStore(self.native, self.root / 'memory-outbox')
        self.backends = BackendRegistry(self.store)
        self.pool = AgentPool(self.store, self.backends)
        self.store.register_source({
            'reviewer_id': 'fixture-dsh', 'kind': 'dsh',
            'source_ref': 'fixture-dsh-ref', 'provenance': '测试'
        })

    def test_failure_with_resource_plan_invalidates_node_test_even_when_require_test_is_false(self):
        # 对应卡 1 留下的 backends.py:123/128：带 resource_plan 的卡失败后使资格失效
        entry_path = self.root / 'fake-cli'
        entry_path.touch()
        source = self.pool.add({
            'node_id': 'node-gated-test', 'name': '计划节点', 'agent': 'custom',
            'kind': 'peer', 'interface': 'cli', 'entry': str(entry_path), 'model': 'Gemini 3.8 Flash'
        })
        source = self.pool.change('node-gated-test', source['node']['revision'], 'toggle', True)

        class GatedAdapter:
            kind = 'peer'
            source_ref = source['source_ref']
            supports_resource_plan = True
            def resource_identity(self):
                return {'TEST': 'gated identity'}
            def test_resource(self, plan, challenge):
                return {'output': challenge, 'model': plan['model'], 'provider': 'TEST', 'route_source': 'TEST'}
            def run(self, card):
                raise RuntimeError('实际调用失败')

        self.backends.register('node-gated-test', GatedAdapter())
        revision = source['node']['revision']
        test_res = self.pool.runtime.test_run('node-gated-test', revision, 'TEST-gated-gate')
        self.assertEqual(test_res['status'], 'passed')

        with self.native.read_only() as db:
            s = self.store.source(db, 'node-gated-test')
            self.assertTrue(self.pool.runtime.readiness(db, s)['ready'])

        candidate = self.native.create_authored_candidate(
            '测试经验2', '正文', self.root / 'memory-outbox', author='test', experience_kind='method')
        self.native.review_memory(candidate['task_id'], 'delivery', 'approve', '初审', candidate['evidence_sha256'])

        req = self.store.create(
            candidate['task_id'], 'fixture-dsh', 'node-gated-test',
            '范围', [{'ref': 'ref-1', 'sha256': 'a'*64}],
            dsh_channel='external_handoff', peer_channel='backend_adapter')
        assignment = next(a for a in req['assignments'] if a['role'] == 'peer')

        # 即使 require_test=False，但卡带 resource_plan，调用失败也必须使资格失效
        with self.assertRaises(RuntimeError):
            self.backends.execute(assignment['id'], require_test=False)

        with self.store.transaction() as db:
            after = self.store.source(db, 'node-gated-test')
            self.assertEqual(after['node']['test']['status'], 'failed')
            self.assertEqual(after['node']['test']['reason'], 'task_execution_failed')
            self.assertFalse(self.pool.runtime.readiness(db, after)['ready'])


class Card21AcceptanceTests(fixtures.HttpTest):
    """卡 21 验收反例与新功能测试。"""

    def setUp(self):
        super().setUp()
        self.workspaces = self.root / 'workspaces'
        self.workspaces.mkdir(exist_ok=True)
        from xiaoba_review.resource_stdio import json_stdio_factory
        self.module.backends.resource_factory = lambda source: json_stdio_factory(source, self.workspaces, agy_mode='cli')
        self.backends = self.module.backends
        self.pool = self.module.pool
        self.entry_agy = self.root / 'agy'
        self.entry_agy.touch()
        self.entry_agy.chmod(0o700)

    def test_http_create_task_card_without_review_type_rejected_with_it_creates_v2(self):
        # 1. 通过 HTTP 创建任务卡不带 review_type：被拒
        payload_missing = {
            'candidate_id': self.candidate['task_id'],
            'dsh_id': 'fixture-dsh',
            'peer_id': 'fixture-peer',
            'scope': '测试范围',
            'evidence': [{'ref': 'ref-1', 'sha256': 'a' * 64}]
        }
        code, res = self.fetch('/api/open-review/requests', payload_missing)
        self.assertEqual(code, 400)
        self.assertFalse(res['ok'])
        self.assertIn('review_type', res['error'])

        # 2. 带了 review_type：任务卡是第 2 版
        payload_valid = dict(payload_missing, review_type='new_experience')
        code, res = self.fetch('/api/open-review/requests', payload_valid)
        self.assertEqual(code, 200)
        self.assertTrue(res['ok'])
        req = res['result']
        for a in req['assignments']:
            self.assertEqual(a['card']['receipt_contract']['version'], 2)
            self.assertEqual(a['card']['review_type'], 'new_experience')

    def test_http_dispatch_requires_review_type_and_no_interface_accepts_contract_version(self):
        # 卡 21 遗留：主要派工路径也必须指定审查类型；两个创建接口都不再接受 contract_version。
        base = {'candidate_id': self.candidate['task_id'], 'scope': '测试范围',
                'evidence': [{'ref': 'ref-1', 'sha256': 'a' * 64}], 'request_id': 'TEST-dispatch-1'}
        code, res = self.fetch('/api/agent-pool/dispatch', base)
        self.assertEqual(code, 400)
        self.assertIn('review_type', res['error'])

        code, res = self.fetch('/api/agent-pool/dispatch',
                               dict(base, review_type='new_experience', contract_version=1))
        self.assertEqual(code, 400)
        self.assertIn('contract_version', res['error'])

        code, res = self.fetch('/api/open-review/requests', {
            'candidate_id': self.candidate['task_id'], 'dsh_id': 'fixture-dsh', 'peer_id': 'fixture-peer',
            'scope': '测试范围', 'evidence': [{'ref': 'ref-1', 'sha256': 'a' * 64}],
            'review_type': 'new_experience', 'contract_version': 1})
        self.assertEqual(code, 400)
        self.assertIn('contract_version', res['error'])
        # 被拒绝的请求一张卡都不留下，第 1 版只能来自已存在的任务卡。
        self.assertEqual(self.count('ri_requests'), 0)

    def test_json_stdio_run_forwards_scores_field_to_the_receipt(self):
        # 卡 21 遗留：run 改成白名单后丢了 scores，backends 仍会透传它。
        from xiaoba_review.resource_stdio import JsonStdioResource
        adapter = JsonStdioResource({'kind': 'peer', 'source_ref': 'TEST-scores',
                                     'node': {'entry': '/TEST/bin/entry'}})
        adapter.call = lambda payload: {
            'protocol': 'xiaoba-resource-v1', 'card_digest': 'TEST-DIGEST', 'verdict': 'PASS',
            'findings': 'TEST 仅测试白名单透传', 'evidence_refs': ['ref-1'],
            'review_type': 'revalidation', 'score': 8, 'scores': {'dsh': 8, 'peer': 9},
            'applicability': '仅测试夹具', 'duplicate_of': None}
        result = adapter.run({'card_digest': 'TEST-DIGEST'})
        self.assertEqual(result['scores'], {'dsh': 8, 'peer': 9})
        self.assertEqual(result['score'], 8)

    def test_fake_agy_via_agy_cli_writes_full_receipt_valid_and_scores_in_packet(self):
        # 假 agy 经 AgyCliResource.run 写出带全字段的回执：回执 valid，score 进入材料包
        entry = self.root / 'fake-agy-cli'
        entry.write_text('#!' + sys.executable + '\n' + textwrap.dedent(r'''
            import json, os, re, sys
            from pathlib import Path
            message = json.loads(sys.stdin.readline())
            prompt = message['message']['content']
            cardpath = Path(json.loads(prompt.split('XIAOBA_TASK_CARD=', 1)[1]))
            card = cardpath.read_text()
            logpath = Path(re.search(r'^工作回执文件: (.+)$', card, re.M)[1])
            taskid = re.search(r'^任务ID: (.+)$', card, re.M)[1]
            step = re.search(r'^步骤码: (.+)$', card, re.M)[1]
            nonce = re.search(r'^本轮标识: (.+)$', card, re.M)[1]
            if not logpath.exists():
                header = card.split('先写以下文件头：\n\n', 1)[1].split('\n文件已存在时', 1)[0]
                logpath.write_text(header)
            meta = (f'任务ID: {taskid}\n步骤码: {step}\n状态: completed\n'
                    '审查结论: PASS\n证据引用: ref-1\n'
                    '审查类型: new_experience\n评分: 9\n适用条件: 仅测试夹具环境\n重复经验: 无\n')
            with logpath.open('a') as out:
                out.write('\n<!-- task:' + nonce + ' -->\n' + meta + '---\n完整审查正文\n<!-- done:' + nonce + ' -->\n')
            print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'written'}}))
        '''))
        entry.chmod(0o700)

        source = self.pool.add({
            'node_id': 'node-cli-full', 'name': '全字段节点', 'agent': 'custom',
            'kind': 'peer', 'interface': 'cli', 'entry': str(entry), 'model': 'Gemini 3.8 Flash'
        })
        source = self.pool.change(source['reviewer_id'], source['node']['revision'], 'toggle', True)
        adapter = AgyCliResource(source, self.workspaces)
        card = {
            'task_id': 'TASK-CLI-FULL', 'assignment_id': 'ASSIGN-CLI-FULL', 'step_code': 'REVIEW-01',
            'receipt_contract': {'version': 2},
            'evidence_refs': [{'ref': 'ref-1', 'sha256': 'a' * 64}],
            'resource_plan': {'model': 'Gemini 3.8 Flash'}
        }
        res = adapter.run(card)
        self.assertEqual(res['verdict'], 'PASS')
        self.assertEqual(res['score'], 9)
        self.assertEqual(res['review_type'], 'new_experience')
        self.assertEqual(res['applicability'], '仅测试夹具环境')

        # 验证 score 进入材料包
        req = self.store.create(
            self.candidate['task_id'], 'fixture-dsh', source['reviewer_id'],
            '范围', [{'ref': 'ref-1', 'sha256': 'a' * 64}], review_type='new_experience')
        dsh_assign = next(a for a in req['assignments'] if a['role'] == 'dsh')
        peer_assign = next(a for a in req['assignments'] if a['role'] == 'peer')

        # dsh 回执
        self.store.receive({
            'receipt_id': 'r-dsh', 'card_digest': dsh_assign['card']['card_digest'],
            'review_request_id': req['id'], 'assignment_id': dsh_assign['id'],
            'reviewer_role': 'dsh', 'plan_revision': req['revision'],
            'reviewer_id': 'fixture-dsh', 'source_ref': 'fixture-source-a',
            'input_digest': dsh_assign['card']['input_digest'], 'candidate_sha256': self.candidate['evidence_sha256'],
            'verdict': 'PASS', 'findings': 'DSH PASS', 'evidence_refs': ['ref-1'],
            'review_type': 'new_experience', 'score': 8, 'applicability': 'DSH适用条件',
            'started_at': time.time(), 'finished_at': time.time()
        })
        self.store.attest('r-dsh', self.store.get(req['id'])['receipts'][0]['digest'], '核对DSH')

        # peer 回执由 adapter 运行结果组装
        peer_payload = {
            'receipt_id': 'r-peer', 'card_digest': peer_assign['card']['card_digest'],
            'review_request_id': req['id'], 'assignment_id': peer_assign['id'],
            'reviewer_role': 'peer', 'plan_revision': req['revision'],
            'reviewer_id': source['reviewer_id'], 'source_ref': source['source_ref'],
            'input_digest': peer_assign['card']['input_digest'], 'candidate_sha256': self.candidate['evidence_sha256'],
            'started_at': time.time(), 'finished_at': time.time(),
            **res
        }
        self.store.receive(peer_payload)
        self.store.attest('r-peer', self.store.get(req['id'])['receipts'][1]['digest'], '核对Peer')

        with self.store.transaction() as db:
            row = db.execute("SELECT data FROM ri_outbox WHERE request_id=?", (req['id'],)).fetchone()
            packet = json.loads(row['data'])
            self.assertEqual(packet.get('scores'), {'dsh': 8, 'peer': 9})

    def test_fake_agy_via_agy_cli_missing_score_raises_review_receipt_error_preserves_health_and_body(self):
        # 同样路径缺评分：走 ReviewReceiptError，节点健康资格保留，正文保留
        entry = self.root / 'fake-agy-cli-noscore'
        entry.write_text('#!' + sys.executable + '\n' + textwrap.dedent(r'''
            import json, os, re, sys
            from pathlib import Path
            message = json.loads(sys.stdin.readline())
            prompt = message['message']['content']
            cardpath = Path(json.loads(prompt.split('XIAOBA_TASK_CARD=', 1)[1]))
            card = cardpath.read_text()
            logpath = Path(re.search(r'^工作回执文件: (.+)$', card, re.M)[1])
            taskid = re.search(r'^任务ID: (.+)$', card, re.M)[1]
            step = re.search(r'^步骤码: (.+)$', card, re.M)[1]
            nonce = re.search(r'^本轮标识: (.+)$', card, re.M)[1]
            if not logpath.exists():
                header = card.split('先写以下文件头：\n\n', 1)[1].split('\n文件已存在时', 1)[0]
                logpath.write_text(header)
            meta = (f'任务ID: {taskid}\n步骤码: {step}\n状态: completed\n'
                    '审查结论: PASS\n证据引用: ref-1\n'
                    '审查类型: new_experience\n适用条件: 缺少评分条件\n')
            with logpath.open('a') as out:
                out.write('\n<!-- task:' + nonce + ' -->\n' + meta + '---\n已保留的工作正文测试\n<!-- done:' + nonce + ' -->\n')
            print(json.dumps({'event': 'result', 'result': {'status': 'SUCCESS', 'response': 'written'}}))
        '''))
        entry.chmod(0o700)

        source = self.pool.add({
            'node_id': 'node-cli-noscore', 'name': '无评分节点', 'agent': 'custom',
            'kind': 'peer', 'interface': 'cli', 'entry': str(entry), 'model': 'Gemini 3.8 Flash'
        })
        adapter = AgyCliResource(source, self.workspaces)
        card = {
            'task_id': 'TASK-CLI-NOSCORE', 'assignment_id': 'ASSIGN-CLI-NOSCORE', 'step_code': 'REVIEW-01',
            'receipt_contract': {'version': 2},
            'evidence_refs': [{'ref': 'ref-1', 'sha256': 'a' * 64}],
            'resource_plan': {'model': 'Gemini 3.8 Flash'}
        }
        with self.assertRaises(ReviewReceiptError) as ctx:
            adapter.run(card)
        self.assertTrue(getattr(ctx.exception, 'preserve_node_health', False))

        # 正文保留在工作日志文件中
        log_files = list(adapter.workspace.path.glob('*-工作内容.md'))
        self.assertTrue(bool(log_files))
        content = log_files[0].read_text()
        self.assertIn('已保留的工作正文测试', content)

    def test_object_evidence_ref_sha256_mismatch_marks_review_unqualified_and_blocks_good(self):
        # 对象引用 sha256 对不上：工作事实 review_qualified 为假，评 good 被拒
        node = self.pool.add({
            'node_id': 'node-obj-ref', 'name': '对象引用节点', 'agent': 'custom',
            'kind': 'peer', 'interface': 'cli', 'entry': str(self.entry_agy), 'model': 'Gemini 3.8 Flash'
        })
        with self.native.read_only() as db:
            source = self.store.source(db, node['reviewer_id'])
        adapter = self.backends.adapter_for(source)
        workspace = adapter.workspace

        context_data = {
            'receipt_contract': {'version': 2},
            'evidence_refs': [{'ref': 'evidence-item-1', 'sha256': 'a' * 64}]
        }
        payload = {
            'task_id': 'TASK-REF-MISMATCH',
            'step_code': 'REVIEW-01',
            'model': 'Gemini 3.8 Flash',
            'context': context_data
        }
        record = workspace.begin('review', 'TASK-REF-MISMATCH', payload)
        ticket = adapter.workday.prepare(record, 'TASK-REF-MISMATCH', 'REVIEW-01', '审查指令', context_data)

        # 写入合法的当日 Markdown 日志块
        prefix = ticket['log'].read_bytes()[:ticket['prefix_size']].decode() if ticket['prefix_size'] else ticket['header']
        body = (
            prefix + '\n' + ticket['marker'] + '\n' +
            '任务ID: TASK-REF-MISMATCH\n步骤码: REVIEW-01\n状态: completed\n' +
            '审查结论: PASS\n证据引用: evidence-item-1\n审查类型: new_experience\n评分: 9\n适用条件: 限制说明\n' +
            '---\n完整审查正文\n' + ticket['done'] + '\n'
        )
        ticket['log'].write_text(body)

        value = adapter.workday.read(ticket)
        self.assertIsNotNone(value)
        # 将回执中的证据引用设为对象，但 sha256 不匹配 ('b'*64 != 'a'*64)
        value['evidence_refs'] = [{'ref': 'evidence-item-1', 'sha256': 'b' * 64}]

        evidence = dict(adapter._evidence(), reply_source='daily_worklog', worklog=value['worklog'])
        workspace.finish(record, 'execution_completed', value, evidence)

        facts = self.pool.runtime.workday_inputs(node['reviewer_id'], today())['tasks']
        fact = next(f for f in facts if f['task_id'] == 'TASK-REF-MISMATCH')
        self.assertTrue(fact['is_review'])
        self.assertFalse(fact['review_qualified'])

        scorer = self.pool.runtime.scoring
        with self.assertRaisesRegex(ValueError, '审查不合格的回执不能记为完成得好'):
            scorer.assess(
                node_id=node['reviewer_id'], day=today(), task_id='TASK-REF-MISMATCH', task_type='code-review',
                outcome='good', evidence_refs=['核验证据'], expected_revision=0, assessment_id='ASSESS-MISMATCH-GOOD'
            )

    def test_json_stdio_entry_returns_score_fields_to_receipt(self):
        # JSON stdio 入口返回带评分的结果：字段到达回执
        entry = self.root / 'fake-json-stdio'
        entry.write_text('#!' + sys.executable + '\n' + textwrap.dedent(r'''
            import json, sys
            msg = json.load(sys.stdin)
            op = msg.get('operation')
            if op == 'test':
                print(json.dumps({
                    'protocol': 'xiaoba-resource-v1',
                    'challenge': msg['challenge'],
                    'output': msg['challenge'],
                    'model': msg['resource_plan']['model'],
                    'provider': 'TEST provider',
                    'route_source': 'TEST stdio'
                }))
            elif op == 'review':
                c = msg['card']
                print(json.dumps({
                    'protocol': 'xiaoba-resource-v1',
                    'card_digest': c['card_digest'],
                    'verdict': 'PASS',
                    'findings': 'JSON stdio 独立审查通过',
                    'evidence_refs': [{'ref': 'ref-1', 'sha256': 'a' * 64}],
                    'review_type': 'revalidation',
                    'score': 8.5,
                    'applicability': '适用于容器化部署',
                    'duplicate_of': 'DUP-EXP-1',
                    'execution_evidence': {'model': c.get('resource_plan', {}).get('model', 'TEST')}
                }))
        '''))
        entry.chmod(0o700)

        source = self.pool.add({
            'node_id': 'node-json-stdio', 'name': 'JSON Stdio 节点', 'agent': 'custom',
            'kind': 'peer', 'interface': 'cli', 'entry': str(entry), 'model': 'Gemini 3.8 Flash'
        })
        source = self.pool.change(source['reviewer_id'], source['node']['revision'], 'toggle', True)
        self.pool.runtime.test_run(source['reviewer_id'], source['node']['revision'], 'TEST-CHALLENGE')

        req = self.store.create(
            self.candidate['task_id'], 'fixture-dsh', source['reviewer_id'],
            '范围', [{'ref': 'ref-1', 'sha256': 'a' * 64}],
            review_type='revalidation', peer_channel='backend_adapter'
        )
        peer_assign = next(a for a in req['assignments'] if a['role'] == 'peer')
        res = self.backends.execute(peer_assign['id'], require_test=True)
        self.assertEqual(res['status'], 'valid')

        receipt = self.store.get(req['id'])['receipts'][0]
        data = receipt['data']
        self.assertEqual(data['review_type'], 'revalidation')
        self.assertEqual(data['score'], 8.5)
        self.assertEqual(data['applicability'], '适用于容器化部署')
        self.assertEqual(data['duplicate_of'], 'DUP-EXP-1')
        self.assertEqual(data['verdict'], 'PASS')


if __name__ == '__main__':
    unittest.main()
