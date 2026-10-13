from __future__ import annotations

import re
import json
import hashlib
import os
import signal
import subprocess
import tempfile
import time
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


_TRANSIENT_CLI_ERROR_MARKERS = (
    "rate limit",
    "timeout",
    "timed out",
    "temporar",
    "429",
    "quota",
    "reconnecting",
    "error sending request",
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection closed",
    "network is unreachable",
    "server disconnected",
    "unexpected eof",
)

_NON_ANSWER_MARKERS = NON_ANSWER_MARKERS

_FINAL_ACCEPTANCE_CONTRACT = re.compile(
    r"^[ \t]*FINAL_ACCEPTANCE: (PASS|FAIL)[ \t]*$", re.MULTILINE
)

_OUTPUT_LIMIT_ERROR = (
    "DSH 单轮输出达到上限（max-tokens），没有产生最终答案；"
    "这是任务过大而不是候选代码问题，请把任务拆小后再交给后台"
)


def _is_transient_cli_error(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _TRANSIENT_CLI_ERROR_MARKERS)


def _output_limit_failure(error: str) -> PermanentAdapterError:
    # Permanent on purpose: retrying re-runs the same oversized task and burns
    # the invocation budget until the task freezes.
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
    value = re.sub(
        r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|token)(\s*[:=]\s*)[^\s,;]+",
        r"\1\2[REDACTED]",
        value,
    )
    value = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
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


def _parse_dsh_ndjson(value: str) -> tuple[str, str]:
    """Return (answer, error) from DeepSeek Harness ndjson stream."""
    final_text = ""
    accumulated_text = []
    error = ""
    parsed_any = False
    output_limit_hit = False
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        parsed_any = True
        item_type = str(item.get("type", ""))
        if item_type == "error":
            error = str(item.get("message") or item.get("error") or "dsh error")
        elif item_type == "final":
            final_text = str(item.get("text", ""))
        elif item_type == "text":
            accumulated_text.append(str(item.get("text", "")))
        elif item_type == "status":
            reason = item.get("reason")
            if (
                str(item.get("phase", "")) == "turn_end"
                and isinstance(reason, dict)
                and str(reason.get("kind", "")) == "max-tokens"
            ):
                output_limit_hit = True
    if error:
        return "", _redact_error(error)
    answer = final_text.strip() or "".join(accumulated_text).strip()
    if answer:
        return answer, ""
    if output_limit_hit:
        return "", _OUTPUT_LIMIT_ERROR
    return ("", "") if parsed_any else (value.strip(), "")


def run_cli(argv, *, input=None, text=True, capture_output=True, timeout=None, check=False, **kwargs):
    """Kill the entire CLI process group on timeout, including Node launcher children."""
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=text, start_new_session=True, **kwargs)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
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
            f"{self.config.timeout_seconds}s",
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
                    timeout=self.config.timeout_seconds,
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
            "local_account",
        )


def _agy_result_payload(raw_output: str) -> Dict[str, Any]:
    """Return the final result object from agy's stream-json output.

    The stream is one JSON object per line; the turn outcome is the last
    {"event": "result", "result": {...}} line. A single bare result object is
    accepted as well.
    """
    result: Optional[Dict[str, Any]] = None
    parsed_any = False
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed_any = True
        if isinstance(item, dict) and item.get("event") == "result" and isinstance(item.get("result"), dict):
            result = item["result"]
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
        return self._run_model(prompt, context, model, self.config.timeout_seconds)

    def _run_model(self, prompt: str, context: str, model: str, timeout: float) -> AgentResult:
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


class MockAdapter(BaseAdapter):
    def run(self, prompt: str, context: str) -> AgentResult:
        template = self.config.raw.get("template", "离线智能体 {name} 已分析任务：{prompt}")
        return AgentResult(self.config.name, template.format(name=self.config.name, prompt=prompt), 1)


def build_adapter(config: AgentConfig) -> BaseAdapter:
    if config.type == "local_cli":
        driver = getattr(config, "driver", str(config.raw.get("driver", "")))
        if driver == "dsh":
            return DshAdapter(config)
        return AgyAdapter(config)
    kinds = {"mock": MockAdapter}
    try:
        return kinds[config.type](config)
    except KeyError as exc:
        raise PermanentAdapterError(f"未知适配器类型：{config.type}") from exc
