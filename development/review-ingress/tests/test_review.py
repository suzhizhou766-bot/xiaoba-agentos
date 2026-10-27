import copy
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

REPO = Path(__file__).resolve().parents[3]
PACKAGE = REPO / 'architecture-package-20261027'
sys.path[:0] = [str(PACKAGE / 'implementation/candidate-v1.6.5/src'),
                str(PACKAGE / 'shared/工具/小八监看'), str(Path(__file__).resolve().parents[1])]
sys.dont_write_bytecode = True

from xiaoba_agentos.store import EventStore
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.experience_workflow import trial_summary
from monitor_core import config, logs
from monitor_core.web import App, Context
from xiaoba_review.store import ReviewStore, DecisionInbox, Conflict
from xiaoba_review.backends import BackendRegistry, NativeDsh
from xiaoba_review.monitor import OpenReviewModule


class FixtureMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.native = EventStore(self.root / 'agentos.sqlite3')
        self.native.initialize()
        self.store = ReviewStore(self.native, self.root / 'memory-outbox')
        self.inbox = DecisionInbox()
        self.candidate = self.native.create_authored_candidate('方法夹具', '隔离测试，非真实经验。',
                    self.root / 'memory-outbox', author='test', experience_kind='method')
        self.native.review_memory(self.candidate['task_id'], 'delivery', 'approve', '仅测试初审', self.candidate['evidence_sha256'])
        for reviewer, kind, ref in [('fixture-dsh', 'dsh', 'fixture-source-a'),
                                     ('fixture-peer', 'peer', 'fixture-source-b'),
                                     ('other-peer', 'peer', 'fixture-source-c')]:
            self.store.register_source({'reviewer_id': reviewer, 'kind': kind,
                'source_ref': ref, 'provenance': '隔离测试夹具，未调用真实后台'})

    def request(self, **kwargs):
        return self.store.create(self.candidate['task_id'], 'fixture-dsh', 'fixture-peer',
                                  '核查夹具方法', [{'ref': 'fixture-execution', 'sha256': 'a'*64}], **kwargs)

    def receipt(self, assignment, verdict='PASS'):
        card = assignment['card']
        payload = {key: card[key] for key in ('review_request_id', 'assignment_id', 'reviewer_role', 'plan_revision', 'reviewer_id',
                   'source_ref', 'candidate_sha256', 'input_digest', 'card_digest')}
        payload.update(receipt_id='receipt-'+assignment['id'], verdict=verdict,
                       findings='仅测试夹具审查正文；未执行真实任务。', evidence_refs=['fixture'],
                       started_at=time.time()-1, finished_at=time.time())
        return payload

    def complete(self, request, verdict='PASS'):
        for assignment in request['assignments']:
            received = self.store.receive(self.receipt(assignment, verdict if assignment['role']=='peer' else 'PASS'))
            self.store.attest(received['id'], received['digest'], '测试操作者核对夹具来源')

    def count(self, table):
        with self.store.transaction() as db:
            return db.execute('SELECT count(*) FROM '+table).fetchone()[0]


class ReviewTest(FixtureMixin, unittest.TestCase):

    def test_two_receipts_deliver_once_to_original_card_without_approval(self):
        request = self.request()
        self.complete(request)
        for _ in range(3):
            self.store.deliver(self.inbox)
            self.store.get(request['id'])
        self.assertEqual(self.store.get(request['id'])['state'], 'submitted')
        self.assertEqual(self.count('ri_inbox'), 1)
        self.assertEqual(self.count('memory_candidates'), 1)
        self.assertEqual(self.count('memory_reviews'), 1)
        with self.native.read_only() as db:
            candidate = db.execute('SELECT * FROM memory_candidates').fetchone()
            self.assertFalse(trial_summary(db, candidate)['ready'])
        self.assertTrue(self.native.verify_event_chain()['ok'])

    def test_missing_dsh_or_peer_never_submits(self):
        for role in ['dsh', 'peer']:
            request = self.request()
            assignment = next(a for a in request['assignments'] if a['role']==role)
            receipt = self.store.receive(self.receipt(assignment))
            self.store.attest(receipt['id'], receipt['digest'], '核对夹具')
            self.store.deliver(self.inbox)
        self.assertEqual(self.count('ri_inbox'), 0)

    def test_self_claimed_identity_does_not_count(self):
        request = self.request()
        for assignment in request['assignments']:
            payload = self.receipt(assignment)
            payload['identity_verified'] = True
            self.assertEqual(self.store.receive(payload)['status'], 'identity_pending')
        self.store.deliver(self.inbox)
        self.assertEqual(self.count('ri_inbox'), 0)

    def test_alias_same_actual_source_and_non_dsh_fixed_rejected(self):
        self.store.register_source({'reviewer_id': 'alias', 'kind':'peer', 'source_ref':'fixture-source-a', 'provenance':'alias'})
        with self.assertRaises(ValueError):
            self.store.create(self.candidate['task_id'], 'fixture-dsh', 'alias', 'scope', ['fixture'])
        with self.assertRaises(ValueError):
            self.store.create(self.candidate['task_id'], 'fixture-peer', 'other-peer', 'scope', ['fixture'])
        self.assertEqual(self.count('ri_requests'), 0)

    def test_offline_retry_does_not_rerun_reviewers(self):
        request = self.request()
        self.complete(request)
        self.inbox.online = False
        self.store.deliver(self.inbox)
        self.assertEqual(self.store.get(request['id'])['state'], 'pending_center')
        self.assertEqual(self.count('ri_inbox'), 0)
        self.inbox.online = True
        self.store.deliver(self.inbox)
        self.store.deliver(self.inbox)
        self.assertEqual(self.count('ri_inbox'), 1)
        self.assertEqual(self.count('ri_receipts'), 2)

    def test_fail_and_needs_info_still_reach_center(self):
        for verdict in ['FAIL', 'NEEDS_INFO']:
            request = self.request()
            self.complete(request, verdict)
            self.store.deliver(self.inbox)
        packets = self.store.state()['decision_packets']
        self.assertEqual(len(packets), 2)
        self.assertTrue(all(not p['data']['all_pass'] for p in packets))
        self.assertEqual(self.count('memory_reviews'), 1)

    def test_content_drift_invalidates_old_inbox(self):
        request = self.request()
        self.complete(request)
        self.store.deliver(self.inbox)
        Path(self.candidate['staged_path']).write_text('changed fixture')
        self.assertEqual(self.store.get(request['id'])['state'], 'stale')
        self.assertFalse(self.store.state()['decision_packets'][0]['current'])

    def test_initial_review_change_invalidates_receipts(self):
        request = self.request()
        self.complete(request)
        self.native.review_memory(self.candidate['task_id'], 'delivery', 'approve', '重新测试初审', self.candidate['evidence_sha256'])
        self.store.deliver(self.inbox)
        self.assertEqual(self.store.get(request['id'])['state'], 'stale')
        self.assertEqual(self.count('ri_inbox'), 0)

    def test_repeat_and_parallel_receipts_are_idempotent_conflicts_preserved(self):
        assignment = self.request()['assignments'][0]
        payload = self.receipt(assignment)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.receive(payload), range(8)))
        self.assertTrue(all(r['digest']==results[0]['digest'] for r in results))
        self.assertEqual(self.count('ri_receipts'), 1)
        with self.assertRaises(Conflict):
            self.store.receive({**payload, 'findings':'different content'})

    def test_concurrent_dispatch_single_inbox_and_single_ack_event(self):
        request = self.request()
        self.complete(request)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.store.deliver(self.inbox), range(8)))
        self.assertEqual(self.count('ri_inbox'), 1)
        history = self.store.get(request['id'])['history']
        self.assertEqual(sum(h['event_type']=='open_review.center_acknowledged' for h in history), 1)

    def test_new_invalid_receipt_cannot_leave_old_success_current(self):
        request = self.request()
        self.complete(request)
        self.store.deliver(self.inbox)
        payload = self.receipt(request['assignments'][0])
        payload.update(receipt_id='new-invalid', input_digest='old-version')
        self.assertEqual(self.store.receive(payload)['status'], 'invalid')
        state = self.store.state()
        self.assertEqual(state['requests'][0]['state'], 'waiting_receipts')
        self.assertFalse(state['decision_packets'][0]['current'])

    def test_replace_peer_preserves_dsh_and_failure_history(self):
        request = self.request()
        self.complete(request, 'FAIL')
        replacement = self.store.replace_peer(request['id'], 'other-peer', '用户更换第二方')
        self.assertEqual(len(replacement['assignments']), 3)
        self.assertEqual(len(replacement['receipts']), 2)
        new = next(a for a in replacement['assignments'] if a['active'] and a['role']=='peer')
        received = self.store.receive(self.receipt(new))
        self.store.attest(received['id'], received['digest'], '核对新来源')
        self.store.deliver(self.inbox)
        self.assertEqual(self.count('ri_receipts'), 3)
        self.assertEqual(self.store.get(request['id'])['state'], 'submitted')
        packet = next(p for p in self.store.state()['decision_packets'] if p['current'])
        self.assertFalse(packet['data']['all_pass'])
        self.assertEqual(len(packet['data']['prior_failures']), 1)
        stale = next(a for a in replacement['assignments'] if not a['active'])
        payload = self.receipt(stale)
        payload['receipt_id'] = 'late-replaced'
        self.assertEqual(self.store.receive(payload)['status'], 'invalid')

    def test_b19_and_version_mismatch_retained_as_invalid(self):
        assignment = self.request()['assignments'][0]
        for i, changed in enumerate([{'findings':''}, {'findings':'<invoke tool="x"></invoke>'},
                                     {'card_digest':'wrong'}, {'evidence_refs':[]}, {'finished_at':0}]):
            payload = self.receipt(assignment)
            payload.update(changed, receipt_id='invalid-'+str(i))
            self.assertEqual(self.store.receive(payload)['status'], 'invalid')
        self.assertEqual(self.count('ri_receipts'), 5)

    def test_arbitrary_backend_registry_and_unknown_no_fallback(self):
        request = self.request(peer_channel='backend_adapter')
        peer = next(a for a in request['assignments'] if a['role']=='peer')
        registry = BackendRegistry(self.store)
        with self.assertRaisesRegex(ValueError, '未登记后台'):
            registry.execute(peer['id'])
        class AnyBackend:
            kind, source_ref = 'peer', 'fixture-source-b'
            def run(self, card):
                return {'verdict':'PASS', 'findings':'任意测试后台实际回调。', 'evidence_refs':['fixture-run']}
        registry.register('fixture-peer', AnyBackend())
        registry.execute(peer['id'])
        self.assertEqual(self.count('ri_receipts'), 1)
        with self.assertRaises(Conflict):
            registry.execute(peer['id'])

    def test_native_dsh_never_accepts_mock_or_unknown_agent(self):
        config = AppConfig(self.root/'config.json', {'agents':[{'name':'dsh', 'type':'mock', 'driver':'dsh'}]})
        with self.assertRaises(ValueError):
            NativeDsh(config, 'dsh', 'real-source')

    def test_broken_native_chain_stops_dual_review_writes(self):
        with sqlite3.connect(self.native.path) as db:
            db.execute("UPDATE events SET payload_json='{}' WHERE sequence=(SELECT max(sequence) FROM events)")
        self.assertFalse(self.native.verify_event_chain()['ok'])
        with self.assertRaisesRegex(ValueError, '事件链损坏'):
            self.request()

    def test_restart_preserves_sources_packets_and_ack(self):
        request = self.request()
        self.complete(request)
        self.store.deliver(self.inbox)
        restarted = ReviewStore(EventStore(self.native.path), self.root/'memory-outbox')
        restarted.deliver(DecisionInbox())
        self.assertEqual(restarted.get(request['id'])['state'], 'submitted')
        self.assertEqual(len(restarted.state()['decision_packets']), 1)

    def test_scores_are_preserved_without_inventing_thresholds(self):
        request = self.request()
        for assignment in request['assignments']:
            payload = self.receipt(assignment)
            if assignment['role']=='peer':
                payload['scores']={'scale_version':'fixture-only-v1','dimensions':[{'name':'适用性','value':3,'reason':'仅夹具结果'}]}
            received = self.store.receive(payload)
            self.store.attest(received['id'], received['digest'], '核对夹具')
        self.store.deliver(self.inbox)
        reviews = self.store.state()['decision_packets'][0]['data']['reviews']
        peer = next(r['receipt'] for r in reviews if r['receipt']['reviewer_id']=='fixture-peer')
        dsh = next(r['receipt'] for r in reviews if r['receipt']['reviewer_id']=='fixture-dsh')
        self.assertEqual(peer['scores']['dimensions'][0]['value'], 3)
        self.assertNotIn('scores', dsh)
        self.assertEqual(self.count('memory_reviews'), 1)

    def test_inbox_partial_write_failure_rolls_back_before_retry(self):
        request = self.request()
        self.complete(request)
        class FailsAfterWrite(DecisionInbox):
            def accept(self, db, row):
                super().accept(db, row)
                raise OSError('simulated receiver crash after write')
        self.store.deliver(FailsAfterWrite())
        self.assertEqual(self.count('ri_inbox'),0)
        self.assertEqual(self.store.get(request['id'])['state'],'pending_center')
        self.store.deliver(self.inbox)
        self.assertEqual(self.count('ri_inbox'),1)


class HttpTest(FixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        ctx = Context(config.load(self.root/'none.json', {'port':0}), logs.setup(None), 0)
        self.module = OpenReviewModule(ctx, self.store, self.inbox, BackendRegistry(self.store))
        self.app = App(ctx)
        self.app.register(self.module)
        self.server = self.app.server()
        port = self.server.server_port
        ctx.port = port
        ctx.hosts = {'127.0.0.1:'+str(port)}
        ctx.origins = {'http://127.0.0.1:'+str(port)}
        self.ctx = ctx
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def fetch(self, path, payload=None, authorized=True):
        conn=http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        headers={'Content-Type':'application/json','Origin':next(iter(self.ctx.origins))}
        if authorized:
            headers['X-Xiaoba-Token'] = self.ctx.page_token
        try:
            conn.request('POST' if payload is not None else 'GET', path,
                         json.dumps(payload) if payload is not None else None, headers)
            response = conn.getresponse()
            raw=response.read()
            return response.status, json.loads(raw) if 'application/json' in response.getheader('Content-Type','') else raw
        finally:
            conn.close()

    def test_http_operator_workflow_and_csrf(self):
        self.assertEqual(self.fetch('/open-review')[0], 200)
        self.assertEqual(self.fetch('/api/open-review/receipts', {}, False)[0], 403)
        request = self.request()
        for assignment in request['assignments']:
            code, result = self.fetch('/api/open-review/receipts', self.receipt(assignment))
            self.assertEqual(code, 200)
            self.assertEqual(self.fetch('/api/open-review/attest', {
                'receipt_id':result['result']['id'], 'expected_digest':result['result']['digest'], 'note':'HTTP 夹具来源核对'})[0],200)
        code,state = self.fetch('/api/open-review/state')
        self.assertEqual(code,200)
        self.assertEqual(state['requests'][0]['state'],'submitted')
        self.assertEqual(len(state['decision_packets']),1)
        self.assertEqual(self.count('memory_reviews'),1)


if __name__=='__main__':
    unittest.main()
