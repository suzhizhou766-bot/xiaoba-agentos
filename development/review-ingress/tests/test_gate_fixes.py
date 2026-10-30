"""任务卡 1 的反例：默认新核心、派工前校验事件链、resource_plan 卡必须过测试资格。

全部使用隔离临时目录和 TEST 子进程夹具，不调用真实模型。
"""
import threading
import unittest
from pathlib import Path

import test_review as fixtures
from test_agy_cli import CliFixture
from xiaoba_review.backends import BackendRegistry
from xiaoba_review.store import Conflict


class DefaultCoreTests(unittest.TestCase):
    """接入口默认加载新核心：不再依赖调用方自己插 sys.path。"""

    def test_default_core_is_the_integrated_candidate(self):
        import run
        import xiaoba_agentos
        expected = (run.REPO / 'development/integrated-candidate/src').resolve()
        self.assertEqual(run.CORE.resolve(), expected)
        self.assertNotIn('candidate-v1.6.5/src', Path(run.__file__).read_text())
        self.assertEqual(Path(xiaoba_agentos.__file__).resolve().parent, expected / 'xiaoba_agentos')

    def test_ingress_tests_and_browser_smoke_point_at_the_same_core(self):
        import run
        tests = Path(run.__file__).with_name('tests')
        for name in ('test_review.py', 'browser_smoke.py'):
            text = (tests / name).read_text()
            self.assertIn("'development/integrated-candidate/src'", text, name)
            self.assertNotIn('candidate-v1.6.5/src', text, name)


class HttpFixture:
    fetch = fixtures.HttpTest.fetch

    def start_http(self):
        self.ctx = fixtures.Context(fixtures.config.load(self.root/'none.json', {'port':0}),
                                    fixtures.logs.setup(None), 0)
        module = fixtures.OpenReviewModule(self.ctx, self.store, self.inbox, self.backends)
        app = fixtures.App(self.ctx)
        app.register(module)
        self.server = app.server()
        port = self.server.server_port
        self.ctx.port = port
        self.ctx.hosts = {'127.0.0.1:'+str(port)}
        self.ctx.origins = {'http://127.0.0.1:'+str(port)}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)


class BrokenChainGateTests(HttpFixture, CliFixture, unittest.TestCase):
    """事件链坏了不派活：抛错，子进程 0 次。"""

    def setUp(self):
        super().setUp()
        self.start_http()

    def payload(self, node):
        return dict(node_id=node, expected_revision=self.node(node)['revision'], task_id='TASK-gate',
                    step_code='STEP-01', instruction='TEST authorized work in this owned directory',
                    materials={'goal':'TEST fixture'})

    def break_chain(self):
        with self.native.connect() as db:
            db.execute("UPDATE events SET event_hash='TEST-broken' WHERE sequence=1")
        self.assertFalse(self.native.verify_event_chain()['ok'])

    def event_count(self):
        with self.native.read_only() as db:
            return db.execute('SELECT count(*) FROM events').fetchone()[0]

    def test_broken_event_chain_blocks_execute_work_before_any_child_process(self):
        node = self.add()
        self.assertEqual(self.probe(node)['status'], 'passed')
        payload = self.payload(node)
        started = len(self.starts())
        events = self.event_count()
        self.break_chain()
        with self.assertRaises(Conflict):
            self.pool.runtime.execute_work(**payload)
        self.assertEqual(len(self.starts()), started)
        self.assertEqual(self.event_count(), events)

    def test_same_request_over_http_returns_an_error_and_starts_nothing(self):
        node = self.add()
        self.probe(node)
        payload = self.payload(node)
        started = len(self.starts())
        self.break_chain()
        code, value = self.fetch('/api/agent-pool/work', payload)
        self.assertEqual(code, 409, value)
        self.assertFalse(value['ok'])
        self.assertEqual(len(self.starts()), started)


class TestQualificationGateTests(HttpFixture, CliFixture, unittest.TestCase):
    """带 resource_plan 的任务卡必须过测试资格，旧四字段来源保持原行为。"""

    def setUp(self):
        super().setUp()
        self.start_http()

    def plan_card(self, node):
        request = self.store.create(self.candidate['task_id'], 'fixture-dsh', node, 'TEST gate',
            [{'ref':'TEST plan card','sha256':'a'*64}], peer_channel='backend_adapter')
        assignment = next(a for a in request['assignments'] if a['role'] == 'peer')
        self.assertIn('resource_plan', assignment['card'])
        return assignment

    def test_untested_node_cannot_run_a_plan_card_through_the_http_route(self):
        assignment = self.plan_card(self.add())
        code, value = self.fetch('/api/open-review/run', {'assignment_id': assignment['id']})
        self.assertEqual(code, 400, value)
        self.assertIn('测试', value['error'])
        self.assertEqual(self.starts(), [])
        self.assertEqual(self.count('ri_receipts'), 0)

    def test_direct_call_without_require_test_is_gated_too(self):
        assignment = self.plan_card(self.add())
        with self.assertRaisesRegex(ValueError, '测试'):
            self.backends.execute(assignment['id'])
        self.assertEqual(self.starts(), [])

    def test_same_plan_card_runs_once_the_node_has_its_test(self):
        node = self.add()
        assignment = self.plan_card(node)
        self.assertEqual(self.fetch('/api/open-review/run', {'assignment_id': assignment['id']})[0], 400)
        self.assertEqual(self.probe(node)['status'], 'passed')
        code, value = self.fetch('/api/open-review/run', {'assignment_id': assignment['id']})
        self.assertEqual(code, 200, value)
        self.assertEqual(value['result']['status'], 'valid')
        self.assertEqual(len(self.starts()), 2)  # 探针 + 审查，各一次子进程。


class LegacySourceTests(fixtures.FixtureMixin, unittest.TestCase):
    """旧四字段来源没有 resource_plan，直跑行为不变。"""

    def test_legacy_four_field_source_keeps_untested_direct_execution(self):
        request = self.request(peer_channel='backend_adapter')
        peer = next(a for a in request['assignments'] if a['role'] == 'peer')
        self.assertNotIn('resource_plan', peer['card'])
        registry = BackendRegistry(self.store)

        class AnyBackend:
            kind, source_ref = 'peer', 'fixture-source-b'
            def run(self, card):
                return {'verdict':'PASS', 'findings':'TEST 旧四字段回调。', 'evidence_refs':['TEST']}

        registry.register('fixture-peer', AnyBackend())
        self.assertEqual(registry.execute(peer['id'])['status'], 'valid')
        self.assertEqual(self.count('ri_receipts'), 1)
