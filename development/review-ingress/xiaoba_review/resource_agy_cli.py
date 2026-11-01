"""AGY's per-step command and error dialect; lifecycle is shared."""
import json
from xiaoba_agentos.adapters import _agy_result_payload, _is_transient_cli_error, _redact_error
from .resource_agy import InteractiveError, NativeWorkspaceConsent
from .resource_cli import CliWorklogResource

TRUST_PROMPT = 'Do you trust the contents of this project'
MODEL_REJECTED = ('invalid model selection', 'not recognized')


class AgyCliResource(CliWorklogResource):
    def _command(self, entry, timeout):
        seconds = 90 if timeout else 0
        return [entry, '--input-format', 'stream-json', '--output-format', 'stream-json',
                *(['--model', self.model] if self.model else []), '--mode', 'accept-edits', '--sandbox',
                '--print-timeout', '%ds' % seconds]

    def _input(self, ticket, health_only):
        message = {'event': 'user', 'message': {'content': self._dispatch(ticket, health_only)}}
        return (json.dumps(message, ensure_ascii=False) + '\n').encode()

    def _result_status(self):
        try:
            payload = _agy_result_payload(bytes(self._stdout).decode('utf-8', 'replace'))
        except Exception: return None
        return str(payload.get('status', '')).upper() or None

    @staticmethod
    def _model_rejected(*texts):
        """Whether agy itself said the configured model name is unknown.

        Each text is scanned whole and on its own. The redacted tail keeps only
        the last 2000 characters, and a long `Available models:` list pushes the
        error line out of it, so detection cannot run on that tail.
        """
        return any(all(marker in text.casefold() for marker in MODEL_REJECTED)
                   for text in texts if text)

    def _settle_exit(self):
        self._drain()
        self._stderr_thread.join(timeout=1)
        process = self._process
        returncode = process.returncode if process is not None else None
        stdout = bytes(self._stdout).decode('utf-8', 'replace')
        stderr = self._stderr_text()
        if returncode == 0 and self._result_status() == 'SUCCESS':
            raise InteractiveError('receipt_missing',
                'AGY本轮已结束，但没有写出合格的任务卡回执；不自动重试。')
        if TRUST_PROMPT in stdout or TRUST_PROMPT in stderr:
            raise NativeWorkspaceConsent()
        if self._model_rejected(stderr, stdout):
            # The name comes from the node configuration only; agy's own output,
            # including its model list, is never parsed, returned or stored.
            failure = InteractiveError('model_not_recognized',
                'AGY不认节点里的模型名“%s”；请在节点设置里点“修改节点”，改成 agy models 列出的名字后重新测试运行。'
                '不自动重试、不换模型、不换后台。' % self.model)
            failure.failure_class = 'permanent'
            raise failure
        from .cli_identity import specific_failure
        specific = specific_failure('agy', 'AGY', self.model, stdout, stderr, check_model=False)
        if specific: raise specific
        # Only a short class survives; the raw text is never returned or stored.
        detail = _redact_error(stderr or stdout or 'agy cli failed')
        failure = InteractiveError('cli_failed',
            'AGY调用未完成本次任务；不自动重试、不换模型、不换后台。')
        failure.failure_class = 'transient' if _is_transient_cli_error(detail) else 'permanent'
        raise failure

