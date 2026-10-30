"""独立性按“入口程序＋模型”核对；卡 9 的反例。夹具只在本机临时目录，不调用真实模型。"""
import sqlite3
import unittest

import test_review as fixtures
from xiaoba_agentos.validation_evidence import binding, checksum, independence_key, ingress_evidence
from xiaoba_review.agent_pool import AgentPool, independence
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.store import canonical


class NodeIndependenceTests(fixtures.FixtureMixin, unittest.TestCase):
    """创建、汇总、换第二方都走同一个独立性键。"""

    def setUp(self):
        super().setUp()
        self.backends = BackendRegistry(self.store)
        self.pool = AgentPool(self.store, self.backends)

    def node(self, node_id, kind, entry, model, name=None):
        source = self.pool.add({'node_id': node_id, 'name': name or node_id, 'agent': 'TEST agent',
                                'kind': kind, 'interface': 'cli', 'entry': entry, 'model': model})
        return self.pool.change(node_id, source['node']['revision'], 'toggle', True)

    def request(self, dsh, peer, **kwargs):
        return self.store.create(self.candidate['task_id'], dsh['reviewer_id'], peer['reviewer_id'],
                                 'TEST 独立性核对', [{'ref': 'TEST evidence', 'sha256': 'a' * 64}],
                                 review_type='revalidation', **kwargs)

    def test_same_entry_two_models_are_two_sources(self):
        dsh = self.node('TEST-ind-dsh', 'dsh', '/TEST/bin/shared', 'TEST-model-A')
        peer = self.node('TEST-ind-peer', 'peer', '/TEST/bin/shared', 'TEST-model-B')
        self.assertEqual(dsh['source_ref'], peer['source_ref'])  # 入口程序相同
        self.assertNotEqual(independence(dsh), independence(peer))  # 模型不同，算两个
        request = self.request(dsh, peer)
        self.assertEqual(len(request['assignments']), 2)
        plans = {a['role']: a['card']['resource_plan']['model'] for a in request['assignments']}
        self.assertEqual(plans, {'dsh': 'TEST-model-A', 'peer': 'TEST-model-B'})
        # 汇总路径同一规则：两份合格回执能形成材料包，不被“来源不独立”挡住。
        self.complete(request)
        self.assertEqual(self.store.get(request['id'])['state'], 'pending_center')

    def test_same_entry_same_model_different_names_are_one_source(self):
        dsh = self.node('TEST-ind-dsh', 'dsh', '/TEST/bin/shared', 'TEST-model-A', name='TEST 名字一')
        peer = self.node('TEST-ind-peer', 'peer', '/TEST/bin/shared', 'TEST-model-A', name='TEST 名字二')
        self.assertEqual(independence(dsh), independence(peer))
        with self.assertRaisesRegex(ValueError, '来源'):
            self.request(dsh, peer)
        self.assertEqual(self.count('ri_requests'), 0)

    def test_different_entries_same_model_are_two_sources(self):
        dsh = self.node('TEST-ind-dsh', 'dsh', '/TEST/bin/one', 'TEST-model-A')
        peer = self.node('TEST-ind-peer', 'peer', '/TEST/bin/two', 'TEST-model-A')
        self.assertNotEqual(dsh['source_ref'], peer['source_ref'])
        self.assertNotEqual(independence(dsh), independence(peer))
        self.assertEqual(len(self.request(dsh, peer)['assignments']), 2)

    def test_replaced_peer_uses_the_same_key(self):
        dsh = self.node('TEST-ind-dsh', 'dsh', '/TEST/bin/shared', 'TEST-model-A')
        peer = self.node('TEST-ind-peer', 'peer', '/TEST/bin/shared', 'TEST-model-B')
        twin = self.node('TEST-ind-twin', 'peer', '/TEST/bin/shared', 'TEST-model-A', name='TEST 同名模型')
        request = self.request(dsh, peer)
        with self.assertRaisesRegex(ValueError, '不独立'):
            self.store.replace_peer(request['id'], twin['reviewer_id'], 'TEST 换成同入口同模型')
        current = self.store.get(request['id'])
        self.assertEqual(current['revision'], request['revision'])
        self.assertEqual(len(current['assignments']), 2)

    def test_legacy_four_field_sources_keep_source_ref_only(self):
        legacy = {'reviewer_id': 'TEST-legacy', 'kind': 'peer',
                  'source_ref': 'TEST-legacy-ref', 'provenance': 'TEST 旧四字段来源'}
        self.assertEqual(independence(legacy), 'TEST-legacy-ref')
        self.assertEqual(independence_key('TEST-legacy-ref'), 'TEST-legacy-ref')
        request = self.store.create(self.candidate['task_id'], 'fixture-dsh', 'fixture-peer', 'TEST 旧来源',
                                    [{'ref': 'TEST', 'sha256': 'a' * 64}], review_type='new_experience')
        self.assertEqual(len(request['assignments']), 2)
        self.store.register_source({'reviewer_id': 'TEST-alias', 'kind': 'peer',
                                    'source_ref': 'fixture-source-a', 'provenance': 'TEST 同来源别名'})
        with self.assertRaisesRegex(ValueError, '来源'):
            self.store.create(self.candidate['task_id'], 'fixture-dsh', 'TEST-alias', 'TEST 旧来源别名',
                              [{'ref': 'TEST', 'sha256': 'a' * 64}], review_type='new_experience')


class PathConsistencyTests(fixtures.FixtureMixin, unittest.TestCase):
    """开工和 prepare 两条路径用同一个键，结论一致。"""

    class Adapter:
        supports_resource_plan = True
        supports_workday_receipts = True

        def __init__(self, source):
            self.kind, self.source_ref = source['kind'], source['source_ref']

        def resource_identity(self):
            return {'TEST': 'independence adapter'}

        def test_resource(self, plan, challenge):
            return {'output': challenge, 'model': plan['model'], 'provider': 'TEST', 'route_source': 'TEST'}

        def initialize_workday(self, evidence, key):
            return {'TEST workday': key}

    def setUp(self):
        super().setUp()
        self.backends = BackendRegistry(self.store)
        self.pool = AgentPool(self.store, self.backends)
        self.runtime = self.pool.runtime

    def pair(self, suffix, model_a, model_b, entry='/TEST/bin/shared'):
        nodes = []
        for node_id, kind, model in (('TEST-p-dsh-' + suffix, 'dsh', model_a),
                                     ('TEST-p-peer-' + suffix, 'peer', model_b)):
            source = self.pool.add({'node_id': node_id, 'name': node_id, 'agent': 'TEST',
                                    'kind': kind, 'interface': 'cli', 'entry': entry, 'model': model})
            source = self.pool.change(node_id, source['node']['revision'], 'toggle', True)
            self.backends.adapters[node_id] = self.Adapter(source)
            nodes.append(node_id)
        return nodes

    def probe(self, node_id):
        revision = next(n['revision'] for n in self.pool.state()['nodes'] if n['node_id'] == node_id)
        return self.runtime.test_run(node_id, revision, 'TEST-ind-' + node_id)

    def test_workday_and_prepare_agree_on_independence(self):
        same = self.pair('same', 'TEST-model-A', 'TEST-model-A')
        for node in same:
            self.assertEqual(self.probe(node)['status'], 'passed')
        selections = {'dsh': same[0], 'peer': same[1]}
        prepared = self.runtime.prepare(['dsh', 'peer'], selections)
        self.assertEqual(prepared['route'], 'ask_user')
        self.assertEqual(prepared['questions'][0]['field'], 'independent_source')
        workday = self.runtime.start_workday('上班了小八助理', 'TEST-START-SAME', ['dsh', 'peer'], selections)
        self.assertEqual(workday['route'], 'blocked')
        self.assertEqual(workday['blockers'], [{'reason': 'independent_source_required'}])
        self.assertEqual(workday['model_tasks_sent'], 0)

        other = self.pair('diff', 'TEST-model-A', 'TEST-model-B')
        for node in other:
            self.assertEqual(self.probe(node)['status'], 'passed')
        selections = {'dsh': other[0], 'peer': other[1]}
        self.assertEqual(self.runtime.prepare(['dsh', 'peer'], selections)['route'], 'run')
        workday = self.runtime.start_workday('上班了小八助理', 'TEST-START-DIFF', ['dsh', 'peer'], selections)
        self.assertEqual(workday['route'], 'ready', workday)
        self.assertTrue(workday['work_logs_initialized'])


class IngressEvidenceTests(unittest.TestCase):
    """终审入口按同一规则数来源：同一入口两个模型算两个。"""

    def fixture(self, dsh_model, peer_model, shared_entry=True, legacy=False):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript('''
            CREATE TABLE ri_requests (id TEXT, candidate_id TEXT, revision INTEGER, common TEXT, fingerprint TEXT, state TEXT);
            CREATE TABLE ri_assignments (id TEXT, request_id TEXT, role TEXT, source_id TEXT, active INTEGER, card TEXT, running TEXT);
            CREATE TABLE ri_receipts (id TEXT, request_id TEXT, assignment_id TEXT, data TEXT, digest TEXT, status TEXT, reason TEXT, attestation TEXT);
            CREATE TABLE ri_sources (id TEXT, data TEXT);
            CREATE TABLE ri_inbox (id TEXT, request_id TEXT, data TEXT, digest TEXT, current INTEGER, ack_at REAL);''')
        record = {'id': 'TEST-ATTEMPT', 'round': 1, 'task_id': 'TEST-CAND',
                  'plan_digest': 'p' * 64, 'execution_digest': 'e' * 64}
        candidate = {'task_id': 'TEST-CAND', 'evidence_sha256': 'c' * 64}
        approved = {'id': 'TEST-DELIVERY'}
        execution = {'finished_at': 1000.0}
        common = {'candidate_id': 'TEST-CAND', 'candidate_sha256': 'c' * 64,
                  'delivery_review': {'id': 'TEST-DELIVERY'}, 'scope': 'TEST 独立性',
                  'evidence_refs': [{'ref': 'TEST-exec', 'sha256': 'e' * 64}],
                  'contract_version': 2, 'review_type': 'revalidation',
                  'validation_binding': binding(record)}
        conn.execute('INSERT INTO ri_requests VALUES (?,?,?,?,?,?)',
                     ('TEST-REQ', 'TEST-CAND', 1, canonical(common), 'TEST', 'pending_center'))
        reviews = []
        for role, model in (('dsh', dsh_model), ('peer', peer_model)):
            reviewer = 'TEST-' + role
            source_ref = 'agent-entry:TEST-shared' if shared_entry else 'agent-entry:TEST-' + role
            source = {'reviewer_id': reviewer, 'kind': role, 'source_ref': source_ref,
                      'provenance': 'TEST 夹具'}
            if not legacy:
                source['node'] = {'version': 1, 'revision': 1, 'enabled': True, 'removed': False,
                                  'name': reviewer, 'agent': 'TEST', 'interface': 'cli',
                                  'entry': '/TEST/bin/shared', 'model': model}
            conn.execute('INSERT INTO ri_sources VALUES (?,?)', (reviewer, canonical(source)))
            card = {'assignment_id': 'TEST-A-' + role, 'review_request_id': 'TEST-REQ', 'plan_revision': 1,
                    'reviewer_role': role, 'reviewer_id': reviewer, 'source_ref': source_ref,
                    'channel': 'external_handoff', 'input_digest': checksum(common),
                    'candidate_sha256': 'c' * 64, 'evidence_refs': common['evidence_refs']}
            if not legacy:
                card['resource_plan'] = {'model': model}
            card['card_digest'] = checksum(card)
            conn.execute('INSERT INTO ri_assignments VALUES (?,?,?,?,1,?,NULL)',
                         ('TEST-A-' + role, 'TEST-REQ', role, reviewer, canonical(card)))
            data = {k: card[k] for k in ('review_request_id', 'assignment_id', 'reviewer_role', 'plan_revision',
                                         'reviewer_id', 'source_ref', 'candidate_sha256', 'input_digest', 'card_digest')}
            data.update(receipt_id='TEST-R-' + role, verdict='PASS', findings='TEST 仅夹具正文',
                        evidence_refs=['TEST-exec'], started_at=1001.0, finished_at=1002.0)
            digest = checksum(data)
            conn.execute('INSERT INTO ri_receipts VALUES (?,?,?,?,?,?,?,?)',
                         ('TEST-R-' + role, 'TEST-REQ', 'TEST-A-' + role, canonical(data), digest,
                          'valid', '', 'TEST 操作者核对来源'))
            reviews.append({'receipt': data, 'digest': digest, 'identity_basis': 'TEST 操作者核对来源'})
        payload = {'review_request_id': 'TEST-REQ', 'plan_revision': 1, 'subject': common, 'reviews': reviews,
                   'prior_failures': [], 'all_pass': True, 'approval': 'human_required',
                   'native_validation_gate': 'unchanged'}
        conn.execute('INSERT INTO ri_inbox VALUES (?,?,?,?,1,?)',
                     ('TEST-REQ:' + checksum(payload), 'TEST-REQ', canonical(payload), checksum(payload), 1003.0))
        return conn, candidate, approved, record, execution

    def test_same_entry_two_models_pass_ingress_evidence(self):
        conn, candidate, approved, record, execution = self.fixture('TEST-model-A', 'TEST-model-B')
        self.assertIsNone(ingress_evidence(conn, candidate, approved, record, execution))

    def test_same_entry_same_model_is_rejected(self):
        conn, candidate, approved, record, execution = self.fixture('TEST-model-A', 'TEST-model-A')
        with self.assertRaisesRegex(ValueError, '来源不独立'):
            ingress_evidence(conn, candidate, approved, record, execution)

    def test_legacy_cards_keep_source_ref_only(self):
        conn, candidate, approved, record, execution = self.fixture(None, None, shared_entry=False, legacy=True)
        self.assertIsNone(ingress_evidence(conn, candidate, approved, record, execution))
        conn, candidate, approved, record, execution = self.fixture(None, None, shared_entry=True, legacy=True)
        with self.assertRaisesRegex(ValueError, '来源不独立'):
            ingress_evidence(conn, candidate, approved, record, execution)


if __name__ == '__main__':
    unittest.main()
