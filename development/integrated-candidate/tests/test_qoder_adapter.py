"""qodercli standby adapter: command contract, parsing, classification, isolation.

No test here talks to the real qodercli.  `QoderStub` is a shell script that
records its argv, cwd and stdin and replays a fixed JSON payload, so the whole
subprocess path (arguments, temporary working directory, stdin prompt, stdout
parsing) is exercised without spending a remote inference request.
"""

import inspect
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import (
    CLI_IDLE_TIMEOUT_SECONDS,
    PermanentAdapterError,
    QoderAdapter,
    TransientAdapterError,
    build_adapter,
    run_cli,
)
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.gateway import (
    command_environment,
    local_cli_readiness,
    local_cli_route,
)
from xiaoba_agentos.output_hygiene import contains_non_answer_markers
from xiaoba_agentos.recovery import classify_failure


def write_settings(path, model="qfmodel", effort="xhigh"):
    """A qodercli settings.json shaped like the real one (only the model block)."""
    block = {"name": model} if model is not None else {}
    if model is not None and effort is not None:
        block["preferences"] = {model: {"reasoning": {"effort": effort}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"model": block}), encoding="utf-8")

PROJECT = Path(__file__).resolve().parents[1]


class QoderStub:
    """Fake qodercli: records the invocation, replays a fixed payload."""

    def __init__(self, root):
        self.root = Path(root)
        self.argv_file = self.root / "stub-argv.txt"
        self.cwd_file = self.root / "stub-cwd.txt"
        self.stdin_file = self.root / "stub-stdin.txt"
        self.output_file = self.root / "stub-output.json"
        self.error_file = self.root / "stub-error.txt"
        self.code_file = self.root / "stub-code.txt"
        self.path = self.root / "qodercli-stub"
        self.output_file.write_text("", encoding="utf-8")
        self.error_file.write_text("", encoding="utf-8")
        self.code_file.write_text("0", encoding="utf-8")
        self.path.write_text(
            "#!/bin/sh\n"
            'printf "%s\\n" "$@" > "' + str(self.argv_file) + '"\n'
            'pwd > "' + str(self.cwd_file) + '"\n'
            'cat > "' + str(self.stdin_file) + '"\n'
            'cat "' + str(self.output_file) + '"\n'
            'cat "' + str(self.error_file) + '" >&2\n'
            'exit $(cat "' + str(self.code_file) + '")\n',
            encoding="utf-8",
        )
        self.path.chmod(0o755)

    def reply(self, payload, code=0, stderr=""):
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        self.output_file.write_text(text, encoding="utf-8")
        self.error_file.write_text(stderr, encoding="utf-8")
        self.code_file.write_text(str(code), encoding="utf-8")

    def argv(self):
        if not self.argv_file.exists():
            return None
        return self.argv_file.read_text(encoding="utf-8").splitlines()

    def stdin(self):
        return self.stdin_file.read_text(encoding="utf-8")

    def cwd(self):
        return self.cwd_file.read_text(encoding="utf-8").strip()


class QoderAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stub = QoderStub(self.root)
        self.settings = self.root / ".qoder" / "settings.json"
        write_settings(self.settings)
        raw = json.loads((PROJECT / "tests/fixtures/agentos-with-backends.json").read_text(encoding="utf-8"))
        raw["project_dir"] = str(self.root)
        raw["runtime_dir"] = str(self.root / "runtime")
        raw["memory"].update(knowlp_enabled=False, obsidian_vault=str(self.root), publish_enabled=False)
        raw["integrations"]["qoder_command"] = str(self.stub.path)
        # The user's home is the temporary root; settings paths must be exactly
        # the user's own files.
        home = patch.object(Path, "home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        raw["cli_routes"]["local_qoder"] = {
            "driver": "qoder",
            "selection": "qodercli_settings",
            "settings_path": str(self.settings),
        }
        # Replace the template's standby with the one under test (not a second
        # entry with the same name), and load through AppConfig.load so the
        # duplicate-name, route and standby rules apply to this fixture too.
        raw["agents"] = [agent for agent in raw["agents"] if agent.get("name") != "qoder_review"]
        self.fixture_role = "备用只读审查员（测试夹具）：不得调用工具或修改文件。"
        raw["agents"].append({
            "name": "qoder_review",
            "type": "local_cli",
            "driver": "qoder",
            "cli_route": "local_qoder",
            "enabled": True,
            "standby": True,
            "timeout_seconds": 300,
            "role": self.fixture_role,
        })
        self.raw = raw
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.config = AppConfig.load(config_path)
        matching = [agent for agent in self.config.agents if agent.name == "qoder_review"]
        self.assertEqual(1, len(matching))
        self.agent = matching[0]
        self.assertEqual(self.fixture_role, self.agent.role)

    def payload(self, text="结论：通过。依据：冻结证据。风险：无。验证：复查。", model="qfmodel"):
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "modelUsage": {model: {"inputTokens": 12, "outputTokens": 34}},
        }

    def run_agent(self, prompt="审查方案", context="仅有这份冻结证据"):
        return build_adapter(self.agent).run(prompt, context)

    # --- command contract -------------------------------------------------

    def test_command_arguments_are_exactly_the_read_only_contract(self):
        self.stub.reply(self.payload())
        answer = self.run_agent()
        # The stub recording any invocation at all proves the configured
        # qoder_command is the binary that ran.
        self.assertEqual(
            [
                "-p",
                "--model", "qfmodel",
                "--setting-sources", "project,local",
                "--tools",
                "",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--settings",
                '{"disableAllHooks":true}',
                "--output-format",
                "stream-json",
                "--reasoning-effort", "xhigh",
                "--include-partial-messages",
            ],
            self.stub.argv(),
        )
        self.assertIn("--model", self.stub.argv())
        self.assertNotIn("-m", self.stub.argv())
        self.assertNotIn("--allowed-tools", self.stub.argv())
        self.assertEqual("qoder", answer.provider)
        self.assertEqual("qfmodel", answer.model)
        self.assertEqual("qodercli_settings", answer.route_source)

    def test_prompt_travels_on_stdin_inside_an_ephemeral_directory(self):
        self.stub.reply(self.payload())
        self.run_agent(prompt="审查这份改动", context="冻结上下文：只读证据")
        supplied = self.stub.stdin()
        self.assertIn("审查这份改动", supplied)
        self.assertIn("冻结上下文：只读证据", supplied)
        self.assertIn("备用只读审查员", supplied)
        # The known qodercli habit of printing tool-call markup is answered with
        # an explicit reminder in the prompt, not with a silent retry.
        self.assertIn("不要输出任何工具调用标记", supplied)
        cwd = Path(self.stub.cwd())
        self.assertNotEqual(self.root.resolve(), cwd)
        self.assertFalse(cwd.exists(), "临时工作目录必须在调用结束后消失")
        self.assertNotIn("审查这份改动", " ".join(self.stub.argv()))

    # --- model is the one the user last chose in qodercli (user 2026-10-20) --

    def test_model_comes_from_the_settings_file_and_is_read_per_call(self):
        self.stub.reply(self.payload(model="qfmodel"))
        self.assertEqual("qfmodel", self.run_agent().model)
        write_settings(self.settings, model="another-model", effort="high")
        self.stub.reply(self.payload(model="another-model"))
        answer = self.run_agent()
        self.assertEqual("another-model", answer.model)
        args = self.stub.argv()
        self.assertEqual('another-model', args[args.index('--model') + 1])
        self.assertEqual('high', args[args.index('--reasoning-effort') + 1])
        self.assertEqual(
            "another-model", local_cli_route(self.config, "local_qoder", "qoder_review")["model"]
        )

    # --- the configured model is the model in use (user 2026-11-01) --------
    #
    # What the CLI reports about which model answered is never checked: a
    # differing, missing or ambiguous report is not a failure. The result records
    # the model the user selected in qodercli, which is the one handed to
    # --model.

    def test_a_differing_reported_model_is_not_a_failure(self):
        # Used to raise PermanentAdapterError("实际返回模型与配置不一致").
        self.stub.reply(self.payload(model="auto"))
        answer = self.run_agent()
        self.assertEqual("qfmodel", answer.model)
        self.assertEqual("qfmodel", self.stub.argv()[self.stub.argv().index("--model") + 1])

    def test_missing_model_usage_is_not_a_failure(self):
        # Used to raise PermanentAdapterError("缺少实际模型证据").
        payload = self.payload()
        payload.pop("modelUsage")
        self.stub.reply(payload)
        self.assertEqual("qfmodel", self.run_agent().model)

    def test_two_model_keys_are_not_a_failure(self):
        # Used to raise on "不一致" and, for an alias selection, on "多个模型".
        payload = self.payload()
        payload["modelUsage"]["another-model"] = {"inputTokens": 1}
        self.stub.reply(payload)
        self.assertEqual("qfmodel", self.run_agent().model)
        write_settings(self.settings, model="auto", effort=None)
        payload = self.payload(model="auto")
        payload["modelUsage"]["another-model"] = {"inputTokens": 1}
        self.stub.reply(payload)
        self.assertEqual("auto", self.run_agent().model)

    def test_the_docstring_matches_what_the_code_actually_does(self):
        flat = " ".join(QoderAdapter.__doc__.split())
        # The old text claimed plugins were disabled and that the model came
        # from Qoder Desktop; neither was true after v1.6.5.
        self.assertNotIn("plugins and session persistence disabled", flat)
        self.assertNotIn("Desktop", flat)
        # What is actually closed has to be named, so the claim stays checkable
        # against _command().
        self.assertIn('--tools ""', flat)
        self.assertIn("disableAllHooks", flat)
        self.assertIn("qodercli", flat)

    def test_cn_cli_and_symlink_to_cn_cli_are_rejected_before_execution(self):
        # 1. The configured command is itself named as the CN CLI: refused even
        #    when it is a launcher pointing at another binary.
        cn = self.root / 'qoderclicn'
        cn.symlink_to(self.stub.path)
        self.config.integrations['qoder_command'] = str(cn)
        with self.assertRaisesRegex(PermanentAdapterError, 'Qoder CN'):
            self.run_agent()
        # 2. An ordinary name whose final target is the CN binary: refused.
        #    (What decides is the binary that would actually run: a link chain
        #    that only passes through a CN-named link and ends at the standard
        #    CLI runs the standard CLI and is not refused.)
        real_cn = self.root / 'qoderclicn-real'
        real_cn.write_text('#!/bin/sh\nexit 99\n')
        real_cn.chmod(0o755)
        alias = self.root / 'ordinary-cli'
        alias.symlink_to(real_cn)
        self.config.integrations['qoder_command'] = str(alias)
        with self.assertRaisesRegex(PermanentAdapterError, 'Qoder CN'):
            self.run_agent()
        self.assertIsNone(self.stub.argv())

    def test_no_effort_in_settings_passes_no_effort_flag(self):
        write_settings(self.settings, effort=None)
        self.stub.reply(self.payload())
        self.run_agent()
        args = self.stub.argv()
        self.assertNotIn('--reasoning-effort', args)
        self.assertEqual('project,local', args[args.index('--setting-sources') + 1])

    def test_missing_settings_file_stops_before_any_call(self):
        self.settings.unlink()
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_agent()
        self.assertIn("qodercli 设置", str(caught.exception))
        self.assertIsNone(self.stub.argv(), "设置不可读时不得调用 qodercli")

    def test_settings_without_model_name_is_unavailable_not_defaulted(self):
        for broken in ({"model": {}}, {"model": {"name": ""}}, {"model": {"name": "has space"}}, {}, []):
            with self.subTest(settings=broken):
                self.settings.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(PermanentAdapterError) as caught:
                    self.run_agent()
                self.assertIn("明确的模型名", str(caught.exception))
                self.assertIsNone(self.stub.argv())

    def _stream(self, *extra_lines, result_text="结论：通过。依据：冻结证据。风险：无。验证：复查。",
                assistant_text=None):
        lines = [
            {"type": "system", "subtype": "init", "model": "qfmodel"},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "thinking_delta", "thinking": "（思考，不应进入答案）"}}},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta", "text": "结论："}}},
            {"type": "assistant", "message": {"content": [{"type": "text",
                                                            "text": assistant_text or result_text}]}},
            dict(self.payload(text=result_text)),
        ]
        body = [json.dumps(line, ensure_ascii=False) for line in lines]
        for position, raw in extra_lines:
            body.insert(position, raw)
        return "\n".join(body) + "\n"

    def test_a_real_multi_line_stream_answers_from_the_single_result_line(self):
        # _command asks for stream-json: the answer is the one terminal result,
        # never the thinking or partial deltas before it (pre-review R10).
        # The assistant message differs from the result line, so an adapter that
        # read the assistant message instead would fail here (round-37 review).
        self.stub.reply(self._stream(assistant_text="（assistant 消息，不是最终答案）"))
        answer = self.run_agent()
        self.assertEqual("结论：通过。依据：冻结证据。风险：无。验证：复查。", answer.output.strip())
        self.assertNotIn("思考", answer.output)
        self.assertNotIn("assistant 消息", answer.output)

    def test_a_stray_non_json_line_in_the_stream_is_not_an_answer(self):
        self.stub.reply(self._stream((2, "warning: something printed to stdout")))
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_agent()
        self.assertEqual("internal_adapter_candidate", classify_failure(caught.exception).name)

    def test_a_flag_shaped_model_name_is_not_a_model(self):
        # It is handed to --model; something shaped like a CLI flag must stop
        # the call instead (pre-review R9).
        for name in ("--tools", "-c", "--no-session-persistence", "_x", ".hidden"):
            with self.subTest(name=name):
                write_settings(self.settings, model=name, effort=None)
                with self.assertRaises(PermanentAdapterError) as caught:
                    self.run_agent()
                self.assertIn("明确的模型名", str(caught.exception))
                self.assertIsNone(self.stub.argv())

    def test_unsupported_effort_in_settings_stops_before_any_call(self):
        write_settings(self.settings, effort="extreme")
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_agent()
        self.assertIn("思考强度不支持", str(caught.exception))
        self.assertIsNone(self.stub.argv())

    # --- failure classification -------------------------------------------

    def test_login_failure_is_external_auth_not_a_candidate_fault(self):
        self.stub.reply(
            {"type": "result", "subtype": "success", "is_error": True,
             "result": "Not logged in · Please run /login", "modelUsage": {}},
            code=1,
        )
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_agent()
        self.assertEqual("external_auth", getattr(caught.exception, "failure_class", ""))
        self.assertTrue(classify_failure(caught.exception).external)
        self.assertTrue(classify_failure(caught.exception).user_action_required)

    def test_unparsable_stdout_without_a_stderr_signal_is_an_internal_candidate_fault(self):
        # stdout that does not parse as a stream -- a result line followed by a
        # non-JSON line, or no JSON at all -- cannot tell a CLI diagnostic from
        # model text, so an empty stderr is not evidence of an upstream outage.
        # Assuming one used to buy a standby substitution for free.
        for stdout in ('{"type": "result", "result": "结论"}\nqodercli: unexpected internal failure',
                       "qodercli: unexpected internal failure"):
            with self.subTest(stdout=stdout):
                self.stub.reply(stdout, code=1)
                with self.assertRaises(PermanentAdapterError) as caught:
                    self.run_agent()
                self.assertEqual("internal_adapter_candidate", getattr(caught.exception, "failure_class", ""))
                self.assertFalse(classify_failure(caught.exception).external)

    def test_unparsable_stdout_with_a_stderr_network_signal_is_external_upstream(self):
        self.stub.reply(
            '{"type": "result", "result": "结论"}\nqodercli: unexpected internal failure',
            code=1,
            stderr="error sending request: connection reset by peer",
        )
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_agent()
        self.assertTrue(caught.exception.external_upstream)
        self.assertTrue(classify_failure(caught.exception).external)
        # The CLI's own explanation is what the user needs; the unparsable
        # stdout it came with is not.
        self.assertIn("connection reset", str(caught.exception))
        self.assertNotIn("unexpected internal failure", str(caught.exception))

    def test_auth_wording_on_stderr_keeps_the_external_auth_label(self):
        # stderr is the CLI's own channel, so a credential failure printed there
        # is still external and still stops the task for the user.
        self.stub.reply(
            "qodercli: cannot start",
            code=1,
            stderr="401 Unauthorized: invalid api key token=fixture-dummy-secret",
        )
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_agent()
        self.assertEqual("external_auth", getattr(caught.exception, "failure_class", ""))
        # The CLI's own explanation reaches the user, with the token redacted.
        self.assertIn("401 Unauthorized", str(caught.exception))
        self.assertNotIn("fixture-dummy-secret", str(caught.exception))

    def test_auth_words_in_the_answer_body_are_not_an_auth_failure(self):
        # The model can write "unauthorized / please login" in its own answer.
        # With is_error false and an empty stderr the CLI reported no auth
        # failure, so this must not become external_auth.
        payload = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "unauthorized: please login and retry",
            "modelUsage": {},
        }
        self.stub.reply(payload, code=1)
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_agent()
        self.assertEqual("internal_adapter_candidate", getattr(caught.exception, "failure_class", ""))
        classification = classify_failure(caught.exception)
        self.assertEqual("internal_adapter_candidate", classification.name)
        self.assertFalse(classification.external)

    def test_rate_limit_quota_and_connection_errors_are_transient(self):
        for message in ("rate limit exceeded", "quota exhausted", "Connection reset by peer",
                        "error sending request for url (https://fixture.invalid/v1)"):
            with self.subTest(message=message):
                self.stub.reply({"is_error": True, "result": message, "modelUsage": {}}, code=1)
                with self.assertRaises(TransientAdapterError) as caught:
                    self.run_agent()
                self.assertTrue(caught.exception.external_upstream)
                self.assertTrue(classify_failure(caught.exception).external)

    def test_daily_usage_limit_in_the_errors_list_is_an_external_quota_fault(self):
        # Measured shape (qodercli, 2026-10-21): the reason is only in `errors`.
        # Reading it as "no explanation" made the standby look internally broken
        # and stopped the queue while AGY was out of capacity.
        message = ("You've reached your daily usage limit for Chat. Come back tomorrow "
                   "to continue working with me. Report Issue (input /feedback)")
        self.stub.reply({
            "type": "result", "subtype": "error_during_execution", "is_error": True,
            "num_turns": 1, "stop_reason": "stop_sequence", "total_cost_usd": 0,
            "modelUsage": {"<synthetic>": {"inputTokens": 0, "outputTokens": 0}},
            "permission_denials": [], "errors": [message], "error_code": 110,
        }, code=1)
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_agent()
        self.assertTrue(caught.exception.external_upstream)
        self.assertIn("daily usage limit", str(caught.exception))
        classification = classify_failure(caught.exception)
        self.assertEqual("external_upstream", classification.name)
        self.assertTrue(classification.external)

    def test_usage_limit_wording_alone_is_classified_external(self):
        # The recovery layer reaches the same verdict from the text alone, so a
        # stored failure without the adapter's attribute is not re-read as internal.
        classification = classify_failure(RuntimeError("You've reached your daily usage limit for Chat."))
        self.assertEqual("external_upstream", classification.name)
        self.assertTrue(classification.external)

    def test_503_is_classified_external_upstream(self):
        self.stub.reply({"is_error": True, "result": "503 Service Unavailable", "modelUsage": {}}, code=1)
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_agent()
        self.assertEqual("external_upstream", classify_failure(caught.exception).name)
        self.assertTrue(classify_failure(caught.exception).external)

    def test_empty_answer_is_external(self):
        self.stub.reply({"is_error": False, "result": "   ", "modelUsage": {"qfmodel": {}}})
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_agent()
        self.assertTrue(caught.exception.external_upstream)

    def test_an_error_subtype_beside_exit_zero_is_never_an_answer(self):
        # Round 24: exit 0 with is_error false but an `error_*` subtype is still
        # the CLI saying the call failed; its `result` must not become an answer.
        payload = self.payload("只写了一半的回答")
        payload["subtype"] = "error_max_turns"
        self.stub.reply(payload)
        with self.assertRaises((TransientAdapterError, PermanentAdapterError)) as caught:
            self.run_agent()
        # Not an upstream outage and not a spent output budget: no standby
        # substitution may follow from it (round-37 review).
        self.assertFalse(classify_failure(caught.exception).external)
        self.assertFalse(getattr(caught.exception, "output_limit_exhausted", False))

    def test_a_route_without_settings_path_reads_the_users_own_file(self):
        # Same as the AGY branch: a route missing settings_path falls back to
        # ~/.qoder/settings.json instead of failing on the missing key.
        self.config.cli_routes["local_qoder"].pop("settings_path")
        write_settings(self.settings, model="qmodel_from_home", effort="high")
        self.stub.reply(self.payload(model="qmodel_from_home"))
        result = self.run_agent()
        self.assertIn("结论：通过", result.output)
        argv = self.stub.argv()
        self.assertEqual("qmodel_from_home", argv[argv.index("--model") + 1])
        # The same file without a model name stops the call: no default model.
        write_settings(self.settings, model=None)
        with self.assertRaises(PermanentAdapterError):
            self.run_agent()

    def test_timeout_is_transient_and_kills_the_call(self):
        with patch(
            "xiaoba_agentos.adapters.run_cli", side_effect=subprocess.TimeoutExpired("qoder", 1)
        ) as call:
            with self.assertRaises(TransientAdapterError) as caught:
                QoderAdapter(self.agent).run("review", "")
        self.assertTrue(caught.exception.external_upstream)
        self.assertEqual(1, call.call_count)
        self.assertIsNone(call.call_args.kwargs["timeout"])

    def test_the_shared_no_progress_window_and_progress_feed_are_wired(self):
        # STANDBY_POLICY: all three CLIs share run_cli's 300 s no-progress rule.
        # Dropping idle_timeout or the progress callback must break this test.
        adapter = QoderAdapter(self.agent)
        adapter.progress_callback = lambda progress: None
        adapter.progress_interval = 2.5
        self.stub.reply(self.payload())
        with patch("xiaoba_agentos.adapters.run_cli", wraps=run_cli) as call:
            adapter.run("review", "")
        kwargs = call.call_args.kwargs
        # The adapter relies on run_cli's shared default, so it must not pass
        # its own idle_timeout, and that default must be the 300 s rule.
        self.assertNotIn("idle_timeout", kwargs)
        self.assertEqual(CLI_IDLE_TIMEOUT_SECONDS, inspect.signature(run_cli).parameters["idle_timeout"].default)
        self.assertEqual(300, CLI_IDLE_TIMEOUT_SECONDS)
        self.assertIs(adapter.progress_callback, kwargs["progress"])
        self.assertEqual(2.5, kwargs["progress_interval"])

    def test_missing_command_is_permanent_and_never_calls(self):
        self.config.raw["integrations"]["qoder_command"] = ""
        with patch("xiaoba_agentos.adapters.run_cli") as caller:
            with self.assertRaises(PermanentAdapterError):
                QoderAdapter(self.agent).run("review", "")
        caller.assert_not_called()

    # --- output hygiene ---------------------------------------------------

    def test_tool_call_markup_is_never_a_candidate_answer(self):
        # Measured 2026-10-20: with `--tools ""` qodercli still prints the call
        # it wanted to make and reports is_error:false.  The complete form ends
        # with `</parameter>` and is caught by the shared marker list; a partial
        # or truncated form is not, so the adapter checks its own markup too.
        markup = (
            "我将并行读取两个文件并列出目录内容。\n\n"
            "<tool_call>\n<function=Read>\n<parameter=file_path>\n/tmp/decoy-in.txt\n"
            "</parameter>\n</function>\n</tool_call>"
        )
        partial = "我将先读取文件。\n<tool_call>\n<function=Read>\n"
        self.assertTrue(contains_non_answer_markers(markup))
        self.assertFalse(contains_non_answer_markers(partial))
        invoke_prefixes = ("<invoke", "<INVOKE", "</invoke", "< invoke", "<invoke=Read", "<invoke>")
        for text in (markup, partial, *invoke_prefixes):
            with self.subTest(text=text[:20]):
                self.stub.reply(self.payload(text=text))
                with self.assertRaises(TransientAdapterError) as caught:
                    self.run_agent()
                self.assertIn("工具调用标记", str(caught.exception))

    def test_tool_words_and_auth_codes_in_plain_answers_are_not_markup(self):
        for text in ("Function parameter tool_call invoke", "PARAMETER 401 403 authentication /login",
                     "本次检视401行，403个参数；普通function与parameter均正常。"):
            with self.subTest(text=text):
                self.stub.reply(self.payload(text=text))
                self.assertEqual(text, self.run_agent().output)

    def test_shared_marker_list_still_rejects_its_own_forms(self):
        for markup in ('<invoke name="Read"></invoke>', "<parameter name=x>", "tool-call serialization"):
            with self.subTest(markup=markup):
                self.stub.reply(self.payload(text=markup))
                with self.assertRaises(TransientAdapterError):
                    self.run_agent()

    def test_reasoning_only_answer_is_an_internal_candidate_fault(self):
        reasoning_only = "<" + "think>整段都是推理，没有给出答案。</" + "think>"
        self.stub.reply(self.payload(text=reasoning_only))
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_agent()
        self.assertEqual("internal_adapter_candidate", getattr(caught.exception, "failure_class", ""))
        self.assertFalse(getattr(caught.exception, "transient", False))

    def test_paired_reasoning_block_is_stripped_before_delivery(self):
        self.stub.reply(self.payload(text="<think>先分解再相加。</think>\n结论：1517。"))
        self.assertEqual("结论：1517。", self.run_agent().output)

    # --- acceptance contract ----------------------------------------------

    def test_final_acceptance_requires_one_standalone_verdict(self):
        prompt = "End with FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL"
        for output in ("PASS", "FINAL_ACCEPTANCE: PASS suffix",
                       "FINAL_ACCEPTANCE: PASS\nFINAL_ACCEPTANCE: FAIL",
                       "结论：合格。 FINAL_ACCEPTANCE: PASS"):
            # A verdict must stand alone on its line and appear exactly once; the
            # same rule as AGY, DSH and the release gate.  Text after that line is
            # not rejected by any of them (the contract only asks for it).
            with self.subTest(output=output):
                self.stub.reply(self.payload(text=output))
                with self.assertRaises(TransientAdapterError):
                    self.run_agent(prompt=prompt)
        valid = "结论：合格。\nFINAL_ACCEPTANCE: PASS"
        self.stub.reply(self.payload(text=valid))
        answer = self.run_agent(prompt=prompt)
        self.assertEqual(valid, answer.output)
        self.assertIn("FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL", self.stub.stdin())

    def test_context_only_acceptance_words_do_not_trigger_the_contract(self):
        self.stub.reply(self.payload(text="这是普通审查结果，不带契约结论行。"))
        answer = self.run_agent(
            prompt="普通审查任务，无需输出验收契约",
            context="历史参考：FINAL_ACCEPTANCE: PASS 或 FINAL_ACCEPTANCE: FAIL",
        )
        self.assertEqual("这是普通审查结果，不带契约结论行。", answer.output)
        self.assertNotIn("Output contract", self.stub.stdin())

    # --- readiness and environment ----------------------------------------

    def test_readiness_checks_command_and_settings_without_inference(self):
        result = local_cli_readiness(self.config, [self.agent])
        self.assertTrue(result["ok"], result)
        self.assertIsNone(self.stub.argv(), "就绪检查不得调用 qodercli")
        check = result["checks"][0]
        self.assertEqual("qoder", check["driver"])
        self.assertEqual("qoder", check["provider"])
        self.assertEqual("qfmodel", check["model"])
        self.assertEqual("managed_by_qodercli_login", check["credential"])

        self.config.raw["integrations"]["qoder_command"] = "/missing/qodercli"
        missing_command = local_cli_readiness(self.config, [self.agent])
        self.assertFalse(missing_command["ok"])

        self.config.raw["integrations"]["qoder_command"] = str(self.stub.path)
        self.settings.unlink()
        missing_settings = local_cli_readiness(self.config, [self.agent])
        self.assertFalse(missing_settings["ok"])

    def test_environment_is_scrubbed_and_qoder_model_overrides_are_removed(self):
        # The model must come only from qodercli's own settings, so QODER_*
        # model overrides in the parent environment are dropped. PATH keeps the
        # inherited entries (as for AGY/DSH since v1.6.4) plus qoder's directory.
        secrets = {
            key: "unit-test-secret"
            for key in ("OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "XIAOBA_ROUTE_API_KEY")
        }
        overrides = {"QODER_MODEL": "x", "QODER_REASONING_EFFORT": "low", "QODER_CONTEXT_WINDOW": "1",
                     "QODER_BASE_URL": "https://example.invalid", "QODER_API_KEY": "k", "QODER_CONFIG_DIR": "/tmp/x",
                     "QODER_UNIT_SENTINEL": "any QODER_ name, not only the listed ones"}
        inherited = "/unit/inherited/bin:/usr/bin:/bin"
        with patch.dict(os.environ, {**secrets, **overrides, "PATH": inherited}):
            env = command_environment(self.config, "qoder")
        self.assertTrue(all(key not in env for key in secrets))
        self.assertEqual([], sorted(k for k in env if k.startswith("QODER_")))
        path_entries = env["PATH"].split(":")
        self.assertIn(str(self.stub.path.parent), path_entries)
        for entry in inherited.split(":"):
            self.assertIn(entry, path_entries)

    def test_build_adapter_dispatches_by_driver(self):
        self.assertIsInstance(build_adapter(self.agent), QoderAdapter)
        for agent in self.config.agents:
            if agent.name == "qoder_review":
                continue
            self.assertNotIsInstance(build_adapter(agent), QoderAdapter)


if __name__ == "__main__":
    unittest.main()
