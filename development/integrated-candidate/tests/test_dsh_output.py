"""卡 16：DSH 输出验收与诊断（卡 7 第 8、9 项）。

四个反例，每个对应卡里的一项要求：
1. --json 模式没有合法结束事件（只有诊断文本 / 流没结束）不许当答案；
2. 路由读取失败保留脱敏后的 stderr，哨兵不丢；
3. 读 Desktop 配置的脚本拒绝软链、超大文件和非普通文件；
4. 横幅行与协议坏行分开：坏行要记录，且不许判成功。

不调用真实模型；node 读写真实的 macOS 路径，但只读临时目录里的夹具。
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoba_agentos.adapters import (
    DshAdapter,
    PermanentAdapterError,
    TransientAdapterError,
    _agy_result_payload,
    _parse_dsh_ndjson,
    build_adapter,
)
from xiaoba_agentos.config import AppConfig
from xiaoba_agentos.gateway import GatewayUnavailable, local_cli_route

ROUTE_SENTINEL = "DSH Desktop routing configuration is unavailable or invalid"


def _dsh_events(*events):
    return "\n".join(json.dumps(event) for event in events)


class DshAdapterCase(unittest.TestCase):
    """A DSH adapter whose Desktop reader and CLI are fixtures, never real."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        raw = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/agentos-with-backends.json").read_text())
        raw["cli_routes"]["local_dsh"] = {
            "driver": "dsh", "selection": "local_harness",
            "desktop_config": str(self.root / "desktop.yml"),
        }
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
        (self.root / "desktop.yml").write_text("backend-owned fixture configuration")
        metadata = {"provider": "Fixture Provider Alpha", "provider_id": "provider-alpha",
                    "model": "model-alpha", "model_source": "dsh_desktop"}
        reader = patch("xiaoba_agentos.gateway.subprocess.run",
                       return_value=subprocess.CompletedProcess([], 0, json.dumps(metadata), ""))
        reader.start()
        self.addCleanup(reader.stop)

    def run_raw(self, stdout, code=0, stderr=""):
        result = subprocess.CompletedProcess([], code, stdout, stderr)
        with patch("xiaoba_agentos.adapters.run_cli", return_value=result):
            return build_adapter(self.agent).run("审查方案", "仅有这份冻结证据")


class DshTerminalEventTest(DshAdapterCase):
    """反例一（卡 7 第 8 项）：诊断信息不是答案。"""

    def test_diagnostics_only_stdout_is_not_an_answer(self):
        # 修复前：stdout 里没有 JSON 事件时，原始文本被当成答案返回。
        diagnostics = (
            "Warning: Cannot find module '@deepseek-ai/dsh-app-boot'\n"
            "at Module._resolveFilename (node:internal/modules/cjs/loader:1145:15)\n"
        )
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_raw(diagnostics, code=0)
        message = str(caught.exception)
        self.assertIn("没有可解析的事件", message)
        self.assertIn("Cannot find module", message)
        self.assertFalse(getattr(caught.exception, "output_limit_exhausted", False))
        self.assertTrue(getattr(caught.exception, "external_upstream", False))

    def test_a_truncated_stream_is_not_an_answer(self):
        # 修复前：有 text 事件但没有结束事件，半条答案照样返回。
        partial = _dsh_events({"type": "session", "sessionId": "s"},
                              {"type": "text", "text": "结论：可行，依据"})
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_raw(partial, code=0)
        self.assertIn("没有结束事件", str(caught.exception))

    def test_a_damaged_protocol_line_is_recorded_and_never_success(self):
        damaged = _dsh_events({"type": "session", "sessionId": "s"}) + '\n{"type":"final","text":"答'
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_raw(damaged, code=0)
        self.assertIn("协议数据无法解析", str(caught.exception))

    def test_a_completed_turn_keeps_the_text_fallback_and_its_answer(self):
        # 对照：结束事件在场时，原文本事件回退和 final 选择都不变。
        completed = _dsh_events(
            {"type": "session", "sessionId": "s"},
            {"type": "text", "text": "第一段；"},
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
        )
        self.assertEqual("第一段；", self.run_raw(completed, code=0).output)
        final = _dsh_events({"type": "final", "text": "结论：可行。"})
        self.assertEqual("结论：可行。", self.run_raw(final, code=0).output)

    def test_a_banner_line_beside_a_valid_stream_is_tolerated(self):
        # 横幅行（不是 JSON 对象形状）允许出现，不能被当成坏行。
        stdout = "Warning: ExperimentalWarning: fetch is experimental\n" + _dsh_events(
            {"type": "final", "text": "结论：可行。"})
        self.assertEqual("结论：可行。", self.run_raw(stdout, code=0).output)

    def test_a_failed_process_keeps_its_stderr_diagnosis(self):
        # 进程已失败时，由 stderr 决定重试还是停止；stdout 的状态不能盖过它。
        with self.assertRaises(PermanentAdapterError) as caught:
            self.run_raw("启动横幅\n", code=1, stderr="invalid profile: model-alpha")
        self.assertIn("invalid profile", str(caught.exception))

    def test_a_damaged_line_never_leaks_a_json_shaped_credential(self):
        damaged = '{"type":"text","api_key":"fixture-dummy-secret","x":"'
        with self.assertRaises(TransientAdapterError) as caught:
            self.run_raw(damaged, code=0)
        self.assertNotIn("fixture-dummy-secret", str(caught.exception))

    def test_the_parser_reports_an_empty_stream_separately(self):
        self.assertEqual(("", ""), _parse_dsh_ndjson(""))
        self.assertEqual(("", ""), _parse_dsh_ndjson('{"type":"session","sessionId":"s"}'))


class DshRouteDiagnosticTest(unittest.TestCase):
    """反例二（卡 7 第 9 项）：路由错误保留脱敏后的 stderr。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        raw = json.loads((Path(__file__).resolve().parents[1] / "tests/fixtures/agentos-with-backends.json").read_text())
        raw["cli_routes"]["local_dsh"] = {
            "driver": "dsh", "selection": "local_harness",
            "desktop_config": str(self.root / "desktop.yml"),
        }
        raw["project_dir"] = str(self.root)
        raw["runtime_dir"] = str(self.root / "runtime")
        raw["memory"].update(knowlp_enabled=False, obsidian_vault=str(self.root), publish_enabled=False)
        self.config = AppConfig(self.root / "config.json", raw)

    def route_error(self, result):
        with patch("xiaoba_agentos.gateway.subprocess.run", return_value=result):
            with self.assertRaises(GatewayUnavailable) as caught:
                local_cli_route(self.config, "local_dsh", "dsh_review")
        return str(caught.exception)

    def test_route_failure_keeps_the_reader_sentinel(self):
        # 修复前：只抛固定的“读取失败，停止调用”，stderr 里的哨兵全丢。
        message = self.route_error(
            subprocess.CompletedProcess([], 1, "", ROUTE_SENTINEL + "\n"))
        self.assertIn("DSH Desktop 路由读取失败，停止调用", message)
        self.assertIn(ROUTE_SENTINEL, message)
        self.assertIn("退出码 1", message)

    def test_route_failure_redacts_credentials_in_stderr(self):
        stderr = (f"{ROUTE_SENTINEL}\napi_key=fixture-dummy-secret "
                  "Authorization: Bearer fixture-dummy-token")
        message = self.route_error(subprocess.CompletedProcess([], 1, "", stderr))
        self.assertIn(ROUTE_SENTINEL, message)
        self.assertNotIn("fixture-dummy-secret", message)
        self.assertNotIn("fixture-dummy-token", message)
        self.assertIn("[REDACTED]", message)

    def test_route_failure_redacts_json_and_basic_credentials(self):
        stderr = ('{"access_token": "fixture-dummy-secret"} '
                  "Authorization: Basic fixture-dummy-basic sk-fixturedummykey0123")
        message = self.route_error(subprocess.CompletedProcess([], 1, "", stderr))
        for secret in ("fixture-dummy-secret", "fixture-dummy-basic", "sk-fixturedummykey0123"):
            self.assertNotIn(secret, message)

    def test_route_error_detail_is_bounded(self):
        message = self.route_error(subprocess.CompletedProcess([], 1, "", "x" * 5000))
        self.assertLess(len(message), 600)

    def test_route_failure_without_stderr_keeps_the_plain_message(self):
        message = self.route_error(subprocess.CompletedProcess([], 1, "", ""))
        self.assertTrue(message.startswith("DSH Desktop 路由读取失败，停止调用："), message)
        self.assertIn("退出码 1", message)


class DshDesktopReaderGuardTest(unittest.TestCase):
    """反例三：读配置脚本拒软链、超大文件和非普通文件。"""

    @classmethod
    def setUpClass(cls):
        cls.project = Path(__file__).resolve().parents[1]
        integrations = json.loads((cls.project / "tests/fixtures/agentos-with-backends.json").read_text())["integrations"]
        cls.node = integrations["node_command"]
        cls.dsh = integrations["dsh_command"]
        if not Path(cls.node).is_file():
            raise unittest.SkipTest("node runtime is not installed")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.valid = self.root / "desktop.yml"
        self.valid.write_text(json.dumps([
            {"id": "agent-default-model", "config": {"provider": "provider-alpha", "model": "model-alpha"}},
        ]), encoding="utf-8")

    def read(self, config_path):
        return subprocess.run([self.node, str(self.project / "scripts/dsh_desktop_route.cjs"),
                               self.dsh, str(config_path)],
                              capture_output=True, text=True, timeout=10, check=False)

    def assert_refused(self, result):
        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual(ROUTE_SENTINEL + "\n", result.stderr)

    def test_a_regular_file_still_reads(self):
        result = self.read(self.valid)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("model-alpha", json.loads(result.stdout)["model"])

    def test_a_symlinked_profile_is_refused(self):
        # 修复前：307301 字节的软链配置被接受（卡 7 第 9 项）。
        link = self.root / "linked.yml"
        os.symlink(self.valid, link)
        self.assert_refused(self.read(link))

    def test_an_oversized_profile_is_refused(self):
        oversized = self.root / "oversized.yml"
        payload = json.dumps([{"id": "agent-default-model",
                               "config": {"provider": "provider-alpha", "model": "model-alpha"}}])
        oversized.write_text(payload + "\n#" + "x" * (300 * 1024), encoding="utf-8")
        self.assertGreater(oversized.stat().st_size, 300 * 1024)
        self.assert_refused(self.read(oversized))

    def test_a_non_regular_file_is_refused(self):
        self.assert_refused(self.read(self.root))
        fifo = self.root / "profile.fifo"
        os.mkfifo(fifo)
        self.assert_refused(self.read(fifo))


class AgyDamagedStreamTest(unittest.TestCase):
    """反例四（卡 7 第 8 项其二）：横幅行与协议坏行分开。"""

    RESULT = {"event": "result", "result": {"status": "SUCCESS", "response": "结论：可行。"}}

    def stream(self, *lines):
        return "\n".join(lines)

    def test_bad_line_plus_success_result_plus_truncated_tail_is_a_failure(self):
        # 修复前：坏行被静默跳过，SUCCESS result 照常返回（截断尾行没人看）。
        stdout = self.stream(
            json.dumps({"event": "init", "init": {}}),
            "not-json garbage line",
            json.dumps(self.RESULT, ensure_ascii=False),
            '{"event": "step_update", "step_update": {"text_delta": "尾部被截',
        )
        with self.assertRaises(TransientAdapterError) as caught:
            _agy_result_payload(stdout)
        message = str(caught.exception)
        self.assertIn("输出流损坏", message)
        self.assertIn("不判成功", message)
        self.assertIn("允许 1 行横幅", message)
        self.assertNotIn("结论：可行。", message)

    def test_a_clean_stream_is_parsed_exactly_as_before(self):
        stdout = self.stream(
            json.dumps({"event": "init", "init": {}}),
            json.dumps({"event": "step_update", "step_update": {"text_delta": "结论"}}, ensure_ascii=False),
            json.dumps(self.RESULT, ensure_ascii=False),
        )
        self.assertEqual(self.RESULT["result"], _agy_result_payload(stdout))
        # 单个裸对象的老格式仍然可读。
        self.assertEqual({"status": "SUCCESS", "response": "结论：可行。"},
                         _agy_result_payload(json.dumps({"status": "SUCCESS", "response": "结论：可行。"})))

    def test_a_pretty_printed_bare_result_is_still_read(self):
        # 裸对象老格式可以跨行，它的第一行“{”不是坏行。
        payload = {"status": "SUCCESS", "response": "结论：可行。"}
        self.assertEqual(payload, _agy_result_payload(json.dumps(payload, ensure_ascii=False, indent=2)))

    def test_banner_lines_alone_are_still_an_unparseable_stream(self):
        # AGY 行为不变：只有横幅、没有 result 时，仍是“不可解析的 JSON”。
        with self.assertRaises(TransientAdapterError) as caught:
            _agy_result_payload("Warning: something\nanother banner line")
        self.assertIn("不可解析", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
