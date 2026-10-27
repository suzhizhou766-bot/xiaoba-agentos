"""Measured progress signals for the two silent CLI backends.

Event types, field names and cadence below were measured on 2026-10-20 with
real short calls (probe records: 进度信号/探测/DSH-探测记录.md 与
进度信号/探测/Qoder-探测记录.md).  Bodies are replaced with fake text; no
credentials, desktop settings or remote requests are used here.

Measured facts encoded in these tests:
* DSH headless --json commits one line per stage boundary: session /
  status{phase: turn_start|step_start|step_end|turn_end} / tool_call /
  tool_result / thinking{text} / text{text} / final{text}.  There is no
  token-level increment: inside one generation step stdout stays silent
  (measured 16s on a tiny task), so a long single step cannot be seen as
  progress.  That limitation is reported as-is, never papered over.
* qodercli only streams mid-generation with the hidden
  --include-partial-messages switch (it requires --print and
  --output-format=stream-json): stream_event wrappers then carry
  content_block_delta with non-empty delta.text / delta.thinking, and the
  terminal result stays exactly one line with unchanged field names.
"""

import json
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from xiaoba_agentos.adapters import (
    DshAdapter,
    NoProgressTimeout,
    PermanentAdapterError,
    QoderAdapter,
    TransientAdapterError,
    _cli_progress_evidence,
    _parse_dsh_ndjson,
    _parse_qoder_result,
    run_cli,
)
from xiaoba_agentos.recovery import classify_failure

# Every field run_cli's progress callback may carry. Model text, thinking and
# tool output are not among them and must never reach a progress receipt.
PROGRESS_FIELDS = {"stage", "pid", "elapsed_seconds", "stdout_bytes", "activity_events",
                   "no_progress_seconds", "idle_timeout_seconds"}
PROGRESS_STAGES = {"process_running_unconfirmed", "cli_ready", "model_activity"}

# --- measured event shapes, bodies replaced with fake text ----------------

DSH_FINAL = "结论：可行。依据：实测事件形状。风险：单步静默。验证：复查。"
QODER_FINAL = "结论：可行。依据：实测增量形状。风险：工具期未测。验证：复查。"

# 实测 dsh-count50 / dsh-tool50 的完整事件序列（正文换假文本）。
DSH_RUN = [
    {"type": "session", "sessionId": "probe-fixture", "cwd": "/tmp/probe"},
    {"type": "status", "phase": "turn_start", "turn": 1},
    {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
    {"type": "thinking", "text": "假思考：核对基线。"},
    {"type": "text", "text": "基线一致。"},
    {"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": {"input_tokens": 1}},
    {"type": "status", "phase": "step_start", "turn": 1, "step": 2},
    {"type": "tool_call", "callId": "call-1", "tool": "read", "input": {"path": "/tmp/probe"}},
    {"type": "tool_result", "callId": "call-1", "status": "completed", "result": "假工具结果"},
    {"type": "status", "phase": "step_end", "turn": 1, "step": 2},
    {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
    {"type": "final", "text": DSH_FINAL},
]

# 实测 qoder-partial-count50 的部分事件序列（正文换假文本；result 行字段名不变）。
QODER_PARTIAL_RUN = [
    {"type": "system", "subtype": "init"},
    {"type": "stream_event", "event": {"type": "message_start", "message": {"role": "assistant"}}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                       "content_block": {"type": "thinking"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "thinking_delta", "thinking": "假思考一"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "thinking_delta", "thinking": "假思考二"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "thinking_delta", "thinking": "假思考三"}}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
    {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
                                       "content_block": {"type": "text"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
                                       "delta": {"type": "text_delta", "text": "假正文一"}}},
    {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
                                       "delta": {"type": "text_delta", "text": "假正文二"}}},
    {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    {"type": "stream_event", "event": {"type": "message_delta",
                                       "delta": {"stop_reason": "end_turn"}, "usage": {}}},
    {"type": "stream_event", "event": {"type": "message_stop"}},
    {"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "假思考一二三", "signature": "sig"},
        {"type": "text", "text": "假正文一二", "citations": []}]}},
    {"type": "result", "subtype": "success", "is_error": False, "result": QODER_FINAL,
     "modelUsage": {"qmodel": {"inputTokens": 12, "outputTokens": 34}}},
]


def qoder_result(text=QODER_FINAL, is_error=False, model="qmodel"):
    return {"type": "result", "subtype": "success", "is_error": is_error, "result": text,
            "modelUsage": {model: {"inputTokens": 12, "outputTokens": 34}}}


def _emit_code(events, gap=0.0, tail=""):
    """A fake CLI that prints one measured-shape JSON line per event."""
    lines = ""
    for event in events:
        # Raw UTF-8 like the real CLIs, so a leak of their text into progress
        # would show up as the text itself rather than as \uXXXX escapes.
        lines += "print(" + repr(json.dumps(event, ensure_ascii=False)) + ", flush=True)\n"
        lines += "time.sleep(" + repr(gap) + ")\n"
    return "import json, time\n" + lines + tail


class MeasuredEvidenceClassificationTest(unittest.TestCase):
    """Lock the documented classification of every measured shape."""

    def test_dsh_measured_shapes_are_classified_as_documented(self):
        cases = [
            ({"type": "session", "sessionId": "s"}, ("init", False)),
            ({"type": "status", "phase": "turn_start", "turn": 1}, ("phase:turn_start", False)),
            ({"type": "status", "phase": "step_start", "turn": 1, "step": 1}, ("phase:step_start", False)),
            ({"type": "status", "phase": "step_end", "turn": 1, "step": 1}, ("phase:step_end", False)),
            ({"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
             ("phase:turn_end", False)),
            ({"type": "tool_call", "callId": "c", "tool": "read", "input": {}}, ("phase:tool_call", False)),
            ({"type": "tool_result", "callId": "c", "status": "completed", "result": "假"},
             ("phase:tool_result", False)),
            ({"type": "thinking", "text": "假思考"}, ("", True)),
            ({"type": "thinking", "text": ""}, ("", False)),
            ({"type": "text", "text": "假正文"}, ("", True)),
            ({"type": "text", "text": ""}, ("", False)),
            ({"type": "final", "text": "假终稿"}, ("", False)),
        ]
        for event, expected in cases:
            with self.subTest(kind=event.get("type")):
                self.assertEqual(expected, _cli_progress_evidence(dict(event)))

    def test_qoder_wrapped_shapes_are_classified_as_documented(self):
        cases = [
            ({"type": "stream_event", "event": {"type": "message_start", "message": {}}},
             ("phase:message_start", False)),
            ({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                                "content_block": {"type": "thinking"}}},
             ("phase:content_block_start", False)),
            ({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "thinking_delta", "thinking": "假"}}},
             ("", True)),
            ({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": "假"}}},
             ("", True)),
            ({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": ""}}},
             ("", False)),
            ({"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
             ("phase:content_block_stop", False)),
            ({"type": "stream_event", "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                                                "usage": {}}},
             ("", False)),
            ({"type": "stream_event", "event": {"type": "message_stop"}},
             ("phase:message_stop", False)),
            ({"type": "assistant", "message": {"content": [{"type": "text", "text": "假"}]}}, ("", True)),
            ({"type": "system", "subtype": "init"}, ("init", False)),
            ({"type": "result", "subtype": "success", "result": "假"}, ("", False)),
        ]
        for event, expected in cases:
            kind = event.get("event", {}).get("type") or event.get("type")
            with self.subTest(kind=kind):
                self.assertEqual(expected, _cli_progress_evidence(dict(event)))


class MeasuredSignalTimingTest(unittest.TestCase):
    """Real subprocesses emit the measured cadence.  The idle windows below are
    shortened only so the suite stays fast: production keeps
    CLI_IDLE_TIMEOUT_SECONDS = 300 and no total deadline for the task."""

    def assert_only_progress_fields(self, updates):
        # The progress callback may only ever carry these fields; anything else
        # (model text, thinking, tool output) must never reach the receipt.
        self.assertTrue(updates)
        for row in updates:
            self.assertLessEqual(set(row), PROGRESS_FIELDS, row)
            # Whitelisted fields hold only a known stage name or numbers, so
            # text cannot hide inside them either, whatever its encoding.
            self.assertIn(row["stage"], PROGRESS_STAGES, row)
            for key in PROGRESS_FIELDS - {"stage"}:
                if key in row and row[key] is not None:
                    self.assertIsInstance(row[key], (int, float), (key, row))
                    self.assertNotIsInstance(row[key], bool, (key, row))

    def test_dsh_measured_stage_transitions_keep_a_multi_step_run_alive(self):
        updates = []
        started = time.monotonic()
        # 缩短窗口只为测试速度（比例不变，绝对值留足调度余量）；线上是 300 秒且没有总时长截止。
        result = run_cli([sys.executable, "-B", "-c", _emit_code(DSH_RUN, 0.15)],
                         idle_timeout=0.6, progress=updates.append, progress_interval=0.08)
        self.assertEqual(0, result.returncode)
        self.assertGreater(time.monotonic() - started, 0.3)
        final_line = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(DSH_FINAL, final_line["text"])
        stages = [row["stage"] for row in updates]
        self.assertIn("cli_ready", stages)
        self.assertIn("model_activity", stages)
        self.assertEqual(2, updates[-1]["activity_events"])  # 只有 thinking/text 是模型增量
        # ensure_ascii=False: the default escapes Chinese to \uXXXX, which would
        # make these membership checks impossible to fail.
        self.assertNotIn("假思考", json.dumps(updates, ensure_ascii=False))
        self.assertNotIn("假工具结果", json.dumps(updates, ensure_ascii=False))
        self.assert_only_progress_fields(updates)

    def test_qoder_partial_messages_keep_generation_alive_and_result_parseable(self):
        updates = []
        # 缩短窗口只为测试速度（比例不变，绝对值留足调度余量）；线上是 300 秒且没有总时长截止。
        result = run_cli([sys.executable, "-B", "-c", _emit_code(QODER_PARTIAL_RUN, 0.15)],
                         idle_timeout=0.6, progress=updates.append, progress_interval=0.08)
        self.assertEqual(0, result.returncode)
        self.assertEqual(6, updates[-1]["activity_events"])  # 3 thinking_delta + 2 text_delta + 1 assistant
        self.assertEqual("model_activity", updates[-1]["stage"])
        payload, _ = _parse_qoder_result(result.stdout)
        self.assertEqual(QODER_FINAL, payload["result"])  # 增量不干扰唯一终态 result
        self.assertNotIn("假思考", json.dumps(updates, ensure_ascii=False))
        self.assertNotIn("假正文", json.dumps(updates, ensure_ascii=False))
        self.assert_only_progress_fields(updates)

    def test_repeated_identical_stage_lines_and_empty_deltas_never_refresh(self):
        # 第25、26轮审查查出：空增量要在窗口内真的被读到，而且重复的阶段行和空增量
        # 要在仍不断写出的时候就判无进展——一次写完再静默的写法分不出"刷新"与"不刷新"。
        # 这里每 0.05 秒写一行、共约 3 秒，窗口 0.6 秒：不刷新时约 0.6 秒就超时，
        # 若误把它们当进展，则要等写完再过 0.6 秒。缩短窗口只为测试速度。
        heartbeat = []
        line = {"type": "status", "phase": "step_start", "turn": 1, "step": 1}
        empties = [
            {"type": "thinking", "text": ""},
            {"type": "stream_event", "event": {"type": "content_block_delta",
                                               "delta": {"type": "text_delta", "text": ""}}},
        ]
        events = [DSH_RUN[0], line] + ([line] + empties) * 20
        baseline = sum(len((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
                       for event in events[:2])
        started = time.monotonic()
        with self.assertRaises(NoProgressTimeout):
            run_cli([sys.executable, "-B", "-c", _emit_code(events, 0.05)],
                    idle_timeout=0.6, progress=heartbeat.append, progress_interval=0.08)
        elapsed = time.monotonic() - started
        # Timed out while the stream was still being written (about 3 s long).
        self.assertLess(elapsed, 2.0)
        # The empty deltas after the baseline step were read before the timeout.
        self.assertGreater(heartbeat[-1]["stdout_bytes"], baseline)
        self.assertEqual(0, heartbeat[-1]["activity_events"])
        self.assertEqual("cli_ready", heartbeat[-1]["stage"])

    def test_dsh_single_long_generation_step_cannot_be_seen_as_progress(self):
        # 照实报告的局限：DSH 在单个生成步内不发出任何中途事件（小任务实测静默 16 秒）。
        # step_start 之后静默必须判无进展，而不是拿替代信号凑数。
        events = DSH_RUN[:3]  # session, turn_start, step_start
        updates = []
        code = _emit_code(events, 0.15, tail="time.sleep(2.0)\n")
        with self.assertRaises(NoProgressTimeout):
            run_cli([sys.executable, "-B", "-c", code], idle_timeout=0.6,
                    progress=updates.append, progress_interval=0.08)
        self.assertEqual(0, updates[-1]["activity_events"])
        self.assertEqual("cli_ready", updates[-1]["stage"])


class MeasuredParseTest(unittest.TestCase):
    """Final-answer parsing stays correct under the new event shapes."""

    def test_dsh_measured_run_yields_only_the_final_answer(self):
        text = "\n".join(json.dumps(item) for item in DSH_RUN)
        answer, error = _parse_dsh_ndjson(text)
        self.assertEqual(DSH_FINAL, answer)
        self.assertEqual("", error)
        self.assertNotIn("假思考", answer)
        self.assertNotIn("假工具结果", answer)

    def test_dsh_text_stream_fallback_excludes_thinking(self):
        events = [item for item in DSH_RUN if item.get("type") != "final"]
        answer, error = _parse_dsh_ndjson("\n".join(json.dumps(item) for item in events))
        self.assertEqual("基线一致。", answer)
        self.assertEqual("", error)
        self.assertNotIn("假思考", answer)

    def test_dsh_max_tokens_turn_end_still_reports_output_limit(self):
        event = {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "max-tokens"}}
        answer, error = _parse_dsh_ndjson(json.dumps(event))
        self.assertEqual("", answer)
        self.assertIn("上限", error)

    def test_qoder_terminal_result_stays_exactly_one_with_partial_lines(self):
        text = "\n".join(json.dumps(item) for item in QODER_PARTIAL_RUN)
        payload, _ = _parse_qoder_result(text)
        self.assertEqual(QODER_FINAL, payload["result"])
        self.assertFalse(payload["is_error"])
        two = text + "\n" + json.dumps(qoder_result())
        self.assertIsNone(_parse_qoder_result(two)[0])


class MeasuredClassificationTest(unittest.TestCase):
    """Measured error shapes keep the existing failure vocabulary."""

    def _dsh_adapter(self):
        app = SimpleNamespace(integrations={"node_command": "/fixture/node",
                                            "dsh_command": "/fixture/dsh.js"})
        agent = SimpleNamespace(name="dsh_review", role="", timeout_seconds=30, raw={}, app_config=app)
        adapter = DshAdapter(agent)
        adapter._selection = {"provider": "DSH Desktop Fixture", "model": "model-alpha",
                              "model_source": "dsh_desktop", "desktop_config": "/fixture/desktop.yml"}
        return adapter

    def _qoder_adapter(self):
        app = SimpleNamespace(integrations={"qoder_command": "/fixture/qodercli"})
        agent = SimpleNamespace(name="qoder_review", role="", timeout_seconds=30, raw={}, app_config=app)
        adapter = QoderAdapter(agent)
        adapter._selection = {"provider": "qoder", "model": "qmodel",
                              "model_source": "qodercli_settings", "reasoning_effort": "xhigh"}
        return adapter

    def _run(self, adapter, code):
        command = [sys.executable, "-B", "-c", code]
        with patch.object(adapter, "_model", return_value=str(adapter._selection["model"])), \
             patch.object(adapter, "_command", return_value=command), \
             patch("xiaoba_agentos.adapters.command_environment", return_value=dict(os.environ)):
            return adapter.run("审查方案", "仅有这份冻结证据")

    def test_dsh_measured_run_returns_only_the_final_answer(self):
        answer = self._run(self._dsh_adapter(), _emit_code(DSH_RUN, 0.0))
        self.assertEqual(DSH_FINAL, answer.output)
        self.assertNotIn("假思考", answer.output)
        self.assertEqual("DSH Desktop Fixture", answer.provider)
        self.assertEqual("local_harness", answer.route_source)

    def test_dsh_error_event_keeps_the_shared_transient_classification(self):
        code = _emit_code([DSH_RUN[0], {"type": "thinking", "text": "假思考"},
                           {"type": "error", "message": "dsh: read ECONNRESET"}]) + "import sys\nsys.exit(1)\n"
        with self.assertRaises(TransientAdapterError) as caught:
            self._run(self._dsh_adapter(), code)
        self.assertTrue(caught.exception.external_upstream)
        self.assertEqual("external_upstream", classify_failure(caught.exception).name)

    def test_dsh_non_transient_error_event_stays_permanent(self):
        code = _emit_code([DSH_RUN[0], {"type": "error", "message": "invalid request format"}])
        with self.assertRaises(PermanentAdapterError):
            self._run(self._dsh_adapter(), code)

    def test_dsh_max_tokens_turn_end_keeps_the_output_limit_failure(self):
        code = _emit_code([DSH_RUN[0], {"type": "status", "phase": "turn_end", "turn": 1,
                                        "reason": {"kind": "max-tokens"}}])
        with self.assertRaises(PermanentAdapterError) as caught:
            self._run(self._dsh_adapter(), code)
        self.assertTrue(caught.exception.output_limit_exhausted)
        self.assertEqual("external_upstream", classify_failure(caught.exception).name)
        self.assertIn("上限", str(caught.exception))

    def test_qoder_partial_stream_returns_only_the_result_answer(self):
        answer = self._run(self._qoder_adapter(), _emit_code(QODER_PARTIAL_RUN, 0.0))
        self.assertEqual(QODER_FINAL, answer.output)
        self.assertNotIn("假思考", answer.output)
        self.assertEqual("qmodel", answer.model)
        self.assertEqual("qodercli_settings", answer.route_source)

    def test_qoder_is_error_payload_keeps_its_classification(self):
        code = _emit_code(QODER_PARTIAL_RUN[:-1]
                          + [qoder_result(text="401 Unauthorized: please log in", is_error=True)])
        with self.assertRaises(PermanentAdapterError) as caught:
            self._run(self._qoder_adapter(), code)
        self.assertEqual("external_auth", classify_failure(caught.exception).name)
        code = _emit_code(QODER_PARTIAL_RUN[:-1]
                          + [qoder_result(text="503 Service Unavailable", is_error=True)])
        with self.assertRaises(TransientAdapterError) as caught:
            self._run(self._qoder_adapter(), code)
        self.assertTrue(caught.exception.external_upstream)
        self.assertEqual("external_upstream", classify_failure(caught.exception).name)
