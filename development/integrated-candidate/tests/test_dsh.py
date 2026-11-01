import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import DshAdapter, AgentResult, PermanentAdapterError, TransientAdapterError, build_adapter
from xiaoba_agentos.capabilities import CapabilityRegistry, agent_provider_model, capable_agents
from xiaoba_agentos.config import AppConfig, ConfigError
from xiaoba_agentos.gateway import command_environment, local_cli_readiness, local_cli_route, GatewayUnavailable
from xiaoba_agentos.health import model_paths
from xiaoba_agentos.orchestrator import Orchestrator
from xiaoba_agentos.output_hygiene import NON_ANSWER_MARKERS, detect_reasoning_leak
from xiaoba_agentos.store import EventStore


class DshIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        raw = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/agentos-with-backends.json").read_text())
        raw["cli_routes"]["local_dsh"] = {"driver":"dsh", "selection":"local_harness", "desktop_config":str(self.root / "desktop.yml")}
        raw["project_dir"] = str(self.root)
        raw["runtime_dir"] = str(self.root / "runtime")
        fake_node = self.root / "node"
        fake_node.write_text("#!/bin/sh\nexit 0\n")
        fake_node.chmod(0o755)
        fake_dsh = self.root / "dsh.js"
        fake_dsh.write_text("// fake dsh\n")
        raw["integrations"]["node_command"] = str(fake_node)
        raw["integrations"]["dsh_command"] = str(fake_dsh)
        raw["memory"].update(knowlp_enabled=False, obsidian_vault=str(self.root), publish_enabled=False)
        self.config = AppConfig(self.root / "config.json", raw)
        self.agent = next(a for a in self.config.agents if a.raw.get("driver") == "dsh")
        self.desktop = self.root / 'desktop.yml'
        self.desktop.write_text('backend-owned fixture configuration')
        metadata = {'provider':'Fixture Provider Alpha','provider_id':'provider-alpha','model':'model-alpha','model_source':'dsh_desktop'}
        reader = patch('xiaoba_agentos.gateway.subprocess.run',
                       return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata), ''))
        reader.start()
        self.addCleanup(reader.stop)

    def run_ndjson(self, lines, code=0, stderr="", prompt="审查方案"):
        output = "\n".join(json.dumps(item) if isinstance(item, dict) else str(item) for item in lines)
        result = subprocess.CompletedProcess([], code, output, stderr)
        with patch("xiaoba_agentos.adapters.run_cli", return_value=result) as run:
            answer = build_adapter(self.agent).run(prompt, "仅有这份冻结证据")
        return answer, run

    def test_structured_success_reports_configured_route_and_isolated_plan(self):
        events = [
            {"type": "session", "sessionId": "sess-123"},
            {"type": "thinking", "text": "思考中..."},
            {"type": "text", "text": "初步结论"},
            {"type": "final", "text": "结论：可行。依据：测试。风险：缺证据。验证：复查。"},
        ]
        answer, run = self.run_ndjson(events)
        self.assertEqual("Fixture Provider Alpha", answer.provider)
        self.assertEqual("model-alpha", answer.model)
        self.assertEqual("local_harness", answer.route_source)
        argv = run.call_args.args[0]
        self.assertEqual("--profile", argv[2])
        self.assertEqual("headless", argv[3])
        self.assertEqual("--patch", argv[4])
        self.assertTrue(argv[5].endswith(".yml"))
        self.assertEqual("--json", argv[6])
        self.assertEqual("-", argv[7])
        self.assertNotEqual(str(self.root), run.call_args.kwargs["cwd"])
        self.assertFalse(Path(run.call_args.kwargs["cwd"]).exists())
        self.assertIn("仅有这份冻结证据", run.call_args.kwargs["input"])

    def test_text_stream_fallback_when_final_missing(self):
        # 卡 16: the text fallback survives, but only for a turn that ended.
        events = [
            {"type": "text", "text": "第一段文本；"},
            {"type": "text", "text": "第二段结论。"},
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
        ]
        answer, _ = self.run_ndjson(events)
        self.assertEqual("第一段文本；第二段结论。", answer.output)

    def test_text_stream_without_a_terminal_event_is_not_an_answer(self):
        # 卡 16 反例一（卡 7 第 8 项）：没有结束事件的流不能当答案。
        events = [{"type": "text", "text": "第一段文本；"}, {"type": "text", "text": "第二段结论。"}]
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_ndjson(events)
        self.assertIn("没有结束事件", str(caught.exception))
        self.assertNotIn("第一段文本；第二段结论。", str(caught.exception))

    def test_timeout_does_not_switch_model_or_generate_override(self):
        before = self.desktop.read_bytes()
        with patch("xiaoba_agentos.adapters.run_cli", side_effect=subprocess.TimeoutExpired("dsh", 1)) as call:
            with self.assertRaises(TransientAdapterError) as caught:
                DshAdapter(self.agent).run("review", "")
        self.assertTrue(caught.exception.external_upstream)
        self.assertEqual(1, call.call_count)
        argv = call.call_args.args[0]
        self.assertEqual(['--patch', str(self.desktop)], argv[4:6])
        self.assertEqual(1, argv.count('--patch'))
        self.assertIsNone(call.call_args.kwargs['timeout'])
        self.assertEqual(before, self.desktop.read_bytes())

    def test_shared_backend_configuration_is_forwarded_without_model_patch(self):
        _, call = self.run_ndjson([{'type':'final','text':'完成'}])
        self.assertEqual(1, call.call_count)
        self.assertEqual(str(self.desktop), call.call_args.args[0][5])
        self.assertEqual(1, call.call_args.args[0].count('--patch'))

    def test_auth_failure_does_not_retry_shared_credentials(self):
        result = subprocess.CompletedProcess([], 1, "", "unauthorized")
        with patch("xiaoba_agentos.adapters.run_cli", return_value=result) as call:
            with self.assertRaises(PermanentAdapterError):
                DshAdapter(self.agent).run("review", "")
        self.assertEqual(1, call.call_count)

    def test_desktop_selection_is_read_again_without_static_model_pool(self):
        self.config.raw["cli_routes"]["local_dsh"] = {
            "driver":"dsh", "selection":"local_harness", "desktop_config":"/fixture/desktop.yml"}
        self.config._validate_cli_routes()
        def response(model):
            return subprocess.CompletedProcess([], 0, json.dumps({"provider":"Fixture Provider Alpha",
                "provider_id":"provider-alpha", "model":model, "model_source":"dsh_desktop"}), "")
        with patch("xiaoba_agentos.gateway.subprocess.run", side_effect=[
                response("model-alpha"), response("model-beta")]) as reader:
            self.assertEqual("model-alpha", local_cli_route(self.config, "local_dsh", "dsh_review")["model"])
            self.assertEqual("model-beta", local_cli_route(self.config, "local_dsh", "dsh_review")["model"])
            self.assertEqual(2, reader.call_count)
            self.assertEqual("/fixture/desktop.yml", reader.call_args.args[0][-1])
            self.assertTrue(reader.call_args.args[0][1].endswith("/scripts/dsh_desktop_route.cjs"))
        with patch("xiaoba_agentos.gateway.subprocess.run", return_value=subprocess.CompletedProcess([], 1, "", "invalid")):
            with self.assertRaises(GatewayUnavailable):
                local_cli_route(self.config, "local_dsh", "dsh_review")

    def test_desktop_configuration_is_the_only_backend_overlay(self):
        adapter = DshAdapter(self.agent)
        adapter._selection = {"desktop_config":"/fixture/desktop.yml", "provider_id":"provider-alpha"}
        command = adapter._command()
        self.assertEqual(["--patch", "/fixture/desktop.yml", "--json", "-"], command[4:])

    def test_missing_desktop_selection_never_runs_an_unpatched_cli(self):
        adapter = DshAdapter(self.agent)
        with self.assertRaises(PermanentAdapterError):
            adapter._command()

    def test_same_model_follows_changed_desktop_provider_on_next_call(self):
        def response(provider):
            return subprocess.CompletedProcess([], 0, json.dumps({"provider":provider,
                "provider_id":provider, "model":"shared-model", "model_source":"dsh_desktop"}), "")
        output = subprocess.CompletedProcess([], 0, json.dumps({"type":"final", "text":"完成"}), "")
        adapter = DshAdapter(self.agent)
        with patch("xiaoba_agentos.gateway.subprocess.run", side_effect=[
                response("provider-alpha"), response("provider-beta")]) as reader, \
                patch("xiaoba_agentos.adapters.run_cli", return_value=output) as caller:
            first = adapter.run("review", "")
            second = adapter.run("review", "")
        self.assertEqual(("provider-alpha", "shared-model"), (first.provider, first.model))
        self.assertEqual(("provider-beta", "shared-model"), (second.provider, second.model))
        self.assertEqual(2, reader.call_count)
        self.assertEqual(2, caller.call_count)
        for call in caller.call_args_list:
            self.assertEqual(["--patch", str(self.desktop), "--json", "-"], call.args[0][4:])

    def test_desktop_metadata_is_validated_and_whitelisted(self):
        valid = {"provider":"Fixture Provider Alpha", "provider_id":"provider-alpha",
                 "model":"model-alpha", "model_source":"dsh_desktop"}
        for invalid in ({}, [], {**valid, "model_source":"dsh_web"}, {**valid, "model":" "},
                        {**valid, "provider_id":None}, {**valid, "provider":3}):
            with self.subTest(invalid=invalid), patch("xiaoba_agentos.gateway.subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0, json.dumps(invalid), "")):
                with self.assertRaises(GatewayUnavailable):
                    local_cli_route(self.config, "local_dsh", "dsh_review")
        unsafe = {**valid, "api_key":"fixture-dummy-secret", "model_pool":["model-beta"],
                  "base_url":"https://fixture.invalid"}
        with patch("xiaoba_agentos.gateway.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps(unsafe), "")):
            metadata = local_cli_route(self.config, "local_dsh", "dsh_review")
        self.assertFalse({"api_key", "model_pool", "base_url"} & metadata.keys())
        self.assertNotIn("fixture-dummy-secret", json.dumps(metadata))

    def test_reader_failure_stops_before_backend_call_without_leaking_errors(self):
        # 卡 16: the reader's own diagnostic is kept now, so this locks the
        # narrower contract: the sentinel survives, credentials do not.
        stderr = "DSH Desktop routing configuration is unavailable\napi_key=fixture-dummy-secret"
        with patch("xiaoba_agentos.gateway.subprocess.run", return_value=
                subprocess.CompletedProcess([], 1, "", stderr)), \
                patch("xiaoba_agentos.adapters.run_cli") as caller:
            with self.assertRaises(PermanentAdapterError) as caught:
                DshAdapter(self.agent).run("review", "")
        caller.assert_not_called()
        message = str(caught.exception)
        self.assertIn("DSH Desktop 路由读取失败，停止调用", message)
        self.assertIn("routing configuration is unavailable", message)
        self.assertNotIn("fixture-dummy-secret", message)
        self.assertIn("[REDACTED]", message)

    def test_explicit_output_contract_suppresses_generic_review_wrapper(self):
        prompt = "计算 37 × 41。只输出最终的阿拉伯数字，不要输出任何其他文字。"
        _, run = self.run_ndjson([{"type": "final", "text": "1517"}], prompt=prompt)
        supplied = run.call_args.kwargs["input"]
        self.assertNotIn("请给出明确结论、依据、风险和可验证步骤", supplied)
        self.assertIn("严格遵守任务题面中的输出约束", supplied)

    def test_paired_reasoning_block_is_stripped_before_delivery(self):
        events = [
            {"type": "final", "text": "<think>先分解数字再相加。</think>\n结论：1517。依据：逐位相加。"}
        ]
        answer, _ = self.run_ndjson(events)
        self.assertEqual("结论：1517。依据：逐位相加。", answer.output)
        self.assertEqual([], detect_reasoning_leak(answer.output))

    def test_reasoning_only_response_is_permanent_not_retryable(self):
        reasoning_only = "<" + "think>整段都是推理，没有给出答案。</" + "think>"
        events = [{"type": "final", "text": reasoning_only}]
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_ndjson(events)
        self.assertEqual("internal_adapter_candidate", getattr(caught.exception, "failure_class", ""))
        self.assertFalse(getattr(caught.exception, "transient", False))

    def test_empty_or_tool_markup_is_not_a_success(self):
        for output in ("", "  ", '<invoke name="Read"></invoke>', '<｜DSML｜tool_calls><｜DSML｜invoke name="bash"></｜DSML｜invoke>'):
            with self.subTest(output=output), self.assertRaises(TransientAdapterError):
                self.run_ndjson([{"type": "final", "text": output}])

    def test_all_eight_non_answer_markers_are_rejected_with_casing(self):
        for marker in NON_ANSWER_MARKERS:
            for variant in (f"前缀 {marker} 后缀", f"前缀 {marker.upper()} 后缀"):
                with self.subTest(variant=variant), self.assertRaises(TransientAdapterError):
                    self.run_ndjson([{"type": "final", "text": variant}])

    def test_prompt_and_context_markers_do_not_filter_clean_output(self):
        prompt_with_markers = '请审查包含 <invoke name="bash"> 和 tool-call serialization 的样本'
        clean_response = '结论：审核通过。依据：充分。风险：无。验证：完成。'
        events = [{"type": "final", "text": clean_response}]
        answer, _ = self.run_ndjson(events, prompt=prompt_with_markers)
        self.assertEqual(clean_response, answer.output)

    def test_context_only_acceptance_words_do_not_trigger_contract(self):
        prompt = '普通审查任务，无需输出验收契约'
        context_with_contract = '历史验收参考要求：FINAL_ACCEPTANCE: PASS 或 FINAL_ACCEPTANCE: FAIL'
        plain_response = '这是普通审查结果，不带契约结论行。'
        output = json.dumps({"type": "final", "text": plain_response})
        result = subprocess.CompletedProcess([], 0, output, "")
        with patch("xiaoba_agentos.adapters.run_cli", return_value=result):
            answer = build_adapter(self.agent).run(prompt, context_with_contract)
        self.assertEqual(plain_response, answer.output)

    def test_connection_failures_are_transient_and_bad_requests_are_permanent(self):
        for message in ("ERROR: Reconnecting... 5/5", "error sending request for url (https://example.invalid/v1)",
                        "Connection reset by peer", "network is unreachable"):
            with self.subTest(message=message), self.assertRaises(TransientAdapterError):
                self.run_ndjson([{"type": "error", "message": message}])
        with self.assertRaises(PermanentAdapterError):
            self.run_ndjson([{"type": "error", "message": "invalid model configuration"}])

    def test_an_overloaded_relay_503_is_a_retryable_upstream_fault(self):
        # Measured 2026-10-21 (round 28): this failed tasks outright.
        from xiaoba_agentos.adapters import _is_transient_cli_error
        from xiaoba_agentos.recovery import classify_failure
        message = ('SERVER: 503: {"message":"key-pool: no upstream available (all disabled, '
                   'busy or not configured)","type":"overloaded_error"}')
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_ndjson([{"type": "error", "message": message}])
        self.assertTrue(caught.exception.external_upstream)
        self.assertEqual("external_upstream", classify_failure(caught.exception).name)
        # Each signal is enough on its own; a bare number still is not.
        self.assertTrue(_is_transient_cli_error("SERVER: 503"))
        self.assertTrue(_is_transient_cli_error('{"type":"overloaded_error"}'))
        self.assertFalse(_is_transient_cli_error("waited 503 seconds"))

    def test_a_gateway_html_page_after_a_5xx_is_a_retryable_upstream_fault(self):
        # Measured 2026-10-21 (round 36): it was read as an internal candidate,
        # DSH was marked degraded and the review queue stopped.
        from xiaoba_agentos.adapters import _is_transient_cli_error
        from xiaoba_agentos.recovery import classify_failure
        message = 'INVALID_REQUEST: 502 <!DOCTYPE html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_ndjson([{"type": "error", "message": message}])
        self.assertEqual("external_upstream", classify_failure(caught.exception).name)
        self.assertTrue(_is_transient_cli_error("INVALID_REQUEST: 503 <html><body>x</body></html>"))
        self.assertFalse(_is_transient_cli_error("INVALID_REQUEST: missing field 502 widgets"))

    def test_error_event_in_stream_raises(self):
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_ndjson([{"type": "error", "message": "unauthorized token=secret-token"}])
        self.assertNotIn("secret-token", str(caught.exception))

    def test_quota_and_timeout_use_bounded_external_retry(self):
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_ndjson([{"type": "error", "message": "rate limit exceeded"}])
        self.assertTrue(caught.exception.external_upstream)

        with patch("xiaoba_agentos.adapters.run_cli", side_effect=subprocess.TimeoutExpired("dsh", 1)):
            with self.assertRaises(TransientAdapterError) as caught:
                DshAdapter(self.agent).run("review", "")
        self.assertTrue(caught.exception.external_upstream)

    def test_exhausted_output_budget_is_external_and_never_a_candidate_fault(self):
        events = [
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "max-tokens"}},
            {"type": "final", "text": ""},
        ]
        for code in (1, 0):
            with self.subTest(code=code), self.assertRaises(PermanentAdapterError) as caught:
                self.run_ndjson(events, code=code)
            self.assertEqual("external_upstream", getattr(caught.exception, "failure_class", ""))
            self.assertTrue(getattr(caught.exception, "output_limit_exhausted", False))
            self.assertIn("上限", str(caught.exception))
            self.assertFalse(getattr(caught.exception, "transient", False))

    def test_other_errors_do_not_have_output_limit_exhausted_attribute(self):
        for message in ("rate limit exceeded", "unauthorized", "invalid model configuration"):
            with self.subTest(message=message), self.assertRaises((PermanentAdapterError, TransientAdapterError)) as caught:
                self.run_ndjson([{"type": "error", "message": message}])
            self.assertFalse(getattr(caught.exception, "output_limit_exhausted", False))
        with patch("xiaoba_agentos.adapters.run_cli", side_effect=subprocess.TimeoutExpired("dsh", 1)):
            with self.assertRaises(TransientAdapterError) as caught:
                DshAdapter(self.agent).run("review", "")
            self.assertFalse(getattr(caught.exception, "output_limit_exhausted", False))

    def test_exhausted_output_budget_with_an_answer_still_returns_it(self):
        body = "结论：可行。依据：测试。风险：缺证据。验证：复查。"
        events = [
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "max-tokens"}},
            {"type": "final", "text": body},
        ]
        answer, _ = self.run_ndjson(events)
        self.assertEqual(body, answer.output)

    def test_final_acceptance_requires_one_standalone_verdict(self):
        prompt = "End with FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL"
        for output in ("PASS", "FINAL_ACCEPTANCE: PASS suffix", "FINAL_ACCEPTANCE: PASS\nFINAL_ACCEPTANCE: FAIL"):
            with self.subTest(output=output):
                with self.assertRaises(TransientAdapterError):
                    result = subprocess.CompletedProcess([], 0, json.dumps({"type": "final", "text": output}), "")
                    with patch("xiaoba_agentos.adapters.run_cli", return_value=result):
                        DshAdapter(self.agent).run(prompt, "")
        valid_output = "结论：合格。\nFINAL_ACCEPTANCE: PASS"
        result = subprocess.CompletedProcess([], 0, json.dumps({"type": "final", "text": valid_output}), "")
        with patch("xiaoba_agentos.adapters.run_cli", return_value=result):
            res = DshAdapter(self.agent).run(prompt, "")
            self.assertEqual(valid_output, res.output)

    def test_local_route_does_not_inherit_other_provider_credentials(self):
        secrets = {key: "unit-test-secret" for key in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "XIAOBA_ROUTE_API_KEY")}
        with patch.dict(os.environ, secrets):
            env = command_environment(self.config, "dsh")
        self.assertTrue(all(key not in env for key in secrets))

    def test_readiness_validates_node_and_dsh_paths(self):
        result = local_cli_readiness(self.config, [self.agent])
        self.assertTrue(result["ok"])
        self.assertEqual(
            ["dsh_cli", "dsh_local_settings", "provider_endpoint"],
            model_paths([self.agent])[self.agent.name],
        )

        # Missing dsh script
        self.config.raw["integrations"]["dsh_command"] = "/missing/dsh.js"
        result_missing = local_cli_readiness(self.config, [self.agent])
        self.assertFalse(result_missing["ok"])

    def test_capability_gate_uses_actual_dsh_model(self):
        self.assertEqual("Fixture Provider Alpha", agent_provider_model(self.agent)[0])
        self.assertEqual("model-alpha", agent_provider_model(self.agent)[1])

    def test_route_rejects_cc_switch_credentials(self):
        self.config.raw["cli_routes"] = {"local_dsh": {**self.config.raw["cli_routes"]["local_dsh"], "credential": "forbidden"}}
        self.config.raw["agents"] = [self.agent.raw]
        with self.assertRaises(ConfigError):
            self.config._validate_cli_routes()

    def test_route_rejects_legacy_web_static_upstream_and_relative_desktop_path(self):
        valid = dict(self.config.raw["cli_routes"]["local_dsh"])
        for key in ("web_config", "credential", "base_url", "endpoint", "provider", "provider_id",
                    "model", "model_pool", "api_key", "apiKey", "key", "token", "auth_token", "headers",
                    "api_token", "unknown_field"):
            self.config.raw["cli_routes"]["local_dsh"] = {**valid, key:"forbidden"}
            with self.subTest(key=key), self.assertRaises(ConfigError):
                self.config._validate_cli_routes()
        self.config.raw["cli_routes"]["local_dsh"] = {**valid, "desktop_config":"relative.yml"}
        with self.assertRaises(ConfigError):
            self.config._validate_cli_routes()
        self.config.raw["cli_routes"]["local_dsh"] = {
            "driver":"dsh", "selection":"local_harness", "web_config":"/fixture/web.yml"}
        with self.assertRaises(ConfigError):
            self.config._validate_cli_routes()

    def test_route_schema_rejects_nested_credentials_without_echoing_keys_or_values(self):
        self.config.raw["cli_routes"]["local_dsh"]["upstream"] = {
            "api_token":"fixture-dummy-secret", "model":"model-alpha"}
        with self.assertRaises(ConfigError) as caught:
            self.config._validate_cli_routes()
        self.assertEqual("DSH 路由仅允许 driver、selection、desktop_config；不得配置上游、模型或其他字段", str(caught.exception))
        self.assertNotIn("fixture-dummy-secret", str(caught.exception))
        self.assertNotIn("api_token", str(caught.exception))

    def test_cli_timeout_kills_descendants_holding_output_pipe(self):
        import sys, time, subprocess
        from xiaoba_agentos.adapters import run_cli
        code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); time.sleep(20)"
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_cli([sys.executable, "-c", code], timeout=0.2)
        self.assertLess(time.monotonic() - start, 3)


class DshDesktopReaderTest(unittest.TestCase):
    """Exercise the actual reader with DSH's native parser, without inference."""

    @classmethod
    def setUpClass(cls):
        cls.project = Path(__file__).resolve().parents[1]
        integrations = json.loads((cls.project / "tests/fixtures/agentos-with-backends.json").read_text())["integrations"]
        cls.node = integrations["node_command"]
        cls.dsh = integrations["dsh_command"]
        if not Path(cls.node).is_file() or not Path(cls.dsh).is_file():
            raise unittest.SkipTest("native DSH parser is not installed")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.desktop = Path(self.tmp.name) / "desktop.yml"
        self.entries = [
            {"id":"agent-default-model", "config":{"provider":"provider-alpha", "model":"shared-model"}},
            {"id":"llm-pi-ai", "config":{"providers":{
                "provider-alpha":{"displayName":"Fixture Provider Alpha",
                    "baseUrl":"https://fixture-alpha.invalid", "apiKey":"fixture-dummy-secret-alpha",
                    "models":[{"id":"shared-model"}, {"id":"model-alpha"}]},
                "provider-beta":{"displayName":"Fixture Provider Beta",
                    "baseUrl":"https://fixture-beta.invalid", "apiKey":"fixture-dummy-secret-beta",
                    "models":[{"id":"shared-model"}, {"id":"model-beta"}]},
            }}},
        ]

    def read(self, entries=None):
        if entries is not None:
            self.desktop.write_text(json.dumps(entries), encoding="utf-8")
        return subprocess.run([self.node, str(self.project / "scripts/dsh_desktop_route.cjs"),
                               self.dsh, str(self.desktop)],
                              capture_output=True, text=True, timeout=10, check=False)

    def test_native_reader_follows_model_and_same_model_provider_changes(self):
        first = self.read(self.entries)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual({"provider":"Fixture Provider Alpha", "provider_id":"provider-alpha",
                          "model":"shared-model", "model_source":"dsh_desktop"}, json.loads(first.stdout))
        self.entries[0]["config"]["provider"] = "provider-beta"
        second = self.read(self.entries)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual({"provider":"Fixture Provider Beta", "provider_id":"provider-beta",
                          "model":"shared-model", "model_source":"dsh_desktop"}, json.loads(second.stdout))
        self.entries[0]["config"]["model"] = "model-beta"
        third = self.read(self.entries)
        self.assertEqual("model-beta", json.loads(third.stdout)["model"])
        for result in (first, second, third):
            self.assertEqual("", result.stderr)
            self.assertNotIn("fixture-dummy-secret", result.stdout)
            self.assertNotIn("fixture-alpha.invalid", result.stdout)
            self.assertNotIn("fixture-beta.invalid", result.stdout)
            self.assertEqual({"provider", "provider_id", "model", "model_source"}, json.loads(result.stdout).keys())

    def test_native_reader_rejects_invalid_selection_with_fixed_nonsecret_error(self):
        invalid_entries = [None, {}, self.entries + [self.entries[0]],
            [{**self.entries[0], "config":{"provider":3, "model":"shared-model"}}, self.entries[1]],
            [{**self.entries[0], "config":{"provider":" ", "model":"shared-model"}}, self.entries[1]],
            [{**self.entries[0], "config":{"provider":"provider-alpha", "model":" "}}, self.entries[1]],
        ]
        for entries in invalid_entries:
            with self.subTest(entries=entries):
                # null is passed explicitly as serialized input, rather than omitted.
                self.desktop.write_text(json.dumps(entries), encoding="utf-8")
                result = self.read()
                self.assertEqual(1, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("DSH Desktop routing configuration is unavailable or invalid\n", result.stderr)
                self.assertNotIn("fixture-dummy-secret", result.stderr)

    def test_native_reader_leaves_catalog_and_account_provider_resolution_to_harness(self):
        del self.entries[1]["config"]["providers"]["provider-alpha"]["models"]
        catalog = self.read(self.entries)
        self.assertEqual(0, catalog.returncode, catalog.stderr)
        self.assertEqual("Fixture Provider Alpha", json.loads(catalog.stdout)["provider"])
        account = self.read([self.entries[0]])
        self.assertEqual(0, account.returncode, account.stderr)
        self.assertEqual({"provider":"provider-alpha", "provider_id":"provider-alpha",
                          "model":"shared-model", "model_source":"dsh_desktop"}, json.loads(account.stdout))
        self.entries[0]["config"] = {"provider":"native-provider", "model":"native-catalog-model"}
        native = self.read(self.entries)
        self.assertEqual(0, native.returncode, native.stderr)
        self.assertEqual("native-provider", json.loads(native.stdout)["provider"])
        self.assertNotIn("fixture-dummy-secret", native.stdout)

    def test_native_reader_never_prints_yaml_error_or_missing_path(self):
        self.desktop.write_text("secret: fixture-dummy-secret\ninvalid: [\n", encoding="utf-8")
        invalid = self.read()
        self.desktop.unlink()
        missing = self.read()
        for result in (invalid, missing):
            self.assertEqual(1, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual("DSH Desktop routing configuration is unavailable or invalid\n", result.stderr)
            self.assertNotIn("fixture-dummy-secret", result.stderr)
            self.assertNotIn(str(self.desktop), result.stderr)


class DualReviewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = AppConfig(root / "config.json", {
            "schema_version": 1,
            "project_dir": str(root),
            "runtime_dir": str(root / "runtime"),
            "worker": {
                "poll_seconds": 1,
                "lease_seconds": 60,
                "max_attempts": 3,
                "retry_base_seconds": 0,
                "retry_max_seconds": 0,
                "parallelism": 2,
                "minimum_successful_agents": 2,
                "max_agent_invocations_per_task": 6,
            },
            "memory": {
                "knowlp_enabled": False,
                "obsidian_vault": str(root),
                "obsidian_subdir": "AgentOS",
                "publish_enabled": False,
            },
            "agents": [
                {"name": "agy_review", "type": "mock", "enabled": True, "role": "AGY审查员", "timeout_seconds": 1},
                {"name": "dsh_review", "type": "mock", "enabled": True, "role": "DSH审查员", "timeout_seconds": 1},
            ],
            "evolution": {"minimum_samples": 1, "promotion_margin": 0.05, "latency_penalty": 0.01},
        })
        self.store = EventStore(self.config.database_path)
        self.store.initialize()

    def test_dual_results_both_required_and_deterministic_judge(self):
        task_id = self.store.submit("验证双后台独立审查：AGY + DSH", 2)
        orch = Orchestrator(self.config, self.store)
        res = orch.run_once()
        self.assertEqual("completed", res["status"])
        task = self.store.get_task(task_id)
        self.assertEqual("completed", task["status"])
        self.assertTrue(task["result"].startswith("本回答：小八 AgentOS 双 Agent 裁判"))
        self.assertEqual(2, len(res["scores"]))


if __name__ == "__main__":
    unittest.main()
