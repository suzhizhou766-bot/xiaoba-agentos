"""Real HTTP API and fake executables; never use the production monitor."""
import http.client
import json
import threading
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import test_review as fixtures
import test_pool_worker_cli as worker_fixtures
from xiaoba_agentos.cli import _submit_task
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.pool_dispatch import worker_roster
from xiaoba_review.store import ReviewStore


class Card37IngressTests(unittest.TestCase):
    setUp = worker_fixtures.PoolWorkerCliTests.setUp
    _fake = worker_fixtures.PoolWorkerCliTests._fake
    _add_ready = worker_fixtures.PoolWorkerCliTests._add_ready
    _calls = worker_fixtures.PoolWorkerCliTests._calls

    def two_nodes(self):
        self._add_ready('north', self._fake('codex'), 'alpha', 'codex')
        self._add_ready('south', self._fake('claude'), 'beta', 'claude')

    def test_exactly_two_reviewers_run_three_without_scores_ask(self):
        self.two_nodes()
        self.assertEqual('run', self.pool.runtime.prepare(['dsh', 'peer'])['route'])
        self._add_ready('third', self._fake('qoder'), 'gamma', 'qoder')
        self.assertEqual('ask_user', self.pool.runtime.prepare(['dsh', 'peer'])['route'])

    def test_worker_uses_page_workspace_and_workday_reads_worker_receipts(self):
        self.two_nodes()
        task = _submit_task(self.config, self.native, 'TEST shared workspace', experience_kind='audit')
        with patch('xiaoba_agentos.orchestrator.release_binding_status', return_value={'ok': True}):
            result = Orchestrator(self.config, self.native).run_once()
        self.assertEqual('completed', result['status'], result)
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        tasks = self.pool.runtime.workday_inputs('north', day)['tasks']
        self.assertIn(task, [v['task_id'] for v in tasks if not v['health_check']])
        calls = self._calls('codex')
        self.assertTrue(all(str(self.workspaces) in v['cwd'] for v in calls))

    def test_runner_digest_change_stops_worker_and_requires_retest(self):
        self.two_nodes()
        task = _submit_task(self.config, self.native, 'TEST digest drift', experience_kind='audit')
        from xiaoba_review.resource_vendor_cli import CodexCliResource
        original = CodexCliResource.resource_identity
        def changed(adapter):return dict(original(adapter), changed='TEST adapter code drift')
        with patch.object(CodexCliResource, 'resource_identity', changed):
            with self.native.read_only() as db:
                source = self.review.source(db, 'north')
                self.assertEqual('test_required', self.pool.runtime.readiness(db, source)['reason'])
            roster = worker_roster(self.config, self.native, self.native.get_task(task))
            self.assertNotIn('north', [v.name for v in roster['agents']])
            self.assertIn('重测', str(roster))

    def test_native_work_api_records_actual_participant_and_excludes_from_review(self):
        self._add_ready('worker', self._fake('codex'), 'alpha', 'codex', kind='both')
        self._add_ready('south', self._fake('claude'), 'beta', 'claude')
        self._add_ready('third', self._fake('qoder'), 'gamma', 'qoder')
        work = _submit_task(self.config, self.native, 'TEST work', experience_kind='audit', needs_review=False)
        with self.native.read_only() as db:
            revision = self.review.source(db, 'worker')['node']['revision']
        result = self.pool.runtime.execute_work('worker', revision, work, 'STEP-01', 'TEST work only', {'fixture': True})
        self.assertEqual('completed', result['status'])
        self.assertEqual({'worker'}, set(self.native.latest_agent_runs(work)))
        review = _submit_task(self.config, self.native, 'TEST review', experience_kind='audit', work_task_id=work)
        self.assertEqual({'south','third'}, {a.name for a in worker_roster(self.config, self.native, self.native.get_task(review))['agents']})
        candidate = self.native.create_authored_candidate('TEST work source', 'TEST isolated method', self.config.runtime_dir/'memory-outbox',
                                                          author='worker', experience_kind='method', source_task_ids=[work])
        prepared = self.pool.runtime.prepare(['dsh','peer'], candidate_id=candidate['task_id'])
        self.assertEqual('run', prepared['route'], prepared)
        self.assertEqual({'south','third'}, set(prepared['selections'].values()))

    def test_workspace_binding_survives_restart_and_rejects_a_second_root(self):
        from xiaoba_agentos.pool_dispatch import bound_workspace_root
        from xiaoba_review.store import Conflict
        self.assertEqual(self.workspaces.resolve(), bound_workspace_root(self.native))
        reopened = ReviewStore(self.native, self.config.runtime_dir/'memory-outbox')
        self.assertEqual(self.workspaces.resolve(), reopened.workspace_root)
        with self.assertRaises(Conflict):
            ReviewStore(self.native, self.config.runtime_dir/'memory-outbox', self.root/'different')

    def test_http_create_test_prepare_dispatch_receipts(self):
        ctx = fixtures.Context(fixtures.config.load(self.root/'none.json', {'port': 0}), fixtures.logs.setup(None), 0)
        module = fixtures.OpenReviewModule(ctx, self.review, fixtures.DecisionInbox(), self.backends)
        app = fixtures.App(ctx);app.register(module)
        server = app.server();ctx.port = server.server_port
        ctx.hosts = {'127.0.0.1:'+str(ctx.port)};ctx.origins = {'http://127.0.0.1:'+str(ctx.port)}
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        def post(path, body):
            conn = http.client.HTTPConnection('127.0.0.1', ctx.port, timeout=20)
            try:
                conn.request('POST', '/api/agent-pool/'+path, json.dumps(body),
                             {'Content-Type': 'application/json', 'Origin': next(iter(ctx.origins)), 'X-Xiaoba-Token': ctx.page_token})
                response = conn.getresponse();value = json.loads(response.read())
                self.assertEqual(200, response.status, value)
                return value['result']
            finally:conn.close()
        for name, kind in [('north', 'codex'), ('south', 'claude')]:
            source = post('nodes', {'node_id': name, 'name': name, 'agent': kind, 'kind': 'review',
                                   'interface': 'cli', 'entry': str(self._fake(kind)), 'agent_type': kind, 'model': name})
            source = post('toggle', {'node_id': name, 'expected_revision': source['node']['revision'], 'enabled': True})
            test = post('test', {'node_id': name, 'expected_revision': source['node']['revision'], 'test_id': 'HTTP-'+name})
            self.assertEqual('passed', test['status'])
        prepared = post('prepare', {'required_kinds': ['dsh', 'peer']})
        self.assertEqual('run', prepared['route'], prepared)
        candidate = self.native.create_authored_candidate('TEST HTTP candidate', 'TEST isolated fixture', self.config.runtime_dir/'memory-outbox', author='fixture', experience_kind='method')
        result = post('dispatch', {'candidate_id': candidate['task_id'], 'scope': 'TEST fake cli receipt',
                                  'evidence': [{'ref': 'TEST fake cli receipt', 'sha256': 'a'*64}],
                                  'request_id': 'HTTP-review', 'review_type': 'new_experience'})
        self.assertEqual('completed', result['route'], result)
        self.assertEqual(2, len(result['request']['receipts']))
        self.assertTrue(all(v['status']=='valid' for v in result['request']['receipts']))
