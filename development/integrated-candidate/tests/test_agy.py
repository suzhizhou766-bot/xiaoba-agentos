import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import AgyAdapter, AgentResult, PermanentAdapterError, TransientAdapterError, build_adapter
from xiaoba_agentos.output_hygiene import NON_ANSWER_MARKERS
from xiaoba_agentos.capabilities import CapabilityRegistry, agent_provider_model, capable_agents
from xiaoba_agentos.config import AppConfig, ConfigError
from xiaoba_agentos import gateway
from xiaoba_agentos.gateway import GatewayUnavailable, command_environment, local_cli_readiness, local_cli_route
from xiaoba_agentos.health import model_paths
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.recovery import HEALTH_MARKER, live_agent_probe
from xiaoba_agentos.store import EventStore


class AgyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # The user's home is the temporary root, so any settings file the tests
        # plant sits exactly where a real one would be.
        home = patch.object(Path, 'home', return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        raw = json.loads((Path(__file__).resolve().parents[1] / 'config/agentos.json').read_text())
        raw['project_dir'] = str(self.root)
        raw['runtime_dir'] = str(self.root / 'runtime')
        raw['integrations']['agy_command'] = '/usr/bin/true'
        raw['memory'].update(knowlp_enabled=False, obsidian_vault=str(self.root), publish_enabled=False)
        if 'local_qoder' in raw['cli_routes']:
            raw['cli_routes']['local_qoder']['settings_path'] = str(self.root / '.qoder' / 'settings.json')
        # 用户 2026-11-01 裁决：配置里的模型名就是实际使用的模型。AGY 的模型写在
        # cli_routes.local_agy 上，agy CLI 设置文件不再被读取。
        self.model = raw['cli_routes']['local_agy']['model']
        self.settings = self.root / '.gemini' / 'antigravity-cli' / 'settings.json'
        self.config = AppConfig(self.root / 'config.json', raw)
        self.agent = next(a for a in self.config.agents if a.raw.get('driver') == 'agy')

    def plant_settings(self, model):
        """Put an agy CLI settings file where a real one would be. It must be ignored."""
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps({'model': model, 'trustedWorkspaces': []}), encoding='utf-8')

    def run_payload(self, payload, code=0):
        result = subprocess.CompletedProcess([], code, json.dumps(payload), '')
        with patch('xiaoba_agentos.adapters.run_cli', return_value=result) as run:
            answer = build_adapter(self.agent).run('审查方案', '仅有这份冻结证据')
        return answer, run

    def run_stream(self, lines, code=0):
        stdout = '\n'.join(json.dumps(item, ensure_ascii=False) for item in lines) + '\n'
        result = subprocess.CompletedProcess([], code, stdout, '')
        with patch('xiaoba_agentos.adapters.run_cli', return_value=result) as run:
            return build_adapter(self.agent).run('审查方案', '仅有这份冻结证据'), run

    def test_stream_output_uses_the_result_event(self):
        answer, run = self.run_stream([
            {'event': 'init', 'init': {'model': 'm', 'tools': ['x']}},
            {'event': 'step_update', 'step_update': {'text_delta': '结论'}},
            {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '结论：可行。依据：测试。风险：缺证据。验证：复查。'}},
        ])
        self.assertIn('结论：可行', answer.output)
        argv = run.call_args.args[0]
        self.assertEqual('stream-json', argv[argv.index('--input-format') + 1])
        self.assertEqual('stream-json', argv[argv.index('--output-format') + 1])

    def test_stream_error_result_and_missing_result_are_failures(self):
        with self.assertRaises((PermanentAdapterError, TransientAdapterError)):
            self.run_stream([{'event': 'init', 'init': {}},
                             {'event': 'result', 'result': {'status': 'ERROR', 'response': '', 'error': 'bad request'}}])
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_stream([{'event': 'init', 'init': {}}, {'event': 'step_update', 'step_update': {}}])
        self.assertIn('没有 result 事件', str(caught.exception))

    def test_structured_success_reports_configured_route_and_isolated_plan(self):
        answer, run = self.run_payload({'status': 'SUCCESS', 'response': '结论：可行。依据：测试。风险：缺证据。验证：复查。'})
        self.assertEqual('Antigravity', answer.provider)
        self.assertEqual(self.model, answer.model)
        self.assertEqual('configured_model', answer.route_source)
        argv = run.call_args.args[0]
        self.assertEqual('plan', argv[argv.index('--mode') + 1])
        self.assertIn('--sandbox', argv)
        self.assertNotIn('--disable-slash-commands', argv)
        self.assertNotIn('--dangerously-skip-permissions', argv)
        self.assertNotEqual(str(self.root), run.call_args.kwargs['cwd'])
        self.assertFalse(Path(run.call_args.kwargs['cwd']).exists())
        self.assertNotIn('仅有这份冻结证据', ' '.join(argv))
        self.assertNotIn('审查方案', ' '.join(argv))
        self.assertFalse(any(item in ('--print', '-p', '--prompt') or item.startswith('--print=') for item in argv))
        sent = json.loads(run.call_args.kwargs['input'])
        self.assertEqual('user', sent['event'])
        self.assertIn('仅有这份冻结证据', sent['message']['content'])

    def test_empty_or_tool_markup_is_not_a_success(self):
        for output in ('', '  ', '<invoke name="Read"></invoke>', '<｜DSML｜tool_calls><｜DSML｜invoke name="bash"></｜DSML｜invoke>'):
            with self.subTest(output=output), self.assertRaises(TransientAdapterError):
                self.run_payload({'status': 'SUCCESS', 'response': output})

    def test_all_eight_non_answer_markers_are_rejected_with_casing(self):
        for marker in NON_ANSWER_MARKERS:
            for variant in (f"前缀 {marker} 后缀", f"前缀 {marker.upper()} 后缀"):
                with self.subTest(variant=variant), self.assertRaises(TransientAdapterError):
                    self.run_payload({'status': 'SUCCESS', 'response': variant})

    def test_prompt_and_context_markers_do_not_filter_clean_output(self):
        prompt_with_markers = '请审查包含 <invoke name="bash"> 和 tool-call serialization 的样本'
        context_with_markers = '历史冻结证据：<parameter name="x">1</parameter>'
        clean_response = '结论：审核通过。依据：充分。风险：无。验证：完成。'
        with patch('xiaoba_agentos.adapters.run_cli', return_value=subprocess.CompletedProcess([], 0, json.dumps({'status': 'SUCCESS', 'response': clean_response}), '')):
            answer = build_adapter(self.agent).run(prompt_with_markers, context_with_markers)
        self.assertEqual(clean_response, answer.output)

    def test_context_only_acceptance_words_do_not_trigger_contract(self):
        prompt = '普通审查任务，无需输出验收契约'
        context_with_contract = '历史验收参考要求：FINAL_ACCEPTANCE: PASS 或 FINAL_ACCEPTANCE: FAIL'
        plain_response = '这是普通审查结果，不带契约结论行。'
        with patch('xiaoba_agentos.adapters.run_cli', return_value=subprocess.CompletedProcess([], 0, json.dumps({'status': 'SUCCESS', 'response': plain_response}), '')):
            answer = build_adapter(self.agent).run(prompt, context_with_contract)
        self.assertEqual(plain_response, answer.output)

    def test_connection_failures_are_transient_and_bad_requests_are_permanent(self):
        for message in ('ERROR: Reconnecting... 5/5', 'error sending request for url (https://example.invalid/v1)',
                        'Connection reset by peer', 'network is unreachable'):
            with self.subTest(message=message), self.assertRaises(TransientAdapterError):
                self.run_payload({'status': 'ERROR', 'error': message})
        with self.assertRaises(PermanentAdapterError):
            self.run_payload({'status': 'ERROR', 'error': 'invalid model configuration'})

    def test_the_configured_model_goes_to_the_cli_verbatim_and_no_settings_file_is_read(self):
        # 用户 2026-11-01 裁决。这里故意放一个内容不同的 agy CLI 设置文件：调用参数
        # 和回执都必须只反映配置里写的模型，设置文件一次都不读。
        self.plant_settings('Chosen-By-The-Settings-File')
        self.config._validate_cli_routes()
        answer, run = self.run_payload({'status': 'SUCCESS', 'response': '审查结果'})
        argv = run.call_args.args[0]
        self.assertEqual(self.model, argv[argv.index('--model') + 1])
        self.assertEqual(self.model, answer.model)
        self.assertEqual('configured_model', answer.route_source)
        self.assertNotIn('--effort', argv)
        self.assertIn('--sandbox', argv)
        self.assertEqual('plan', argv[argv.index('--mode') + 1])
        self.assertFalse(hasattr(gateway, 'read_agy_cli_selection'),
                         '读取 agy CLI 设置文件的入口必须已经移除')

    def test_a_route_without_a_usable_model_stops_before_any_call(self):
        # 没有可回退的默认模型，也没有文件可以补：报错要说清去配置里填。
        for route in ({'driver': 'agy', 'selection': 'configured_model'},
                      {'driver': 'agy', 'selection': 'configured_model', 'model': ''},
                      {'driver': 'agy', 'selection': 'configured_model', 'model': '   '},
                      {'driver': 'agy', 'selection': 'configured_model', 'model': 5}):
            with self.subTest(model=route.get('model')):
                self.config.raw['cli_routes']['local_agy'] = dict(route)
                with self.assertRaises(ConfigError) as caught:
                    self.config._validate_cli_routes()
                self.assertIn('必须写明 model', str(caught.exception))
                with self.assertRaises(GatewayUnavailable) as caught:
                    local_cli_route(self.config, 'local_agy', self.agent.name)
                self.assertIn('必须写明 model', str(caught.exception))
                with self.assertRaises(PermanentAdapterError) as caught:
                    build_adapter(self.agent).run('审查方案', '')
                self.assertIn('必须写明 model', str(caught.exception))
        # 形状像命令行参数的模型名在到达 --model 之前就被拒绝。
        for model in ('--model', '-c', '--tools', '_x', '.hidden', 'x' * 81, ' padded '):
            with self.subTest(model=model):
                self.config.raw['cli_routes']['local_agy']['model'] = model
                with self.assertRaises(ConfigError) as caught:
                    self.config._validate_cli_routes()
                self.assertIn('不是可用的模型名', str(caught.exception))
        self.config.raw['cli_routes']['local_agy']['model'] = self.model
        self.config._validate_cli_routes()

    def test_retired_agy_selections_are_refused_with_the_migration_spelled_out(self):
        # 旧配置不能静默继续跑，也不能被偷偷解释成读设置文件或读桌面端。
        self.plant_settings('Chosen-By-The-Settings-File')
        for selection, text in (('agy_cli_settings', 'AGY 不再读 agy CLI 设置文件'),
                                ('local_account', 'AGY 的桌面 local_account 路由已停用')):
            with self.subTest(selection=selection):
                self.config.raw['cli_routes']['local_agy'] = {
                    'driver': 'agy', 'selection': selection, 'settings_path': str(self.settings),
                    'desktop_debug_port': str(self.root / 'DevToolsActivePort'),
                    'desktop_auto_start': True, 'desktop_app_path': '/Applications/Antigravity.app',
                }
                # 切换前没法先改配置（旧代码不认 model），所以旧配置要能加载，只有调用 AGY 被拒。
                self.config._validate_cli_routes()
                with self.assertRaises(GatewayUnavailable) as caught:
                    local_cli_route(self.config, 'local_agy', 'agy_review')
                self.assertIn(text, str(caught.exception))
                self.assertIn('selection=configured_model', str(caught.exception))
                with patch('xiaoba_agentos.gateway.subprocess.run',
                           side_effect=AssertionError('desktop must not be read')):
                    with self.assertRaises(GatewayUnavailable) as caught:
                        local_cli_route(self.config, 'local_agy', self.agent.name)
                self.assertIn(text, str(caught.exception))
        self.config.raw['cli_routes']['local_agy'] = {
            'driver': 'agy', 'selection': 'configured_model', 'model': self.model}

    def test_an_unknown_selection_is_refused(self):
        self.config.raw['cli_routes']['local_agy']['selection'] = 'something_new'
        with self.assertRaises(ConfigError) as caught:
            self.config._validate_cli_routes()
        self.assertIn('必须是 configured_model', str(caught.exception))
        self.config.raw['cli_routes']['local_agy']['selection'] = 'configured_model'

    def test_an_agy_route_takes_no_upstream_or_settings_field(self):
        for key in ('model_pool', 'provider', 'provider_id', 'endpoint', 'base_url',
                    'api_key', 'settings_path', 'desktop_debug_port'):
            with self.subTest(key=key):
                self.config.raw['cli_routes']['local_agy'] = {
                    'driver': 'agy', 'selection': 'configured_model', 'model': self.model,
                    key: ['x'] if key == 'model_pool' else 'forbidden',
                }
                with self.assertRaises(ConfigError) as caught:
                    self.config._validate_cli_routes()
                self.assertIn('仅允许 driver、selection、model', str(caught.exception))
                self.assertNotIn('forbidden', str(caught.exception))
        self.config.raw['cli_routes']['local_agy'] = {
            'driver': 'agy', 'selection': 'configured_model', 'model': self.model}
        self.config._validate_cli_routes()

    def test_bad_json_and_nonobject_are_rejected(self):
        for output in ('broken', '[]'):
            with self.subTest(output=output), patch('xiaoba_agentos.adapters.run_cli', return_value=subprocess.CompletedProcess([], 0, output, '')):
                with self.assertRaises(TransientAdapterError):
                    AgyAdapter(self.agent).run('review', '')

    def test_auth_error_is_terminal_and_redacted(self):
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_payload({'status': 'ERROR', 'error': '401 unauthorized token=secret-value'})
        self.assertNotIn('secret-value', str(caught.exception))

    def test_quota_and_timeout_use_bounded_external_retry(self):
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_payload({'status': 'ERROR', 'error': '429 quota exhausted'})
        self.assertTrue(caught.exception.external_upstream)
        with patch('xiaoba_agentos.adapters.run_cli', side_effect=subprocess.TimeoutExpired('agy', 1)):
            with self.assertRaises(TransientAdapterError) as caught:
                AgyAdapter(self.agent).run('review', '')
        self.assertTrue(caught.exception.external_upstream)

    def test_a_network_blip_carrying_a_status_code_stays_a_retryable_upstream_fault(self):
        # The Qoder login vocabulary must not reach AGY: "403" inside a transport
        # error used to turn a retryable upstream fault into a terminal auth one.
        from xiaoba_agentos.recovery import classify_failure
        for message in ('connection reset by peer (403)', 'request timeout: unauthorized',
                        'temporarily unavailable, authentication failed'):
            with self.subTest(message=message), self.assertRaises(TransientAdapterError) as caught:
                self.run_payload({'status': 'ERROR', 'error': message})
            self.assertTrue(caught.exception.external_upstream)
            self.assertEqual('external_upstream', classify_failure(caught.exception).name)

    def test_a_dropped_stream_ending_in_eof_takes_the_bounded_retry(self):
        # Measured 2026-10-21 (round 26): this froze two tasks as non-retryable.
        from xiaoba_agentos.recovery import classify_failure
        message = ('API error (attempt 1): request failed: Post '
                   '"https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse": EOF')
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_payload({'status': 'ERROR', 'error': message})
        self.assertTrue(caught.exception.external_upstream)
        self.assertEqual('external_upstream', classify_failure(caught.exception).name)
        # "eof" inside a longer word is not a dropped connection.
        with self.assertRaises(PermanentAdapterError):
            self.run_payload({'status': 'ERROR', 'error': 'invalid geofence parameter'})

    def test_print_timeout_is_zero_for_a_task_and_whole_seconds_for_a_probe(self):
        # `agy --help`: "0 waits until the turn completes"; a real task has no
        # total deadline, an explicit health probe is bounded.
        _answer, run = self.run_payload({'status': 'SUCCESS', 'response': '审查结果'})
        argv = run.call_args.args[0]
        self.assertEqual('0s', argv[argv.index('--print-timeout') + 1])
        adapter = build_adapter(self.agent)
        adapter.execution_timeout = 300.0
        with patch('xiaoba_agentos.adapters.run_cli', return_value=subprocess.CompletedProcess(
                [], 0, json.dumps({'status': 'SUCCESS', 'response': HEALTH_MARKER}), '')) as probe:
            adapter.run('probe', '')
        argv = probe.call_args.args[0]
        self.assertEqual('300s', argv[argv.index('--print-timeout') + 1])

    def test_final_acceptance_requires_one_standalone_verdict(self):
        prompt = 'End with FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL'
        for output in ('PASS', 'FINAL_ACCEPTANCE: PASS suffix', 'FINAL_ACCEPTANCE: PASS\nFINAL_ACCEPTANCE: FAIL'):
            with self.subTest(output=output), patch('xiaoba_agentos.adapters.run_cli', return_value=subprocess.CompletedProcess([], 0, json.dumps({'status': 'SUCCESS', 'response': output}), '')):
                with self.assertRaises(TransientAdapterError):
                    AgyAdapter(self.agent).run(prompt, '')
        valid_output = '分析依据充分。\nFINAL_ACCEPTANCE: PASS'
        with patch('xiaoba_agentos.adapters.run_cli', return_value=subprocess.CompletedProcess([], 0, json.dumps({'status': 'SUCCESS', 'response': valid_output}), '')):
            res = AgyAdapter(self.agent).run(prompt, '')
            self.assertEqual(valid_output, res.output)

    def test_local_route_does_not_inherit_other_provider_credentials(self):
        secrets = {key: 'unit-test-secret' for key in ('OPENAI_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'XIAOBA_ROUTE_API_KEY')}
        with patch.dict(os.environ, secrets):
            env = command_environment(self.config, 'agy')
        self.assertTrue(all(key not in env for key in secrets))

    def test_readiness_rejects_missing_local_command(self):
        self.config.raw['integrations']['agy_command'] = '/missing/agy'
        result = local_cli_readiness(self.config, [self.agent])
        self.assertFalse(result['ok'])
        self.assertTrue(any('/missing/agy' in problem for problem in result['problems']))
        self.assertEqual(['agy_cli', 'antigravity_local_account', 'antigravity_service'], model_paths([self.agent])[self.agent.name])

    def test_capability_gate_uses_actual_agy_model(self):
        # 能力门读的是 cli_routes.local_agy 里用户写的模型；换成一个未声明的模型，
        # 它不会借用别的模型的能力声明。
        self.assertEqual(('Antigravity', self.model), agent_provider_model(self.agent))
        self.config.raw['cli_routes']['local_agy']['model'] = 'Gemini 3.8 Flash (Low)'
        self.assertEqual(('Antigravity', 'Gemini 3.8 Flash (Low)'), agent_provider_model(self.agent))
        enabled, _ = capable_agents([self.agent], CapabilityRegistry(), ['reasoning'])
        self.assertEqual([self.agent], enabled)
        enabled, _ = capable_agents([self.agent], CapabilityRegistry(), ['shell_write_workspace'])
        self.assertEqual([], enabled)
        self.config.raw['cli_routes']['local_agy']['model'] = 'Undeclared AGY Model'
        enabled, _ = capable_agents([self.agent], CapabilityRegistry(), ['reasoning'])
        self.assertEqual([], enabled)

    def test_the_model_written_in_the_shipped_config_is_known(self):
        # 卡 29：配置里写 Gemini 3.8 Flash (High) 时能力是已知的，AGY 能进需要
        # reasoning 的任务；补这份声明不放宽 shell 与 filesystem。
        self.assertEqual('Gemini 3.8 Flash (High)', self.model)
        enabled, _ = capable_agents([self.agent], CapabilityRegistry(), ['reasoning'])
        self.assertEqual([self.agent], enabled)
        for token in ('shell', 'filesystem'):
            enabled, _ = capable_agents([self.agent], CapabilityRegistry(), [token])
            self.assertEqual([], enabled, token)

    def test_route_rejects_cc_switch_credentials(self):
        self.config.raw['cli_routes'] = {'local_agy': {**self.config.raw['cli_routes']['local_agy'], 'credential': 'forbidden'}}
        self.config.raw['agents'] = [self.agent.raw]
        with self.assertRaises(ConfigError):
            self.config._validate_cli_routes()


class ThreeReviewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = AppConfig(root / 'config.json', {
            'schema_version': 1, 'project_dir': str(root), 'runtime_dir': str(root / 'runtime'),
            'worker': {'poll_seconds': 1, 'lease_seconds': 60, 'max_attempts': 3, 'retry_base_seconds': 0, 'retry_max_seconds': 0, 'parallelism': 3, 'minimum_successful_agents': 3, 'max_agent_invocations_per_task': 9},
            'memory': {'knowlp_enabled': False, 'obsidian_vault': str(root), 'obsidian_subdir': 'AgentOS', 'publish_enabled': False},
            'agents': [{'name': name, 'type': 'mock', 'enabled': True, 'role': '', 'timeout_seconds': 1} for name in ('codex', 'claude', 'agy')],
            'evolution': {'minimum_samples': 1, 'promotion_margin': .05, 'latency_penalty': .01},
        })
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def test_all_three_results_required_and_successes_reused(self):
        task_id = self.store.submit('验证三方独立审查', 3)
        calls = dict.fromkeys(('codex', 'claude', 'agy'), 0)

        class Adapter:
            def __init__(self, agent): self.name = agent.name
            def run(self, prompt, context):
                calls[self.name] += 1
                if self.name == 'agy' and calls[self.name] == 1:
                    error = TransientAdapterError('429 quota')
                    error.external_upstream = True
                    raise error
                return AgentResult(self.name, '结论：三方独立审查。依据：冻结证据。风险：遗漏。验证：三份结果。', 1)

        with patch('xiaoba_agentos.orchestrator.build_adapter', side_effect=Adapter):
            first = Orchestrator(self.config, self.store).run_once()
            self.assertEqual('retry', first['status'])
            self.assertEqual([], self.store.review_queue('all'))
            second = Orchestrator(self.config, self.store).run_once()
        self.assertEqual('completed', second['status'])
        self.assertEqual({'codex': 1, 'claude': 1, 'agy': 2}, calls)
        self.assertEqual(3, len(second['scores']))
        self.assertTrue(self.store.get_task(task_id)['result'].startswith('本回答：小八 AgentOS 三 Agent 裁判'))
        self.assertTrue(self.store.verify_event_chain()['ok'])

    def test_missing_third_probe_blocks_queue(self):
        task_id = self.store.submit('queued', 3)
        for name in ('codex', 'claude'):
            self.store.record_agent_health(name, 'healthy')
        result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual('degraded_health', result['status'])
        self.assertEqual('pending', self.store.get_task(task_id)['status'])

    def test_probe_requires_all_three_without_memory_candidate(self):
        class Adapter:
            def __init__(self, agent): self.name = agent.name
            def run(self, prompt, context):
                if self.name == 'agy': raise PermanentAdapterError('401 unauthorized')
                return AgentResult(self.name, HEALTH_MARKER, 1)
        with patch('xiaoba_agentos.recovery.build_adapter', side_effect=Adapter):
            result = live_agent_probe(self.config, self.store)
        self.assertFalse(result['ok'])
        self.assertEqual(['agy'], result['failed_agents'])
        self.assertEqual([], self.store.list_tasks())

    def test_concurrent_events_keep_a_single_valid_chain(self):
        # 起点条数由 setUp 决定，不写死：策略进化退役后新库不再预置策略行，
        # 也就没有那条 strategy.initialized 事件。
        initial = self.store.verify_event_chain()['events']

        def write_events(writer):
            for i in range(30):
                self.store.append_event(None, 'concurrent.test', {'writer': writer, 'i': i})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_events, range(8)))
        chain = self.store.verify_event_chain()
        self.assertTrue(chain['ok'])
        # 8 个写者各写 30 条，一条都不能丢也不能多。
        self.assertEqual(initial + 240, chain['events'])

    def test_corrupt_audit_chain_blocks_before_lease(self):
        task_id = self.store.submit('queued', 3)
        with self.store.connect() as conn:
            # 破坏链尾：写死 sequence=2 会随起点条数变化而失效。
            last = conn.execute("SELECT MAX(sequence) FROM events").fetchone()[0]
            conn.execute("UPDATE events SET prev_hash='bad' WHERE sequence=?", (last,))
        result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual('blocked_configuration', result['status'])
        self.assertEqual('pending', self.store.get_task(task_id)['status'])
