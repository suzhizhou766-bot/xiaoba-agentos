from __future__ import annotations

import re
import json
import hashlib
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import AgentConfig
from .adjudication import detect_output_constraint_state
from .output_hygiene import (
    NON_ANSWER_MARKERS,
    contains_non_answer_markers,
    strip_reasoning_markup,
)
from .user_rules import USER_RULES_TEXT
from .gateway import (
    redact_credentials,
    GatewayUnavailable,
    command_environment,
    local_cli_route,
)


class AdapterError(RuntimeError):
    transient = False


class TransientAdapterError(AdapterError):
    transient = True


class PermanentAdapterError(AdapterError):
    pass


CLI_IDLE_TIMEOUT_SECONDS = 300


class NoProgressTimeout(TransientAdapterError):
    """A local observation rule, not proof that the upstream stopped computing."""
    external_upstream = True
    failure_class = "external_upstream"
    no_progress_timeout = True


def _cli_progress_evidence(item):
    """Recognize actual deltas/stage transitions; never retain their contents."""
    if isinstance(item.get("event"), dict):
        item = item["event"]  # Wrapped stream-json partial event.
    kind = item.get("event") or item.get("type")
    if kind in ("init", "system", "session"):
        # DSH headless --json announces its run with one {"type":"session"} line
        # before any model work; that is readiness, exactly like init/system.
        return "init", False
    if kind in ("message_start", "content_block_start", "content_block_stop", "message_stop"):
        return "phase:" + kind, False
    if kind in ("tool_call", "tool_result"):
        # DSH --json commits one line per tool invocation.  A tool starting and a
        # tool finishing are explicit stage transitions, so they refresh the
        # no-progress clock.  Consecutive identical lines still do not (run_cli
        # only moves when the phase actually changes).
        return "phase:" + kind, False
    if kind == "status" and item.get("phase") in (
            "turn_start", "request_started", "streaming", "tool_start", "tool_end", "turn_end",
            "step_start", "step_end"):
        # DSH commits a step_start/step_end pair per model step, so a long review
        # that keeps taking steps is visibly alive between them.
        return "phase:" + item["phase"], False
    payload = item.get("step_update") if kind == "step_update" else item
    if not isinstance(payload, dict):
        return "", False
    delta = payload.get("delta")
    values = [payload.get(key) for key in ("text", "text_delta", "thinking_delta", "reasoning_delta")]
    if isinstance(delta, dict):
        values += [delta.get(key) for key in ("text", "thinking", "reasoning", "partial_json")]
    if kind == "assistant":
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            values.append(content)
        elif isinstance(content, list):
            values += [part.get(key) for part in content if isinstance(part, dict)
                       for key in ("text", "thinking")]
    # "thinking" is DSH's committed reasoning increment ({"type":"thinking","text":"..."});
    # an empty one still proves nothing, so it goes through the same non-empty check.
    active_kind = kind in ("step_update", "text", "text_delta", "content_block_delta", "assistant",
                           "message_delta", "thinking")
    return "", active_kind and any(isinstance(value, str) and bool(value) for value in values)


_TRANSIENT_CLI_ERROR_MARKERS = (
    "rate limit",
    "timeout",
    "timed out",
    "temporar",
    "429",
    "quota",
    "usage limit",
    # DSH's relay when every upstream key is busy (measured 2026-10-21:
    # 'SERVER: 503: {"message":"key-pool: no upstream available ...",
    # "type":"overloaded_error"}'); it used to fail the task outright.
    "overloaded",
    "no upstream available",
    "reconnecting",
    "error sending request",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection closed",
    "network is unreachable",
    "server disconnected",
    "unexpected eof",
    "socket hang up",
    "broken pipe",
    "econnreset",
    "econnrefused",
    "etimedout",
    "eai_again",
    "enotfound",
    "tls handshake",
    "no such host",
    "nodename nor servname provided",
)

_NON_ANSWER_MARKERS = NON_ANSWER_MARKERS

_FINAL_ACCEPTANCE_CONTRACT = re.compile(
    r"^[ \t]*FINAL_ACCEPTANCE: (PASS|FAIL)[ \t]*$", re.MULTILINE
)

_OUTPUT_LIMIT_ERROR = (
    "DSH 单轮输出达到上限（max-tokens），没有产生最终答案；"
    "这是任务过大而不是候选代码问题，请把任务拆小后再交给后台"
)

# qodercli is a tool-capable harness.  With `--tools ""` it cannot execute a
# tool, but it still prints the invocation it wanted to make as plain text and
# reports `is_error: false` (measured 2026-10-20).  The shared marker list only
# catches the complete form (via `</parameter>`), so a partial or truncated
# `<tool_call>`/`<function=...>` block is checked here as well; truncated invoke tags are also covered.
# The literal opening angle bracket is required; ordinary words are not markup.
_QODER_TOOL_MARKUP = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_use|function|parameter|invoke)\b", re.IGNORECASE
)

# A failed login is an external condition: the standby must report it as such
# instead of becoming an internal candidate fault that freezes the task.
# These markers are checked only against CLI failure signals in _qoder_failure,
# never against a successful answer body (even if it mentions 401 or 403).
_QODER_AUTH_MARKERS = (
    "not logged in",
    "/login",
    "unauthorized",
    "401",
    "403",
    "forbidden",
    "authentication",
    "invalid api key",
    "missing api key",
    "credential",
    "oauth",
    "token expired",
    "subscription",
)

_QODER_NO_TOOL_REMINDER = (
    "\n工具已全部关闭：不要输出任何工具调用标记（如 tool_call、function、parameter、invoke），"
    "只输出正文结论。"
)


# An HTTP 5xx only counts with context ("HTTP 503", "status 502", "upstream
# 503", "SERVER: 503" as DSH prints it, "503 Service Unavailable"); a bare number such as "waited 503 seconds"
# is not an upstream status. Shared with recovery.classify_failure so both
# layers read status codes the same way.
HTTP_5XX = re.compile(
    r"(?:\bhttp\b|\bstatus\b|\bcode\b|\bupstream\b|\berror\b|\bserver\b|状态码)\W{0,3}(?:code\W{0,3})?\b(?:500|502|503|504)\b"
    r"|\b(?:500|502|503|504)\s+(?:internal server error|bad gateway|service unavailable|gateway time-?out|unavailable)"
    # A gateway's HTML error page right after the code (measured 2026-10-21:
    # DSH 'INVALID_REQUEST: 502 <!DOCTYPE html>...') is an upstream status too.
    r"|\b(?:500|502|503|504)\s*<(?:!doctype\s+html|html)\b",
    # Both callers lowercase first; matching case-insensitively keeps the
    # pattern right on its own as well (round-38 review).
    re.IGNORECASE,
)


def _is_transient_cli_error(value: str) -> bool:
    # Shared by every driver, so it carries no driver-specific vocabulary: the
    # Qoder login markers belong to _qoder_failure alone (they used to turn an
    # AGY/DSH "connection reset (403)" into a non-retryable auth failure).
    # A standalone EOF is a dropped upstream connection (measured 2026-10-21:
    # AGY 'request failed: Post ".../streamGenerateContent?alt=sse": EOF' a few
    # minutes into a long stream). recovery.classify_failure already reads it as
    # external; without it here the adapter raised a non-retryable error and the
    # task froze instead of taking its one bounded retry.
    lowered = value.lower()
    return (any(marker in lowered for marker in _TRANSIENT_CLI_ERROR_MARKERS)
            or bool(HTTP_5XX.search(lowered))
            or bool(re.search(r"\beof\b", lowered)))


def _output_limit_failure(error: str) -> PermanentAdapterError:
    # Permanent on purpose: retrying re-runs the same oversized task and burns
    # the invocation budget until the task freezes.
    # The class stays external_upstream for the freeze record, but the
    # output_limit_exhausted marker makes the orchestrator treat it as a task
    # budget, not a backend fault: no standby substitution and no health row.
    failure = PermanentAdapterError(error)
    failure.failure_class = "external_upstream"
    failure.output_limit_exhausted = True
    return failure


@dataclass
class AgentResult:
    agent: str
    output: str
    latency_ms: int
    provider: str = ""
    model: str = ""
    route_source: str = ""


def _redact_error(value: str) -> str:
    value = redact_credentials(value)
    return value[-2000:]


def _sanitize_answer(text: str) -> str:
    """Strip reasoning markup from a parsed answer before any contract check.

    An answer that was nothing but reasoning markup has no deliverable content;
    it is permanent rather than transient because a retry re-runs the same
    prompt-hash-selected model and reproduces the identical leak.
    """
    cleaned = strip_reasoning_markup(text)
    if text.strip() and not cleaned:
        error = PermanentAdapterError("命令仅返回推理过程，剥离后没有可用答案")
        error.failure_class = "internal_adapter_candidate"
        raise error
    return cleaned


# Marker for a ``--json`` run whose stream is damaged or never ended. Such a
# run is a protocol fault of the CLI, not an answer and not a candidate fault.
_DSH_STREAM_ERROR_PREFIX = "dsh --json 输出流"


def _stream_snippet(value: str, limit: int = 200) -> str:
    """A bounded, credential-free fragment of a damaged CLI output stream."""
    text = " ".join(str(value or "").split())
    return _redact_error(text[:2000])[:limit]


def _dsh_stream_failure(text: str) -> TransientAdapterError:
    """Retry a damaged stream once, exactly like any other protocol fault."""
    failure = TransientAdapterError(text)
    failure.external_upstream = True
    return failure


def _parse_dsh_ndjson(value: str) -> tuple[str, str]:
    """Return (answer, error) from DeepSeek Harness ndjson stream.

    Only a completed turn can be an answer: the stream must carry a terminal
    event (``{"type": "final"}`` or ``status{phase: turn_end}``). 卡 7 / 卡 16:
    a run that exited 0 after printing only diagnostics, or whose tail was cut
    off, used to be returned as the answer. Diagnostics stay in the error text.

    Banner lines (not JSON, and not shaped like a JSON object) are allowed and
    ignored. A line that starts like a JSON object but does not parse is a
    damaged protocol line: it is recorded and the run is not a success.
    """
    final_text = ""
    accumulated_text = []
    error = ""
    parsed_any = False
    output_limit_hit = False
    terminal_seen = False
    damaged_lines: list[str] = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            if line.startswith("{"):
                damaged_lines.append(line)
            continue
        if not isinstance(item, dict):
            continue
        parsed_any = True
        item_type = str(item.get("type", ""))
        if item_type == "error":
            error = str(item.get("message") or item.get("error") or "dsh error")
        elif item_type == "final":
            final_text = str(item.get("text", ""))
            terminal_seen = True
        elif item_type == "text":
            accumulated_text.append(str(item.get("text", "")))
        elif item_type == "status":
            reason = item.get("reason")
            if str(item.get("phase", "")) == "turn_end":
                terminal_seen = True
                if isinstance(reason, dict) and str(reason.get("kind", "")) == "max-tokens":
                    output_limit_hit = True
    if error:
        return "", _redact_error(error)
    answer = final_text.strip() or "".join(accumulated_text).strip()
    if damaged_lines:
        return "", (
            f"{_DSH_STREAM_ERROR_PREFIX}损坏：{len(damaged_lines)} 行协议数据无法解析，"
            f"不判成功；首行片段：{_stream_snippet(damaged_lines[0])}"
        )
    if answer and terminal_seen:
        return answer, ""
    if answer:
        return "", (
            f"{_DSH_STREAM_ERROR_PREFIX}不完整：没有结束事件（final 或 turn_end），"
            f"不判成功；尾部片段：{_stream_snippet(value[-400:])}"
        )
    if output_limit_hit:
        return "", _OUTPUT_LIMIT_ERROR
    if parsed_any:
        return "", ""
    if not value.strip():
        # An empty stream keeps its own diagnosis ("成功但没有有效文本").
        return "", ""
    return "", (
        f"{_DSH_STREAM_ERROR_PREFIX}没有可解析的事件：stdout 不是 JSON 事件流，"
        f"不能当成答案；首段片段：{_stream_snippet(value)}"
    )


def run_cli(argv, *, input=None, text=True, capture_output=True, timeout=None, check=False,
            progress=None, progress_interval=5.0, idle_timeout=CLI_IDLE_TIMEOUT_SECONDS, **kwargs):
    """Wait for completion; an explicit deadline is reserved for bounded probes.

    communicate drains both pipes. Its partial output is cumulative, so only new
    complete JSON lines contribute to activity. Never persist their contents.
    A living PID is local process evidence, not proof of upstream acceptance.
    """
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=text, start_new_session=True, **kwargs)
    started = time.monotonic()
    offset = 0
    stage = "process_running_unconfirmed"
    activity_events = 0
    last_progress_at = started
    last_phase = ""
    initialized = False

    def report(raw):
        nonlocal offset, stage, activity_events, last_progress_at, last_phase, initialized
        value = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else (raw or "")
        # Init only proves CLI readiness; only known streaming events show
        # model activity. Neither one is a completed result or acceptance proof.
        end = value.rfind("\n") + 1
        for line in value[offset:end].splitlines():
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(item, dict):
                continue
            phase, activity = _cli_progress_evidence(item)
            moved = False
            if phase == "init" and not initialized:
                initialized = True
                if stage == "process_running_unconfirmed":
                    stage = "cli_ready"
                moved = True
            elif phase.startswith("phase:") and phase != last_phase:
                last_phase = phase
                moved = True
            if activity:
                stage = "model_activity"
                activity_events += 1
                moved = True
            if moved:
                last_progress_at = time.monotonic()
        offset = end
        if progress is not None:
            progress({"stage": stage, "pid": process.pid,
                      "elapsed_seconds": round(time.monotonic() - started, 3),
                      "stdout_bytes": len(value.encode("utf-8")),
                      "activity_events": activity_events,
                      "no_progress_seconds": round(time.monotonic() - last_progress_at, 3),
                      "idle_timeout_seconds": idle_timeout})

    try:
        report("")
        pending_input = input
        while True:
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            interval = max(0.01, float(progress_interval))
            wait = interval if remaining is None else min(interval, remaining)
            if idle_timeout is not None:
                idle_left = idle_timeout - (time.monotonic() - last_progress_at)
                if idle_left <= 0:
                    raise NoProgressTimeout(f"CLI 连续 {idle_timeout:g} 秒无实际进展（非整项任务总截止）")
                wait = min(wait, idle_left)
            try:
                stdout, stderr = process.communicate(input=pending_input, timeout=wait)
                report(stdout)
                break
            except subprocess.TimeoutExpired as exc:
                pending_input = None
                report(exc.output)
                if timeout is not None and time.monotonic() - started >= timeout:
                    raise subprocess.TimeoutExpired(argv, timeout, output=exc.output, stderr=exc.stderr) from exc
                if idle_timeout is not None and time.monotonic() - last_progress_at >= idle_timeout:
                    raise NoProgressTimeout(f"CLI 连续 {idle_timeout:g} 秒无实际进展（非整项任务总截止）") from exc
    except BaseException:
        # Includes lease loss, cancellation and receipt failures. Never dispatch
        # a substitute while a locally owned CLI process group is still alive.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


class BaseAdapter:
    def __init__(self, config: AgentConfig):
        self.config = config
        # None for real tasks: no total deadline, only the 300-second
        # no-progress window. Explicit health probes set a bound before calling
        # run() (recovery.live_agent_probe: agent.timeout_seconds).
        self.execution_timeout = None
        self.progress_callback = None
        self.progress_interval = 5.0

    def run(self, prompt: str, context: str) -> AgentResult:
        raise NotImplementedError

    def _full_prompt(self, prompt: str, context: str) -> str:
        parts = [self.config.role, "\n用户公共规则（执行权限规则，无需在答案中复述）：", USER_RULES_TEXT,
                 "\n任务：", prompt]
        if context:
            parts += ["\n任务上下文与参考资料（按各段来源区分当前证据、技能和历史记忆）：", context]
        detection = detect_output_constraint_state(prompt)
        if detection.status != "no_constraint":
            parts.append(
                "\n严格遵守任务题面中的输出约束；不要添加题面禁止的解释、标题、风险或验证步骤。"
                "你当前只产出独立候选答案，最终来源标签由 AgentOS 统一添加。"
            )
        else:
            parts.append(
                "\n请给出明确结论、依据、风险和可验证步骤。你当前只产出独立候选答案；"
                "不要自行声明这是会话模型直答或最终裁判结果，最终来源标签由 AgentOS 统一添加。"
            )
        return "\n".join(parts)


class AgyAdapter(BaseAdapter):
    """Plain-text Antigravity CLI adapter backed by its local account state."""

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._selection: Optional[Dict[str, Any]] = None

    def _model(self) -> str:
        try:
            self._selection = local_cli_route(
                self.config.app_config,
                str(self.config.raw["cli_route"]),
                self.config.name,
            )
        except GatewayUnavailable as exc:
            raise PermanentAdapterError(str(exc)) from exc
        return str(self._selection["model"])

    def _command(self) -> list[str]:
        # The prompt (task plus frozen context) is sent on stdin as one
        # stream-json message. Command-line arguments are readable by every
        # local process through `ps`, so the prompt must never appear there.
        model = self._model()
        integrations = self.config.app_config.integrations
        return [
            str(integrations["agy_command"]),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--mode",
            "plan",
            "--sandbox",
            "--print-timeout",
            # `agy --help`: "0 waits until the turn completes (default 0s)". A
            # real task therefore has no total deadline (the 300 s no-progress
            # rule lives in run_cli); only an explicit health probe sets one.
            f"{int(self.execution_timeout or 0)}s",
        ]

    def run(self, prompt: str, context: str) -> AgentResult:
        full_prompt = self._full_prompt(prompt, context)
        if "FINAL_ACCEPTANCE: PASS" in prompt and "FINAL_ACCEPTANCE: FAIL" in prompt:
            full_prompt += (
                "\nOutput contract: end with exactly one standalone line: "
                "FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL. "
                "Put every qualification before that line; no suffix, explanation, or Markdown on the verdict line."
            )
        argv = self._command()
        stream_input = json.dumps(
            {"event": "user", "message": {"content": full_prompt}}, ensure_ascii=False
        ) + "\n"
        started = time.monotonic()
        try:
            # Keep project discovery away from the user's source and credentials.
            # agy ignores --mode when --disable-slash-commands is used.
            with tempfile.TemporaryDirectory(prefix="xiaoba-agy-") as review_dir:
                completed = run_cli(
                    argv,
                    cwd=review_dir,
                    input=stream_input,
                    text=True,
                    capture_output=True,
                    timeout=self.execution_timeout,
                    progress=self.progress_callback,
                    progress_interval=self.progress_interval,
                    check=False,
                    env=command_environment(self.config.app_config, "agy"),
                )
        except subprocess.TimeoutExpired as exc:
            error = TransientAdapterError(f"agy timeout：{self.config.name}")
            error.external_upstream = True
            raise error from exc
        except OSError as exc:
            raise PermanentAdapterError(_redact_error(str(exc))) from exc
        if completed.returncode != 0:
            error = _redact_error(completed.stderr or completed.stdout or "agy CLI failed")
            if _is_transient_cli_error(error):
                failure = TransientAdapterError(error)
                failure.external_upstream = True
                raise failure
            raise PermanentAdapterError(error)
        raw_output = (completed.stdout or "").strip()
        if not raw_output:
            failure = TransientAdapterError("agy 命令成功但未返回 JSON")
            failure.external_upstream = True
            raise failure
        payload = _agy_result_payload(raw_output)
        status = str(payload.get("status", "")).upper()
        if status != "SUCCESS":
            error = _redact_error(
                str(payload.get("error") or payload.get("response") or status or "agy CLI failed")
            )
            if _is_transient_cli_error(error):
                failure = TransientAdapterError(error)
                failure.external_upstream = True
                raise failure
            raise PermanentAdapterError(error)
        output = payload.get("response")
        if not isinstance(output, str) or not output.strip():
            failure = TransientAdapterError("agy 成功响应没有纯文本 response")
            failure.external_upstream = True
            raise failure
        output = _sanitize_answer(output.strip())
        if contains_non_answer_markers(output):
            raise TransientAdapterError("agy 返回了未执行的工具调用标记，不能作为候选答案")
        if (
            "FINAL_ACCEPTANCE: PASS" in prompt
            and "FINAL_ACCEPTANCE: FAIL" in prompt
            and len(_FINAL_ACCEPTANCE_CONTRACT.findall(output)) != 1
        ):
            raise TransientAdapterError("agy 未履行明确的最终验收输出契约，不能作为成功结果")
        selection = self._selection or {}
        return AgentResult(
            self.config.name,
            output,
            int((time.monotonic() - started) * 1000),
            str(selection.get("provider", "")),
            str(selection.get("model", "")),
            str(selection.get("selection") or "configured_model"),
        )


def _is_json_document(value: str) -> bool:
    try:
        json.loads(value)
    except json.JSONDecodeError:
        return False
    return True


def _agy_result_payload(raw_output: str) -> Dict[str, Any]:
    """Return the final result object from agy's stream-json output.

    The stream is one JSON object per line; the turn outcome is the last
    {"event": "result", "result": {...}} line. A single bare result object is
    accepted as well.

    卡 7 / 卡 16: banner lines stay allowed, a damaged protocol line does not.
    A line shaped like a JSON object that does not parse is recorded, and the
    run is never a success even when a valid result line was seen earlier
    ("坏行 + SUCCESS result + 截断尾行" used to be returned as SUCCESS).
    """
    result: Optional[Dict[str, Any]] = None
    parsed_any = False
    banners = 0
    damaged_lines: list[str] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            # A plain banner line is tolerated; a half-written JSON object is a
            # damaged protocol line and must not be skipped silently.
            if line.startswith("{"):
                damaged_lines.append(line)
            else:
                banners += 1
            continue
        parsed_any = True
        if isinstance(item, dict) and item.get("event") == "result" and isinstance(item.get("result"), dict):
            result = item["result"]
    if damaged_lines and not _is_json_document(raw_output):
        tolerated = f"（另允许 {banners} 行横幅）" if banners else ""
        raise TransientAdapterError(
            f"agy 输出流损坏：{len(damaged_lines)} 行协议数据无法解析，不判成功{tolerated}；"
            f"首行片段：{_stream_snippet(damaged_lines[0])}"
        )
    if result is not None:
        return result
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError as exc:
        if parsed_any:
            raise TransientAdapterError("agy 输出流中没有 result 事件") from exc
        raise TransientAdapterError("agy 返回了不可解析的 JSON") from exc
    if not isinstance(payload, dict):
        raise TransientAdapterError("agy 返回的 JSON 不是对象")
    if "event" in payload and "status" not in payload:
        raise TransientAdapterError("agy 输出流中没有 result 事件")
    return payload


class DshAdapter(BaseAdapter):
    """Plain-text Harness CLI adapter following the Desktop's native settings."""

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._selection: Optional[Dict[str, Any]] = None

    def _model(self) -> str:
        try:
            self._selection = local_cli_route(
                self.config.app_config,
                str(self.config.raw["cli_route"]),
                self.config.name,
            )
        except GatewayUnavailable as exc:
            raise PermanentAdapterError(str(exc)) from exc
        return str(self._selection["model"])

    def _command(self) -> list[str]:
        integrations = self.config.app_config.integrations
        if not self._selection or not self._selection.get("desktop_config"):
            raise PermanentAdapterError("DSH 桌面配置未解析，停止调用")
        return [
            str(integrations["node_command"]),
            str(integrations["dsh_command"]),
            "--profile", "headless",
            "--patch", str(self._selection["desktop_config"]),
            "--json",
            "-",
        ]

    def run(self, prompt: str, context: str) -> AgentResult:
        # Observe the backend's selection for routing evidence, without choosing
        # or retrying another model. Task-level recovery owns retries/freezing.
        model = self._model()
        return self._run_model(prompt, context, model, self.execution_timeout)

    def _run_model(self, prompt: str, context: str, model: str, timeout: Optional[float]) -> AgentResult:
        full_prompt = self._full_prompt(prompt, context)
        if "FINAL_ACCEPTANCE: PASS" in prompt and "FINAL_ACCEPTANCE: FAIL" in prompt:
            full_prompt += (
                "\nOutput contract: end with exactly one standalone line: "
                "FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL. "
                "Put every qualification before that line; no suffix, explanation, or Markdown on the verdict line."
            )
        self._selection = {**(self._selection or {}), "model": model}
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoba-dsh-") as review_dir:
                completed = run_cli(
                    self._command(),
                    cwd=review_dir,
                    input=full_prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    progress=self.progress_callback,
                    progress_interval=self.progress_interval,
                    check=False,
                    env=command_environment(self.config.app_config, "dsh"),
                )
        except subprocess.TimeoutExpired as exc:
            error = TransientAdapterError(f"dsh timeout：{self.config.name}")
            error.external_upstream = True
            raise error from exc
        except OSError as exc:
            raise PermanentAdapterError(_redact_error(str(exc))) from exc

        raw_output = (completed.stdout or "").strip()
        output, dsh_error = _parse_dsh_ndjson(raw_output)

        if completed.returncode != 0:
            if dsh_error == _OUTPUT_LIMIT_ERROR:
                raise _output_limit_failure(dsh_error)
            # A failed process keeps its own diagnosis: stderr decides between
            # retry and stop, not the state of whatever reached stdout.
            if dsh_error.startswith(_DSH_STREAM_ERROR_PREFIX):
                dsh_error = ""
            err = dsh_error or completed.stderr or raw_output or "dsh CLI failed"
            error = _redact_error(err)
            if _is_transient_cli_error(error):
                failure = TransientAdapterError(error)
                failure.external_upstream = True
                raise failure
            raise PermanentAdapterError(error)

        if dsh_error:
            if dsh_error == _OUTPUT_LIMIT_ERROR:
                raise _output_limit_failure(dsh_error)
            error = _redact_error(dsh_error)
            if dsh_error.startswith(_DSH_STREAM_ERROR_PREFIX):
                raise _dsh_stream_failure(error)
            if _is_transient_cli_error(error):
                failure = TransientAdapterError(error)
                failure.external_upstream = True
                raise failure
            raise PermanentAdapterError(error)

        if not output:
            failure = TransientAdapterError("dsh 命令成功但未返回有效文本响应")
            failure.external_upstream = True
            raise failure

        output = _sanitize_answer(output.strip())
        if contains_non_answer_markers(output):
            raise TransientAdapterError("dsh 返回了未执行的工具调用标记，不能作为候选答案")
        if (
            "FINAL_ACCEPTANCE: PASS" in prompt
            and "FINAL_ACCEPTANCE: FAIL" in prompt
            and len(_FINAL_ACCEPTANCE_CONTRACT.findall(output)) != 1
        ):
            raise TransientAdapterError("dsh 未履行明确的最终验收输出契约，不能作为成功结果")
        selection = self._selection or {}
        return AgentResult(
            self.config.name,
            output,
            int((time.monotonic() - started) * 1000),
            str(selection.get("provider", "")),
            str(selection.get("model", "")),
            "local_harness",
        )


def _parse_qoder_result(raw_output: str) -> tuple[Optional[Dict[str, Any]], str]:
    """Only a terminal result is an answer; init/partial streams never are.

    Older single-object final JSON remains readable for compatibility.
    """
    text = (raw_output or "").strip()
    if not text:
        return None, ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and (payload.get("type") == "result" or
            ("result" in payload and "type" not in payload and "event" not in payload)):
        return payload, text
    terminal = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            return None, text
        if isinstance(item, dict) and item.get("type") == "result":
            terminal.append(item)
    return (terminal[0], text) if len(terminal) == 1 else (None, text)


def _qoder_failure(signal: str, detail: str = "") -> AdapterError:
    """Classify a qodercli failure from the signals the CLI itself produced.

    ``signal`` is stderr, or the error fields of a result object the CLI marked
    as failed (``is_error``/``subtype``/``error``).  It is never the answer body:
    that is text the model writes, and the class decided here triggers standby
    substitution and changes what the release gate weighs, so it must not be
    something the model can type its way into.

    Anything the CLI does not explain is an internal adapter candidate, not an
    assumed upstream outage.
    """
    evidence = (signal or "").lower()
    message = detail or signal
    if any(marker in evidence for marker in _QODER_AUTH_MARKERS):
        failure = PermanentAdapterError(message)
        failure.failure_class = "external_auth"
        return failure
    if _is_transient_cli_error(evidence):
        failure = TransientAdapterError(message)
        failure.external_upstream = True
        return failure
    failure = PermanentAdapterError(message)
    failure.failure_class = "internal_adapter_candidate"
    return failure


class QoderAdapter(BaseAdapter):
    """Standby read-only reviewer backed by the local qodercli login.

    The model is the one the user last selected in qodercli, read from
    ``~/.qoder/settings.json`` on every call
    (``gateway.read_qodercli_selection``); AgentOS passes that value on as
    written and records it as the model of the result. Per the user's
    2026-11-01 ruling the configured model is the model in use, so what
    answered is never verified and a differing or missing report is not a
    failure.

    Each call runs in an empty temporary directory with the tool surface closed:
    ``--tools ""`` so no tool can execute, an empty ``--strict-mcp-config`` so no
    MCP server is offered, ``--settings '{"disableAllHooks":true}'`` so no hook
    runs, and ``--no-session-persistence`` so nothing is saved.  Qoder plugins
    themselves are not switched off by these flags -- the read-only boundary
    rests on tools and MCP being closed, plus the prompt telling the model not to
    print tool markup.
    """

    MCP_CONFIG = '{"mcpServers":{}}'
    # Qoder plugins may register SessionStart/prompt hooks (the user's
    # qoder-context plugin reports turns).  None fired in -p mode when checked
    # on 2026-10-20, but a review must not depend on that staying true.
    SETTINGS = '{"disableAllHooks":true}'

    def __init__(self, config: AgentConfig):
        super().__init__(config)
        self._selection: Optional[Dict[str, Any]] = None

    def _model(self) -> str:
        try:
            self._selection = local_cli_route(
                self.config.app_config,
                str(self.config.raw["cli_route"]),
                self.config.name,
            )
        except GatewayUnavailable as exc:
            raise PermanentAdapterError(str(exc)) from exc
        return str(self._selection["model"])

    def _command(self) -> list[str]:
        command = str(self.config.app_config.integrations.get("qoder_command", "")).strip()
        if not command:
            raise PermanentAdapterError("缺少 qoder_command，停止调用")
        selection = self._selection
        if not selection or selection.get("model_source") != "qodercli_settings":
            raise PermanentAdapterError("缺少 qodercli 设置里的选定模型，不调用 CLI")
        if "qoderclicn" in Path(command).name.lower() or "qoderclicn" in Path(command).resolve().name.lower():
            raise PermanentAdapterError("拒绝 Qoder CN CLI；仅允许标准版 Qoder")
        args = [
            command,
            "-p",
            "--model", str(selection["model"]),
            "--setting-sources", "project,local",
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            self.MCP_CONFIG,
            "--settings",
            self.SETTINGS,
            "--output-format",
            "stream-json",
        ]
        if selection.get("reasoning_effort") is not None:
            args += ["--reasoning-effort", str(selection["reasoning_effort"])]
        if selection.get("context_window") is not None:
            args += ["--context-window", str(selection["context_window"])]
        # 实测（qodercli-1.1.74，2026-10-20）：不加这个隐藏开关时，qodercli 从
        # init 到生成结束只在最后一次性写 system/assistant/result，一次长生成
        # 期间 stdout 完全静默，长审查会被「连续无实际进展」误判。加上之后生成
        # 期间持续吐 stream_event（内层 content_block_delta 带非空 text/thinking
        # 增量，相邻间隔约 0.2~0.4 秒），且 result 行仍恰好 1 条、字段名不变，
        # 答案解析不受影响。
        args += ["--include-partial-messages"]
        return args

    def run(self, prompt: str, context: str) -> AgentResult:
        model = self._model()
        full_prompt = self._full_prompt(prompt, context) + _QODER_NO_TOOL_REMINDER
        if "FINAL_ACCEPTANCE: PASS" in prompt and "FINAL_ACCEPTANCE: FAIL" in prompt:
            full_prompt += (
                "\nOutput contract: end with exactly one standalone line: "
                "FINAL_ACCEPTANCE: PASS or FINAL_ACCEPTANCE: FAIL. "
                "Put every qualification before that line; no suffix, explanation, or Markdown on the verdict line."
            )
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="xiaoba-qoder-") as review_dir:
                completed = run_cli(
                    self._command(),
                    cwd=review_dir,
                    input=full_prompt,
                    text=True,
                    capture_output=True,
                    timeout=self.execution_timeout,
                    progress=self.progress_callback,
                    progress_interval=self.progress_interval,
                    check=False,
                    env=command_environment(self.config.app_config, "qoder"),
                )
        except subprocess.TimeoutExpired as exc:
            error = TransientAdapterError(f"qoder timeout：{self.config.name}")
            error.external_upstream = True
            raise error from exc
        except OSError as exc:
            raise PermanentAdapterError(_redact_error(str(exc))) from exc

        raw_output = (completed.stdout or "").strip()
        payload, parse_error = _parse_qoder_result(raw_output)
        if payload is None:
            # No result object at all, so the only CLI evidence left is stderr.
            # Network/upstream wording there means an external fault; anything
            # else (including an empty stderr) is an internal adapter candidate.
            # stdout is not evidence: without a result object there is no way to
            # tell a CLI diagnostic from model text, and assuming "upstream" for
            # every unparsable stdout handed the standby substitution and the
            # gate an outcome the CLI never reported.
            stderr_text = _redact_error((completed.stderr or "").strip())
            # stderr is the CLI's own explanation, so it is what the user should
            # read; the raw stdout is only a fallback for diagnosis.
            detail = _redact_error(
                stderr_text or parse_error or "qoder CLI 未返回可解析的 JSON"
            )
            raise _qoder_failure(stderr_text, detail)

        result_text = payload.get("result")
        result_text = result_text if isinstance(result_text, str) else ""
        # A result object the CLI itself marked as failed -- is_error, or an
        # `error_*` subtype even beside exit 0 and is_error false -- never yields
        # an answer: there `result` is the CLI's own error message ("Not logged
        # in ...") and stays part of the signal.  Otherwise the same field is the
        # model's answer body and is never a signal.
        marked_by_cli = bool(payload.get("is_error", False)) or str(
            payload.get("subtype", "")
        ).startswith("error")
        if completed.returncode != 0 or marked_by_cli:
            answer_body = result_text.strip() if marked_by_cli else ""
            # qodercli puts some failures only in an `errors` list (measured
            # 2026-10-21: the daily usage limit came back as is_error,
            # subtype error_during_execution, error_code 110, and the text in
            # `errors` alone). It is a field of the CLI's own result object,
            # not model text, so it is part of the signal.
            listed = payload.get("errors")
            cli_errors = "\n".join(
                str(item) for item in (listed if isinstance(listed, list) else [])
                if isinstance(item, str) and item.strip()
            )
            signal = "\n".join(
                part for part in (
                    (completed.stderr or "").strip(),
                    answer_body,
                    str(payload.get("error") or ""),
                    str(payload.get("error_message") or ""),
                    cli_errors,
                ) if part
            )
            detail = (
                (completed.stderr or "").strip()
                or answer_body
                or str(payload.get("error") or "")
                or cli_errors
                or f"qoder CLI 未给出错误说明（退出码 {completed.returncode}）"
            )
            raise _qoder_failure(_redact_error(signal), _redact_error(detail))
        if not result_text.strip():
            # Same rule as AGY and DSH: the CLI reported success yet the model
            # sent no answer, which is an upstream symptom, not an adapter bug.
            # Qoder is only ever the standby, so this never triggers a
            # substitution; it only decides that its health row reads
            # external_blocked rather than degraded.
            failure = TransientAdapterError("qoder 命令成功但没有返回回答正文")
            failure.external_upstream = True
            raise failure

        output = _sanitize_answer(result_text.strip())
        if contains_non_answer_markers(output) or _QODER_TOOL_MARKUP.search(output):
            raise TransientAdapterError("qoder 返回了未执行的工具调用标记，不能作为候选答案")
        if (
            "FINAL_ACCEPTANCE: PASS" in prompt
            and "FINAL_ACCEPTANCE: FAIL" in prompt
            and len(_FINAL_ACCEPTANCE_CONTRACT.findall(output)) != 1
        ):
            raise TransientAdapterError("qoder 未履行明确的最终验收输出契约，不能作为成功结果")
        selection = self._selection or {}
        return AgentResult(
            self.config.name,
            output,
            int((time.monotonic() - started) * 1000),
            str(selection.get("provider", "qoder")),
            model,
            "qodercli_settings",
        )


class MockAdapter(BaseAdapter):
    def run(self, prompt: str, context: str) -> AgentResult:
        template = self.config.raw.get("template", "离线智能体 {name} 已分析任务：{prompt}")
        return AgentResult(self.config.name, template.format(name=self.config.name, prompt=prompt), 1)


def build_adapter(config: AgentConfig) -> BaseAdapter:
    if config.type == "local_cli":
        driver = getattr(config, "driver", str(config.raw.get("driver", "")))
        if driver == "agy":
            return AgyAdapter(config)
        if driver == "dsh":
            return DshAdapter(config)
        if driver == "qoder":
            return QoderAdapter(config)
        # No fallback to AGY: an empty or unknown driver used to be silently
        # handed to the Antigravity CLI, which ran a different agent than the
        # one the config named.  Report it instead of guessing.
        raise PermanentAdapterError(
            f"未知本地 CLI 驱动：{driver!r}（{config.name}）；已知驱动为 agy、dsh、qoder，"
            "空 driver 或未知 driver 不得回落到 AGY"
        )
    kinds = {"mock": MockAdapter}
    try:
        return kinds[config.type](config)
    except KeyError as exc:
        raise PermanentAdapterError(f"未知适配器类型：{config.type}") from exc
