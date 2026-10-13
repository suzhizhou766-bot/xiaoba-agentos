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
        raw = json.loads((Path(__file__).resolve().parents[1] / 'config/agentos.json').read_text())
        raw['project_dir'] = str(self.root)
        raw['runtime_dir'] = str(self.root / 'runtime')
        raw['integrations']['agy_command'] = '/usr/bin/true'
        raw['memory'].update(knowlp_enabled=False, obsidian_vault=str(self.root), publish_enabled=False)
        self.config = AppConfig(self.root / 'config.json', raw)
        self.agent = next(a for a in self.config.agents if a.raw.get('driver') == 'agy')
        metadata = {'provider': 'Antigravity', 'model': 'gemini-3.8-flash-low',
                    'model_source': 'antigravity_desktop'}
        reader = patch('xiaoba_agentos.gateway.subprocess.run',
                       return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata), ''))
        reader.start()
        self.addCleanup(reader.stop)

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
        self.assertEqual('gemini-3.8-flash-low', answer.model)
        self.assertEqual('local_account', answer.route_source)
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

    def desktop_route(self):
        self.config.raw['cli_routes']['local_agy'] = {
            'driver': 'agy', 'selection': 'local_account',
            'desktop_debug_port': str(self.root / 'DevToolsActivePort'),
        }

    def test_desktop_selection_is_read_again_for_each_invocation_without_effort_override(self):
        self.desktop_route()
        self.config._validate_cli_routes()
        for model in ('Gemini 3.8 Flash (High)', 'Claude Opus 4.6 (Thinking)'):
            metadata = {'provider': 'Antigravity', 'model': model,
                        'model_source': 'antigravity_desktop'}
            with patch('xiaoba_agentos.gateway.subprocess.run', return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata), '')) as reader:
                answer, run = self.run_payload({'status': 'SUCCESS', 'response': '审查结果'})
            self.assertEqual(model, answer.model)
            argv = run.call_args.args[0]
            self.assertEqual(model, argv[argv.index('--model') + 1])
            self.assertNotIn('--effort', argv)
            self.assertIn('--sandbox', argv)
            self.assertEqual('plan', argv[argv.index('--mode') + 1])
            self.assertEqual(1, reader.call_count)

    def test_desktop_read_failure_never_uses_static_model(self):
        self.desktop_route()
        for result in (subprocess.CompletedProcess([], 1, '', 'unavailable'),
                       subprocess.CompletedProcess([], 0, '{}', ''),
                       subprocess.CompletedProcess([], 0, '[]', '')):
            with patch('xiaoba_agentos.gateway.subprocess.run', return_value=result):
                with self.assertRaises(GatewayUnavailable):
                    local_cli_route(self.config, 'local_agy', self.agent.name)

    def test_desktop_route_rejects_static_override_and_relative_port_file(self):
        self.desktop_route()
        self.config.raw['cli_routes']['local_agy']['model_pool'] = ['gemini-3.8-flash-low']
        with self.assertRaises(ConfigError): self.config._validate_cli_routes()
        del self.config.raw['cli_routes']['local_agy']['model_pool']
        self.config.raw['cli_routes']['local_agy']['desktop_debug_port'] = 'relative-path'
        with self.assertRaises(ConfigError): self.config._validate_cli_routes()

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
        self.assertEqual(('Antigravity', 'gemini-3.8-flash-low'), agent_provider_model(self.agent))
        enabled, _ = capable_agents([self.agent], CapabilityRegistry(), ['reasoning'])
        self.assertEqual([self.agent], enabled)
        enabled, _ = capable_agents([self.agent], CapabilityRegistry(), ['shell_write_workspace'])
        self.assertEqual([], enabled)

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
        def write_events(writer):
            for i in range(30):
                self.store.append_event(None, 'concurrent.test', {'writer': writer, 'i': i})
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write_events, range(8)))
        self.assertTrue(self.store.verify_event_chain()['ok'])
        self.assertEqual(241, self.store.verify_event_chain()['events'])

    def test_corrupt_audit_chain_blocks_before_lease(self):
        task_id = self.store.submit('queued', 3)
        with self.store.connect() as conn:
            conn.execute("UPDATE events SET prev_hash='bad' WHERE sequence=2")
        result = Orchestrator(self.config, self.store).run_once()
        self.assertEqual('blocked_configuration', result['status'])
        self.assertEqual('pending', self.store.get_task(task_id)['status'])
